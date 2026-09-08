"""
Historial de mercado del token: fotos, hitos, ATH y supervivencia.
(Embudo v2, fase 4 — 08/09/2026)

POR QUE EXISTE
--------------
El embudo viejo mira el token UNA vez, cuando lo descubre: guarda su
subida de 24 h, su volumen y su liquidez de ESE instante y no vuelve a
mirar. Con eso no se puede contestar lo que el dueño pregunta —"¿este
token llego a 500K o a un millon, y SIGUE VIVO?"— porque un token puede
haber tocado 5M y estar hoy en 50K, y al reves.

Ninguna API gratuita da market cap historico. Asi que el historial se
CONSTRUYE con fotos periodicas (`token_snapshots`) y de ahi salen los
hitos (`token_milestones`), el maximo historico y la supervivencia.

DE DONDE SALEN LOS DATOS (cero creditos de Helius)
--------------------------------------------------
1. DexScreener en lote: `token_check.datos_lote`, 30 tokens por peticion.
2. RELLENO GRATIS desde `signals`: la base del dueño ya tiene 146.975
   observaciones de MC sobre 33.765 tokens —cada vez que una billetera
   vigilada opero un token se guardo su MC y su liquidez—, y 360 de esos
   tokens llegaron a 1M sin que el embudo llegara a analizarlos. Ese
   historial ya esta pagado; solo habia que leerlo.

HONESTIDAD DEL DATO (regla 54 del encargo)
------------------------------------------
Una foto sacada de `signals` solo existe cuando alguien opero: la curva
es irregular y el primer cruce que se ve es "la primera vez que se
OBSERVO por encima de X", no necesariamente la primera vez que ocurrio.
Por eso cada hito guarda `source` y `confidence`, y la confianza es alta
solo cuando hay una foto POR DEBAJO del nivel poco antes (el cruce queda
acotado entre dos observaciones).

Tampoco se confunde FDV con market cap: `mc_source` dice cual de los dos
se uso. En memecoins, GeckoTerminal casi siempre devuelve FDV.
"""

import time

import config
from db import get_conn

# Un token no necesita una foto cada minuto: por debajo de este hueco se
# considera la misma observacion y no se guarda (la clave es (mint, ts),
# asi que sin esto una pasada rapida llenaria la tabla de casi-duplicados).
FOTO_HUECO_MIN_S = 300

# Para acotar el cruce de un hito entre dos observaciones: si la foto
# anterior (por debajo del nivel) esta a menos de esto, la marca de
# tiempo del cruce es fiable.
HITO_VENTANA_FIABLE_S = 3 * 3600

# Cuantas horas se aceptan de margen al comprobar la supervivencia: la
# foto de "24 h despues" puede no existir justo a las 24 h.
SUPERV_MARGEN_H = 12.0

COLUMNAS_ESTADO = ("current_mc", "current_price", "current_liquidity",
                   "current_volume_24h", "current_mc_ts", "mc_source",
                   "ath_mc", "ath_mc_ts", "survival_24h", "survival_7d",
                   "survival_score", "survival_reason",
                   "survival_confidence", "peak_to_current_pct",
                   "token_class", "check_priority", "last_checked",
                   "data_confidence")


# ── Fotos ────────────────────────────────────────────────────────────

def guardar_foto(conn, mint: str, d: dict, ts=None,
                 source: str = "dexscreener") -> bool:
    """Guarda una foto del mercado del token. Devuelve True si se guardo.

    Idempotente: la clave es (mint, ts) y ademas se respeta un hueco
    minimo, asi que repetir la pasada no duplica nada.
    """
    if not mint or not d:
        return False
    mc = d.get("mc")
    if mc is None and d.get("price") is None:
        return False            # una foto sin precio ni MC no dice nada
    ts = int(ts if ts is not None else time.time())
    try:
        ultima = conn.execute(
            "SELECT MAX(ts) t FROM token_snapshots WHERE mint = ?",
            (mint,)).fetchone()["t"]
        if ultima and 0 <= ts - int(ultima) < FOTO_HUECO_MIN_S:
            return False
        txns = d.get("txns24") or {}
        n_tx = None
        if isinstance(txns, dict):
            try:
                n_tx = int((txns.get("buys") or 0) + (txns.get("sells") or 0))
            except (TypeError, ValueError):
                n_tx = None
        conn.execute(
            "INSERT OR IGNORE INTO token_snapshots "
            "(mint, ts, price, mc, fdv, liquidity, volume_24h, txns_24h, "
            "mc_source, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mint, ts, d.get("price"), mc, d.get("fdv"), d.get("liq"),
             d.get("vol24"), n_tx, d.get("mc_source"), source))
        return True
    except Exception as e:
        print(f"· Historial: no pude guardar la foto de {mint[:8]}… ({e})")
        return False


