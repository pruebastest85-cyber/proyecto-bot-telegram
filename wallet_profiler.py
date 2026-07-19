"""
Perfilador de billeteras: investiga a fondo una billetera candidata.

Responde las preguntas clave:
  - ¿Sigue activa? (última transacción, ritmo reciente)
  - ¿En qué otros tokens operó y cuánto ganó/perdió en cada uno?
  - ¿Cuál es su PnL realizado aproximado en SOL? (total y últimos 30 días)
  - ¿Parece bot? (frecuencia de transacciones)

Método: descarga sus últimas ~1000 transacciones parseadas (Helius),
detecta compras (SOL sale + token entra) y ventas (token sale + SOL
entra) y agrega por token. El SOL de cada swap se mide SIN comisiones
de red ni propinas de Jito, para no sesgar el costo. El PnL realizado
es SOL que entró por ventas menos SOL que salió por compras; el PnL
neto le suma el valor de los tokens que aún mantiene (ver unrealized_pnl).
El PnL 30d se calcula sobre las txs de la muestra de los últimos 30 días.
"""

import time
from collections import defaultdict

import requests

import config

LAMPORTS = 1_000_000_000
WSOL = "So11111111111111111111111111111111111111112"
# Mints que NO son "apuestas" de memecoin: se saltan como posición. WSOL
# se trata aparte como efectivo (1 WSOL = 1 SOL).
STABLE_MINTS = {
    WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# Cuentas de propina de Jito (MEV bundles). La propina NO es precio del
# token: se resta para no inflar el costo de las operaciones.
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}


def _fetch_txs(address: str, pages: int = 10) -> list[dict]:
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
    """
    SOL del SWAP para la billetera (negativo = compró, positivo = vendió).

    Parte del cambio de SOL nativo y RESTA lo que no es precio del token:
    la comisión de red (base + prioridad, campo `fee`) y las propinas de
    Jito. Antes se usaba el cambio bruto, lo que inflaba el costo de las
    compras y encogía las ventas → PnL sesgado sistemáticamente a negativo.

    Nota: no descuenta el rent de las cuentas de token (~0.002 SOL, casi
    siempre recuperable al cerrar la posición); su efecto es marginal.
    """
    # 1) Cambio de SOL nativo bruto. accountData ya incluye la comisión;
    #    la suma de nativeTransfers NO la incluye (se marca con from_balance).
    raw = None
    for acc in (tx.get("accountData") or []):
        if acc.get("account") == wallet:
            raw = int(acc.get("nativeBalanceChange", 0)) / LAMPORTS
            break
    from_balance = raw is not None
    if raw is None:
        raw = 0.0
        for n in (tx.get("nativeTransfers") or []):
            if n.get("fromUserAccount") == wallet:
                raw -= int(n.get("amount", 0)) / LAMPORTS
            if n.get("toUserAccount") == wallet:
                raw += int(n.get("amount", 0)) / LAMPORTS

    # 2) Comisión de red: solo si la paga esta billetera y solo si está
    #    reflejada en raw (rama accountData).
    fee = 0.0
    if from_balance and tx.get("feePayer") == wallet:
        try:
            fee = int(tx.get("fee", 0)) / LAMPORTS
        except (TypeError, ValueError):
            fee = 0.0

    # 3) Propinas de Jito enviadas por esta billetera (siempre en raw).
    tip = 0.0
    for n in (tx.get("nativeTransfers") or []):
        if (n.get("fromUserAccount") == wallet
                and n.get("toUserAccount") in JITO_TIP_ACCOUNTS):
            tip += int(n.get("amount", 0)) / LAMPORTS

    # fee y tip siempre restan SOL a la billetera; los devolvemos para
    # quedarnos solo con el SOL que realmente pagó/recibió por el token.
    return raw + fee + tip


