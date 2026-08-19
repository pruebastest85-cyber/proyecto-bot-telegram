"""
Identidad legible de una billetera: nombre + posición en el /top.

Antes, cuando una billetera no tenía alias guardado, las tarjetas y alertas
mostraban la dirección cruda ("9xQeWvG8…"), que no dice nada. Como los alias
son DETERMINISTAS (se derivan de la dirección), aquí siempre se puede
generar un nombre estable aunque la base aún no lo tenga.

La posición usa EXACTAMENTE el mismo orden que /top (db.top_wallets), para
que el número que ves en la tarjeta coincida con el del ranking.
"""


def nombre(conn, address: str) -> str:
    """Nombre legible. Nunca devuelve la dirección cruda."""
    if not address:
        return "?"
    try:
        w = conn.execute("SELECT alias FROM wallets WHERE address=?",
                         (address,)).fetchone()
        if w and w["alias"]:
            return w["alias"]
    except Exception:
        pass
    try:
        from aliases import make_alias
        return make_alias(address)          # determinista y estable
    except Exception:
        return f"{address[:8]}…"


def posicion(conn, address: str, tope: int = 200):
    """
    Posición en el ranking (mismo orden que /top). None si no aparece
    dentro del tope o si algo falla.
    """
    if not address:
        return None
    try:
        import time as _t
        import os as _os
        # ESPEJO de db.top_wallets (19/8): mismo corte de actividad de
        # 48 h, ajustable con TOP_ACTIVITY_HOURS. Si cambia alla, aqui.
        _horas = float(_os.getenv("TOP_ACTIVITY_HOURS", "48"))
        corte = int(_t.time()) - int(_horas * 3600)
        rows = conn.execute(
            """SELECT w.address FROM wallets w
               LEFT JOIN (SELECT wallet, MAX(last_ts) AS ult FROM positions
                          GROUP BY wallet) actividad
                    ON actividad.wallet = w.address
               WHERE w.is_bot = 0
               ORDER BY w.is_tracked DESC,
                        -- Debe ser IDENTICO al de db.top_wallets (ver alli
                        -- el porque): si divergen, /top y la posicion que
                        -- se muestra en las tarjetas dejan de coincidir.
                        CASE WHEN w.pnl_total IS NOT NULL
                                  AND w.pnl_total < 0
                             THEN 1 ELSE 0 END,
                        CASE WHEN COALESCE(actividad.ult, 0) < ?
                             THEN 1 ELSE 0 END,
                        CASE WHEN w.wallet_score IS NULL THEN 1 ELSE 0 END,
                        w.wallet_score DESC,
                        COALESCE(w.pnl_total, -1e9) DESC,
                        w.score DESC
               LIMIT ?""", (corte, tope)).fetchall()
        for i, r in enumerate(rows, 1):
            if r["address"] == address:
                return i
    except Exception:
        pass
    return None


def identidad(conn, address: str) -> dict:
    """{'nombre': str, 'pos': int|None, 'etiqueta': 'Nombre · #3 del top'}"""
    nom = nombre(conn, address)
    pos = posicion(conn, address)
    etiqueta = f"{nom}  ·  #{pos} del top" if pos else nom
    return {"nombre": nom, "pos": pos, "etiqueta": etiqueta}
