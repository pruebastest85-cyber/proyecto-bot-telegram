"""
Puntuación COPIABLE de cada ⭐ (Ola 19-AH, 04/09/2026).

Hasta ahora el top ordenaba por `wallet_score`: la nota del perfil de
Helius de la PROPIA billetera (su PnL, su ROI, su win rate). Medido en la
base del dueño el 04/09: 12 de las 30 primeras no tenían ni una señal
medida, 8 llevaban más de 48 h dormidas, y la billetera con mejor
historial medido de toda la base (19 señales, 74 % en verde a 24 h, la
única copia grande ganadora) estaba en el puesto 51. Las fuentes
coinciden (Binance: "revisa el PnL de los copiadores, un líder rentable
no siempre se traduce en copiadores rentables"; estudio de 90 días: 97 %
de líderes en positivo, 43,6 % de copiadores; experimento de Essex: el
primero del ranking suele ser el que más riesgo asumió): lo que hay que
ordenar es EL RESULTADO DE COPIARLA.

Cómo se calcula (todo en % sobre el importe):

  observaciones  cada señal de COMPRA de la ⭐ con `chg_24h` medido en la
                 ventana (VENTANA_DIAS). Si esa señal se copió en el paper
                 y cerró con neto, la observación es el neto REAL
                 (pnl_usd_neto / stake_usd); si no, es chg_24h menos la
                 brecha típica de esa ⭐ (o de la población si aún no
                 tiene copias). Se recorta a [TOPE_MIN, TOPE_MAX] para que
                 un x68 no convierta una billetera mediocre en la primera.
  media          media de las observaciones recortadas.
  score          media ENCOGIDA hacia la media de la población según la
                 muestra: (n·media + K·media_poblacion) / (n + K). Con K=5,
                 dos aciertos de dos no valen más que doce de diecinueve.
                 Si la brecha mediana de sus copias supera BRECHA_MAX_PCT
                 (su ganancia es mover el pool: no se puede copiar), el
                 score positivo se reduce a la mitad.
  pf             factor de beneficio: suma de las positivas / suma de las
                 negativas (tope 9,99; 9,99 si no hay negativas).
  dd             máxima caída de la curva acumulada de observaciones
                 (puntos de %). Se guarda para /top; no entra en el score.
  n / n_real     observaciones totales / de ellas, copias reales.

Solo cuenta como MEDIDA una ⭐ con al menos MIN_N observaciones; el
orden del top pone primero las medidas con score > 0, luego las no
medidas (por wallet_score, como antes) y al final las medidas con score
<= 0 (se sabe que copiarlas pierde). Ver `db.orden_top`.
"""

import statistics
import time

from db import get_conn

VENTANA_DIAS = 60
K_ENCOGIMIENTO = 5
MIN_N = 5
TOPE_MIN = -100.0
TOPE_MAX = 300.0
BRECHA_MAX_PCT = 15.0
BRECHA_DEFECTO_PCT = 14.0     # mediana medida en la base el 04/09
COLUMNAS = ("copi_score", "copi_n", "copi_n_real", "copi_media", "copi_pf",
            "copi_dd", "copi_brecha", "copi_ts")


def _recorta(x):
    return max(TOPE_MIN, min(TOPE_MAX, float(x)))


def _pf(obs):
    pos = sum(x for x in obs if x > 0)
    neg = -sum(x for x in obs if x < 0)
    if neg <= 0:
        return 9.99 if pos > 0 else None
    return min(9.99, pos / neg)


def _dd(obs):
    """Máxima caída (puntos de %) de la curva acumulada, en orden temporal."""
    pico = acum = 0.0
    peor = 0.0
    for x in obs:
        acum += x
        pico = max(pico, acum)
        peor = max(peor, pico - acum)
    return peor


