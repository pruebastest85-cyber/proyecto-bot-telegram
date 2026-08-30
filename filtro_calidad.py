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
        "neto_min": g("FILTRO_NETO_MIN", 0.0),
        # (19-N) Profit factor minimo. 0 = puerta APAGADA, que es el
        # defecto a proposito: desplegar el codigo no cambia a quien
        # copia el bot de nadie. Se enciende poniendo el valor en el
        # entorno, que es una decision del dueño, no de un despliegue.
        "pf_min": g("FILTRO_PF_MIN", 0.0),
    }


def _mediana(vals):
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    m = n // 2
    return v[m] if n % 2 else (v[m - 1] + v[m]) / 2.0


def historial(conn, wallet: str | None = None,
              todas: bool = False) -> dict:
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

    Con `todas=True` mira a TODA la base, no solo a las ⭐ — es lo que
    necesita `promocion()`. (19-J) Sin este parámetro el embudo era de
    UNA SOLA DIRECCIÓN: `reevaluacion` recorre `WHERE is_tracked = 1` y
    esta consulta hace `JOIN … WHERE w.is_tracked = 1`, así que las dos
    solo podían MIRAR a quien ya tenía estrella. Una billetera que
    perdía la estrella no volvía a ser examinada por nadie
    (`evaluate_tracked` solo mira las que no tienen evaluación o la
    tienen caducada, y estas ya tienen nota), en contra de lo que
    promete el comentario de `reevaluacion`: "las descartadas quedan
    como candidatas… nada es irreversible". Sí lo era. Medido en la base
    del dueño el 30/8: 28 billeteras pasaban las puertas 1-2 y solo 7
    tenían estrella; entre las 21 excluidas para siempre había una con
    +663 SOL netos, 57% de acierto en 23 cerradas y 45 min de retención.

    Los bots quedan fuera de la pasada `todas` en la propia consulta: no
    tiene sentido calcularles el historial para luego descartarlos, y
    así ninguna ruta de promoción puede colarlos por descuido.
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
    elif todas:
        # (19-J) La consulta va ENTERA y literal, sin `.format` ni f-string
        # para cambiar solo el WHERE: `auditoria.py` (clase 1) solo sabe
        # leer consultas que son una cadena constante, así que una armada
        # con `.format` se la salta EN SILENCIO y deja de estar auditada.
        # Repetir el SELECT es más feo y mucho más seguro que perder la
        # red de seguridad justo en la consulta que decide a quién copia
        # el bot.
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
               WHERE t.ts >= ? AND COALESCE(w.is_bot, 0) = 0
               GROUP BY t.wallet, t.mint""", (desde,)).fetchall()
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
                            "holds": [], "neto": 0.0,
                            "gana": 0.0, "pierde": 0.0})
        sol_in = r["sol_in"] or 0
        sol_out = r["sol_out"] or 0
        tok_in = r["tok_in"] or 0
        tok_out = r["tok_out"] or 0
        if sol_in > 0:
            d["tokens"] += 1
            # (18-N) El NETO de la ventana suma TODOS los mints comprados,
            # cerrados o no: las perdidas gordas suelen vivir en bolsas
            # que nunca se cierran y el winrate de las cerradas no las ve
            # (caso real cazado por el dueño: 62% de acierto y -21,8 SOL).
            # Los mints sin compra (airdrops vendidos) quedan fuera:
            # ingreso regalado no es habilidad copiable. LIMITE conocido
            # (auditoria 18-N): una compra-polvo de 0.001 SOL convierte
            # el mint en "comprado" y sus ventas por transferencias SI
            # entrarian al neto — burlarlo requiere intencion, y la
            # puerta 3 (señales medidas en vivo) sigue detras.
            d["neto"] += sol_out - sol_in
        if sol_in > 0 and sol_out > 0 and tok_in > 0 \
                and 0.7 * tok_in <= tok_out <= 1.05 * tok_in:
            d["cerradas"] += 1
            if sol_out > sol_in:
                d["ganadas"] += 1
                d["gana"] += sol_out - sol_in
            else:
                d["pierde"] += sol_in - sol_out
            if r["t_compra"] and r["t_venta"] \
                    and r["t_venta"] > r["t_compra"]:
                d["holds"].append((r["t_venta"] - r["t_compra"]) / 60.0)
    res = {}
    for w, d in por.items():
        wr = (100.0 * d["ganadas"] / d["cerradas"]) if d["cerradas"] else None
        # (19-N) PROFIT FACTOR: por cada SOL perdido, cuantos ganados.
        # El winrate cuenta CUANTAS veces aciertas; esto cuenta CUANTO,
        # que en memecoins es lo que paga. Caso real de la base del
        # dueño: "Jaguar Dorado Beta" acierta el 44% —el filtro la
        # echaba— pero en 90 dias gano 36,8 SOL y perdio 2,8: profit
        # factor 13. En el paper hizo +1.565 USD, la mejor con
        # diferencia.
        #
        # Sin perdidas se usa 99.99 como tope simbolico, igual que en
        # `wallet_metrics.py`, para no dividir por cero ni inventar un
        # infinito; con pocas operaciones eso puede ser suerte, y por eso
        # la puerta de `min_cerradas` va DELANTE.
        if d["pierde"] > 0:
            pf = d["gana"] / d["pierde"]
        elif d["gana"] > 0:
            pf = 99.99
        else:
            pf = None                # sin cerradas medibles
        res[w] = {"cerradas": d["cerradas"], "wr": wr,
                  "tokens": d["tokens"], "hold_min": _mediana(d["holds"]),
                  "neto": d["neto"], "pf": pf}
    return res


def medidas(conn, wallet: str | None = None) -> dict:
    """{billetera: {"n", "acierto", "mediana"}} de sus compras medidas.

    SIN ventana temporal, y es DELIBERADO (18-O, ronda 2). Se probó a
    ponerle la misma ventana que al historial y la auditoría demostró
    que ahí la caducidad juega al revés: con FILTRO_PROVISIONAL=1 (lo
    que eligió el dueño), "pocas medidas" NO significa suspenso, sino
    aprobado provisional. Es decir, dejar caducar unas medidas malas no
    baja la nota: la SUBE, y una billetera silenciada por sus resultados
    reales volvía al altavoz sola con solo pasar el tiempo. Olvidar es
    peligroso cuando la ausencia de datos se premia.
    """
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


# Palabras que solo aparecen en los motivos de la PUERTA 2. `resumen()`
# (/filtro) las usa para saber si una billetera se quedó en la puerta 1 o
# en la 2 sin volver a escribir las condiciones. Si se cambia el texto de
# un motivo, hay que mirar aquí (y la suite lo caza).
_PALABRAS_PUERTA2 = ("retención", "tokens operados")


def puertas_historial(hist: dict | None) -> tuple:
    """(pasa, motivo) de las puertas 1 y 2 SOLAS — las que se juzgan con
    el historial guardado en `trades`, sin señales medidas.

    (18-O) Existe para que haya UNA sola versión de las puertas 1-2.
    Antes vivían escritas tres veces: dentro de `puertas`, copiadas a
    mano en `reevaluacion` (/reembudo) y ausentes en la promoción de la
    IA — y esa ausencia era el agujero que metió 17 ⭐ malas en un día
    (medido en la base del dueño el 28/8: de 24 ⭐, 7 pasaban; las 17
    restantes las había promovido la IA sin pasar por aquí).
    """
    cfg = _cfg()
    h = hist or {"cerradas": 0, "wr": None, "tokens": 0, "hold_min": None,
                 "neto": 0.0}
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
    if h.get("neto", 0.0) <= cfg["neto_min"]:
        # (18-N) Ganar "por acierto" no basta: el neto de TODA la ventana
        # (bolsas sin cerrar incluidas) tiene que ser positivo. Cazado
        # por el dueño: 62% de winrate en cerradas y -21,8 SOL netos.
        _n = h.get("neto", 0.0)
        if _n <= 0:
            return (False, f"pierde dinero: neto {_n:+.1f} SOL "
                           f"en {cfg['ventana_dias']} días (las bolsas "
                           f"sin cerrar también cuentan)")
        # neto positivo pero por debajo del minimo que puso el dueño:
        # decir "pierde" seria mentira.
        return (False, f"no gana lo suficiente: neto {_n:+.1f} SOL "
                       f"(mínimo >{cfg['neto_min']:g}) en "
                       f"{cfg['ventana_dias']} días")
    # (19-N) PROFIT FACTOR — cuánto se gana por cada SOL perdido.
    #
    # El winrate cuenta CUÁNTAS veces se acierta; esto cuenta CUÁNTO, y
    # en memecoins es lo que paga: las ganadoras son pocas y enormes.
    # Medido sobre las 20 billeteras del dueño con historial y copias
    # medidas de verdad, correlación con el USD/copia real del paper:
    # profit factor +0,44 · expectancy +0,37 · winrate +0,18 · ROI
    # mediano +0,11. Y probando configuraciones enteras, con el corte de
    # winrate el filtro iba AL REVÉS (las que entraban perdían 4,2
    # USD/copia y las que quedaban fuera ganaban), mientras que
    # `pf >= 2` separaba bien y seguía separando bien al repetir la
    # prueba quitando la mejor billetera, las dos mejores y las tres
    # mejores — que es la comprobación que importa cuando una sola
    # billetera hace la mayor parte del beneficio.
    #
    # Va DESPUÉS de `min_cerradas` a propósito: con tres operaciones,
    # un profit factor altísimo es suerte, no habilidad.
    _pf = h.get("pf")
    if cfg["pf_min"] > 0:
        if _pf is None:
            return (False, "sin operaciones cerradas medibles para "
                           "calcular cuánto gana por cada SOL que pierde")
        if _pf < cfg["pf_min"]:
            return (False, f"gana {_pf:.2f} SOL por cada SOL que pierde "
                           f"(mínimo {cfg['pf_min']:g}): sus ganadoras no "
                           f"compensan sus perdedoras")
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
    return (True, "historial y copiabilidad OK")


def puertas(hist: dict | None, med: dict | None) -> tuple:
    """(pasa_todo, motivo). El motivo dice SIEMPRE en qué puerta se queda.

    `hist`/`med` son las entradas de historial()/medidas() para UNA
    billetera (None = sin datos en esa fuente).
    """
    cfg = _cfg()
    ok12, motivo12 = puertas_historial(hist)
    if not ok12:
        return (False, motivo12)
    h = hist or {"cerradas": 0, "wr": None, "tokens": 0, "hold_min": None,
                 "neto": 0.0}
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
                # (18-O) `AND is_tracked = 1`: la lista `est` se leyó al
                # empezar la pasada, y clasificar tarda. Si mientras
                # tanto otro hilo le quita la ⭐ (racha perdedora,
                # /descartar, revisión de rendimiento), sin esta
                # condición la confirmación resucitaba media billetera:
                # is_tracked=0 + confirmada=1. Ese estado imposible no
                # alerta hoy (el conjunto operativo exige las dos), pero
                # dejaba puesta la confirmación para la siguiente
                # promoción, que entraría al altavoz SIN pasar puertas.
                cur = conn.execute(
                    """UPDATE wallets SET confirmada = 1,
                       ai_reason = SUBSTR(? || COALESCE(ai_reason,''),
                                          1, 500)
                       WHERE address = ? AND is_tracked = 1""",
                    (f"✅ confirmada: {motivo} · ", w))
                if getattr(cur, "rowcount", 1) == 0:
                    n_conf -= 1        # se la llevó otro hilo
            continue
        # No pasa: en prueba. El reloj de la prueba empieza la primera
        # vez que se la clasifica (prueba_desde), no al descubrirla.
        desde = r["prueba_desde"]
        if r["confirmada"]:
            # Confirmada que dejó de cumplir: vuelve a prueba con motivo
            # y con el plazo de prueba fresco (como en 18-L: este reloj
            # es el de la INACTIVIDAD, no el que decide qué señales la
            # juzgan — eso vive en `turno_desde` y aquí no se toca).
            cur = conn.execute(
                """UPDATE wallets SET confirmada = 0, prueba_desde = ?,
                   ai_reason = SUBSTR(? || COALESCE(ai_reason,''), 1, 500)
                   WHERE address = ? AND is_tracked = 1""",
                (ahora, f"🔎 vuelve a prueba: {motivo} · ", w))
            if getattr(cur, "rowcount", 1) != 0:
                n_prueba += 1
            continue
        if not desde:
            # (18-O) `AND is_tracked = 1` también aquí: sin ella, una ⭐
            # degradada a mitad de la pasada acababa con
            # is_tracked=0 + prueba_desde puesto — un estado que no
            # existe en ningún otro sitio y que le regalaría el plazo de
            # prueba entero si volviera a entrar.
            cur = conn.execute(
                "UPDATE wallets SET prueba_desde = ? "
                "WHERE address = ? AND is_tracked = 1",
                (ahora, w))
            if getattr(cur, "rowcount", 1) != 0:
                n_prueba += 1
            continue
        # Retiro por inactividad: lleva toda la prueba sin operar.
        if desde < corte_prueba \
                and (ult_senal.get(w) or 0) < corte_prueba:
            # `AND prueba_desde = ?` es un candado sobre el reloj que se
            # acaba de leer: si entre el SELECT y este UPDATE el dueño
            # hizo /rastrear (reloj nuevo), la billetera NO se retira con
            # el reloj viejo. Es la misma carrera que 18-O cierra en la
            # racha perdedora.
            cur = conn.execute(
                """UPDATE wallets SET is_tracked = 0, ai_follow = 0,
                   confirmada = 0, prueba_desde = NULL, turno_desde = NULL,
                   ai_reason = SUBSTR(? || COALESCE(ai_reason,''), 1, 500)
                   WHERE address = ? AND is_tracked = 1
                     AND prueba_desde = ?""",
                (f"💤 sin ⭐: {cfg['prueba_dias']} días con ⭐ sin "
                 f"operar · ", w, desde))
            # Si no tocó ninguna fila, otro hilo se la llevó (o el
            # dueño la restauró): no se cuenta ni como retirada ni como
            # en prueba, porque esta pasada no hizo ni una cosa ni otra.
            if getattr(cur, "rowcount", 1) != 0:
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
                            "hold_min": None, "neto": 0.0}
        # (18-O, ronda 3) El veredicto de las puertas 1-2 lo da SIEMPRE
        # `puertas_historial`. Aquí estaba copiado a mano y /filtro podía
        # acabar contando con un criterio distinto del que decide de
        # verdad — el mando que el dueño usa para entender el embudo
        # mostrando otra cosa que el embudo.
        _ok12, _m12 = puertas_historial(h)
        # El desglose 1 vs 2 se saca del MOTIVO que devuelve esa misma
        # función; las palabras que lo delatan viven en UNA constante
        # (_PALABRAS_PUERTA2) y hay prueba que las fija.
        _es_p2 = any(p in _m12 for p in _PALABRAS_PUERTA2)
        if _ok12 or _es_p2:
            p1 += 1
            if _ok12:
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
            f"≥{cfg['min_cerradas']} cerradas / {cfg['ventana_dias']}d, "
            f"neto >{cfg['neto_min']:g} SOL): pasan {p1}\n"
            f"2️⃣ Copiable (retención ≥{cfg['hold_min_min']:.0f} min, "
            f"≥{cfg['min_tokens']} tokens): pasan {p2}\n"
            f"{_linea3}\n\n"
            f"_Solo las confirmadas alertan y se copian. Las demás se "
            f"miden en silencio y, si pasan {cfg['prueba_dias']} días "
            f"sin operar, pierden la estrella. Umbrales: variables "
            f"FILTRO\\_\\* del entorno._")


def reevaluacion(conn, ejecutar: bool = False) -> dict:
    """Pasa TODAS las ⭐ por el embudo VIGENTE de un solo golpe.

    Pedido del dueño (28/8): "cuando el embudo cambia, una función de
    re-evaluar todas las billeteras en una sola ola de evaluaciones y
    descartar lo que no sirve con el nuevo embudo y dejar lo que
    sobrevive". Sin esto, una ⭐ vieja que jamás pasaría el embudo nuevo
    conservaba la estrella "en prueba" hasta agotar los 14 días de
    inactividad — semanas de lista inflada tras cada cambio de criterio.

    Criterio: se DESCARTA (pierde la estrella ya) la ⭐ cuyo HISTORIAL no
    pasa las puertas 1-2 — historial y copiabilidad no mejoran por
    esperar: si sus 90 días de operaciones no dan el winrate, la
    retención o la diversificación exigidos, medir sus señales no lo va
    a arreglar. SOBREVIVE la que pasa 1-2: confirmada si además pasa la
    puerta 3 (o el modo provisional), en prueba si aún le faltan
    medidas. Las descartadas quedan como candidatas normales: si su
    historial mejora, el embudo puede re-promoverlas — nada es
    irreversible.

    `ejecutar=False` (por defecto) es un ENSAYO: cuenta y lista sin
    tocar nada, para que el dueño confirme viendo números. Con
    `ejecutar=True` aplica los descartes y clasifica al final.

    Con el interruptor maestro apagado devuelve un error en vez de
    actuar: purgar por un embudo que el dueño apagó sería contradecirle.
    """
    cfg = _cfg()
    if not cfg["activo"]:
        return {"error": "el filtro de tres puertas está APAGADO "
                         "(FILTRO_TRES_PUERTAS=0); enciéndelo antes de "
                         "re-evaluar con él"}
    ahora = int(time.time())
    est = conn.execute(
        """SELECT address, alias FROM wallets
           WHERE is_tracked = 1""").fetchall()
    hist = historial(conn)
    caen, sobreviven = [], []
    for r in est:
        w = r["address"]
        h = hist.get(w) or {"cerradas": 0, "wr": None, "tokens": 0,
                            "hold_min": None, "neto": 0.0}
        # Solo puertas 1-2 (la 3 se gana midiendo en vivo, no se le
        # puede exigir a quien acaba de sobrevivir al re-embudo).
        # (18-O) Antes esta condicion estaba copiada a mano aqui y podia
        # separarse de puertas(); ahora las dos leen la MISMA funcion.
        pasa_12, motivo = puertas_historial(h)
        if pasa_12:
            sobreviven.append((w, r["alias"]))
            continue
        caen.append((w, r["alias"], motivo))
    res = {"total": len(est), "caen": len(caen),
           "sobreviven": len(sobreviven),
           "detalle_caen": caen[:400], "detalle_viven": sobreviven[:50],
           "ejecutado": False}
    if not ejecutar:
        return res
    for w, _alias, motivo in caen:
        try:
            conn.execute(
                """UPDATE wallets SET is_tracked = 0, ai_follow = 0,
                   confirmada = 0, prueba_desde = NULL, turno_desde = NULL,
                   ai_reason = SUBSTR(? || COALESCE(ai_reason,''), 1, 500)
                   WHERE address = ? AND is_tracked = 1""",
                (f"🧹 re-evaluación del embudo ({ahora}): {motivo} · ", w))
        except Exception as _e:
            print(f"· re-evaluación: {w[:8]}… omitida ({_e})")
    conn.commit()
    # Clasificar a las supervivientes con el embudo vigente (confirma o
    # deja en prueba) y refrescar el conjunto operativo ya.
    clasificar(conn)
    try:
        from db import invalidar_copiables
        invalidar_copiables()
    except Exception:
        pass
    res["ejecutado"] = True
    # El estado FINAL se cuenta DESPUES de clasificar (auditoria 18-M):
    # una superviviente del corte puede caer un instante despues por la
    # regla de inactividad (14 dias en prueba sin operar), y el mensaje
    # de "sobreviven N" mentiria. Se informa lo que de verdad quedo.
    res["quedan"] = conn.execute(
        """SELECT COUNT(*) AS c FROM wallets
           WHERE is_tracked = 1""").fetchone()["c"]
    res["confirmadas"] = conn.execute(
        """SELECT COUNT(*) AS c FROM wallets
           WHERE is_tracked = 1 AND confirmada = 1""").fetchone()["c"]
    return res


def promocion(conn, ejecutar: bool = False) -> dict:
    """La mitad que faltaba del embudo: PONE estrellas, no solo las quita.

    (19-J) `reevaluacion` y `depurar_estrellas` solo recorren
    `is_tracked = 1`, y `evaluate_tracked` solo mira billeteras sin
    evaluar o con la evaluación caducada. Resultado: una billetera que
    ya tiene nota y no tiene estrella NO la vuelve a examinar nadie,
    aunque su historial pase el embudo de sobra. El embudo solo sabía
    restar. Medido en la base del dueño el 30/8: 28 pasaban las puertas
    1-2 y solo 7 tenían estrella.

    Aquí se aplican las MISMAS puertas 1-2 que en el resto del embudo
    (`puertas_historial`, una sola fuente de verdad) sobre TODA la base.
    Lo que entra lo hace EN PRUEBA (`confirmada = 0`): la puerta 3 se
    gana midiendo señales en vivo y no se regala por tener buen pasado
    — que es justo el error que este filtro existe para no repetir. El
    `clasificar` final decide si el modo provisional la confirma.

    Los bots quedan fuera desde la consulta (`historial(todas=True)`).

    `ejecutar=False` (por defecto) es un ENSAYO: cuenta y lista sin
    tocar nada. Mismo trato que `/reembudo`: el dueño ve los números
    antes de que su bot empiece a copiar a nadie nuevo.

    Con el interruptor maestro apagado devuelve un error en vez de
    actuar, por el mismo motivo que `reevaluacion`: promover con un
    embudo que el dueño apagó sería contradecirle.
    """
    cfg = _cfg()
    if not cfg["activo"]:
        return {"error": "el filtro de tres puertas está APAGADO "
                         "(FILTRO_TRES_PUERTAS=0); enciéndelo antes de "
                         "promover con él"}
    ahora = int(time.time())
    ya = {r["address"] for r in conn.execute(
        "SELECT address FROM wallets WHERE is_tracked = 1").fetchall()}
    # (19-M) LA NOTA DEL EMBUDO MANDA TAMBIÉN AQUÍ.
    #
    # `depurar_estrellas` QUITA la estrella a quien tiene una nota que la
    # bloquea (`nota_bloquea`, hoy: 'Descartada'). `promocion` no miraba
    # la nota, así que DABA la estrella a esas mismas billeteras: el
    # embudo se contradecía consigo mismo y las subía y bajaba en bucle,
    # gastando créditos de Helius en re-perfilarlas cada vuelta.
    #
    # No es hipótesis. Medido en la base del dueño el 30/8: de las 21 que
    # `/promover si` subió, **8 perdieron la estrella en menos de una
    # hora**, y son EXACTAMENTE las 8 que ya tenían `grade='Descartada'`
    # en el momento de subirlas. Ninguna de las otras 13 cayó.
    #
    # Las dos mitades del embudo usan ahora la MISMA función para decidir
    # si una nota permite estrella, así que no pueden volver a
    # separarse. Una billetera con mala nota vuelve a ser promovible en
    # cuanto un perfilado nuevo se la mejore — que es lo correcto: la
    # nota se gana, no se ignora.
    try:
        from ai_analyst import nota_bloquea as _nota_bloquea
    except Exception as e:                       # pragma: no cover
        print(f"· promoción: sin guarda de nota ({e})")

        def _nota_bloquea(_c, _t):
            return False
    notas = {r["address"]: r["grade"] for r in conn.execute(
        "SELECT address, grade FROM wallets").fetchall()}
    hist = historial(conn, todas=True)
    suben = []
    frenadas_por_nota = 0
    for w, h in hist.items():
        if w in ya:
            continue
        pasa, _motivo = puertas_historial(h)
        if not pasa:
            continue
        if _nota_bloquea(conn, notas.get(w)):
            frenadas_por_nota += 1
            continue
        suben.append((w, h))
    # Mejores primero: si el dueño corta la lista por donde sea, se
    # queda con las de más recorrido, no con las que salieron antes del
    # diccionario.
    suben.sort(key=lambda x: (-(x[1].get("neto") or 0.0),
                              -(x[1].get("wr") or 0.0)))
    alias = {r["address"]: r["alias"] for r in conn.execute(
        "SELECT address, alias FROM wallets").fetchall()}
    res = {"candidatas": len(suben), "estrellas_ahora": len(ya),
           "frenadas_por_nota": frenadas_por_nota,
           "detalle": [(w, alias.get(w), h["cerradas"], h["wr"],
                        h["tokens"], h["hold_min"], h.get("neto", 0.0))
                       for w, h in suben[:60]],
           "ejecutado": False}
    if not ejecutar:
        return res
    for w, h in suben:
        try:
            motivo = (f"⬆️ promovida por el embudo ({ahora}): "
                      f"{h['cerradas']} cerradas, "
                      f"{(h['wr'] or 0):.0f}% de acierto, "
                      f"{h['tokens']} tokens, "
                      f"neto {h.get('neto', 0.0):+.1f} SOL · ")
            # `AND is_tracked = 0`: entre el cálculo y este UPDATE el
            # ciclo automático pudo darle la estrella por su cuenta.
            # Sin esta verja le pisaríamos `prueba_desde` y volvería a
            # empezar los 14 días de prueba desde cero.
            conn.execute(
                """UPDATE wallets
                   SET is_tracked = 1, ai_follow = 1, confirmada = 0,
                       prueba_desde = ?, turno_desde = NULL,
                       ai_reason = SUBSTR(? || COALESCE(ai_reason,''),
                                          1, 500)
                   WHERE address = ? AND is_tracked = 0""",
                (ahora, motivo, w))
        except Exception as _e:
            print(f"· promoción: {w[:8]}… omitida ({_e})")
    conn.commit()
    clasificar(conn)
    try:
        from db import invalidar_copiables
        invalidar_copiables()
    except Exception:
        pass
    res["ejecutado"] = True
    # Igual que en `reevaluacion`: se informa lo que QUEDÓ en la base,
    # no lo prometido. `clasificar` puede retirar a una recién promovida
    # (por ejemplo por la regla de inactividad) y decir "subieron 21"
    # cuando quedaron 19 sería mentir.
    res["quedan"] = conn.execute(
        """SELECT COUNT(*) AS c FROM wallets
           WHERE is_tracked = 1""").fetchone()["c"]
    res["confirmadas"] = conn.execute(
        """SELECT COUNT(*) AS c FROM wallets
           WHERE is_tracked = 1 AND confirmada = 1""").fetchone()["c"]
    return res
