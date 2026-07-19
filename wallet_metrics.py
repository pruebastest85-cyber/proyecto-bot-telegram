"""
Métricas quant por billetera, calculadas SOBRE LOS DATOS QUE YA
DESCARGA wallet_profiler (no requiere APIs nuevas).

A partir del desglose por token (sol_out, sol_in, pnl_sol, sells,
first_sell_ts) calcula lo que un trader profesional miraría:

  - roi_avg / roi_median / roi_max : ROI por operación (%),
  - profit_factor  : ganancias totales / pérdidas totales,
  - expectancy_sol : PnL esperado por operación cerrada,
  - sharpe         : media/desviación de los ROI por operación,
  - max_drawdown_sol / _pct : peor caída pico-a-valle de la curva
    de resultados (operaciones cerradas en orden cronológico).

Una posición "cerrada" es un token con al menos una venta.
"""

from statistics import median, pstdev


def _roi(info: dict):
    out = info.get("sol_out") or 0.0
    return (100.0 * info["pnl_sol"] / out) if out > 0 else None


def trade_metrics(tokens: dict) -> dict:
    """Devuelve el bloque de métricas quant (todo None si no hay cerradas)."""
    m = {"closed": 0, "roi_avg": None, "roi_median": None, "roi_max": None,
         "profit_factor": None, "expectancy_sol": None, "sharpe": None,
         "max_drawdown_sol": None, "max_drawdown_pct": None}

    closed = [i for i in tokens.values() if i.get("sells", 0) > 0]
    if not closed:
        return m
    m["closed"] = len(closed)

    pnls = [i["pnl_sol"] for i in closed]
    rois = [r for r in (_roi(i) for i in closed) if r is not None]

    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses > 0:
        m["profit_factor"] = round(gains / losses, 2)
    elif gains > 0:
        m["profit_factor"] = 99.99          # sin pérdidas: tope simbólico
    m["expectancy_sol"] = round(sum(pnls) / len(pnls), 3)

    if rois:
        m["roi_avg"] = round(sum(rois) / len(rois))
        m["roi_median"] = round(median(rois))
        m["roi_max"] = round(max(rois))
        if len(rois) >= 2:
            sd = pstdev(rois)
            if sd > 0:
                m["sharpe"] = round((sum(rois) / len(rois)) / sd, 2)

    # Max drawdown sobre la curva acumulada de PnL, en orden cronológico
    # (usamos el momento de la primera venta como cierre aproximado).
    ordered = sorted(closed, key=lambda i: (i.get("first_sell_ts") or 0))
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for i in ordered:
        equity += i["pnl_sol"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    m["max_drawdown_sol"] = round(max_dd, 2)
    if peak > 0:
        m["max_drawdown_pct"] = round(100 * max_dd / peak)
    return m


def format_metrics(m: dict) -> list[str]:
    """Líneas legibles para Telegram (omite lo que no se pudo calcular)."""
    if not m or not m.get("closed"):
        return []
    out = ["📊 *Métricas quant (operaciones cerradas):*"]
    if m.get("roi_avg") is not None:
        out.append(f"ROI prom/mediano/máx: {m['roi_avg']:+d}% / "
                   f"{m['roi_median']:+d}% / {m['roi_max']:+d}%")
    if m.get("profit_factor") is not None:
        out.append(f"Profit Factor: {m['profit_factor']}")
    if m.get("expectancy_sol") is not None:
        out.append(f"Expectancy: {m['expectancy_sol']:+.3f} SOL/op")
    if m.get("sharpe") is not None:
        out.append(f"Sharpe (por op): {m['sharpe']}")
    if m.get("max_drawdown_sol") is not None:
        dd = f"{m['max_drawdown_sol']:.2f} SOL"
        if m.get("max_drawdown_pct") is not None:
            dd += f" ({m['max_drawdown_pct']}%)"
        out.append(f"Max Drawdown: -{dd}")
    return out
