"""
Chat con la base de datos: /preguntar <pregunta en lenguaje natural>.

Arma un snapshot compacto de la base (billeteras, señales recientes con
resultados, posiciones/transacciones, totales) y se lo pasa a Claude para
que responda preguntas como "¿qué billetera tuvo mejor ROI este mes?",
"¿quién acumula hoy?" o "¿cuánto profit lleva la billetera X y qué tiene?".
Si la pregunta menciona una dirección, se adjunta el detalle de esa
billetera (tokens que tiene, invertido, profit realizado, compras/ventas).
"""

import json
import os
import re

import requests

from db import get_conn, wallet_positions_summary

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"

# Detecta una dirección estilo Solana dentro de la pregunta
_ADDR = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def _snapshot() -> dict:
    conn = get_conn()
    wallets = [dict(r) for r in conn.execute(
        """SELECT address, alias, ai_class, score, wallet_score,
                  winning_tokens_count, pnl_30d, pnl_total, is_tracked
           FROM wallets WHERE is_bot=0
           ORDER BY score DESC LIMIT 15""").fetchall()]
    # TODAS las billeteras rastreadas (⭐) — sin límite, para no confundir
    rastreadas = [dict(r) for r in conn.execute(
        """SELECT address, alias, ai_class, score, wallet_score,
                  winning_tokens_count, pnl_30d, pnl_total
           FROM wallets WHERE is_tracked=1
           ORDER BY score DESC""").fetchall()]
    senales = [dict(r) for r in conn.execute(
        """SELECT wallet, mint, symbol, side, sol, ts, chg_1h, chg_24h, mc
           FROM signals ORDER BY ts DESC LIMIT 20""").fetchall()]
    posiciones = [dict(r) for r in conn.execute(
        """SELECT wallet, mint, tokens, sol_cost, realized_sol, buys, sells
           FROM positions ORDER BY last_ts DESC LIMIT 25""").fetchall()]
    tot = {
        "billeteras": conn.execute(
            "SELECT COUNT(*) c FROM wallets").fetchone()["c"],
        "rastreadas": conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"],
        "descartadas_bots": conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_bot=1").fetchone()["c"],
        "tokens_ganadores": conn.execute(
            "SELECT COUNT(*) c FROM winning_tokens").fetchone()["c"],
        "senales_totales": conn.execute(
            "SELECT COUNT(*) c FROM signals").fetchone()["c"],
    }
    conn.close()
    return {"totales": tot, "top_billeteras": wallets,
            "billeteras_rastreadas": rastreadas,
            "senales_recientes": senales,
            "posiciones_recientes": posiciones}


def answer_question(pregunta: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "Falta ANTHROPIC_API_KEY para usar el chat."
    try:
        data = _snapshot()
    except Exception as e:
        return f"No pude leer la base: {e}"

    # Si la pregunta menciona una dirección, adjunta su detalle de posiciones
    detalle_billetera = None
    m = _ADDR.search(pregunta or "")
    if m:
        try:
            conn = get_conn()
            detalle_billetera = wallet_positions_summary(conn, m.group(0))
            conn.close()
        except Exception:
            detalle_billetera = None
    if detalle_billetera:
        data["billetera_consultada"] = {"address": m.group(0),
                                        **detalle_billetera}

    prompt = (
        "Eres el analista del sistema de rastreo de billeteras rentables "
        "en Solana del usuario. Responde su pregunta usando SOLO los datos "
        "del snapshot JSON. 'billeteras_rastreadas' es la lista COMPLETA de "
        "billeteras ⭐ que el sistema monitorea (úsala para '¿cuántas/cuáles "
        "rastrea?'); 'top_billeteras' es solo el top 15 por score, así que si "
        "una ⭐ no aparece ahí NO significa que falte. Notas de campos: montos "
        "en SOL salvo mc (USD); "
        "ts es epoch; chg_1h/chg_24h son % del token tras la señal; "
        "'posiciones_recientes' y 'billetera_consultada' traen lo que cada "
        "billetera tiene ahora (tokens_actuales), lo invertido (sol_cost), "
        "el profit ya realizado (realized_sol) y cuántas compras/ventas "
        "hizo. Si 'billetera_consultada' existe, la pregunta es sobre esa "
        "billetera: responde con sus tenencias y profit. "
        "Responde en español, breve y directo, abrevia direcciones a los "
        "primeros 8 caracteres. Si los datos no alcanzan, dilo y sugiere un "
        "comando (/top, /senales, /perfil, /ficha).\n\n"
        f"SNAPSHOT: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
        f"PREGUNTA: {pregunta}")
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        return text.strip() or "La IA no devolvió respuesta."
    except Exception as e:
        return f"Error consultando a la IA: {e}"
