"""
Score de señal 0-100: combina todo lo que sabemos en un solo número.

Componentes:
  Billetera (0-35): su Wallet Score
  Track record (0-20): tasa de acierto real de sus señales pasadas
  Seguridad token (0-25): mint/freeze, LP lock, holders, riesgos RugCheck
  Momentum (0-10): edad, volumen, flujo de compras 5min
  Consenso (0-10): cuántas billeteras ⭐ hicieron lo mismo
  Patrón (±5): si el MC está dentro del rango habitual de la billetera

El umbral mínimo para alertar se guarda en settings ('min_signal_score',
0 por defecto = alertar todo) y se cambia desde el chat del bot.
"""


def compute_signal_score(t: dict, wallet_score, track: dict | None,
                         consensus: int, patron_ok) -> tuple[int, str]:
    """Devuelve (score 0-100, desglose legible)."""
    # Billetera (0-35)
    ws = wallet_score if wallet_score is not None else 50.0
    p_wallet = 0.35 * max(0.0, min(100.0, float(ws)))

    # Track record (0-20)
    if track and track.get("tasa_acierto_24h_pct") is not None:
        p_track = track["tasa_acierto_24h_pct"] / 5
    elif track and track.get("tasa_acierto_1h_pct") is not None:
        p_track = track["tasa_acierto_1h_pct"] / 6
    else:
        p_track = 8.0

    # Seguridad del token (0-25)
    p_seg = 25.0
    if t.get("mint_auth"):
        p_seg -= 10
    if t.get("freeze_auth"):
        p_seg -= 8
    lp = t.get("lp_locked_pct")
    if lp is None:
        p_seg -= 3
    elif lp < 50:
        p_seg -= 7
    top10 = t.get("top10_pct")
    if top10 is not None and top10 > 40:
        p_seg -= 5
    p_seg -= min(6, 2 * len(t.get("risks") or []))
    p_seg = max(0.0, p_seg)

    # Momentum (0-10)
    p_mom = 0.0
    age = t.get("age_days")
    if age is not None and age <= 30:
        p_mom += 4
    if (t.get("vol24") or 0) >= 100_000:
        p_mom += 3
    if t.get("buys5") is not None and t.get("sells5") is not None \
            and t["buys5"] > t["sells5"]:
        p_mom += 3

    # Consenso (0-10)
    p_cons = min(10.0, max(0, consensus - 1) * 5.0)

    # Patrón (±5)
    p_pat = 5.0 if patron_ok is True else (-5.0 if patron_ok is False else 0.0)

    total = int(round(max(0.0, min(
        100.0, p_wallet + p_track + p_seg + p_mom + p_cons + p_pat))))
    desglose = (f"billetera {p_wallet:.0f} · track {p_track:.0f} · "
                f"token {p_seg:.0f} · momentum {p_mom:.0f} · "
                f"consenso {p_cons:.0f} · patrón {p_pat:+.0f}")
    return total, desglose