def backfill_desde_signals(conn, limite_tokens: int = 25,
                           min_mc: float | None = None) -> int:
    """Convierte en fotos el MC que `signals` ya guardo, para los tokens
    que alguna vez se vieron por encima de `min_mc`. Devuelve cuantas
    fotos nuevas se escribieron.

    Se hace por tandas y una sola vez por token: el token queda marcado
    con `data_confidence='historico'` en winning_tokens si esta ahi, y
    los que ya tienen fotos de esta fuente no se vuelven a recorrer.
    """
    min_mc = config.MIN_WINNER_MC if min_mc is None else min_mc
    try:
        candidatos = conn.execute(
            """SELECT s.mint, MAX(s.mc) pico, COUNT(*) n
               FROM signals s
               WHERE s.mc IS NOT NULL AND s.mc >= ?
                 AND NOT EXISTS (SELECT 1 FROM token_snapshots t
                                 WHERE t.mint = s.mint
                                   AND t.source = 'signals')
               GROUP BY s.mint
               ORDER BY pico DESC
               LIMIT ?""", (min_mc, limite_tokens)).fetchall()
    except Exception as e:
        print(f"· Historial: no pude listar tokens para el relleno ({e})")
        return 0
    escritas = 0
    for c in candidatos:
        mint = c["mint"]
        try:
            filas = conn.execute(
                "SELECT ts, mc, liq, price_usd FROM signals "
                "WHERE mint = ? AND mc IS NOT NULL AND mc > 0 "
                "ORDER BY ts", (mint,)).fetchall()
        except Exception as e:
            print(f"· Historial: relleno de {mint[:8]}… fallo ({e})")
            continue
        ultimo = 0
        for f in filas:
            ts = int(f["ts"] or 0)
            if not ts or ts - ultimo < FOTO_HUECO_MIN_S:
                continue        # una observacion cada 5 min basta
            ultimo = ts
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO token_snapshots "
                    "(mint, ts, price, mc, fdv, liquidity, volume_24h, "
                    "txns_24h, mc_source, source) "
                    "VALUES (?,?,?,?,NULL,?,NULL,NULL,NULL,'signals')",
                    (mint, ts, f["price_usd"], f["mc"], f["liq"]))
                escritas += cur.rowcount or 0
            except Exception as e:
                print(f"· Historial: foto de {mint[:8]}… no entro ({e})")
                break
        conn.commit()
        recalcular_hitos(conn, mint)
        recalcular_ath(conn, mint)
    if escritas:
        print(f"📈 Historial: {escritas} fotos recuperadas de `signals` "
              f"({len(candidatos)} tokens)")
    return escritas


# ── Hitos ────────────────────────────────────────────────────────────

def recalcular_hitos(conn, mint: str) -> int:
    """Registra el PRIMER cruce observado de cada nivel de MC. Devuelve
    cuantos hitos nuevos se anotaron.

    `confidence`:
      · 'alta'  hay una foto POR DEBAJO del nivel menos de 3 h antes: el
                cruce queda acotado entre dos observaciones.
      · 'baja'  no la hay: el token pudo cruzar mucho antes sin que
                nadie mirara. NO se finge precision que no existe.
    """
    try:
        fotos = conn.execute(
            "SELECT ts, mc, price, liquidity, volume_24h, source "
            "FROM token_snapshots WHERE mint = ? AND mc IS NOT NULL "
            "ORDER BY ts", (mint,)).fetchall()
    except Exception as e:
        print(f"· Historial: no pude leer las fotos de {mint[:8]}… ({e})")
        return 0
    if not fotos:
        return 0
    nuevos = 0
    for nivel in config.TOKEN_MILESTONES_USD:
        anterior = None
        for f in fotos:
            if (f["mc"] or 0) < nivel:
                anterior = f
                continue
            hueco = (int(f["ts"]) - int(anterior["ts"])) if anterior else None
            conf = ("alta" if hueco is not None
                    and hueco <= HITO_VENTANA_FIABLE_S else "baja")
            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO token_milestones "
                    "(mint, milestone_usd, first_reached_ts, mc, price, "
                    "liquidity, volume, source, confidence) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (mint, float(nivel), int(f["ts"]), f["mc"], f["price"],
                     f["liquidity"], f["volume_24h"], f["source"], conf))
                nuevos += cur.rowcount or 0
            except Exception as e:
                print(f"· Historial: hito {nivel:.0f} de {mint[:8]}… "
                      f"no entro ({e})")
            break
    if nuevos:
        conn.commit()
    return nuevos