def profile_wallet(address: str, with_holdings: bool = True) -> dict:
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
        "unrealized_sol": 0.0,     # valor en SOL de tokens aún en cartera
        "net_pnl_sol": 0.0,        # realizado + no realizado (mark-to-market)
        "held_tokens": 0,
        "priced_tokens": 0,
        "metrics": {},        # métricas quant (ver wallet_metrics)
        "possible_bot": False,
    }
    if not txs:
        return result

    result["last_tx_ts"] = txs[0].get("timestamp")
    tokens = defaultdict(lambda: {"sol_out": 0.0, "sol_in": 0.0,
                                  "buys": 0, "sells": 0, "symbol": "",
                                  "first_buy_ts": None, "first_sell_ts": None,
                                  "holding_sol": 0.0})
    timestamps = []
    buy_sizes = []

    for tx in txs:
        ts = tx.get("timestamp") or 0
        timestamps.append(ts)   # incluye fallidas: cuenta para frecuencia/bot
        if tx.get("transactionError"):
            continue
        if now - ts <= 7 * 86400:
            result["tx_7d"] += 1

        # Lado "efectivo" del swap en SOL (nativo, ya sin fee/propina).
        delta = _sol_delta(tx, address)
        # C: si el SOL nativo casi no se movió pero la billetera movió WSOL
        # (SOL pre-envuelto), usar ese WSOL como efectivo. Solo cuando el
        # nativo es ~0, para NO duplicar el caso normal financiado con SOL.
        if abs(delta) <= 0.001:
            wsol = 0.0
            for t in (tx.get("tokenTransfers") or []):
                if t.get("mint") != WSOL:
                    continue
                try:
                    amt = float(t.get("tokenAmount") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                if t.get("toUserAccount") == address:
                    wsol += amt
                elif t.get("fromUserAccount") == address:
                    wsol -= amt
            if abs(wsol) > 0.001:
                delta = wsol
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
                buy_sizes.append(round(abs(delta), 2))
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
    result["closed_positions"] = len(closed)

    # ── Huellas de bot / MEV / market maker ─────────────────────────
    # Flips: % de posiciones cerradas en menos de 1 minuto (MEV/sniper bot)
    result["flips_1min_pct"] = (
        round(100 * sum(1 for h in holds if h <= 1) / len(holds))
        if holds else None)
    # Actividad 24/7: horas del día (0-23) con transacciones; un humano duerme
    result["active_hours_24"] = len(
        {time.gmtime(t).tm_hour for t in timestamps if t})
    # Compras de tamaño idéntico: % de la compra más repetida
    if len(buy_sizes) >= 5:
        mas_comun = max(buy_sizes.count(s) for s in set(buy_sizes))
        result["uniform_buys_pct"] = round(100 * mas_comun / len(buy_sizes))
    else:
        result["uniform_buys_pct"] = None
    # Market maker: tokens operados en ambas direcciones con posición neta ~0
    mm = sum(1 for i in tokens.values()
             if i["buys"] >= 3 and i["sells"] >= 3
             and abs(i["pnl_sol"]) <= 0.05 * (i["sol_in"] + i["sol_out"]))
    result["mm_tokens"] = mm

    result["net_pnl_sol"] = round(result["pnl_total_sol"], 2)
    result["tokens"] = dict(tokens)

    # Métricas quant (Profit Factor, Sharpe, Expectancy, Drawdown, ROI…)
    try:
        from wallet_metrics import trade_metrics
        result["metrics"] = trade_metrics(result["tokens"])
    except Exception as e:
        print(f"  · Métricas quant no disponibles: {e}")

    # ── PnL no realizado: valora los tokens que la billetera aún tiene ──
    # El realizado castiga a los que acumulan (compra contabilizada como
    # gasto, venta aún no ocurrida). Sumar el valor de mercado de la bolsa
    # abierta da el PnL NETO real (mark-to-market).
    if with_holdings:
        try:
            from unrealized_pnl import holdings_value
            hv = holdings_value(address)
            result["unrealized_sol"] = hv["unrealized_sol"]
            result["net_pnl_sol"] = round(
                result["pnl_total_sol"] + hv["unrealized_sol"], 2)
            result["held_tokens"] = hv["held_tokens"]
            result["priced_tokens"] = hv["priced_tokens"]
            for mint, val in hv["holdings"].items():
                if mint in result["tokens"]:
                    result["tokens"][mint]["holding_sol"] = val
        except Exception as e:
            print(f"  · PnL no realizado no disponible: {e}")

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
    huellas = []
    if p.get("flips_1min_pct"):
        huellas.append(f"flips <1min: {p['flips_1min_pct']}%")
    if p.get("active_hours_24"):
        huellas.append(f"horas activas: {p['active_hours_24']}/24")
    if p.get("uniform_buys_pct"):
        huellas.append(f"compras idénticas: {p['uniform_buys_pct']}%")
    if p.get("mm_tokens"):
        huellas.append(f"tokens estilo MM: {p['mm_tokens']}")
    if huellas:
        lines.append("🤖 Huellas bot/MM: " + " · ".join(huellas) + "\n")

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
    if p.get("held_tokens"):
        cob = f"{p.get('priced_tokens', 0)}/{p['held_tokens']} con precio"
        lines.append(f"🎒 *En cartera (sin vender):* "
                     f"{p.get('unrealized_sol', 0.0):+.2f} SOL ({cob})")
        lines.append(f"🧾 *PnL neto (realizado + cartera):* "
                     f"{p.get('net_pnl_sol', p['pnl_total_sol']):+.2f} SOL")
    if p.get("win_rate_pct") is not None:
        lines.append(f"🎯 *Win rate (cerradas):* {p['win_rate_pct']}%")
    try:
        from wallet_metrics import format_metrics
        ml = format_metrics(p.get("metrics") or {})
        if ml:
            lines.append("")
            lines.extend(ml)
    except Exception:
        pass
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

    lines.append("\n_PnL realizado sobre las últimas ~1000 txs. "
                 "«En cartera» valora las posiciones abiertas a precio actual "
                 "(DexScreener); «neto» = realizado + cartera._")
    lines.append(f"🔗 gmgn.ai/sol/address/{addr}")
    return "\n".join(lines)
