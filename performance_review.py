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
    """Señales de compra ya medidas de una billetera (hasta 30).

    (Ola 17-A) Antes esto metía en el MISMO saco los rendimientos medidos
    a 1 h y a 24 h: un +5% en una hora pesaba igual que un +5% en un día,
    y esa media mezclada era el criterio que RETIRA la ⭐. Ahora los dos
    horizontes se calculan por separado y se usa uno solo — el de 24 h si
    hay muestra suficiente, el de 1 h si no — diciendo siempre cuál.
    """
    rows = conn.execute(
        """SELECT chg_1h, chg_24h FROM signals
           WHERE wallet=? AND side='compra'
             AND (chg_1h IS NOT NULL OR chg_24h IS NOT NULL)
           ORDER BY ts DESC LIMIT 30""", (wallet,)).fetchall()
    v24 = [float(r["chg_24h"]) for r in rows if r["chg_24h"] is not None]
    v1 = [float(r["chg_1h"]) for r in rows if r["chg_1h"] is not None]

    def _agg(vals, etiqueta):
        if not vals:
            return None
        return {"n": len(vals),
                "wr": round(100.0 * sum(1 for v in vals if v > 0) / len(vals)),
                "media": round(sum(vals) / len(vals), 1),
                "horizonte": etiqueta}

    s24, s1 = _agg(v24, "24h"), _agg(v1, "1h")
    # Se prefiere 24 h; solo se cae a 1 h si 24 h no llega al mínimo.
    if s24 and s24["n"] >= REVIEW_MIN_SIGNALS:
        st = s24
    elif s1 and s1["n"] >= REVIEW_MIN_SIGNALS:
        st = s1
    else:
        st = s24 or s1
    if st is None:
        return None
    st = dict(st)
    st["n_24h"] = s24["n"] if s24 else 0
    st["n_1h"] = s1["n"] if s1 else 0
    return st


def perdedora_confirmada(conn, wallet: str) -> str | None:
    """
    ¿Las señales YA EMITIDAS de esta billetera perdieron dinero de forma
    consistente? Devuelve el motivo (texto) o None.

    Se usa como GUARDA en la re-evaluación de la IA: sin ella, la
    re-evaluación de cada 3 días devolvía la ⭐ a billeteras que este
    sistema ya había degradado por rendimiento medido — es decir, la IA
    revertía en silencio un hecho medido. Mismo criterio conservador que
    review_tracked: hacen falta las tres condiciones.
    """
    try:
        st = _stats(conn, wallet)
    except Exception:
        return None
    if not st or st["n"] < REVIEW_MIN_SIGNALS:
        return None                      # sin evidencia suficiente
    if st["wr"] < REVIEW_MIN_WR and st["media"] < 0:
        return (f"{st['wr']}% de acierto y {st['media']:+.1f}% promedio "
                f"en {st['n']} señales medidas a {st['horizonte']}")
    return None


# ── Creadores de mercado (26/8/2026) ─────────────────────────────────────
# Regla del dueño: "creadores de mercado o billeteras que compran y venden
# un token más de 5 veces no deberían tener estrella, sus estrategias no
# son copiables".
#
# POR QUÉ hacía falta algo nuevo: ya existía un detector de MM en
# `wallet_profiler` (`mm_tokens`), pero exige TRES condiciones a la vez —
# 3+ tokens distintos, 3+ compras y 3+ ventas en cada uno, y PnL neto
# ~0 — y encima se calcula sobre una muestra de Helius. Una billetera que
# da 64 vueltas a UN SOLO token y encima gana dinero no cumple ninguna de
# las tres, así que pasaba limpia. El caso real que lo destapó: 21 ⭐
# dándole vueltas al mismo token (GASSPAS), 491 operaciones en 14 horas,
# compra-venta-compra-venta perfectamente alternado.
#
# Esta comprobación usa NUESTRAS señales ya guardadas: no gasta créditos
# de Helius ni de IA, y mide comportamiento observado, no una muestra.

def _tope_mm() -> tuple:
    try:
        import config as _cfg
        return (int(getattr(_cfg, "MM_VUELTAS_MAX", 5)),
                int(getattr(_cfg, "MM_VENTANA_DIAS", 30)))
    except Exception:
        return (5, 30)