def hito_ts(conn, mint: str, nivel: float):
    """Cuando se vio por primera vez ese nivel de MC. None si nunca."""
    try:
        r = conn.execute(
            "SELECT first_reached_ts FROM token_milestones "
            "WHERE mint = ? AND milestone_usd = ?",
            (mint, float(nivel))).fetchone()
    except Exception as e:
        print(f"· Historial: no pude leer el hito de {mint[:8]}… ({e})")
        return None
    return r["first_reached_ts"] if r else None


# ── Maximo historico ─────────────────────────────────────────────────

def recalcular_ath(conn, mint: str) -> tuple:
    """(ath_mc, ath_mc_ts) desde las fotos; lo escribe en winning_tokens
    si mejora lo guardado. El ATH nunca baja: es historia."""
    try:
        r = conn.execute(
            "SELECT mc, ts FROM token_snapshots WHERE mint = ? "
            "AND mc IS NOT NULL ORDER BY mc DESC, ts ASC LIMIT 1",
            (mint,)).fetchone()
    except Exception as e:
        print(f"· Historial: no pude calcular el ATH de {mint[:8]}… ({e})")
        return (None, None)
    if not r:
        return (None, None)
    ath, ath_ts = r["mc"], int(r["ts"])
    try:
        conn.execute(
            "UPDATE winning_tokens SET ath_mc = ?, ath_mc_ts = ? "
            "WHERE mint = ? AND (ath_mc IS NULL OR ath_mc < ?)",
            (ath, ath_ts, mint, ath))
        conn.commit()
    except Exception as e:
        print(f"· Historial: no pude guardar el ATH de {mint[:8]}… ({e})")
    return (ath, ath_ts)


# ── Supervivencia ────────────────────────────────────────────────────

def _puntuar(mc, liq, vol24, txns24, ath, edad_dias) -> tuple:
    """(score 0-100, razon, confianza) con lo que haya. Determinista: no
    interviene ninguna IA (regla 27 del encargo)."""
    partes, faltan, score = [], [], 0.0
    # Liquidez (0-30): es lo que decide si se puede entrar y salir.
    if liq is None:
        faltan.append("liquidez")
    else:
        tope = float(config.SURVIVAL_MIN_LIQ_USD)
        p = 30.0 * min(1.0, liq / tope) if tope > 0 else 30.0
        score += p
        partes.append(f"liq ${liq:,.0f}")
    # Volumen 24 h (0-25): si nadie opera, el token esta abandonado.
    if vol24 is None:
        faltan.append("volumen")
    else:
        tope = float(config.SURVIVAL_MIN_VOL24_USD)
        score += 25.0 * min(1.0, vol24 / tope) if tope > 0 else 25.0
        partes.append(f"vol24 ${vol24:,.0f}")
    # Cuanto queda de su maximo (0-25).
    if ath and mc:
        pct = 100.0 * mc / ath
        objetivo = float(config.SURVIVAL_MIN_PCT_ATH)
        score += 25.0 * min(1.0, pct / objetivo) if objetivo > 0 else 25.0
        partes.append(f"{pct:.0f}% de su maximo")
    else:
        faltan.append("maximo historico")
    # Actividad reciente (0-10).
    if txns24 is None:
        faltan.append("transacciones")
    elif txns24 > 0:
        score += 10.0
        partes.append(f"{txns24} tx/24h")
    # Edad (0-10): sobrevivir tiempo ya es informacion.
    if edad_dias is not None:
        score += 10.0 * min(1.0, edad_dias / 7.0)
        partes.append(f"{edad_dias:.1f} d de vida")
    else:
        faltan.append("edad")
    if len(faltan) >= 3:
        conf = "baja"
    elif faltan:
        conf = "media"
    else:
        conf = "alta"
    razon = " · ".join(partes) if partes else "sin datos"
    if faltan:
        razon += f" (sin dato de: {', '.join(faltan)})"
    return (round(min(100.0, score), 1), razon[:400], conf)


