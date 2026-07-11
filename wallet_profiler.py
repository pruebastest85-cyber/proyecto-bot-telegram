"""
Perfilador de billeteras: investiga a fondo una billetera candidata.

Responde las preguntas clave:
  - ¿Sigue activa? (última transacción, ritmo reciente)
  - ¿En qué otros tokens operó y cuánto ganó/perdió en cada uno?
  - ¿Cuál es su PnL realizado aproximado en SOL? (total y últimos 30 días)
  - ¿Parece bot? (frecuencia de transacciones)

Método: descarga sus últimas ~300 transacciones parseadas (Helius),
detecta compras (SOL sale + token entra) y ventas (token sale + SOL
entra) y agrega por token. El PnL es REALIZADO y aproximado: SOL que
entró por ventas menos SOL que salió por compras; no incluye tokens
que aún mantiene sin vender. El PnL 30d se calcula sobre las txs de
la muestra que caen en los últimos 30 días.
"""

import time
from collections import defaultdict

import requests

import config

LAMPORTS = 1_000_000_000
STABLE_MINTS = {
    "So11111111111111111111111111111111111111112",  # WSOL
}


def _fetch_txs(address: str, pages: int = 3) -> list[dict]:
    url = config.HELIUS_PARSED_TX.format(address=address)
    all_txs, before = [], None
    for _ in range(pages):
        params = {"api-key": config.HELIUS_API_KEY, "limit": 100}
        if before:
            params["before"] = before
        try:
            r = requests.get(url, params=params, timeout=30)
            time.sleep(config.HELIUS_DELAY)
            if r.status_code == 429:
                time.sleep(15)
                r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            batch = r.json()
        except requests.RequestException as e:
            print(f"  · Error Helius en perfil: {e}")
            break
        if not isinstance(batch, list) or not batch:
            break
        all_txs.extend(batch)
        if len(batch) < 100:
            break
        before = batch[-1].get("signature")
    return all_txs


def _sol_delta(tx: dict, wallet: str) -> float:
    """SOL neto de la billetera en esta tx (negativo = gastó)."""
    for acc in (tx.get("accountData") or []):
        if acc.get("account") == wallet:
            return int(acc.get("nativeBalanceChange", 0)) / LAMPORTS
    total = 0.0
    for n in (tx.get("nativeTransfers") or []):
        if n.get("fromUserAccount") == wallet:
            total -= int(n.get("amount", 0)) / LAMPORTS
        if n.get("toUserAccount") == wallet:
            total += int(n.get("amount", 0)) / LAMPORTS
    return total


