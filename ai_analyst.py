"""
Analista IA: usa la API de Claude para clasificar billeteras candidatas.

Cuando una billetera alcanza el umbral de apariciones (⭐ candidata),
este módulo la perfila y le pide a Claude un veredicto:
  - clasificacion: trader / sniper / bot / insider / copiador / indeterminado
  - seguir: true/false (¿vale la pena rastrearla?)
  - razon: explicación breve en español

Los veredictos se guardan en la tabla wallets (columnas ai_*) y las
billeteras descartadas por la IA pierden su ⭐ automáticamente.

Modelo: claude-haiku (económico; ~fracciones de centavo por análisis).
Requiere la variable de entorno ANTHROPIC_API_KEY.
"""

import json
import os

import requests

from wallet_profiler import profile_wallet

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"

PROMPT = """Eres un analista experto en trading on-chain de Solana. Analiza esta billetera candidata y clasifícala.

DATOS DEL PERFIL (últimas ~300 transacciones):
{perfil}

EVIDENCIA (por qué está en nuestra base):
{evidencia}

Clasificaciones posibles:
- "trader": opera con criterio, PnL positivo, ritmo humano. VALE seguirla.
- "sniper": entra muy temprano en lanzamientos con método consistente y gana. VALE seguirla.
- "insider": compra de primerísimo en tokens que luego explotan; probable dev/equipo. NO vale (no replicable).
- "bot": frecuencia inhumana de transacciones, spray a decenas de tokens. NO vale.
- "copiador": parece replicar a otros con retraso. NO vale.
- "indeterminado": datos insuficientes.

Considera: PnL realizado, nº de tokens operados vs días, tamaños de compra, posición de compra (buy_rank) en la evidencia, ratio ganadores/perdedores.

Responde SOLO con JSON válido, sin markdown ni texto extra:
{{"clasificacion": "...", "seguir": true/false, "confianza": 0-100, "razon": "máximo 2 frases en español"}}"""


def _ensure_columns(conn):
    """Agrega columnas de IA a la tabla wallets si no existen."""
    for col, typ in [("ai_class", "TEXT"), ("ai_follow", "INTEGER"),
                     ("ai_reason", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE wallets ADD COLUMN {col} {typ}")
        except Exception:
            pass  # ya existe
    conn.commit()


def _resumir_perfil(p: dict) -> str:
    """Compacta el perfil para no gastar tokens de más."""
    import time as _t
    days_ago = ((_t.time() - p["last_tx_ts"]) / 86400) if p["last_tx_ts"] else None
    tokens = sorted(p["tokens"].items(), key=lambda x: x[1]["pnl_sol"],
                    reverse=True)
    top = [f"{m[:8]}: {i['pnl_sol']:+.2f} SOL ({i['buys']}c/{i['sells']}v)"
           for m, i in tokens[:8]]
    bottom = [f"{m[:8]}: {i['pnl_sol']:+.2f} SOL ({i['buys']}c/{i['sells']}v)"
              for m, i in tokens[-4:] if i["pnl_sol"] < 0]
    return json.dumps({
        "txs_muestreadas": p["tx_sampled"],
        "ultima_tx_hace_dias": round(days_ago, 1) if days_ago is not None else None,
        "txs_ultimos_7d": p["tx_7d"],
        "tokens_operados": len(p["tokens"]),
        "pnl_total_sol": round(p["pnl_total_sol"], 2),
        "posible_bot_por_frecuencia": p["possible_bot"],
        "mejores": top, "peores": bottom,
    }, ensure_ascii=False)


def ai_verdict(profile: dict, evidence_lines: list[str]) -> dict | None:
    """Llama a Claude y devuelve el veredicto como dict, o None si falla."""
    if not ANTHROPIC_API_KEY:
        return None
    prompt = PROMPT.format(perfil=_resumir_perfil(profile),
                           evidencia="\n".join(evidence_lines) or "(sin datos)")
    try:
        r = requests.post(
            API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        v = json.loads(text)
        if v.get("clasificacion") and isinstance(v.get("seguir"), bool):
            return v
    except Exception as e:
        print(f"  · Error en veredicto IA: {e}")
    return None


def evaluate_tracked(conn) -> int:
    """
    Perfila y clasifica con IA las billeteras ⭐ que aún no tienen
    veredicto. Descarta automáticamente bots/insiders/copiadores.
    Devuelve cuántas evaluó.
    """
    _ensure_columns(conn)
    rows = conn.execute(
        "SELECT address FROM wallets WHERE is_tracked=1 AND ai_class IS NULL"
    ).fetchall()
    if not rows:
        return 0

    evaluated = 0
    for row in rows:
        addr = row["address"]
        print(f"\n🧠 IA evaluando {addr[:16]}…")
        profile = profile_wallet(addr)
        if not profile["tx_sampled"]:
            print("  · Sin datos; se deja pendiente para el próximo ciclo")
            continue

        ev = conn.execute(
            "SELECT reason FROM appearances WHERE wallet=? LIMIT 6",
            (addr,)).fetchall()
        verdict = ai_verdict(profile, [e["reason"] for e in ev])
        if not verdict:
            continue

        seguir = 1 if verdict["seguir"] else 0
        conn.execute(
            """UPDATE wallets SET ai_class=?, ai_follow=?, ai_reason=?,
               is_tracked=?, is_bot=CASE WHEN ?='bot' THEN 1 ELSE is_bot END
               WHERE address=?""",
            (verdict["clasificacion"], seguir,
             verdict.get("razon", ""), seguir,
             verdict["clasificacion"], addr),
        )
        conn.commit()
        evaluated += 1
        icono = "✅" if seguir else "❌"
        print(f"  {icono} {verdict['clasificacion']} "
              f"({verdict.get('confianza', '?')}%): {verdict.get('razon','')}")
    return evaluated
  