def _vivo_en(conn, mint: str, desde_ts: int, horas: float):
    """¿Habia señales de vida `horas` despues de `desde_ts`?

    True / False / None. **None significa "todavia no se sabe"** —no hay
    ninguna foto en esa ventana— y no se puede confundir con False, que
    es una afirmacion. Confundirlos fue justo el error que la ola 18-E
    corrigio en la medicion (tratar "no lo se" como "vale 0").
    """
    objetivo = int(desde_ts + horas * 3600)
    margen = int(SUPERV_MARGEN_H * 3600)
    try:
        r = conn.execute(
            "SELECT mc, liquidity, volume_24h FROM token_snapshots "
            "WHERE mint = ? AND ts BETWEEN ? AND ? "
            "ORDER BY ABS(ts - ?) LIMIT 1",
            (mint, objetivo - margen, objetivo + margen, objetivo)
        ).fetchone()
    except Exception as e:
        print(f"· Historial: supervivencia de {mint[:8]}… ({e})")
        return None
    if r is None:
        return None
    # Vivo = seguia teniendo mercado. Se exige liquidez conocida por
    # encima del suelo, o —si no hay dato de liquidez— MC y volumen.
    if r["liquidity"] is not None:
        return bool(r["liquidity"] >= config.SURVIVAL_MIN_LIQ_USD)
    if r["mc"]:
        if r["volume_24h"] is not None:
            return bool(r["volume_24h"] >= config.SURVIVAL_MIN_VOL24_USD)
        return None             # solo MC: no alcanza para afirmar
    return False


