"""
Las TRES PUERTAS del filtro de calidad (Ola 18-L, 27/8/2026).

Pedido del dueño, textual: "yo no quiero cantidad yo quiero calidad solo
billeteras que en su historial tengan un 60 o 70% de winrate y que su
estrategia sea copiable traders reales no bots no creadores de mercado".

Por qué así y no de otra forma — todo MEDIDO contra su base el 27/8:
  · 188 de 199 ⭐ no tenían NI UNA señal medida: la estrella se regalaba
    por el historial de Helius (un solo token acertado en 185 de 199).
  · La nota del embudo (wallet_score) tiene correlación ~0 con el
    resultado real de copiar (+0.09), y el PnL propio también (−0.06).
  · El winrate histórico ALTO, solo, selecciona scalpers y creadores de
    mercado (ganancias diminutas y rápidas): de 22 billeteras con WR≥60%
    y 10+ cerradas, 6 eran MM y la mayoría retenía 3-15 minutos —
    imposible copiarlas a tiempo. Correlación WR histórico vs resultado
    medido: −0.12. Por eso el WR es una puerta, no EL filtro.
  · Lo único que separó buenas de malas fue el registro de señales YA
    medidas por este bot (Jaguar Dorado 88% de acierto medido vs
    Golondrina 15%, misma nota del embudo). Por eso la puerta final es
    la confirmación en vivo.

Las puertas:
  1. HISTORIAL: WR ≥ FILTRO_WR_MIN sobre ≥ FILTRO_MIN_CERRADAS posiciones
     cerradas de verdad en FILTRO_VENTANA_DIAS días (tabla `trades`, el
     historial que el perfilador ya guarda; no gasta créditos).
  2. COPIABLE: retención mediana ≥ FILTRO_HOLD_MIN_MIN minutos (si vende
     antes de que la alerta llegue, no existe para el copiado) y
     ≥ FILTRO_MIN_TOKENS tokens distintos operados (un trader real
     diversifica; el insider de un token, no). Los bots y creadores de
     mercado ya los matan `_hard_bot_reason` y `creadora_de_mercado`.
  3. CONFIRMACIÓN EN VIVO: ≥ FILTRO_MIN_MEDIDAS compras medidas por ESTE
     bot con acierto ≥ FILTRO_ACIERTO_MIN% o mediana positiva.

Estados de una ⭐ (columna `wallets.confirmada`):
  · confirmada = 1 → alerta y se copia (entra al conjunto operativo).
  · confirmada = 0 → EN PRUEBA: se vigila y sus señales se miden EN
    SILENCIO (elección del dueño: ni alerta ni se copia). Si pasa
    FILTRO_PRUEBA_DIAS días en prueba sin operar, pierde la estrella.

Arranque PROVISIONAL (FILTRO_PROVISIONAL=1, elección del dueño 27/8):
quien pasa las puertas 1 y 2 pero aún no junta FILTRO_MIN_MEDIDAS
señales medidas confirma PROVISIONALMENTE — alerta y se copia mientras
el bot la mide, y la puerta 3 la juzga en cuanto las medidas llegan.
Con 0 es modo estricto: sin medidas buenas nadie alerta.

Interruptor maestro FILTRO_TRES_PUERTAS = 0: la clasificación marca a
TODAS las ⭐ como confirmadas (comportamiento anterior) y no retira a
nadie por inactividad. El interruptor actúa en la clasificación, no en
los consumidores: una sola fuente de verdad.
"""

import time


def _cfg() -> dict:
    try:
        import config as _c
    except Exception:
        _c = None

    def g(nombre, defecto):
        try:
            return type(defecto)(getattr(_c, nombre, defecto))
        except (TypeError, ValueError):
            return defecto
    return {
        "activo": g("FILTRO_TRES_PUERTAS", 1),
        "wr_min": g("FILTRO_WR_MIN", 60.0),
        "min_cerradas": g("FILTRO_MIN_CERRADAS", 10),
        "ventana_dias": g("FILTRO_VENTANA_DIAS", 90),
        "hold_min_min": g("FILTRO_HOLD_MIN_MIN", 30.0),
        "min_tokens": g("FILTRO_MIN_TOKENS", 8),
        "min_medidas": g("FILTRO_MIN_MEDIDAS", 5),
        "acierto_min": g("FILTRO_ACIERTO_MIN", 40.0),
        "prueba_dias": g("FILTRO_PRUEBA_DIAS", 14),
        "provisional": g("FILTRO_PROVISIONAL", 1),
    }


