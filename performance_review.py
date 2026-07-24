"""
Cierre del ciclo de aprendizaje: el rendimiento MEDIDO decide quién conserva
la ⭐.

Hasta ahora una billetera ganaba la estrella por su historial (PnL, win rate,
consistencia) y la conservaba hasta la re-evaluación de la IA. Pero lo que
de verdad importa es si SUS SEÑALES, ya emitidas por este sistema, resultaron
rentables. Eso es un hecho medido, no una predicción.

Aquí se revisa exactamente eso y se degrada a quien acumula señales perdedoras.

Criterio (deliberadamente CONSERVADOR, para no degradar por mala racha):
  - mínimo REVIEW_MIN_SIGNALS señales ya medidas (por defecto 8), y
  - tasa de acierto por debajo de REVIEW_MIN_WR (35%), y
  - cambio promedio NEGATIVO.
Las tres condiciones a la vez. Si solo falla una, se mantiene.

Degradar NO la marca como bot: pierde la ⭐ y las alertas, pero queda en la
base y puede recuperar el grado si vuelve a rendir (la IA la re-evalúa).
"""

import os

from db import get_conn


def _int_env(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


REVIEW_MIN_SIGNALS = _int_env("REVIEW_MIN_SIGNALS", 8)
REVIEW_MIN_WR = _int_env("REVIEW_MIN_WR", 35)


def _stats(conn, wallet: str):
    """Señales de compra ya medidas de una billetera (hasta 30)."""
    rows = conn.execute(
        """SELECT chg_1h, chg_24h FROM signals
           WHERE wallet=? AND side='compra'
             AND (chg_1h IS NOT NULL OR chg_24h IS NOT NULL)
           ORDER BY ts DESC LIMIT 30""", (wallet,)).fetchall()
    vals = []
    for r in rows:
        v = r["chg_24h"] if r["chg_24h"] is not None else r["chg_1h"]
        if v is not None:
            vals.append(float(v))
    if not vals:
        return None
    wr = round(100.0 * sum(1 for v in vals if v > 0) / len(vals))
    return {"n": len(vals), "wr": wr, "media": round(sum(vals) / len(vals), 1)}


def review_tracked(notify: bool = True) -> dict:
    """
    Revisa las ⭐ activas y degrada a las que sus señales medidas pierden.
    Devuelve {revisadas, degradadas, detalle}.
    """
    conn = get_conn()
    degradadas = []
    revisadas = 0
    try:
        estrellas = conn.execute(
            """SELECT address, alias FROM wallets
               WHERE is_tracked=1 AND COALESCE(is_bot,0)=0""").fetchall()
        for w in estrellas:
            addr = w["address"]
            st = _stats(conn, addr)
            if not st or st["n"] < REVIEW_MIN_SIGNALS:
                continue          # aún sin evidencia suficiente
            revisadas += 1
            if st["wr"] < REVIEW_MIN_WR and st["media"] < 0:
                razon = (f"Degradada por rendimiento medido: "
                         f"{st['wr']}% de acierto y {st['media']:+.1f}% "
                         f"promedio en {st['n']} señales")
                conn.execute(
                    """UPDATE wallets SET is_tracked=0, ai_follow=0,
                       grade='Observacion', ai_reason=?
                       WHERE address=?""", (razon, addr))
                degradadas.append({"address": addr,
                                   "alias": w["alias"] or addr[:6],
                                   **st})
        if degradadas:
            conn.commit()
    except Exception as e:
        print(f"· review_tracked falló: {e}")
        return {"revisadas": revisadas, "degradadas": 0, "detalle": []}
    finally:
        conn.close()

    print(f"🔍 Revisión de rendimiento: {revisadas} con datos, "
          f"{len(degradadas)} degradadas")

    if degradadas and notify:
        try:
            from realtime import tg_send
            lineas = [f"• *{d['alias']}* — {d['wr']}% acierto, "
                      f"{d['media']:+.1f}% medio ({d['n']} señales)"
                      for d in degradadas[:8]]
            tg_send("📉 *Revisión de rendimiento*\n\n"
                    f"{len(degradadas)} billetera(s) perdieron la ⭐ porque "
                    "sus señales medidas no fueron rentables:\n\n"
                    + "\n".join(lineas)
                    + "\n\n_No se descartan: si vuelven a rendir, la IA "
                      "puede devolverles el grado._")
        except Exception:
            pass

    return {"revisadas": revisadas, "degradadas": len(degradadas),
            "detalle": degradadas}


def review_text() -> str:
    """Resumen legible para consultar el estado sin degradar nada."""
    conn = get_conn()
    try:
        estrellas = conn.execute(
            """SELECT address, alias FROM wallets
               WHERE is_tracked=1 AND COALESCE(is_bot,0)=0""").fetchall()
        filas = []
        for w in estrellas:
            st = _stats(conn, w["address"])
            if st:
                filas.append((w["alias"] or w["address"][:6], st))
    finally:
        conn.close()
    if not filas:
        return ("📊 *Rendimiento de las ⭐*\n\nAún no hay señales medidas "
                "suficientes. Vuelve cuando el sistema haya seguido más "
                "señales.")
    filas.sort(key=lambda x: x[1]["media"], reverse=True)
    out = ["📊 *Rendimiento medido de las ⭐*\n"]
    for alias, st in filas[:15]:
        ico = "🟢" if st["media"] > 0 else "🔴"
        out.append(f"{ico} *{alias}* — {st['wr']}% acierto · "
                   f"{st['media']:+.1f}% medio · {st['n']} señales")
    out.append(f"\n_Se degrada con <{REVIEW_MIN_WR}% de acierto Y promedio "
               f"negativo, tras {REVIEW_MIN_SIGNALS}+ señales medidas._")
    return "\n".join(out)