def evaluar_supervivencia(conn, mint: str, datos: dict | None = None,
                          ahora=None) -> dict:
    """{survival_24h, survival_7d, survival_score, survival_reason,
    survival_confidence, peak_to_current_pct}. Nunca lanza."""
    ahora = time.time() if ahora is None else ahora
    fila = None
    try:
        fila = conn.execute(
            "SELECT ath_mc, detected_at FROM winning_tokens WHERE mint = ?",
            (mint,)).fetchone()
    except Exception as e:
        print(f"· Historial: no pude leer {mint[:8]}… ({e})")
    ath = fila["ath_mc"] if fila else None
    if not ath:
        ath = recalcular_ath(conn, mint)[0]
    # Estado actual: los datos frescos si los hay, si no la ultima foto.
    d = dict(datos or {})
    if not d:
        try:
            r = conn.execute(
                "SELECT mc, price, liquidity, volume_24h, txns_24h, ts "
                "FROM token_snapshots WHERE mint = ? "
                "ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
            if r:
                d = {"mc": r["mc"], "price": r["price"], "liq": r["liquidity"],
                     "vol24": r["volume_24h"], "txns24": r["txns_24h"]}
        except Exception as e:
            print(f"· Historial: sin foto reciente de {mint[:8]}… ({e})")
    txns = d.get("txns24")
    if isinstance(txns, dict):
        txns = (txns.get("buys") or 0) + (txns.get("sells") or 0)
    # Edad: desde la primera foto que tengamos.
    edad = None
    try:
        r0 = conn.execute(
            "SELECT MIN(ts) t FROM token_snapshots WHERE mint = ?",
            (mint,)).fetchone()
        if r0 and r0["t"]:
            edad = max(0.0, (ahora - float(r0["t"])) / 86400.0)
    except Exception as e:
        print(f"· Historial: sin edad para {mint[:8]}… ({e})")
    score, razon, conf = _puntuar(d.get("mc"), d.get("liq"), d.get("vol24"),
                                 txns, ath, edad)
    # Las ventanas de 24 h y 7 d se cuentan desde que cruzo el nivel de
    # ganador; si no hay hito, desde la primera foto.
    ref = hito_ts(conn, mint, config.MIN_WINNER_MC)
    if not ref:
        try:
            r0 = conn.execute("SELECT MIN(ts) t FROM token_snapshots "
                              "WHERE mint = ?", (mint,)).fetchone()
            ref = r0["t"] if r0 else None
        except Exception as e:
            print(f"· Historial: sin referencia para {mint[:8]}… ({e})")
    s24 = s7 = None
    if ref:
        if config.SURVIVAL_24H_ENABLED:
            s24 = _vivo_en(conn, mint, int(ref), 24.0)
        if config.SURVIVAL_7D_ENABLED:
            s7 = _vivo_en(conn, mint, int(ref), 24.0 * 7)
    pct = None
    if ath and d.get("mc"):
        pct = round(100.0 * d["mc"] / ath, 1)
    return {"survival_24h": s24, "survival_7d": s7,
            "survival_score": score, "survival_reason": razon,
            "survival_confidence": conf, "peak_to_current_pct": pct,
            "ath_mc": ath}


# ── Clase y prioridad de refresco ────────────────────────────────────

def clasificar(ath, score) -> str | None:
    """BREAKOUT (llego a 1M) · SURVIVOR (llego a 500K y sigue sano) ·
    None. Un token de 500K que nunca llego a 1M NO se descarta: son dos
    grupos, no una escalera (regla 3 del encargo)."""
    if not ath:
        return None
    if ath >= config.BREAKOUT_MC:
        return "BREAKOUT"
    if ath >= config.MIN_WINNER_MC and (score or 0) >= config.SURVIVAL_SCORE_MIN:
        return "SURVIVOR"
    return None


def prioridad(ath, score, edad_dias) -> str:
    """Cada cuanto merece la pena volver a mirarlo (regla 33)."""
    if ath and ath >= config.BREAKOUT_MC:
        return "WINNER"
    if ath and ath >= config.MIN_WINNER_MC:
        if (score or 0) >= config.SURVIVAL_SCORE_MIN:
            return "SURVIVOR"
        return "HISTORICAL"
    if edad_dias is not None and edad_dias <= 2:
        return "HOT"            # joven y acercandose: vigilar de cerca
    return "HISTORICAL"


# ── Ciclo ────────────────────────────────────────────────────────────

def _a_revisar(conn, limite: int) -> list:
    """Tokens que toca refrescar, por prioridad y por lo viejo que sea su
    ultimo vistazo. Los HISTORICAL casi no se tocan: gastar en un token
    muerto es tirar peticiones."""
    ahora = int(time.time())
    try:
        filas = conn.execute(
            """SELECT mint, check_priority, last_checked
               FROM winning_tokens
               ORDER BY CASE COALESCE(check_priority, 'HOT')
                          WHEN 'WINNER' THEN 0 WHEN 'HOT' THEN 1
                          WHEN 'SURVIVOR' THEN 2 ELSE 3 END,
                        COALESCE(last_checked, 0) ASC
               LIMIT ?""", (limite * 4,)).fetchall()
    except Exception as e:
        print(f"· Historial: no pude elegir tokens ({e})")
        return []
    espera = {"WINNER": 1800, "HOT": 1800, "SURVIVOR": 6 * 3600,
              "HISTORICAL": 24 * 3600}
    fuera = []
    for f in filas:
        pri = f["check_priority"] or "HOT"
        if ahora - int(f["last_checked"] or 0) >= espera.get(pri, 1800):
            fuera.append(f["mint"])
        if len(fuera) >= limite:
            break
    return fuera


def actualizar(conn=None, limite: int | None = None,
               con_relleno: bool = True) -> dict:
    """Una pasada: refresca fotos de los tokens que toque, recalcula
    hitos, ATH, supervivencia y clase, y rellena historial desde
    `signals`. Cero creditos de Helius. Devuelve un resumen."""
    propia = conn is None
    conn = conn or get_conn()
    limite = (config.TOKEN_HISTORY_TOKENS_POR_PASADA
              if limite is None else limite)
    res = {"fotos": 0, "hitos": 0, "tokens": 0, "relleno": 0}
    try:
        if con_relleno:
            res["relleno"] = backfill_desde_signals(
                conn, config.TOKEN_HISTORY_BACKFILL_POR_PASADA)
        mints = _a_revisar(conn, limite)
        if mints:
            try:
                from token_check import datos_lote
                datos = datos_lote(mints)
            except Exception as e:
                print(f"· Historial: la lectura en lote fallo ({e})")
                datos = {}
            ahora = int(time.time())
            for mint in mints:
                d = datos.get(mint)
                if d:
                    if guardar_foto(conn, mint, d, ahora):
                        res["fotos"] += 1
                    res["hitos"] += recalcular_hitos(conn, mint)
                    recalcular_ath(conn, mint)
                sup = evaluar_supervivencia(conn, mint, d, ahora)
                edad = None
                if d and d.get("pair_created_ms"):
                    try:
                        edad = (ahora - float(d["pair_created_ms"]) / 1000) / 86400
                    except (TypeError, ValueError):
                        edad = None
                clase = clasificar(sup["ath_mc"], sup["survival_score"])
                pri = prioridad(sup["ath_mc"], sup["survival_score"], edad)
                try:
                    conn.execute(
                        """UPDATE winning_tokens SET
                             current_mc = COALESCE(?, current_mc),
                             current_price = COALESCE(?, current_price),
                             current_liquidity = COALESCE(?, current_liquidity),
                             current_volume_24h = COALESCE(?, current_volume_24h),
                             current_mc_ts = ?, mc_source = COALESCE(?, mc_source),
                             survival_24h = ?, survival_7d = ?,
                             survival_score = ?, survival_reason = ?,
                             survival_confidence = ?,
                             peak_to_current_pct = ?, token_class = ?,
                             check_priority = ?, last_checked = ?
                           WHERE mint = ?""",
                        ((d or {}).get("mc"), (d or {}).get("price"),
                         (d or {}).get("liq"), (d or {}).get("vol24"),
                         ahora if d else None, (d or {}).get("mc_source"),
                         _int_o_none(sup["survival_24h"]),
                         _int_o_none(sup["survival_7d"]),
                         sup["survival_score"], sup["survival_reason"],
                         sup["survival_confidence"],
                         sup["peak_to_current_pct"], clase, pri, ahora,
                         mint))
                    res["tokens"] += 1
                except Exception as e:
                    print(f"· Historial: no pude guardar {mint[:8]}… ({e})")
            conn.commit()
        if res["tokens"] or res["fotos"] or res["relleno"]:
            print(f"📊 Historial de tokens: {res['tokens']} revisados · "
                  f"{res['fotos']} fotos · {res['hitos']} hitos nuevos · "
                  f"{res['relleno']} fotos recuperadas del historico")
        return res
    finally:
        if propia:
            conn.close()


def _int_o_none(v):
    """True/False → 1/0; None sigue siendo None ("todavia no se sabe")."""
    return None if v is None else (1 if v else 0)


def resumen_text(conn=None, limite: int = 10) -> str:
    """Texto para Telegram: que tokens hay, cuales siguen vivos."""
    propia = conn is None
    conn = conn or get_conn()
    try:
        tot = conn.execute("SELECT COUNT(*) c FROM winning_tokens"
                           ).fetchone()["c"]
        fotos = conn.execute("SELECT COUNT(*) c FROM token_snapshots"
                             ).fetchone()["c"]
        hitos = conn.execute(
            "SELECT COUNT(*) c FROM token_milestones").fetchone()["c"]
        n1m = conn.execute(
            "SELECT COUNT(DISTINCT mint) c FROM token_milestones "
            "WHERE milestone_usd >= ?", (float(config.BREAKOUT_MC),)
        ).fetchone()["c"]
        n500 = conn.execute(
            "SELECT COUNT(DISTINCT mint) c FROM token_milestones "
            "WHERE milestone_usd >= ?", (float(config.MIN_WINNER_MC),)
        ).fetchone()["c"]
        out = ["📊 *Historial de tokens*",
               f"{tot} tokens · {fotos:,} fotos · {hitos} hitos",
               f"Cruzaron ${config.MIN_WINNER_MC:,.0f}: *{n500}*  ·  "
               f"${config.BREAKOUT_MC:,.0f}: *{n1m}*", ""]
        filas = conn.execute(
            "SELECT mint, symbol, ath_mc, current_mc, survival_score, "
            "token_class, survival_confidence FROM winning_tokens "
            "WHERE ath_mc IS NOT NULL ORDER BY ath_mc DESC LIMIT ?",
            (limite,)).fetchall()
        for f in filas:
            act = (f"${f['current_mc']:,.0f}" if f["current_mc"]
                   else "sin dato")
            out.append(
                f"· {(f['symbol'] or f['mint'][:6])}: máx "
                f"${f['ath_mc']:,.0f} → {act}"
                + (f" · {f['token_class']}" if f["token_class"] else "")
                + (f" · vida {f['survival_score']:.0f}/100"
                   if f["survival_score"] is not None else "")
                + (f" ({f['survival_confidence']})"
                   if f["survival_confidence"] else ""))
        if not filas:
            out.append("_Aún sin fotos: la primera pasada las crea._")
        return "\n".join(out)
    finally:
        if propia:
            conn.close()
