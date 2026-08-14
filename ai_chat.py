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
    """Foto de la base para la IA del chat.

    Rediseñada el 13/8/2026: la version anterior no incluia NADA del paper
    trading (la IA "respondia" sumando el PnL on-chain de las billeteras y
    vendiendolo como resultado del paper), la lista de rastreadas se
    cortaba por el tope de 8000 caracteres ("solo tienes 16 activas") y
    senales_totales (historico completo) se presentaba como si fuera de
    24 h. Ahora: bloque real de paper, top 30 en el ORDEN VERDADERO del
    ranking, campos minimos para caber en el tope, y una leyenda con las
    unidades para que el modelo no confunda de quien es cada cifra.
    """
    import time as _t
    conn = get_conn()
    hace24 = int(_t.time()) - 86400

    # El top 30 REAL (mismo orden que /top y que el copy trading)
    from db import top_wallets
    top30 = [{"puesto": i, "alias": r["alias"], "address": r["address"][:8],
              "score_ia": r["wallet_score"],
              "pnl_onchain_sol": r["pnl_total"]}
             for i, r in enumerate(top_wallets(conn, 30), 1)]

    # Paper trading DE VERDAD (la simulacion del dueño, en USD)
    pt24 = dict(conn.execute(
        """SELECT COUNT(*) cerradas, SUM(pnl_usd) pnl_usd,
                  SUM(pnl_usd_neto) pnl_neto_usd, SUM(stake_usd) invertido
           FROM paper_trades WHERE status<>'abierta'
             AND exit_ts >= ?""", (hace24,)).fetchone())
    pt_tot = dict(conn.execute(
        """SELECT COUNT(*) cerradas, SUM(pnl_usd) pnl_usd,
           (SELECT COUNT(*) FROM paper_trades WHERE status='abierta') abiertas
           FROM paper_trades WHERE status<>'abierta'""").fetchone())
    pt_ult = [dict(r) for r in conn.execute(
        """SELECT symbol, exit_reason, pnl_usd, pnl_usd_neto
           FROM paper_trades WHERE status<>'abierta'
           ORDER BY exit_ts DESC LIMIT 5""").fetchall()]

    senales = [dict(r) for r in conn.execute(
        """SELECT wallet, symbol, side, sol, ts, chg_24h
           FROM signals ORDER BY ts DESC LIMIT 10""").fetchall()]
    tot = {
        "billeteras": conn.execute(
            "SELECT COUNT(*) c FROM wallets").fetchone()["c"],
        "rastreadas_con_estrella": conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1").fetchone()["c"],
        "descartadas_bots": conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_bot=1").fetchone()["c"],
        "tokens_ganadores": conn.execute(
            "SELECT COUNT(*) c FROM winning_tokens").fetchone()["c"],
        "senales_historico_completo": conn.execute(
            "SELECT COUNT(*) c FROM signals").fetchone()["c"],
        "senales_24h": conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE ts >= ?",
            (hace24,)).fetchone()["c"],
    }
    conn.close()
    return {
        "leyenda": {
            "pnl_onchain_sol": "ganancia DE ESA BILLETERA en la cadena, "
                               "en SOL. NO es dinero del dueño ni del paper",
            "paper_trading": "la simulacion de copy trading del dueño, en "
                             "DOLARES (stakes de ~76 USD). Es la unica "
                             "cifra que es 'suya'",
            "senales_historico_completo": "desde el inicio del sistema, "
                                          "NO son de hoy",
            "top_30": "el orden REAL del ranking que alerta y se copia",
        },
        "totales": tot,
        "paper_trading": {"ultimas_24h_usd": pt24, "acumulado_usd": pt_tot,
                          "ultimas_cerradas": pt_ult},
        "top_30": top30,
        "senales_recientes": senales,
    }


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
        "del snapshot JSON y respetando su 'leyenda' (las unidades de cada "
        "bloque estan ahi). Mapa del snapshot: 'totales' trae los conteos "
        "(ojo: senales_historico_completo es DESDE EL INICIO, lo de hoy es "
        "senales_24h; el total de ⭐ es rastreadas_con_estrella, NO el "
        "largo de ninguna lista); 'top_30' es el ranking REAL que alerta y "
        "se copia, con su puesto — si una ⭐ no aparece ahi, existe igual "
        "pero fuera del top; 'paper_trading' es la simulacion de copy "
        "trading del dueño EN DOLARES y es la UNICA cifra que es dinero "
        "'suyo' — el pnl_onchain_sol de una billetera es ganancia DE ESA "
        "billetera en la cadena, jamas la presentes como del dueño ni "
        "como del paper. ts es epoch; chg_1h/chg_24h son % del token tras "
        "la señal. Si 'billetera_consultada' existe, la pregunta es sobre "
        "esa billetera: responde con sus tenencias (tokens_actuales), lo "
        "invertido (sol_cost) y su profit realizado (realized_sol). "
        "PROHIBIDO inventar cifras o conceptos que no esten en el "
        "snapshot. Responde en español, breve y directo, abrevia "
        "direcciones a 8 caracteres. Si los datos no alcanzan, dilo claro "
        "y sugiere el comando exacto (/paper, /salidas, /top, /senales, "
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