def calcular(conn, ahora=None, dias: int = VENTANA_DIAS) -> dict:
    """{address: {score, n, n_real, media, pf, dd, brecha}} para todas las
    ⭐ con al menos UNA observación. `poblacion` va bajo la clave "_pob"."""
    ahora = time.time() if ahora is None else ahora
    desde = ahora - dias * 86400
    # Copias reales cerradas con neto, por firma de la señal.
    copias = {}
    for r in conn.execute(
            """SELECT signature, wallet, stake_usd, pnl_usd, pnl_usd_neto
               FROM paper_trades
               WHERE status='cerrada' AND pnl_usd_neto IS NOT NULL
                 AND stake_usd > 0 AND COALESCE(exit_ts, 0) >= ?""",
            (desde,)).fetchall():
        copias[r["signature"]] = r
    # Brecha mediana por ⭐ (papel - neto, en % del importe) y poblacional.
    brechas = {}
    todas_brechas = []
    for r in copias.values():
        if r["pnl_usd"] is None:
            continue
        b = (r["pnl_usd"] - r["pnl_usd_neto"]) / r["stake_usd"] * 100
        brechas.setdefault(r["wallet"], []).append(b)
        todas_brechas.append(b)
    brecha_pob = (statistics.median(todas_brechas) if todas_brechas
                  else BRECHA_DEFECTO_PCT)
    brecha_w = {w: statistics.median(v) for w, v in brechas.items()
                if len(v) >= 3}
    # Observaciones: señales de compra medidas a 24 h, en orden temporal.
    obs = {}
    for s in conn.execute(
            """SELECT s.signature, s.wallet, s.chg_24h
               FROM signals s JOIN wallets w ON w.address = s.wallet
               WHERE s.side='compra' AND s.chg_24h IS NOT NULL
                 AND s.ts >= ? AND w.is_tracked = 1
               ORDER BY s.ts""", (desde,)).fetchall():
        c = copias.get(s["signature"])
        if c is not None:
            x = _recorta(c["pnl_usd_neto"] / c["stake_usd"] * 100)
            real = True
        else:
            x = _recorta(s["chg_24h"] - brecha_w.get(s["wallet"], brecha_pob))
            real = False
        obs.setdefault(s["wallet"], []).append((x, real))
    todas = [x for v in obs.values() for x, _ in v]
    pob = (sum(todas) / len(todas)) if todas else 0.0
    out = {"_pob": {"media": pob, "n": len(todas), "brecha": brecha_pob}}
    for w, v in obs.items():
        xs = [x for x, _ in v]
        n = len(xs)
        media = sum(xs) / n
        score = (n * media + K_ENCOGIMIENTO * pob) / (n + K_ENCOGIMIENTO)
        brecha = brecha_w.get(w)
        if brecha is not None and brecha > BRECHA_MAX_PCT and score > 0:
            score *= 0.5
        out[w] = {"score": round(score, 2), "n": n,
                  "n_real": sum(1 for _, r in v if r),
                  "media": round(media, 2), "pf": _pf(xs),
                  "dd": round(_dd(xs), 1),
                  "brecha": round(brecha, 2) if brecha is not None else None}
    return out


def actualizar(conn=None) -> int:
    """Recalcula y escribe las columnas copi_* de TODAS las ⭐ (las que se
    quedan sin observaciones en la ventana vuelven a NULL). Devuelve
    cuántas quedaron medidas (n >= MIN_N)."""
    propia = conn is None
    if propia:
        conn = get_conn()
    try:
        res = calcular(conn)
        ahora = int(time.time())
        conn.execute(
            "UPDATE wallets SET copi_score=NULL, copi_n=NULL, copi_n_real=NULL, "
            "copi_media=NULL, copi_pf=NULL, copi_dd=NULL, copi_brecha=NULL, "
            "copi_ts=? WHERE is_tracked = 1", (ahora,))
        medidas = 0
        for w, m in res.items():
            if w == "_pob":
                continue
            conn.execute(
                """UPDATE wallets SET copi_score=?, copi_n=?, copi_n_real=?,
                   copi_media=?, copi_pf=?, copi_dd=?, copi_brecha=?, copi_ts=?
                   WHERE address=?""",
                (m["score"], m["n"], m["n_real"], m["media"], m["pf"],
                 m["dd"], m["brecha"], ahora, w))
            if m["n"] >= MIN_N:
                medidas += 1
        conn.commit()
        try:
            from db import invalidar_copiables
            invalidar_copiables()          # el orden del top cambió
        except Exception as e:
            print(f"· Copiabilidad: no pude invalidar el conjunto operativo ({e})")
        print(f"📐 Copiabilidad: {len(res) - 1} ⭐ con observaciones, "
              f"{medidas} medidas (≥{MIN_N}); media poblacional "
              f"{res['_pob']['media']:+.1f}% · brecha {res['_pob']['brecha']:.1f}%")
        return medidas
    finally:
        if propia:
            conn.close()


def linea_top(w) -> str:
    """Una línea para /top con lo medido. `w` es la fila de top_wallets."""
    def _c(k):
        try:
            return w[k]
        except (KeyError, IndexError, TypeError):
            return None
    n = _c("copi_n")
    if not n:
        return "   📐 copiable: sin medidas (en prueba)"
    score, pf, br, nr = _c("copi_score"), _c("copi_pf"), _c("copi_brecha"), _c("copi_n_real") or 0
    txt = (f"   📐 copiable: {score:+.0f}% · n {n}"
           + (f" ({nr} reales)" if nr else "")
           + (f" · PF {pf:.1f}" if pf is not None else "")
           + (f" · brecha {br:.0f}%" if br is not None else ""))
    if n < MIN_N:
        txt += f" · faltan {MIN_N - n} para contar"
    elif br is not None and br > BRECHA_MAX_PCT:
        txt += " ⚠️ mueve el pool"
    return txt