def _mediana(vals):
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    m = n // 2
    return v[m] if n % 2 else (v[m - 1] + v[m]) / 2.0


def historial(conn, wallet: str | None = None) -> dict:
    """{billetera: {"cerradas", "wr", "tokens", "hold_min"}} desde `trades`.

    Posición CERRADA = un mint con compras Y ventas donde se vendió al
    menos el 70% de los tokens comprados (así una venta testimonial de
    polvo no cuenta como "cerró") y COMO MUCHO el 105% (vender más de lo
    comprado significa tokens recibidos por transferencia — airdrops o
    billeteras hermanas pasándose fichas — y eso no es un trade medible:
    la auditoría demostró que infla el winrate). Ganada = sacó más SOL
    del que metió. La retención se aproxima como (ÚLTIMA venta − ÚLTIMA
    compra): la primera venta castigaba al holder que toma un beneficio
    temprano y aguanta el resto días, y la primera compra premiaba al
    que acumula un mint durante días y lo suelta en minutos — los dos
    sesgos iban justo contra el propósito de la puerta. En la
    diversificación solo cuentan los mints donde hubo COMPRA.

    Sin `wallet` calcula TODAS las ⭐ en una pasada (para la depuración:
    la versión de una en una haría cientos de consultas).
    """
    cfg = _cfg()
    desde = int(time.time()) - int(cfg["ventana_dias"]) * 86400
    # La tabla `trades` la crea trades_store BAJO DEMANDA (al perfilar):
    # en una base recien nacida aun no existe, y sin esto la clasificacion
    # entera moria y el bot se quedaba mudo hasta el primer perfilado
    # (auditoria 18-L, M2). Ademas /filtro era un mando roto en frio.
    try:
        from trades_store import _ensure
        _ensure(conn)
    except Exception:
        pass
    if wallet:
        rows = conn.execute(
            """SELECT wallet, mint,
                      SUM(CASE WHEN side = 'compra' THEN sol ELSE 0 END)
                          AS sol_in,
                      SUM(CASE WHEN side = 'venta' THEN sol ELSE 0 END)
                          AS sol_out,
                      SUM(CASE WHEN side = 'compra' THEN tokens ELSE 0 END)
                          AS tok_in,
                      SUM(CASE WHEN side = 'venta' THEN tokens ELSE 0 END)
                          AS tok_out,
                      MAX(CASE WHEN side = 'compra' THEN ts END) AS t_compra,
                      MAX(CASE WHEN side = 'venta' THEN ts END) AS t_venta
               FROM trades
               WHERE ts >= ? AND wallet = ?
               GROUP BY wallet, mint""", (desde, wallet)).fetchall()
    else:
        rows = conn.execute(
            """SELECT t.wallet AS wallet, t.mint AS mint,
                      SUM(CASE WHEN t.side = 'compra' THEN t.sol ELSE 0 END)
                          AS sol_in,
                      SUM(CASE WHEN t.side = 'venta' THEN t.sol ELSE 0 END)
                          AS sol_out,
                      SUM(CASE WHEN t.side = 'compra' THEN t.tokens
                          ELSE 0 END) AS tok_in,
                      SUM(CASE WHEN t.side = 'venta' THEN t.tokens
                          ELSE 0 END) AS tok_out,
                      MAX(CASE WHEN t.side = 'compra' THEN t.ts END)
                          AS t_compra,
                      MAX(CASE WHEN t.side = 'venta' THEN t.ts END)
                          AS t_venta
               FROM trades t
               JOIN wallets w ON w.address = t.wallet
               WHERE t.ts >= ? AND w.is_tracked = 1
               GROUP BY t.wallet, t.mint""", (desde,)).fetchall()
    por: dict = {}
    for r in rows:
        d = por.setdefault(r["wallet"],
                           {"cerradas": 0, "ganadas": 0, "tokens": 0,
                            "holds": []})
        sol_in = r["sol_in"] or 0
        sol_out = r["sol_out"] or 0
        tok_in = r["tok_in"] or 0
        tok_out = r["tok_out"] or 0
        if sol_in > 0:
            d["tokens"] += 1
        if sol_in > 0 and sol_out > 0 and tok_in > 0 \
                and 0.7 * tok_in <= tok_out <= 1.05 * tok_in:
            d["cerradas"] += 1
            if sol_out > sol_in:
                d["ganadas"] += 1
            if r["t_compra"] and r["t_venta"] \
                    and r["t_venta"] > r["t_compra"]:
                d["holds"].append((r["t_venta"] - r["t_compra"]) / 60.0)
    res = {}
    for w, d in por.items():
        wr = (100.0 * d["ganadas"] / d["cerradas"]) if d["cerradas"] else None
        res[w] = {"cerradas": d["cerradas"], "wr": wr,
                  "tokens": d["tokens"], "hold_min": _mediana(d["holds"])}
    return res


