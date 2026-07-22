"""
Confianza estadística + Calidad de datos.

Un score de 94 con 300 operaciones y uno con 8 NO valen lo mismo. Estas
dos medidas hacen al sistema honesto sobre CUÁNTO fiarse de cada
conclusión, sin cambiar los scores en sí (observabilidad, no más reglas).

  · stat_confidence(perfil): 0-100 según el tamaño de muestra (nº de
    operaciones cerradas). Con pocas ops, aunque el score sea alto, la
    confianza es baja.
  · data_quality(perfil): 0-100 de completitud: ¿hay historial suficiente,
    muestra amplia, diversificación, precios conocidos? La IA/grading
    deberían saber esto antes de sacar conclusiones fuertes.
"""


def stat_confidence(p) -> int:
    """Confianza estadística por tamaño de muestra (operaciones cerradas)."""
    m = p.get("metrics") or {}
    n = m.get("closed") or p.get("closed_positions", 0) or 0
    # n/(n+11): 8→42%, 30→73%, 100→90%, 312→97%. Sube rápido y satura.
    return round(100 * n / (n + 11)) if n > 0 else 0


def data_quality(p) -> int:
    """0-100 de completitud de los datos de la billetera."""
    m = p.get("metrics") or {}
    closed = m.get("closed") or p.get("closed_positions", 0) or 0
    n_tokens = len(p.get("tokens") or {})
    tx = p.get("tx_sampled", 0) or 0
    held = p.get("held_tokens", 0) or 0
    priced = p.get("priced_tokens", 0) or 0

    f_hist = min(1.0, closed / 30.0)          # historial suficiente
    f_tx = min(1.0, tx / 500.0)               # muestra amplia
    f_div = min(1.0, n_tokens / 10.0)         # variedad de tokens
    f_price = 1.0 if held == 0 else (priced / held)   # precios conocidos
    q = 100 * (0.40 * f_hist + 0.25 * f_tx + 0.20 * f_div + 0.15 * f_price)
    return round(q)


def format_reliability(p) -> str | None:
    """Línea para el DNA: confianza + calidad de datos."""
    conf = stat_confidence(p)
    dq = data_quality(p)
    if conf <= 0 and dq <= 0:
        return None
    m = p.get("metrics") or {}
    n = m.get("closed") or p.get("closed_positions", 0) or 0
    aviso = ""
    if conf < 50:
        aviso = " ⚠️ muestra pequeña: tómalo con pinzas"
    return (f"🔎 Confianza estadística: *{conf}%* ({n} ops) · "
            f"Calidad de datos: {dq}/100{aviso}")
