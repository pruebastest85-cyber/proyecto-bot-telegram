"""
Motor de grading en cascada: separa el ALFA real de la suerte.

En vez de una sola métrica, aplica filtros en niveles y combina todo en
un CONSISTENCY SCORE y una clasificación en cuatro grupos:

  ⭐ Elite       rentable y consistente, líder de cluster, bajo riesgo.
  🟢 Seguimiento buenas métricas, aún sin evidencia de liderazgo.
  🟡 Observación prometedora, le falta historial o consistencia.
  🔴 Descartada  historial débil, inconsistente o con señales de riesgo.

Usa datos que el sistema YA calcula (wallet_profiler + wallet_metrics +
influence). Todos los umbrales son constantes tuneables.
"""

import time

# ── Umbrales (tuneables por VARIABLE DE ENTORNO, sin redeploy) ────────
import os as _os


def _env(name, default):
    try:
        return float(_os.getenv(name, default))
    except (TypeError, ValueError):
        return default


MIN_TRADES = int(_env("MIN_CLOSED_TRADES", 20))    # ops cerradas mínimas
MIN_TOKENS = int(_env("MIN_TOKENS", 3))            # tokens distintos mínimos
MAX_INACTIVE_DAYS = int(_env("MAX_INACTIVE_DAYS", 45))
WR_MIN = _env("MIN_WIN_RATE", 60)                  # win rate mínimo (Nivel 2)
PF_MIN = _env("MIN_PROFIT_FACTOR", 1.8)            # profit factor mínimo
MAXDD = _env("MAX_DRAWDOWN_PCT", 35)               # max drawdown máximo (%)
CONC_MAX = _env("MAX_SINGLE_TOKEN_CONCENTRATION", 0.40)  # conc. máx 1 token
LEADER_MIN = _env("LEADER_MIN", 60)                # leader score "lidera"
CONS_ELITE = _env("CONS_ELITE", 75)                # consistency para Elite
CONS_SEG = _env("MIN_CONSISTENCY_SCORE", 58)       # consistency Seguimiento
ELITE_NET = _env("MIN_REALIZED_PNL_ELITE_SOL", 20.0)  # PnL neto min Elite


def _conc(p) -> float:
    gains = [i["pnl_sol"] for i in (p.get("tokens") or {}).values()
             if i.get("pnl_sol", 0) > 0]
    return (max(gains) / sum(gains)) if gains else 1.0


def consistency_score(p) -> int:
    """0-100: estabilidad del rendimiento (lo que separa a los buenos de
    los que tuvieron suerte). Combina Profit Factor, drawdown,
    diversificación, Sharpe, ROI mediano y rendimiento reciente."""
    m = p.get("metrics") or {}
    pf = m.get("profit_factor")
    dd = m.get("max_drawdown_pct")
    sharpe = m.get("sharpe")
    rmed = m.get("roi_median")
    net30 = p.get("pnl_30d_sol")
    conc = _conc(p)

    f_pf = min(1.0, max(0.0, (pf - 1) / 2)) if pf is not None else 0.4
    f_dd = (1 - min(1.0, (dd or 0) / 50)) if dd is not None else 0.6
    f_div = 1 - min(1.0, conc)
    f_sh = min(1.0, max(0.0, (sharpe or 0) / 2)) if sharpe is not None else 0.4
    f_rmed = 1.0 if (rmed or 0) > 0 else 0.0
    f_recent = 1.0 if (net30 or 0) > 0 else 0.3

    score = 100 * (0.28 * f_pf + 0.20 * f_dd + 0.20 * f_div +
                   0.12 * f_sh + 0.10 * f_rmed + 0.10 * f_recent)
    return round(score)


def _res(emoji, tier, cons, reasons):
    return {"emoji": emoji, "tier": tier, "consistency": cons,
            "reasons": reasons}