def medidas(conn, wallet: str | None = None) -> dict:
    """{billetera: {"n", "acierto", "mediana"}} de sus compras medidas."""
    if wallet:
        rows = conn.execute(
            """SELECT wallet, chg_24h FROM signals
               WHERE wallet = ? AND side = 'compra'
                 AND chg_24h IS NOT NULL""", (wallet,)).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.wallet AS wallet, s.chg_24h AS chg_24h
               FROM signals s
               JOIN wallets w ON w.address = s.wallet
               WHERE w.is_tracked = 1 AND s.side = 'compra'
                 AND s.chg_24h IS NOT NULL""").fetchall()
    por: dict = {}
    for r in rows:
        por.setdefault(r["wallet"], []).append(float(r["chg_24h"]))
    return {w: {"n": len(v),
                "acierto": 100.0 * sum(1 for x in v if x > 0) / len(v),
                "mediana": _mediana(v)}
            for w, v in por.items()}


def puertas(hist: dict | None, med: dict | None) -> tuple:
    """(pasa_todo, motivo). El motivo dice SIEMPRE en qué puerta se queda.

    `hist`/`med` son las entradas de historial()/medidas() para UNA
    billetera (None = sin datos en esa fuente).
    """
    cfg = _cfg()
    h = hist or {"cerradas": 0, "wr": None, "tokens": 0, "hold_min": None}
    # Puerta 1 — historial real
    if h["cerradas"] < cfg["min_cerradas"]:
        return (False, f"historial corto: {h['cerradas']} posiciones "
                       f"cerradas en {cfg['ventana_dias']} días "
                       f"(mínimo {cfg['min_cerradas']})")
    if h["wr"] is None or h["wr"] < cfg["wr_min"]:
        # h["wr"] puede ser None si el dueño pone FILTRO_MIN_CERRADAS=0
        # en el entorno: formatearlo a pelo lanzaba TypeError y tumbaba
        # la clasificacion de esa billetera (auditoria 18-L, m1).
        _wr = "?" if h["wr"] is None else f"{h['wr']:.0f}"
        return (False, f"winrate {_wr}% en {h['cerradas']} "
                       f"cerradas (mínimo {cfg['wr_min']:.0f}%)")
    # Puerta 2 — copiable
    if h["hold_min"] is None or h["hold_min"] < cfg["hold_min_min"]:
        _hm = "?" if h["hold_min"] is None else f"{h['hold_min']:.0f}"
        return (False, f"retención mediana {_hm} min: vende antes de que "
                       f"la alerta se pueda copiar "
                       f"(mínimo {cfg['hold_min_min']:.0f} min)")
    if h["tokens"] < cfg["min_tokens"]:
        return (False, f"solo {h['tokens']} tokens operados "
                       f"(mínimo {cfg['min_tokens']}: un trader real "
                       f"diversifica)")
    # Puerta 3 — confirmación en vivo, el único predictor demostrado
    m = med or {"n": 0, "acierto": None, "mediana": None}
    if m["n"] < cfg["min_medidas"]:
        # Arranque PROVISIONAL (elección del dueño, 27/8): quien ya
        # demostró historial y copiabilidad alerta y se copia mientras
        # este bot junta sus primeras medidas; en cuanto llegan, esta
        # misma puerta las juzga y una tanda mala le quita la
        # confirmación. FILTRO_PROVISIONAL=0 = modo estricto.
        if cfg["provisional"]:
            return (True, f"provisional: historial y copiabilidad OK, "
                          f"a la espera de sus primeras "
                          f"{cfg['min_medidas']} señales medidas "
                          f"(lleva {m['n']})")
        return (False, f"en prueba: {m['n']} señales medidas por este bot "
                       f"(hacen falta {cfg['min_medidas']})")
    if (m["acierto"] or 0) < cfg["acierto_min"] \
            and (m["mediana"] is None or m["mediana"] <= 0):
        _ac = "?" if m["acierto"] is None else f"{m['acierto']:.0f}"
        _me = "?" if m["mediana"] is None else f"{m['mediana']:+.0f}"
        return (False, f"señales medidas malas: acierto {_ac}% "
                       f"y mediana {_me}% en {m['n']} señales")
    _ac = "?" if m["acierto"] is None else f"{m['acierto']:.0f}"
    return (True, f"WR {h['wr']:.0f}% en {h['cerradas']} cerradas · "
                  f"retención {h['hold_min']:.0f} min · {h['tokens']} "
                  f"tokens · acierto medido {_ac}% "
                  f"en {m['n']} señales")


def clasificar(conn) -> dict:
    """Aplica las tres puertas a TODAS las ⭐ y escribe `confirmada`.

    Con el interruptor apagado, confirma a todas (comportamiento
    anterior) y no retira a nadie. Devuelve el resumen. No lanza: quien
    llama (depurar_estrellas) ya envuelve, pero cada UPDATE va guardado
    igualmente para que una fila rara no tire la pasada entera.
    """
    cfg = _cfg()
    ahora = int(time.time())
    est = conn.execute(
        """SELECT address, confirmada, prueba_desde FROM wallets
           WHERE is_tracked = 1""").fetchall()
    if not cfg["activo"]:
        conn.execute(
            "UPDATE wallets SET confirmada = 1 WHERE is_tracked = 1")
        conn.commit()
        return {"confirmadas": len(est), "en_prueba": 0, "retiradas": 0,
                "interruptor": "apagado"}
    hist = historial(conn)
    med = medidas(conn)
    ult_senal = {r["wallet"]: r["ult"] for r in conn.execute(
        """SELECT s.wallet AS wallet, MAX(s.ts) AS ult
           FROM signals s
           JOIN wallets w ON w.address = s.wallet
           WHERE w.is_tracked = 1
           GROUP BY s.wallet""").fetchall()}
    corte_prueba = ahora - int(cfg["prueba_dias"]) * 86400
    n_conf = n_prueba = n_ret = 0
    for r in est:
      # try por fila: una fila rara no tira la pasada entera (el resto
      # de billeteras se clasifica igual y el commit final las guarda).
      try:
        w = r["address"]
        ok, motivo = puertas(hist.get(w), med.get(w))
        # Los motivos de la clasificacion van AL PRINCIPIO de la ficha
        # (SUBSTR(? || ai_reason)): con la ficha llena, el patron de
        # "añadir al final" se tragaba el motivo nuevo en silencio, y
        # este rastro es la unica visibilidad del dueño sobre por que
        # una ⭐ alerta o calla (auditoria 18-L, ronda 2).
        if ok:
            n_conf += 1
            if not r["confirmada"]:
                conn.execute(
                    """UPDATE wallets SET confirmada = 1,
                       ai_reason = SUBSTR(? || COALESCE(ai_reason,''),
                                          1, 500)
                       WHERE address = ?""",
                    (f"✅ confirmada: {motivo} · ", w))
            continue
        # No pasa: en prueba. El reloj de la prueba empieza la primera
        # vez que se la clasifica (prueba_desde), no al descubrirla.
        desde = r["prueba_desde"]
        if r["confirmada"]:
            # Confirmada que dejó de cumplir: vuelve a prueba con motivo.
            conn.execute(
                """UPDATE wallets SET confirmada = 0, prueba_desde = ?,
                   ai_reason = SUBSTR(? || COALESCE(ai_reason,''), 1, 500)
                   WHERE address = ?""",
                (ahora, f"🔎 vuelve a prueba: {motivo} · ", w))
            n_prueba += 1
            continue
        if not desde:
            conn.execute(
                "UPDATE wallets SET prueba_desde = ? WHERE address = ?",
                (ahora, w))
            n_prueba += 1
            continue
        # Retiro por inactividad: lleva toda la prueba sin operar.
        if desde < corte_prueba \
                and (ult_senal.get(w) or 0) < corte_prueba:
            conn.execute(
                """UPDATE wallets SET is_tracked = 0, ai_follow = 0,
                   confirmada = 0, prueba_desde = NULL,
                   ai_reason = SUBSTR(? || COALESCE(ai_reason,''), 1, 500)
                   WHERE address = ?""",
                (f"💤 sin ⭐: {cfg['prueba_dias']} días en prueba sin "
                 f"operar · ", w))
            n_ret += 1
        else:
            n_prueba += 1
      except Exception as _e:
        print(f"· clasificacion de una billetera omitida ({_e})")
    conn.commit()
    return {"confirmadas": n_conf, "en_prueba": n_prueba,
            "retiradas": n_ret, "interruptor": "encendido"}


def resumen(conn) -> str:
    """Texto para /filtro: umbrales y cuántas pasan cada puerta hoy."""
    cfg = _cfg()
    est = [r["address"] for r in conn.execute(
        "SELECT address FROM wallets WHERE is_tracked = 1").fetchall()]
    hist = historial(conn)
    med = medidas(conn)
    p1 = p2 = p3 = prov = 0
    for w in est:
        h = hist.get(w) or {"cerradas": 0, "wr": None, "tokens": 0,
                            "hold_min": None}
        if h["cerradas"] >= cfg["min_cerradas"] \
                and h["wr"] is not None and h["wr"] >= cfg["wr_min"]:
            p1 += 1
            if (h["hold_min"] or 0) >= cfg["hold_min_min"] \
                    and h["tokens"] >= cfg["min_tokens"]:
                p2 += 1
                ok, motivo = puertas(h, med.get(w))
                if ok and motivo.startswith("provisional"):
                    prov += 1
                elif ok:
                    p3 += 1
    conf = conn.execute(
        """SELECT COUNT(*) AS c FROM wallets
           WHERE is_tracked = 1 AND confirmada = 1""").fetchone()["c"]
    estado = "encendido" if cfg["activo"] else "APAGADO (todas confirman)"
    _linea3 = (f"3️⃣ Confirmación (≥{cfg['min_medidas']} medidas, acierto "
               f"≥{cfg['acierto_min']:.0f}%): pasan {p3}")
    if cfg["provisional"]:
        _linea3 += f"  ·  provisionales: {prov}"
    return (f"🚪 *Filtro de tres puertas* — {estado}\n\n"
            f"⭐ actuales: {len(est)}  ·  confirmadas: {conf}\n"
            f"1️⃣ Historial (WR ≥{cfg['wr_min']:.0f}% en "
            f"≥{cfg['min_cerradas']} cerradas / "
            f"{cfg['ventana_dias']}d): pasan {p1}\n"
            f"2️⃣ Copiable (retención ≥{cfg['hold_min_min']:.0f} min, "
            f"≥{cfg['min_tokens']} tokens): pasan {p2}\n"
            f"{_linea3}\n\n"
            f"_Solo las confirmadas alertan y se copian. Las demás se "
            f"miden en silencio y, si pasan {cfg['prueba_dias']} días "
            f"sin operar, pierden la estrella. Umbrales: variables "
            f"FILTRO\\_\\* del entorno._")
