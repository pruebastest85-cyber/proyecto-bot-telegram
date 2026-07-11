"""
Wallet Score 0-100 y patrones de comportamiento por billetera.

Score compuesto por: ROI (25), win rate (25), track record real de
señales (20), retención (15) y consistencia (15). El riesgo se deriva
de win rate, concentración de ganancias y velocidad de venta.

Patrón: rango de Market Cap donde la billetera suele entrar, calculado
de sus señales registradas (percentiles 25-75).
"""


def compute_score(p: dict, track: dict | None = None) -> dict:
    """Calcula el Wallet Score a partir del perfil (y track record)."""
    invested = sum(i["sol_out"] for i in p["tokens"].values())
    roi = (100 * p["pnl_total_sol"] / invested) if invested > 0.5 else 0.0
    p_roi = max(0.0, min(25.0, 12.5 + roi / 20))

    wr = p.get("win_rate_pct")
    p_wr = wr / 4 if wr is not None else 8.0

    if track and track.get("tasa_acierto_24h_pct") is not None:
        p_tr = track["tasa_acierto_24h_pct"] / 5
    elif track and track.get("tasa_acierto_1h_pct") is not None:
        p_tr = track["tasa_acierto_1h_pct"] / 6
    else:
        p_tr = 8.0  # neutro: sin señales medidas aún

    h = p.get("hold_median_min")
    if h is None:
        p_hold = 7.0
    elif h < 2:
        p_hold = 2.0     # vende en segundos: no copiable
    elif h < 10:
        p_hold = 8.0
    elif h <= 2880:
        p_hold = 15.0    # sweet spot: 10 min a 48 h
    else:
        p_hold = 10.0

    ganancias = [i["pnl_sol"] for i in p["tokens"].values() if i["pnl_sol"] > 0]
    if ganancias:
        conc = max(ganancias) / sum(ganancias)
        p_cons = max(0.0, min(15.0, 15 * (1 - conc) + 3))
    else:
        conc, p_cons = 1.0, 5.0

    score = int(round(min(100.0, p_roi + p_wr + p_tr + p_hold + p_cons)))

    if (wr or 0) >= 65 and conc < 0.5 and (h or 0) >= 10:
        riesgo = "Bajo"
    elif (wr or 0) < 40 or conc > 0.75 or (h is not None and h < 5):
        riesgo = "Alto"
    else:
        riesgo = "Medio"

    return {"score": score, "roi_pct": round(roi), "win_rate": wr,
            "trades": p.get("closed_positions", 0),
            "pnl_sol": round(p["pnl_total_sol"], 1),
            "pnl_30d": round(p.get("pnl_30d_sol", 0.0), 1),
            "riesgo": riesgo}


def format_ficha(address: str, s: dict, alias: str | None = None,
                 track_line: str = "") -> str:
    """Ficha estilo tarjeta para Telegram."""
    head = f"👤 *{alias}*\n" if alias else ""
    wr = f"{s['win_rate']}%" if s["win_rate"] is not None else "?"
    lines = [f"{head}`{address}`", "",
             f"🧮 *Wallet Score: {s['score']}/100*",
             f"ROI (muestra): {s['roi_pct']:+d}%",
             f"Win Rate: {wr}",
             f"Trades cerrados: {s['trades']}",
             f"PnL: {s['pnl_sol']:+.1f} SOL (30d: {s['pnl_30d']:+.1f})",
             f"Nivel de riesgo: {s['riesgo']}"]
    if track_line:
        lines.append(track_line)
    lines.append("\n_ROI y PnL sobre las últimas ~300 txs._")
    return "\n".join(lines)


def wallet_pattern(conn, wallet: str) -> dict | None:
    """Rango de MC (percentil 25-75) donde la billetera suele comprar."""
    rows = conn.execute(
        """SELECT mc FROM signals WHERE wallet=? AND side='compra'
           AND mc IS NOT NULL AND mc > 0 ORDER BY ts DESC LIMIT 30""",
        (wallet,)).fetchall()
    mcs = sorted(r["mc"] for r in rows)
    if len(mcs) < 3:
        return None
    return {"n": len(mcs),
            "mc_p25": mcs[len(mcs) // 4],
            "mc_p75": mcs[(3 * len(mcs)) // 4]}


def _mc(v: float) -> str:
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def format_pattern(pat: dict | None, mc_actual=None) -> str:
    if not pat:
        return ""
    s = (f"📐 Patrón: suele entrar con MC {_mc(pat['mc_p25'])}–"
         f"{_mc(pat['mc_p75'])} ({pat['n']} compras)")
    if mc_actual:
        dentro = pat["mc_p25"] * 0.5 <= mc_actual <= pat["mc_p75"] * 2
        s += " · este token: " + ("✅ en su rango" if dentro
                                  else "⚠️ FUERA de su rango")
    return s
