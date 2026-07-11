"""
Track record de señales: mide qué pasó con el precio de cada token
1h y 24h después de cada señal, y arma la estadística por billetera.

Esto le da a la IA el dato definitivo para decidir si una billetera
vale la pena: no cómo se ve su historial, sino cuántas de sus señales
REALMENTE ganaron dentro de nuestro sistema.
"""

import time

import requests

import config
from db import get_conn

HOUR = 3600
DAY = 86400

# Hitos de subida (%) que disparan alerta, una sola vez cada uno
MILESTONES = (50, 100, 200, 500, 1000)
WATCH_HOURS = 48   # cuánto tiempo vigilamos el precio tras la señal


def _price(mint: str) -> float | None:
    """Precio actual en USD según DexScreener."""
    try:
        r = requests.get(config.DEXSCREENER_TOKEN.format(address=mint),
                         timeout=15)
        pairs = (r.json() or {}).get("pairs") or []
        if pairs:
            p = pairs[0].get("priceUsd")
            return float(p) if p else None
    except (requests.RequestException, ValueError, TypeError):
        pass
    return None


def _alert_milestone(conn, s, pct: float, price: float):
    """Alerta al admin cuando un token señalado cruza un hito de subida."""
    hito = max((m for m in MILESTONES if pct >= m), default=None)
    if hito is None or (s["alerted_pct"] or 0) >= hito:
        return
    try:
        from realtime import tg_send
    except Exception:
        return
    w = conn.execute("SELECT alias FROM wallets WHERE address=?",
                     (s["wallet"],)).fetchone()
    alias = (w["alias"] if w and w["alias"] else f"{s['wallet'][:8]}…")
    hace = (time.time() - s["ts"]) / 3600
    simbolo = s["symbol"] or s["mint"][:8]
    tg_send(
        f"🚀 *+{pct:.0f}% desde la señal*\n\n"
        f"Token: *{simbolo}*\n`{s['mint']}`\n"
        f"Lo compró: 👤 *{alias}* hace {hace:.1f}h\n"
        f"Precio señal: ${s['price_usd']:.8g} → ahora: ${price:.8g}\n\n"
        f"📊 dexscreener.com/solana/{s['mint']}")
    conn.execute("UPDATE signals SET alerted_pct=? WHERE signature=?",
                 (float(hito), s["signature"]))
    conn.commit()
    print(f"🚀 Alerta de subida: {simbolo} +{pct:.0f}%")


def track_outcomes() -> int:
    """
    1) Rellena price_1h/price_24h (y % de cambio) de las señales de
       compra que ya cumplieron la edad necesaria.
    2) Vigila el precio de las señales recientes (<48h) y alerta al
       cruzar hitos de subida (+50%, +100%, +200%, +500%, +1000%).
    Pensado para correr como job periódico (cada ~15 min).
    """
    now = time.time()
    conn = get_conn()
    rows = conn.execute(
        """SELECT signature, wallet, mint, ts, price_usd, price_1h,
                  price_24h, alerted_pct, symbol
           FROM signals
           WHERE side='compra' AND price_usd IS NOT NULL AND price_usd > 0
             AND (price_1h IS NULL OR price_24h IS NULL OR ts >= ?)
           ORDER BY ts DESC LIMIT 30""",
        (int(now - WATCH_HOURS * HOUR),)).fetchall()
    updated = 0
    for s in rows:
        base = s["price_usd"]
        p = _price(s["mint"])
        time.sleep(config.DEXSCREENER_DELAY)
        if not p:
            continue
        pct = (p / base - 1) * 100
        if s["price_1h"] is None and now - s["ts"] >= HOUR:
            conn.execute(
                "UPDATE signals SET price_1h=?, chg_1h=? WHERE signature=?",
                (p, pct, s["signature"]))
            updated += 1
        if s["price_24h"] is None and now - s["ts"] >= DAY:
            conn.execute(
                "UPDATE signals SET price_24h=?, chg_24h=? WHERE signature=?",
                (p, pct, s["signature"]))
            updated += 1
        # Alertas de subida solo para señales recientes
        if now - s["ts"] <= WATCH_HOURS * HOUR:
            _alert_milestone(conn, s, pct, p)
    conn.commit()
    conn.close()
    if updated:
        print(f"📈 Track record: {updated} mediciones de señales actualizadas")
    return updated


def wallet_track_record(conn, wallet: str) -> dict | None:
    """
    Estadística de las últimas señales de compra medidas de una billetera:
    tasa de acierto y cambio promedio a 1h y 24h. None si aún no hay datos.
    """
    rows = conn.execute(
        """SELECT chg_1h, chg_24h FROM signals
           WHERE wallet=? AND side='compra'
             AND (chg_1h IS NOT NULL OR chg_24h IS NOT NULL)
           ORDER BY ts DESC LIMIT 20""", (wallet,)).fetchall()
    if not rows:
        return None
    c1 = [r["chg_1h"] for r in rows if r["chg_1h"] is not None]
    c24 = [r["chg_24h"] for r in rows if r["chg_24h"] is not None]
    out = {"senales_medidas": len(rows)}
    if c1:
        out["tasa_acierto_1h_pct"] = round(
            100 * sum(1 for x in c1 if x > 0) / len(c1))
        out["cambio_promedio_1h_pct"] = round(sum(c1) / len(c1), 1)
    if c24:
        out["tasa_acierto_24h_pct"] = round(
            100 * sum(1 for x in c24 if x > 0) / len(c24))
        out["cambio_promedio_24h_pct"] = round(sum(c24) / len(c24), 1)
    return out


def format_track_record(tr: dict | None) -> str:
    """Línea corta para mensajes de Telegram."""
    if not tr:
        return ""
    partes = []
    if "tasa_acierto_24h_pct" in tr:
        partes.append(f"acierto 24h: {tr['tasa_acierto_24h_pct']}% "
                      f"({tr['cambio_promedio_24h_pct']:+.0f}% prom)")
    elif "tasa_acierto_1h_pct" in tr:
        partes.append(f"acierto 1h: {tr['tasa_acierto_1h_pct']}% "
                      f"({tr['cambio_promedio_1h_pct']:+.0f}% prom)")
    if not partes:
        return ""
    return (f"🎯 Track record ({tr['senales_medidas']} señales): "
            + " · ".join(partes))
