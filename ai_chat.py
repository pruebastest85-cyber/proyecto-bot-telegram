"""
Chat con la base de datos: /preguntar <pregunta en lenguaje natural>.

Arma un snapshot compacto de la base (billeteras, señales recientes con
resultados, totales) y se lo pasa a Claude para que responda preguntas
como "¿qué billetera tuvo mejor ROI este mes?" o "¿quién acumula hoy?".
"""

import json
import os

import requests

from db import get_conn

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"


def _snapshot() -> dict:
    conn = get_conn()
    wallets = [dict(r) for r in conn.execute(
        """SELECT address, alias, ai_class, score, wallet_score,
                  winning_tokens_count, pnl_30d, pnl_total, is_tracked
           FROM wallets WHERE is_bot=0
           ORDER BY score DESC LIMIT 15""").fetchall()]
    senales = [dict(r) for r in conn.execute(
        """SELECT wallet, mint, symbol, side, sol, ts, chg_1h, chg_24h, mc
           FROM signals ORDER BY ts DESC LIMIT 20""").fetchall()]
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
            "senales_recientes": senales}


def answer_question(pregunta: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "Falta ANTHROPIC_API_KEY para usar el chat."
    try:
        data = _snapshot()
    except Exception as e:
        return f"No pude leer la base: {e}"
    prompt = (
        "Eres el analista del sistema de rastreo de billeteras rentables "
        "en Solana del usuario. Responde su pregunta usando SOLO los datos "
        "del snapshot JSON (campos en SOL salvo mc que es USD; ts es epoch; "
        "chg_1h/chg_24h son % de cambio del token tras la señal). "
        "Responde en español, breve y directo, abrevia direcciones a los "
        "primeros 8 caracteres. Si los datos no alcanzan para responder, "
        "dilo honestamente y sugiere qué comando usar (/top, /senales, "
        "/perfil, /ficha).\n\n"
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
