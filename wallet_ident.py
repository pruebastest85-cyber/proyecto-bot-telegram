"""
Identidad legible de una billetera: nombre + posición en el /top.

Antes, cuando una billetera no tenía alias guardado, las tarjetas y alertas
mostraban la dirección cruda ("9xQeWvG8…"), que no dice nada. Como los alias
son DETERMINISTAS (se derivan de la dirección), aquí siempre se puede
generar un nombre estable aunque la base aún no lo tenga.

La posición usa EXACTAMENTE el mismo orden que /top (db.top_wallets), para
que el número que ves en la tarjeta coincida con el del ranking.
"""
from avisos import aviso as _avisar_ex   # (19-AE)


def nombre(conn, address: str) -> str:
    """Nombre legible. Nunca devuelve la dirección cruda."""
    if not address:
        return "?"
    try:
        w = conn.execute("SELECT alias FROM wallets WHERE address=?",
                         (address,)).fetchone()
        if w and w["alias"]:
            return w["alias"]
    except Exception as _ex:
        _avisar_ex("wallet_ident:nombre:23", _ex)
        pass
    try:
        from aliases import make_alias
        return make_alias(address)          # determinista y estable
    except Exception as _ex:
        _avisar_ex("wallet_ident:nombre:28", _ex)
        return f"{address[:8]}…"


def posicion(conn, address: str, tope: int = 200):
    """
    Posición en el ranking (mismo orden que /top). None si no aparece
    dentro del tope o si algo falla.
    """
    if not address:
        return None
    try:
        # ESPEJO de db.top_wallets (19/8): mismo corte de actividad de
        # 48 h, ajustable con TOP_ACTIVITY_HOURS. (19-A) El calculo ya no
        # esta escrito aqui: lo hace `db.corte_actividad()`, el mismo que
        # usan `top_wallets` y `_operativas`, asi que los tres espejos no
        # pueden discrepar aunque alguien cambie el defecto en un sitio.
        from db import corte_actividad, corte_medidas, orden_top
        corte = corte_actividad()
        corte_m = corte_medidas()
        # (19-AH) El ORDER BY sale de db.orden_top(): un solo sitio.
        rows = conn.execute(
            f"""SELECT w.address FROM wallets w
               LEFT JOIN (SELECT wallet, MAX(last_ts) AS ult FROM positions
                          GROUP BY wallet) actividad
                    ON actividad.wallet = w.address
               WHERE w.is_bot = 0
               ORDER BY {orden_top()}
               LIMIT ?""", (corte_m, corte, tope)).fetchall()
        for i, r in enumerate(rows, 1):
            if r["address"] == address:
                return i
    except Exception as _ex:
        _avisar_ex("wallet_ident:posicion:72", _ex)
        pass
    return None


def identidad(conn, address: str) -> dict:
    """{'nombre': str, 'pos': int|None, 'etiqueta': 'Nombre · #3 del top'}"""
    nom = nombre(conn, address)
    pos = posicion(conn, address)
    etiqueta = f"{nom}  ·  #{pos} del top" if pos else nom
    return {"nombre": nom, "pos": pos, "etiqueta": etiqueta}