def profile_wallet(address: str) -> dict:
    txs = _fetch_txs(address)
    now = time.time()
    result = {
        "address": address,
        "tx_sampled": len(txs),
        "last_tx_ts": None,
        "tx_7d": 0,
        "tokens": {},        # mint -> métricas
        "pnl_total_sol": 0.0,
        "pnl_30d_sol": 0.0,
        "possible_bot": False,
    }
    if not txs:
        return result

    result["last_tx_ts"] = txs[0].get("timestamp")
    tokens = defaultdict(lambda: {"sol_out": 0.0, "sol_in": 0.0,
                                  "buys": 0, "sells": 0, "symbol": "",
                                  "first_buy_ts": None, "first_sell_ts": None})
    timestamps = []

    for tx in txs:
        ts = tx.get("timestamp") or 0
        timestamps.append(ts)
        if now - ts <= 7 * 86400:
            result["tx_7d"] += 1
        if tx.get("transactionError"):
            continue

        delta = _sol_delta(tx, address)
        reciente = (now - ts) <= 30 * 86400
        for t in (tx.get("tokenTransfers") or []):
            mint = t.get("mint")
            if not mint or mint in STABLE_MINTS:
                continue
            info = tokens[mint]
            # Compra: recibió token y su SOL bajó
            if t.get("toUserAccount") == address and delta < -0.001:
                info["buys"] += 1
                info["sol_out"] += abs(delta)
                if ts and (info["first_buy_ts"] is None or ts < info["first_buy_ts"]):
                    info["first_buy_ts"] = ts
                if reciente:
                    result["pnl_30d_sol"] += delta
                break  # una tx cuenta una vez
            # Venta: envió token y su SOL subió
            if t.get("fromUserAccount") == address and delta > 0.001:
                info["sells"] += 1
                info["sol_in"] += delta
                if ts and (info["first_sell_ts"] is None or ts < info["first_sell_ts"]):
                    info["first_sell_ts"] = ts
                if reciente:
                    result["pnl_30d_sol"] += delta
                break

    # ¿Bot? Más de BOT_TX_PER_HOUR_LIMIT txs/hora sostenidas en la muestra
    if len(timestamps) >= 20:
        span_h = max((max(timestamps) - min(timestamps)) / 3600, 0.01)
        if len(timestamps) / span_h > config.BOT_TX_PER_HOUR_LIMIT:
            result["possible_bot"] = True

    holds = []
    for mint, info in tokens.items():
        info["pnl_sol"] = info["sol_in"] - info["sol_out"]
        result["pnl_total_sol"] += info["pnl_sol"]
        if info["first_buy_ts"] and info["first_sell_ts"] \
                and info["first_sell_ts"] > info["first_buy_ts"]:
            holds.append((info["first_sell_ts"] - info["first_buy_ts"]) / 60)
    # Retención mediana: ¿vende en minutos o aguanta horas?
    if holds:
        holds.sort()
        result["hold_median_min"] = round(holds[len(holds) // 2], 1)
    else:
        result["hold_median_min"] = None
    # Win rate sobre posiciones cerradas
    closed = [i for i in tokens.values() if i["sells"] > 0]
    if closed:
        result["win_rate_pct"] = round(
            100 * sum(1 for i in closed if i["pnl_sol"] > 0) / len(closed))
    else:
        result["win_rate_pct"] = None
    result["tokens"] = dict(tokens)
    return result


def format_profile(p: dict) -> str:
    """Formatea el perfil para Telegram."""
    addr = p["address"]
    lines = [f"🔬 *Perfil de* `{addr[:16]}…`\n"]

    if not p["tx_sampled"]:
        return (f"🔬 Sin transacciones recuperadas para `{addr[:16]}…`. "
                "Puede ser una billetera nueva o hubo un error de API.")

    # Actividad
    if p["last_tx_ts"]:
        days = (time.time() - p["last_tx_ts"]) / 86400
        if days < 1:
            act = "🟢 ACTIVA (operó hoy)"
        elif days < 7:
            act = f"🟢 ACTIVA (última tx hace {days:.0f} días)"
        elif days < 30:
            act = f"🟡 Poco activa (última tx hace {days:.0f} días)"
        else:
            act = f"🔴 INACTIVA (última tx hace {days:.0f} días)"
        lines.append(f"{act} · {p['tx_7d']} txs en 7 días\n")

    if p["possible_bot"]:
        lines.append("⚠️ *Posible bot*: frecuencia de txs muy alta\n")

    # Tokens por PnL
    traded = [(m, i) for m, i in p["tokens"].items()
              if i["buys"] + i["sells"] > 0]
    traded.sort(key=lambda x: x[1]["pnl_sol"], reverse=True)

    wins = sum(1 for _, i in traded if i["pnl_sol"] > 0)
    closed = sum(1 for _, i in traded if i["sells"] > 0)
    lines.append(f"📈 *Tokens operados (muestra {p['tx_sampled']} txs):* "
                 f"{len(traded)}")
    if closed:
        lines.append(f"✅ Con ventas: {closed} · ganadores: {wins}")
    lines.append(f"💰 *PnL realizado (muestra):* {p['pnl_total_sol']:+.2f} SOL")
    lines.append(f"📅 *PnL últimos 30 días:* {p['pnl_30d_sol']:+.2f} SOL")
    if p.get("win_rate_pct") is not None:
        lines.append(f"🎯 *Win rate (cerradas):* {p['win_rate_pct']}%")
    if p.get("hold_median_min") is not None:
        h = p["hold_median_min"]
        ret = f"{h:.0f} min" if h < 120 else f"{h / 60:.1f} h"
        lines.append(f"⏱ *Retención mediana:* {ret}")
    lines.append("")

    lines.append("*Top operaciones:*")
    for mint, i in traded[:6]:
        lines.append(
            f"• `{mint[:10]}…` {i['pnl_sol']:+.2f} SOL "
            f"({i['buys']}c/{i['sells']}v)")
    if len(traded) > 6:
        lines.append(f"…y {len(traded) - 6} más")

    lines.append("\n_PnL realizado sobre las últimas ~300 txs; "
                 "no incluye posiciones aún abiertas._")
    lines.append(f"🔗 gmgn.ai/sol/address/{addr}")
    return "\n".join(lines)
