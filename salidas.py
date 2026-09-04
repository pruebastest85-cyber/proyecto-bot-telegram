"""
Deriva post-venta: ¿que hace el precio DESPUES de que una billetera vende?

Fase 1 del copy trading real. Cada venta de una ⭐ se mide igual que las
compras (chg_1h / chg_24h respecto del precio al vender). Con esas
mediciones este modulo responde, POR BILLETERA:

  - deriva positiva  → el token sigue subiendo tras su venta: la billetera
    vende demasiado pronto y deja dinero sobre la mesa. Al copiarla,
    conviene HOLDEAR mas que ella (con trailing stop).
  - deriva negativa  → el token se desploma tras su venta: sale en la cima.
    Al copiarla, conviene VENDER en cuanto ella venda, o antes.

El perfil sale integramente de la base propia (señales medidas), sin
gastar creditos de ninguna API. Es el contexto que recibira la IA que
decide las salidas.
"""

from statistics import median
from avisos import aviso as _avisar_ex   # (19-AE)

# Minimo de ventas medidas para emitir un perfil. Por debajo, None:
# con 1-2 datos la "estadistica" seria ruido disfrazado.
MIN_VENTAS = 3

# Umbral para clasificar. |mediana| por debajo = comportamiento neutral.
UMBRAL_PCT = 10.0


def _ventas_medidas(conn, wallet=None):
    """Filas de ventas con al menos una medicion, de billeteras ⭐."""
    sql = """SELECT s.wallet, s.chg_1h, s.chg_24h
             FROM signals s JOIN wallets w ON w.address = s.wallet
             WHERE s.side='venta' AND w.is_tracked = 1
               AND (s.chg_1h IS NOT NULL OR s.chg_24h IS NOT NULL)"""
    args = ()
    if wallet:
        sql += " AND s.wallet = ?"
        args = (wallet,)
    return conn.execute(sql, args).fetchall()


def _perfil(filas: list) -> dict:
    """Estadistica y clasificacion a partir de las ventas medidas.

    `filas` es una lista de pares (chg_1h, chg_24h), UNO POR VENTA.
    (Ola 8, 21/8) Antes el numerador de pct_sigue_subiendo salia de una
    lista (24h si existia, si no 1h) y el denominador era el maximo de
    las dos: con 10 ventas medidas a 1h y 3 a 24h daba 3 aciertos entre
    10 → porcentaje falso. Ahora cada venta aporta UNA medida (24h si la
    tiene, si no 1h) y numerador y denominador salen de la misma lista;
    ventas_medidas es de verdad el numero de ventas medidas."""
    chgs_1h = [a for a, b in filas if a is not None]
    chgs_24h = [b for a, b in filas if b is not None]
    ref_por_venta = [b if b is not None else a for a, b in filas
                     if a is not None or b is not None]
    n = len(ref_por_venta)
    med_1h = median(chgs_1h) if chgs_1h else None
    med_24h = median(chgs_24h) if chgs_24h else None
    # La referencia es la mediana a 24h si existe (mas señal, menos ruido);
    # si no, la de 1h.
    ref = med_24h if med_24h is not None else med_1h
    if ref is None:
        clase = "sin datos"
    elif ref > UMBRAL_PCT:
        clase = "vende temprano"       # holdear mas que ella
    elif ref < -UMBRAL_PCT:
        clase = "sale en la cima"      # vender ya (o antes)
    else:
        clase = "neutral"              # copiar su venta tal cual
    pos = [c for c in ref_por_venta if c > UMBRAL_PCT]
    return {
        "ventas_medidas": n,
        "deriva_1h": round(med_1h, 1) if med_1h is not None else None,
        "deriva_24h": round(med_24h, 1) if med_24h is not None else None,
        "pct_sigue_subiendo": round(100 * len(pos) / n) if n else 0,
        "clase": clase,
    }


