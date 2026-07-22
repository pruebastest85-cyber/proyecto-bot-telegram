"""
Motor de hipótesis: descubrimiento autónomo.

En vez de solo clasificar, la IA observa el estado del grafo (clusters,
descubridores alfa, estrellas emergentes, líderes) y PROPONE explicaciones
sobre patrones emergentes — SIEMPRE como hipótesis con nivel de confianza,
nunca como hechos. Es lo que convierte el bot en un analista autónomo.

Corre en un job periódico y guarda el último informe; /hipotesis lo pide
al momento. Requiere ANTHROPIC_API_KEY.
"""

import json
import os

import requests

from db import get_conn, get_setting, set_setting

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"
# Haiku por defecto (mucho más barato); tuneable con HYPOTHESES_MODEL.
MODEL = os.getenv("HYPOTHESES_MODEL", "claude-haiku-4-5-20251001")


def _gather_state() -> dict:
    """Resumen compacto del estado del sistema para la IA."""
    state = {}
    try:
        from clusters import find_clusters
        cs = find_clusters()
        state["clusters"] = [
            {"lider": c.get("leader"), "miembros": c["size"],
             "tokens_comunes": c["shared_tokens"],
             "seguidores": [o["alias"] for o in c.get("order", [])[1:5]]}
            for c in cs[:6]]
    except Exception:
        state["clusters"] = []
    try:
        from alpha import top_alpha
        state["descubridores_alfa"] = [
            {"alias": w["alias"], "alpha": w["alpha_score"],
             "originality": w["originality"],
             "adelanto_min": w["avg_lead_min"]}
            for w in top_alpha(8)]
    except Exception:
        state["descubridores_alfa"] = []
    try:
        from similarity import rising_stars
        state["estrellas_emergentes"] = [
            {"alias": w["alias"], "parecida_a": w["like"], "sim": w["sim"],
             "apariciones": w["n"]}
            for w in rising_stars(limit=6)]
    except Exception:
        state["estrellas_emergentes"] = []
    conn = get_conn()
    try:
        state["conteo_por_grado"] = {r["grade"]: r["c"] for r in conn.execute(
            "SELECT grade, COUNT(*) c FROM wallets WHERE grade IS NOT NULL "
            "GROUP BY grade").fetchall()}
    except Exception:
        state["conteo_por_grado"] = {}
    finally:
        conn.close()
    return state


PROMPT = """Eres un analista on-chain autónomo del sistema WALLET EDGE (Solana). Observa este estado del grafo de billeteras y propón HIPÓTESIS sobre patrones emergentes que el sistema no calcula explícitamente.

ESTADO ACTUAL:
{estado}

Reglas:
- Propón 3-5 hipótesis CORTAS y accionables en español.
- Cada una DEBE llevar un nivel de confianza (0-100%) y basarse en la evidencia del estado.
- Preséntalas SIEMPRE como hipótesis ("posiblemente", "podría"), NUNCA como hechos.
- Prioriza hallazgos sobre: nuevos líderes/clusters emergentes, descubridores de alfa, relaciones entre wallets, o cambios de comportamiento.
- Si la evidencia es débil, dilo y baja la confianza.

Formato de cada línea:
• [confianza XX%] <hipótesis>"""


def generate_hypotheses() -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    state = _gather_state()
    # Si no hay materia prima, no gastar IA
    if not any(state.get(k) for k in
               ("clusters", "descubridores_alfa", "estrellas_emergentes")):
        return None
    try:
        from ai_budget import can_call, record_call
        _c = get_conn()
        try:
            if not can_call(_c):
                print("· Hipótesis pospuestas: presupuesto de IA agotado")
                return None
        finally:
            _c.close()
    except Exception:
        pass
    prompt = PROMPT.format(estado=json.dumps(state, ensure_ascii=False,
                                             indent=1)[:4000])
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 700,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        text = text.strip()
        try:
            from ai_budget import record_call
            _c = get_conn()
            try:
                record_call(_c)
            finally:
                _c.close()
        except Exception:
            pass
    except Exception as e:
        print(f"· Motor de hipótesis falló: {e}")
        return None
    if text:
        conn = get_conn()
        try:
            set_setting(conn, "hypotheses", text[:2500])
            set_setting(conn, "hypotheses_ts", __import__("time").strftime(
                "%Y-%m-%d %H:%M"))
        finally:
            conn.close()
    return text


def hypotheses_text() -> str:
    if not ANTHROPIC_API_KEY:
        return ("🧪 El motor de hipótesis necesita ANTHROPIC_API_KEY "
                "configurada.")
    fresh = generate_hypotheses()
    if fresh:
        return "🧪 *Hipótesis del sistema* (generadas ahora)\n\n" + fresh + \
               "\n\n_Son hipótesis con nivel de confianza, no hechos._"
    conn = get_conn()
    try:
        stored = get_setting(conn, "hypotheses", None)
        ts = get_setting(conn, "hypotheses_ts", "")
    finally:
        conn.close()
    if stored:
        return f"🧪 *Hipótesis del sistema* ({ts})\n\n{stored}"
    return ("🧪 Aún no hay suficiente estructura (clusters, descubridores) "
            "para proponer hipótesis. Deja correr el descubrimiento unos días.")
