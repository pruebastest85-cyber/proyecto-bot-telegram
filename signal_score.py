"""
Score de señal 0-100: combina todo lo que sabemos en un solo número.

Componentes:
  Billetera (0-35): su Wallet Score
  Track record (0-20): tasa de acierto real de sus señales pasadas
  Seguridad token (0-25): mint/freeze, LP lock, holders, riesgos RugCheck
  Momentum (0-10): edad, volumen, flujo de compras 5min
  Consenso (0-10): cuántas billeteras ⭐ hicieron lo mismo
  Patrón (±5): si el MC está dentro del rango habitual de la billetera
  Adelanto (+3/−6, Ola 10): líder de cluster suma 3; seguidora crónica
    resta 6
    — su señal es el eco de la compra de otra y llega tarde por diseño.

El umbral mínimo para alertar se guarda en settings ('min_signal_score',
0 por defecto = alertar todo) y se cambia desde el chat del bot.
"""


def compute_signal_score(t: dict, wallet_score, track: dict | None,
                         consensus: int, patron_ok,
                         inf: dict | None = None) -> tuple[int, str]:
    """Devuelve (score 0-100, desglose legible).

    (Ola 17-A) Tres de los seis componentes tenian un valor NEUTRO fijo
    para cuando falta el dato, y el desglose los imprimia como si fueran
    medidos: leias "track 8" sin poder distinguirlo de un 8 real. El
    valor neutro se queda (quitarlo silenciaria alertas legitimas), pero
    ahora va MARCADO con "s/d" — sin datos — en el desglose.
    """
    # Billetera (0-35)
    ws_medido = wallet_score is not None
    ws = wallet_score if ws_medido else 50.0
    p_wallet = 0.35 * max(0.0, min(100.0, float(ws)))

    # Track record (0-20)
    track_medido = True
    if track and track.get("tasa_acierto_24h_pct") is not None:
        p_track = track["tasa_acierto_24h_pct"] / 5
    elif track and track.get("tasa_acierto_1h_pct") is not None:
        p_track = track["tasa_acierto_1h_pct"] / 6
    else:
        p_track = 8.0            # neutro: esta billetera no tiene señales medidas
        track_medido = False

    # Seguridad del token (0-25)
    # Si RugCheck no respondio (`rug_ok` falso), mint/freeze/LP/holders
    # valen None por DESCONOCIDOS, no por estar bien: restar solo 3 daba
    # 22/25 — casi la nota maxima — a un token sin verificar. Sin datos
    # se usa el punto medio, y se dice.
    seg_medido = bool(t.get("rug_ok"))
    if not seg_medido:
        p_seg = 12.5
        p_seg -= min(6, 2 * len(t.get("risks") or []))   # el resumen sí llegó
        p_seg = max(0.0, p_seg)
    else:
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
    # (Ola 17-E) `0 <= age`: una edad negativa (dato malo de DexScreener)
    # premiaba el momentum como si fuera un token recién nacido.
    if age is not None and 0 <= age <= 30:
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

    # Adelanto (±6, Ola 10): copiar al copión es comprar más caro. El dato
    # sale del grafo de influencia (solo cache: sin dato = 0, y se dice).
    p_lead, lead_txt = 0.0, "s/d"
    if inf:
        fol = inf.get("follower_score")
        pf = inf.get("pct_first")
        lid = inf.get("leader_score")
        if fol is not None and fol >= 70 and (pf or 0) <= 20:
            p_lead, lead_txt = -6.0, "-6 (seguidora)"
        elif (lid is not None and lid >= 70) or (pf or 0) >= 50:
            p_lead, lead_txt = 3.0, "+3 (líder)"
        else:
            lead_txt = "0"

    total = int(round(max(0.0, min(
        100.0, p_wallet + p_track + p_seg + p_mom + p_cons + p_pat
        + p_lead))))
    desglose = (f"billetera {p_wallet:.0f}{'' if ws_medido else ' s/d'} · "
                f"track {p_track:.0f}{'' if track_medido else ' s/d'} · "
                f"token {p_seg:.0f}{'' if seg_medido else ' s/d'} · "
                f"momentum {p_mom:.0f} · "
                f"consenso {p_cons:.0f} · patrón {p_pat:+.0f} · "
                f"adelanto {lead_txt}")
    if not (ws_medido and track_medido and seg_medido):
        desglose += "   (s/d = sin datos, valor neutro)"
    return total, desglose