def deriva_post_venta(conn, min_ventas: int = MIN_VENTAS) -> dict:
    """{wallet: perfil} para todas las ⭐ con suficientes ventas medidas."""
    por_wallet: dict = {}
    for r in _ventas_medidas(conn):
        por_wallet.setdefault(r["wallet"], []).append(
            (r["chg_1h"], r["chg_24h"]))
    out = {}
    for w, filas in por_wallet.items():
        if len(filas) >= min_ventas:
            out[w] = _perfil(filas)
    return out


def perfil_salida(conn, wallet: str) -> dict | None:
    """Perfil de UNA billetera (para inyectar a la IA de salidas), o None
    si aun no tiene suficientes ventas medidas."""
    filas = [(r["chg_1h"], r["chg_24h"])
             for r in _ventas_medidas(conn, wallet)]
    if len(filas) < MIN_VENTAS:
        return None
    return _perfil(filas)


def salidas_text(conn) -> str:
    """Resumen para Telegram (/salidas)."""
    perfiles = deriva_post_venta(conn)
    if not perfiles:
        return ("📤 *Deriva post-venta*\n\n"
                "Aún no hay suficientes ventas medidas de billeteras ⭐. "
                "La medición se activó hace poco: cada venta necesita "
                "1-24 h para medirse. Vuelve a probar mañana.")
    alias = {r["address"]: (r["alias"] or r["address"][:8]) for r in
             conn.execute("SELECT address, alias FROM wallets "
                          "WHERE is_tracked=1").fetchall()}
    icono = {"vende temprano": "🕐", "sale en la cima": "🎯",
             "neutral": "⚖️", "sin datos": "·"}
    orden = sorted(perfiles.items(),
                   key=lambda kv: -(kv[1]["deriva_24h"]
                                    if kv[1]["deriva_24h"] is not None
                                    else kv[1]["deriva_1h"] or 0))
    lineas = ["📤 *Deriva post-venta de las ⭐*",
              "_¿Qué hace el token después de que ella vende?_\n"]
    for w, p in orden[:25]:
        d = (p["deriva_24h"] if p["deriva_24h"] is not None
             else p["deriva_1h"])
        lineas.append(
            f"{icono[p['clase']]} *{alias.get(w, w[:8])}* · "
            f"{d:+.0f}% tras vender · {p['ventas_medidas']} ventas · "
            f"{p['clase']}")
    lineas.append(
        "\n🕐 vende temprano → al copiarla conviene holdear más que ella"
        "\n🎯 sale en la cima → conviene vender en cuanto ella venda")
    return "\n".join(lineas)


def hold_report(conn) -> str | None:
    """Resultado medido de los holds extra cerrados: ¿ganar mas que
    copiar la venta de la ⭐, o perder? Compara el precio de salida real
    contra el precio al que vendio la lider. None si aun no hay datos."""
    try:
        rows = conn.execute(
            """SELECT symbol, exit_price, precio_venta_lider, exit_reason
               FROM paper_trades
               WHERE politica='holdear' AND status<>'abierta'
                 AND exit_price IS NOT NULL
                 AND precio_venta_lider IS NOT NULL
                 AND precio_venta_lider > 0
               ORDER BY exit_ts DESC LIMIT 50""").fetchall()
    except Exception as _ex:
        _avisar_ex("salidas:hold_report:148", _ex)
        return None
    if not rows:
        return None
    extras = [(r["exit_price"] / r["precio_venta_lider"] - 1) * 100
              for r in rows]
    gano = sum(1 for e in extras if e > 0)
    med = median(extras)
    lineas = [f"\n🕐 *Holds extra cerrados: {len(extras)}*",
              f"El hold le gano a copiar la venta en {gano}/{len(extras)} "
              f"· extra mediano {med:+.1f}%"]
    for r, e in list(zip(rows, extras))[:5]:
        lineas.append(f"  · {r['symbol']}: {e:+.1f}% ({r['exit_reason']})")
    return "\n".join(lineas)
