"""
Aprendizaje de qué tokens valen la pena, a partir de los que el usuario envía.

Cada token enviado se guarda con una FOTO de sus características (MC, liquidez,
edad, concentración, autoridades, riesgo, cuántas billeteras de calidad lo
compraron). El usuario puede marcarlo 👍 (buena) o 👎 (mala). Una pasada de IA
compara las buenas vs las malas y extrae patrones + SUGIERE ajustes de filtros.

Importante y por diseño: NUNCA cambia los umbrales del embudo en silencio.
Solo aprende y te sugiere; tú decides. Los aprendizajes se inyectan al veredicto
IA de los próximos tokens (ai_token.py) para hacerlo más inteligente con el tiempo.

La tabla se crea de forma perezosa (compatible SQLite y Postgres) sin tocar el
esquema central.
"""

import json
import os
import time

import requests

from db import get_conn, get_setting, set_setting

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MIN_ETIQUETADOS = 8   # mínimo de tokens con 👍/👎 para que la IA aprenda


def _ensure(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS submitted_tokens (
             mint TEXT PRIMARY KEY,
             symbol TEXT, mc REAL, liq REAL, age_days REAL,
             top10_pct REAL, lp_locked_pct REAL,
             mint_auth INTEGER, freeze_auth INTEGER,
             risk_score INTEGER, smart_count INTEGER, elite_count INTEGER,
             chg24 REAL, feedback INTEGER, ts REAL)""")
    conn.commit()


def record_submission(mint: str, t: dict, risk: int,
                      smart_count: int, elite_count: int):
    """Guarda/actualiza la foto del token enviado. Preserva el feedback previo."""
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            vals = (
                t.get("symbol"), t.get("mc"), t.get("liq"), t.get("age_days"),
                t.get("top10_pct"), t.get("lp_locked_pct"),
                1 if t.get("mint_auth") else 0,
                1 if t.get("freeze_auth") else 0,
                int(risk), int(smart_count), int(elite_count),
                t.get("price_change_h24"), time.time())
            cur = conn.execute(
                """UPDATE submitted_tokens SET
                     symbol=?, mc=?, liq=?, age_days=?, top10_pct=?,
                     lp_locked_pct=?, mint_auth=?, freeze_auth=?, risk_score=?,
                     smart_count=?, elite_count=?, chg24=?, ts=?
                   WHERE mint=?""", vals + (mint,))
            if not cur.rowcount:
                conn.execute(
                    """INSERT INTO submitted_tokens
                       (symbol,mc,liq,age_days,top10_pct,lp_locked_pct,
                        mint_auth,freeze_auth,risk_score,smart_count,
                        elite_count,chg24,ts,feedback,mint)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    vals + (mint,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"· record_submission falló: {e}")


def set_feedback(mint: str, good: bool) -> bool:
    """Marca un token enviado como 👍 (bueno) o 👎 (malo)."""
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            conn.execute("UPDATE submitted_tokens SET feedback=? WHERE mint=?",
                         (1 if good else 0, mint))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f"· set_feedback falló: {e}")
        return False


def learnings_text() -> str:
    """Los aprendizajes acumulados (para inyectar al veredicto IA)."""
    try:
        conn = get_conn()
        try:
            return get_setting(conn, "token_learnings", "") or ""
        finally:
            conn.close()
    except Exception:
        return ""


def analyze_submitted() -> str | None:
    """
    Pasada de IA sobre los tokens enviados que ya tienen 👍/👎: extrae qué los
    separa y SUGIERE ajustes de filtros. Guarda el resultado en 'token_learnings'
    y lo manda por Telegram. No cambia ninguna configuración.
    """
    if not ANTHROPIC_API_KEY:
        return None
    conn = get_conn()
    try:
        _ensure(conn)
        rows = [dict(r) for r in conn.execute(
            """SELECT symbol, mc, liq, age_days, top10_pct, lp_locked_pct,
                      mint_auth, freeze_auth, risk_score, smart_count,
                      elite_count, chg24, feedback
               FROM submitted_tokens
               WHERE feedback IS NOT NULL
               ORDER BY ts DESC LIMIT 100""").fetchall()]
    finally:
        conn.close()

    if len(rows) < MIN_ETIQUETADOS:
        print(f"· Aprendizaje de tokens: solo {len(rows)} etiquetados "
              f"(faltan para {MIN_ETIQUETADOS})")
        return None

    buenas = sum(1 for r in rows if r["feedback"] == 1)
    malas = len(rows) - buenas
    prompt = (
        "Eres el analista de un sistema que filtra tokens ganadores en Solana. "
        "El usuario marcó estos tokens como BUENOS (feedback=1, valían la pena) "
        "o MALOS (feedback=0). Cada uno tiene: MC, liquidez, edad en días, "
        "concentración top10 (%), LP bloqueado (%), autoridades mint/freeze "
        "(1=activa), risk_score, smart_count (billeteras de calidad nuestras que "
        "lo compraron), elite_count, chg24 (% 24h).\n\n"
        "1. ¿Qué características separan a los BUENOS de los MALOS?\n"
        "2. Da 3-5 reglas prácticas y cortas ('los buenos suelen tener X').\n"
        "3. Sugiere ajustes CONCRETOS a los filtros de descubrimiento "
        "(MIN_MC_USD, MIN_LIQUIDITY_USD, MAX_TOKEN_AGE_DAYS, concentración), "
        "pero acláralo como SUGERENCIA para que el usuario decida.\n"
        "Responde en español, corto y accionable.\n\n"
        f"DATOS ({buenas} buenos, {malas} malos): "
        f"{json.dumps(rows, ensure_ascii=False, default=str)}")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90)
        r.raise_for_status()
        hallazgos = "".join(b.get("text", "") for b in
                            r.json().get("content", [])).strip()
    except Exception as e:
        print(f"· Aprendizaje de tokens IA falló: {e}")
        return None
    if not hallazgos:
        return None

    conn = get_conn()
    try:
        set_setting(conn, "token_learnings", hallazgos[:2000])
    finally:
        conn.close()
    try:
        from realtime import tg_send
        tg_send("🧠 *Aprendizaje de tokens* "
                f"({buenas} buenos / {malas} malos marcados):\n\n"
                f"{hallazgos[:3000]}\n\n"
                "_Son SUGERENCIAS; no cambié ningún filtro. "
                "Estos aprendizajes ya se inyectan al veredicto de los "
                "próximos tokens que envíes._")
    except Exception:
        pass
    print("🧠 Aprendizaje de tokens guardado")
    return hallazgos