def _vueltas_max(conn, dias: int, wallet: str | None = None) -> dict:
    """{billetera: (vueltas, mint)} — el token al que MÁS vueltas le dio.

    Una "vuelta" es una ALTERNANCIA real compra→venta en orden temporal:
    comprar y después vender. La primera versión contaba
    `min(compras, ventas)` y la auditoría (18-K) enseñó el fallo: un
    trader normal que entra en 6 compras escalonadas y sale en 6 ventas
    parciales daba 6 "vueltas" sin haber dado ninguna — es UNA posición
    escalonada, comportamiento copiable. Con alternancias, ese caso vale
    1 y el creador de mercado real (compra-venta-compra-venta…) vale lo
    que alterna. Medido sobre la base real del 27/8: las DOS métricas
    marcaban exactamente a las mismas 21 billeteras (33-103 alternancias
    cada una), así que el cambio no toca a nadie hoy; solo protege al
    trader escalonado de mañana.

    El conteo se hace en Python: necesita el ORDEN de las señales, y de
    paso evita el `MIN(a, b)` de dos argumentos que SQLite acepta pero
    Postgres no. El orden TAMBIEN se hace en Python (auditoria 18-K,
    ronda 2): un `ORDER BY signature` no da lo mismo en los dos motores
    — SQLite ordena por bytes y un Postgres con colacion de idioma pone
    'a' antes que 'B' — y dos señales del mismo segundo podian contar
    distinto segun el motor. Ordenar la tupla en Python es identico en
    todas partes.
    """
    import time as _t
    desde = int(_t.time()) - int(dias) * 86400
    if wallet:
        rows = conn.execute(
            """SELECT wallet, mint, side, ts, signature
               FROM signals
               WHERE ts >= ? AND wallet = ?""",
            (desde, wallet)).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.wallet AS wallet, s.mint AS mint, s.side AS side,
                      s.ts AS ts, s.signature AS signature
               FROM signals s
               JOIN wallets w ON w.address = s.wallet
               WHERE s.ts >= ? AND w.is_tracked = 1""",
            (desde,)).fetchall()
    # Si `signals` crece a millones de filas, esto debera pasar a
    # fetchmany por trozos (misma deuda que backup.py, ver CLAUDE.md §5).
    # Hoy la ventana de 30 dias lo deja muy por debajo de eso.
    rows = sorted(rows, key=lambda r: (r["wallet"], r["mint"],
                                       r["ts"] or 0, r["signature"] or ""))
    peor: dict = {}
    clave = None
    previa = None
    vueltas = 0

    def _apuntar(k, v):
        if k is None:
            return
        w, m = k
        act = peor.get(w)
        if act is None or v > act[0]:
            peor[w] = (v, m)

    for r in rows:
        k = (r["wallet"], r["mint"])
        if k != clave:
            _apuntar(clave, vueltas)
            clave, previa, vueltas = k, None, 0
        if previa == "compra" and r["side"] == "venta":
            vueltas += 1
        previa = r["side"]
    _apuntar(clave, vueltas)
    return peor


def _motivo_mm(vueltas: int, mint: str | None, dias: int) -> str:
    return (f"creadora de mercado: {vueltas} vueltas compra→venta al MISMO "
            f"token ({(mint or '?')[:8]}…) en {dias} días; esa estrategia "
            f"no se puede copiar")


def creadora_de_mercado(conn, wallet: str) -> str | None:
    """¿Le da vueltas al mismo token? Devuelve el motivo (texto) o None.

    GUARDA en la re-evaluación de la IA: sin ella, la IA le devolvería la
    ⭐ en el siguiente ciclo a quien esta regla acaba de degradar.
    """
    tope, dias = _tope_mm()
    if tope <= 0:                      # regla apagada a propósito
        return None
    try:
        peor = _vueltas_max(conn, dias, wallet)
    except Exception:
        return None                    # sin datos no se castiga a nadie
    dato = peor.get(wallet)
    if not dato or dato[0] <= tope:
        return None
    return _motivo_mm(dato[0], dato[1], dias)


def creadoras_de_mercado(conn) -> dict:
    """{billetera: motivo} de TODAS las ⭐, en una sola consulta.

    La versión de una en una haría 197 consultas en cada depuración.
    """
    tope, dias = _tope_mm()
    if tope <= 0:
        return {}
    peor = _vueltas_max(conn, dias)
    return {a: _motivo_mm(v, m, dias) for a, (v, m) in peor.items()
            if v > tope}


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
                         f"promedio en {st['n']} señales medidas a "
                         f"{st['horizonte']}")
                conn.execute(
                    """UPDATE wallets SET is_tracked=0, ai_follow=0,
                       grade='Observación', ai_reason=?
                       WHERE address=?""", (razon, addr))
                degradadas.append({"address": addr,
                                   "alias": w["alias"] or addr[:6],
                                   **st})
        if degradadas:
            conn.commit()
    except Exception as e:
        # (Ola 17-E) Se informa del fallo para que el job pueda
        # propagarlo: antes devolvia un dict normal y el reloj de exito
        # se marcaba igual, escondiendo una revision que no ocurrio.
        print(f"· review_tracked falló: {e}")
        return {"revisadas": revisadas, "degradadas": 0, "detalle": [],
                "error": str(e)}
    finally:
        conn.close()

    print(f"🔍 Revisión de rendimiento: {revisadas} con datos, "
          f"{len(degradadas)} degradadas")

    if degradadas and notify:
        try:
            from realtime import tg_send
            lineas = [f"• *{d['alias']}* — {d['wr']}% acierto, "
                      f"{d['media']:+.1f}% medio "
                      f"({d['n']} señales a {d['horizonte']})"
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
        # (Ola 17-E) Marcar las que aún no tienen muestra suficiente:
        # antes salían mezcladas con las demás y un "0% acierto sobre 7"
        # se leía igual que uno sobre 30.
        _corta = "" if st["n"] >= REVIEW_MIN_SIGNALS else " ⏳ (muestra corta)"
        out.append(f"{ico} *{alias}* — {st['wr']}% acierto · "
                   f"{st['media']:+.1f}% medio · {st['n']} señales "
                   f"medidas a {st['horizonte']}{_corta}")
    out.append(f"\n_Se degrada con <{REVIEW_MIN_WR}% de acierto Y promedio "
               f"negativo, tras {REVIEW_MIN_SIGNALS}+ señales medidas. "
               f"Los horizontes de 1 h y 24 h ya NO se mezclan: se usa el "
               f"de 24 h si hay muestra, y si no el de 1 h._")
    return "\n".join(out)