def grade_wallet(p, inf=None, ai_class=None) -> dict:
    """Aplica la cascada y devuelve el grado + consistency + razones."""
    m = p.get("metrics") or {}
    closed = m.get("closed") or p.get("closed_positions", 0)
    ntokens = len(p.get("tokens") or {})
    net = p.get("net_pnl_sol", p.get("pnl_total_sol", 0.0))
    wr = p.get("win_rate_pct")
    pf = m.get("profit_factor")
    exp = m.get("expectancy_sol")
    dd = m.get("max_drawdown_pct")
    rmed = m.get("roi_median")
    conc = _conc(p)
    cons = consistency_score(p)

    days = ((time.time() - p["last_tx_ts"]) / 86400) if p.get("last_tx_ts") else 999

    # ── Nivel 1: supervivencia ──
    if closed < MIN_TRADES or ntokens < MIN_TOKENS:
        return _res("🔴", "Descartada", cons,
                    [f"historial insuficiente ({closed} cerradas, "
                     f"{ntokens} tokens)"])
    if days > MAX_INACTIVE_DAYS:
        return _res("🔴", "Descartada", cons, [f"inactiva ({days:.0f} días)"])

    # ── Nivel 8: riesgo grave (bot/manipulación) ──
    riesgos = []
    if p.get("possible_bot"):
        riesgos.append("frecuencia de bot")
    if (p.get("mm_tokens") or 0) >= 3:
        riesgos.append("estilo market maker")
    if (p.get("flips_1min_pct") or 0) >= 50:
        riesgos.append("flips <1min")
    if (p.get("uniform_buys_pct") or 0) >= 80:
        riesgos.append("compras idénticas")
    if riesgos:
        return _res("🔴", "Descartada", cons,
                    ["señales de bot/manipulación: " + ", ".join(riesgos)])

    # PnL debe ser positivo (el objetivo es rentabilidad)
    if net <= 0:
        return _res("🔴", "Descartada", cons,
                    [f"PnL neto no positivo ({net:+.1f} SOL)"])

    # ── Nivel 2: calidad ──
    quality = ((wr is None or wr >= WR_MIN)
               and (pf is None or pf >= PF_MIN)
               and (exp is None or exp > 0)
               and (dd is None or dd < MAXDD)
               and (rmed is None or rmed > 0))
    # ── Nivel 5: diversificación ──
    diversified = conc < CONC_MAX
    # ── Nivel 6: comportamiento social ──
    leads = bool(inf and ((inf.get("leader_score") or 0) >= LEADER_MIN
                          or inf.get("followers_count", 0) >= 2))

    # Razones (Nivel 9: explicación)
    reasons = [f"PnL neto +{net:.1f} SOL"]
    if pf is not None:
        reasons.append(f"Profit Factor {pf}")
    if wr is not None:
        reasons.append(f"Win Rate {wr}%")
    if dd is not None:
        reasons.append(f"Max DD {dd}%")
    if diversified:
        reasons.append("beneficio diversificado")
    else:
        reasons.append(f"⚠️ {round(conc*100)}% del beneficio en 1 token")
    if leads:
        reasons.append("lidera en su cluster")

    # ── Clasificación ──
    if quality and diversified and cons >= CONS_ELITE and net >= ELITE_NET and leads:
        return _res("⭐", "Elite", cons, reasons)
    if quality and cons >= CONS_SEG:
        return _res("🟢", "Seguimiento", cons, reasons)
    return _res("🟡", "Observación", cons,
                reasons + ["falta consistencia o evidencia para subir"])


def format_grade(g: dict) -> str:
    """Línea principal + razones para el Wallet DNA."""
    head = f"{g['emoji']} *{g['tier']}* · Consistency {g['consistency']}/100"
    top = " · ".join(g["reasons"][:4])
    return f"{head}\n   {top}"


def elite_gap(p, inf=None) -> list[str]:
    """
    Explicación CONTRAFACTUAL: qué le falta a la wallet para ser Elite.
    Lista concreta y accionable, no un simple "no cumple".
    """
    m = p.get("metrics") or {}
    closed = m.get("closed") or p.get("closed_positions", 0)
    net = p.get("net_pnl_sol", p.get("pnl_total_sol", 0.0))
    wr = p.get("win_rate_pct")
    pf = m.get("profit_factor")
    dd = m.get("max_drawdown_pct")
    conc = _conc(p)
    cons = consistency_score(p)
    leads = bool(inf and ((inf.get("leader_score") or 0) >= LEADER_MIN
                          or inf.get("followers_count", 0) >= 2))

    faltan = []
    if closed < MIN_TRADES:
        faltan.append(f"{MIN_TRADES - closed} operaciones cerradas más")
    if net < ELITE_NET:
        faltan.append(f"+{ELITE_NET - net:.0f} SOL de PnL neto "
                      f"(ahora {net:+.0f})")
    if wr is not None and wr < WR_MIN:
        faltan.append(f"win rate ≥{WR_MIN}% (ahora {wr}%)")
    if pf is not None and pf < PF_MIN:
        faltan.append(f"Profit Factor ≥{PF_MIN} (ahora {pf})")
    if dd is not None and dd >= MAXDD:
        faltan.append(f"bajar drawdown <{MAXDD}% (ahora {dd}%)")
    if conc >= CONC_MAX:
        faltan.append(f"diversificar: {round(conc*100)}% del beneficio en "
                      f"1 token (máx {round(CONC_MAX*100)}%)")
    if cons < CONS_ELITE:
        faltan.append(f"subir consistency a ≥{CONS_ELITE} (ahora {cons})")
    if not leads:
        faltan.append("liderar en un cluster (hoy no lidera)")
    return faltan


def format_elite_gap(p, inf=None, tier=None) -> str | None:
    """Contrafactual para el DNA: solo si NO es Elite."""
    if tier == "Elite":
        return None
    faltan = elite_gap(p, inf)
    if not faltan:
        return None
    return "🎯 _Para Elite le falta: " + "; ".join(faltan[:4]) + "._"
