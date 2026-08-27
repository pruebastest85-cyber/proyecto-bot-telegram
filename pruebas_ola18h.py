"""Pruebas de la Ola 18-H — los arreglos de la revisión del 26/8/2026.

Se ejecutan con `python3 pruebas_ola18h.py` desde la raíz del repo. No
necesitan red: todas las llamadas a `requests` se sustituyen por dobles.
La base de datos es un archivo SQLite temporal (`DB_PATH`), nunca la del
dueño.

VIVEN DENTRO DEL REPO A PROPOSITO. Las suites de las olas 17 y 18-A..G se
escribieron fuera y se perdieron DOS veces al reciclarse el contenedor de
trabajo. Una prueba que no está versionada no existe.
"""

import os
import sys
import tempfile
import threading
import types

_TMP = tempfile.mkdtemp(prefix="ola18h_")
os.environ["DB_PATH"] = os.path.join(_TMP, "pruebas.db")
os.environ.setdefault("HELIUS_API_KEY", "clave-de-prueba")
os.environ.setdefault("TELEGRAM_TOKEN", "0:token-de-prueba")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("TELEGRAM_ADMIN_ID", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_OK = 0
_FALLOS = []


def comprobar(nombre, condicion, detalle=""):
    global _OK
    if condicion:
        _OK += 1
        print(f"  ✓ {nombre}")
    else:
        _FALLOS.append(f"{nombre}: {detalle}")
        print(f"  ✗ {nombre} — {detalle}")


# ── Oráculo: puerto FIEL de `parse_markdown()` de tdlib ──────────
# (https://github.com/tdlib/td, td/telegram/MessageEntity.cpp).
# Es el código que de verdad decide si Telegram devuelve 400. NO se
# escribe "las reglas que parecen razonables": eso ya se hizo dos
# veces en esta ola y las dos veces la prueba acabó premiando el
# error de la implementación en vez de cazarlo.
def _ch(b, k):
    return b[k] if 0 <= k < len(b) else 0     # fuera de rango → '\0'

def _es_espacio(c):
    return c in (0x20, 0x09, 0x0d, 0x0a, 0x00, 0x0b)

def _valido(t):
    b = t.encode("utf-8")
    size = len(b)
    i = 0
    while i < size:
        c = _ch(b, i)
        if c == 0x5c and _ch(b, i + 1) in (0x5f, 0x2a, 0x60, 0x5b):
            i += 2                            # \_ \* \` \[  → texto
            continue
        if c not in (0x5f, 0x2a, 0x60, 0x5b):
            i += 1
            continue
        begin = i
        fin_car = 0x5d if c == 0x5b else c    # '[' cierra con ']'
        es_pre = False
        i += 1
        if c == 0x60 and _ch(b, i) == 0x60 and _ch(b, i + 1) == 0x60:
            i += 2
            es_pre = True
            fin_leng = i
            while (not _es_espacio(_ch(b, fin_leng))
                   and _ch(b, fin_leng) != 0x60):
                fin_leng += 1
            if i != fin_leng and fin_leng < size \
                    and _ch(b, fin_leng) != 0x60:
                i = fin_leng
            if _ch(b, i) in (0x0a, 0x0d):
                if (_ch(b, i + 1) in (0x0a, 0x0d)
                        and _ch(b, i) != _ch(b, i + 1)):
                    i += 2
                else:
                    i += 1
        ini = i
        while i < size and (_ch(b, i) != fin_car
                            or (es_pre and not (_ch(b, i + 1) == 0x60
                                                and _ch(b, i + 2) == 0x60))):
            i += 1
        if i == size:
            return f"entidad sin cerrar en el byte {begin}"
        if i != ini and c == 0x5b and _ch(b, i + 1) == 0x28:
            i += 2
            while i < size and _ch(b, i) != 0x29:
                i += 1
        if es_pre:
            i += 2
        i += 1
    return None


def bloque(titulo):
    print(f"\n── {titulo}")


# ─────────────────────────────────────────────────────────────────────
# GRAVE 1 · wallet_analyzer: un fallo TOTAL del RPC no cae al camino
#          antiguo, no borra la marca y no gasta 6.000 créditos.
# ─────────────────────────────────────────────────────────────────────
def prueba_grave1():
    bloque("GRAVE 1 · el fallo de Helius no cae al camino antiguo")
    import wallet_analyzer as wa

    llamadas_legacy = {"n": 0}

    def _legacy_espia(mint, max_pages=None, con_estado=False):
        llamadas_legacy["n"] += 1
        # El camino antiguo, cuando funciona, BORRA la marca. Esa es la
        # pieza que convertía el fallo en daño permanente.
        wa._set_fallo(None)
        return ([{"feePayer": "inventada"}], False) if con_estado \
            else [{"feePayer": "inventada"}]

    def _primeras_falla_del_todo(mint, max_txs=1500):
        return ([], False)

    def _primeras_falla_a_medias(mint, max_txs=1500):
        return ([{"feePayer": "real", "timestamp": 1}], False)

    falso_rpc = types.ModuleType("helius_rpc")
    falso_rpc.ultimo_fallo = lambda: "Helius no respondio (timeout)"
    falso_rpc.primeras_txs = _primeras_falla_del_todo

    legacy_real = wa._fetch_earliest_txs_legacy
    modulo_real = sys.modules.get("helius_rpc")
    try:
        wa._fetch_earliest_txs_legacy = _legacy_espia
        sys.modules["helius_rpc"] = falso_rpc

        # (a) Fallo TOTAL: 0 transacciones + marca puesta.
        txs, completo = wa.fetch_earliest_txs("MINT1", con_estado=True)
        comprobar("fallo total: NO se llama al camino antiguo",
                  llamadas_legacy["n"] == 0,
                  f"se llamó {llamadas_legacy['n']} veces")
        comprobar("fallo total: la marca sobrevive",
                  wa.motivo_fallo_descarga() is not None,
                  f"marca = {wa.motivo_fallo_descarga()!r}")
        comprobar("fallo total: historial NO completo", completo is False,
                  f"completo = {completo!r}")
        comprobar("fallo total: no se inventan transacciones",
                  txs == [], f"txs = {txs!r}")

        # (b) Fallo A MEDIAS: página 1 buena, página 2 caída.
        falso_rpc.primeras_txs = _primeras_falla_a_medias
        llamadas_legacy["n"] = 0
        txs, completo = wa.fetch_earliest_txs("MINT2", con_estado=True)
        comprobar("fallo a medias: NO se llama al camino antiguo",
                  llamadas_legacy["n"] == 0,
                  f"se llamó {llamadas_legacy['n']} veces")
        comprobar("fallo a medias: la marca sobrevive",
                  wa.motivo_fallo_descarga() is not None)
        comprobar("fallo a medias: historial NO completo", completo is False)
        comprobar("fallo a medias: se conservan las txs que SÍ llegaron "
                  "(las usa dev_check.find_creator)",
                  len(txs) == 1 and txs[0].get("feePayer") == "real",
                  f"txs = {txs!r}")

        # (c) Sin fallo: el comportamiento de siempre, intacto.
        falso_rpc.ultimo_fallo = lambda: None
        falso_rpc.primeras_txs = lambda mint, max_txs=1500: (
            [{"feePayer": "real", "timestamp": 1}], True)
        llamadas_legacy["n"] = 0
        txs, completo = wa.fetch_earliest_txs("MINT3", con_estado=True)
        comprobar("sin fallo: devuelve las txs y completo=True",
                  completo is True and len(txs) == 1)
        comprobar("sin fallo: la marca queda limpia",
                  wa.motivo_fallo_descarga() is None)
        comprobar("sin fallo: tampoco se llama al camino antiguo",
                  llamadas_legacy["n"] == 0)

        # (d) Sin fallo y sin datos: SÍ se cae al camino antiguo (es el
        #     caso legítimo, y no debe cambiar).
        falso_rpc.primeras_txs = lambda mint, max_txs=1500: ([], True)
        llamadas_legacy["n"] = 0
        wa.fetch_earliest_txs("MINT4", con_estado=True)
        comprobar("sin fallo y sin datos: SÍ usa el camino antiguo",
                  llamadas_legacy["n"] == 1,
                  f"se llamó {llamadas_legacy['n']} veces")
    finally:
        wa._fetch_earliest_txs_legacy = legacy_real
        if modulo_real is not None:
            sys.modules["helius_rpc"] = modulo_real
        else:
            sys.modules.pop("helius_rpc", None)
        wa._set_fallo(None)


# ─────────────────────────────────────────────────────────────────────
# GRAVE 2 · predictions: con el grafo frío NO se degrada lo ya medido.
# ─────────────────────────────────────────────────────────────────────
def prueba_grave2():
    bloque("GRAVE 2 · el grafo frío no degrada la confianza medida")
    import json as _json
    import predictions as pr
    import influence as infl
    from db import get_conn

    conn = get_conn()
    enviados = []
    contextos = []
    alert_real = pr._alert_stage
    graph_real = infl.graph
    cache_real = infl.cache_lista
    infl_real = infl.influence
    evaluar_real = pr.evaluate_due
    try:
        def _espia_alerta(*a, **k):
            enviados.append(a[0]["id"])
            contextos.append(a[6] if len(a) > 6 else {})

        pr._alert_stage = _espia_alerta
        pr.evaluate_due = lambda *a, **k: None

        lider = "L" * 32
        seguidor1, seguidor2 = "F1" + "x" * 30, "F2" + "x" * 30
        pred = [{"wallet": seguidor1, "prob": 100, "shared": 12},
                {"wallet": seguidor2, "prob": 90, "shared": 10}]
        grafo_caliente = {"leader_score": 100,
                          "followers": [dict(p) for p in pred]}

        def montar(mint):
            conn.execute("DELETE FROM predictions WHERE mint=?", (mint,))
            conn.execute(
                """INSERT INTO predictions
                   (leader, mint, created_ts, stage, confidence, meta_score,
                    predicted, arrived, alerted_stage, status, tier, price0)
                   VALUES (?,?,?,?,?,?,?,?,0,'abierta',?,?)""",
                (lider, mint, 1_700_000_000, 1, 89, 95,
                 _json.dumps(pred), "[]", "alpha", 0.001))
            conn.commit()
            return conn.execute(
                "SELECT * FROM predictions WHERE mint=?", (mint,)).fetchone()

        ctx = {"liq": 50000, "symbol": "TT", "price": 0.001}

        # (a) Grafo CALIENTE: se recalcula, como siempre.
        infl.influence = lambda w, construir=True: dict(grafo_caliente)
        infl.cache_lista = lambda: True
        pr.influence = infl.influence
        pr.cache_lista = infl.cache_lista
        montar("MCAL")
        enviados.clear()
        pr.on_buy(conn, seguidor1, "MCAL", 1_700_000_060, ctx,
                  vigiladas={lider, seguidor1, seguidor2}, es_estrella=False)
        cal = conn.execute("SELECT * FROM predictions WHERE mint=?",
                           ("MCAL",)).fetchone()
        comprobar("grafo caliente: se apunta la llegada",
                  _json.loads(cal["arrived"]) == [seguidor1],
                  f"arrived = {cal['arrived']!r}")
        # El valor esperado se calcula con la MISMA función, no a ojo:
        # lo que se comprueba es que con el grafo delante SÍ se recalcula.
        _salud = pr._leader_health(conn, lider)
        _esperado = pr.confidence_score(dict(grafo_caliente), pred, 50000,
                                        _salud, arrived=1)
        comprobar("grafo caliente: la confianza se recalcula con el grafo",
                  cal["confidence"] == _esperado,
                  f"confianza = {cal['confidence']}, esperada = {_esperado}")
        comprobar("grafo caliente: el recálculo cambia el valor guardado "
                  "(la prueba es sensible)",
                  cal["confidence"] != 89,
                  f"confianza = {cal['confidence']} (seguía siendo la de la fila)")
        comprobar("grafo caliente: sigue en alpha y se envía la alerta",
                  cal["tier"] == "alpha" and len(enviados) == 1,
                  f"tier={cal['tier']}, enviados={len(enviados)}")

        # (b) Grafo FRÍO: no se recalcula nada a la baja.
        infl.influence = lambda w, construir=True: None
        infl.cache_lista = lambda: False
        pr.influence = infl.influence
        pr.cache_lista = infl.cache_lista
        montar("MFRIO")
        enviados.clear()
        pr.on_buy(conn, seguidor1, "MFRIO", 1_700_000_060, ctx,
                  vigiladas={lider, seguidor1, seguidor2}, es_estrella=False)
        fri = conn.execute("SELECT * FROM predictions WHERE mint=?",
                           ("MFRIO",)).fetchone()
        comprobar("grafo frío: la llegada SÍ se apunta",
                  _json.loads(fri["arrived"]) == [seguidor1],
                  f"arrived = {fri['arrived']!r}")
        comprobar("grafo frío: la confianza medida NO se degrada",
                  fri["confidence"] == 89, f"confianza = {fri['confidence']}")
        comprobar("grafo frío: el tier medido NO se degrada",
                  fri["tier"] == "alpha", f"tier = {fri['tier']!r}")
        comprobar("grafo frío: la alerta ALPHA sigue saliendo",
                  len(enviados) == 1, f"enviados = {len(enviados)}")
        comprobar("grafo frío: la alerta NO dice que falten los puntos de "
                  "liderazgo (la confianza enviada SÍ los incluye)",
                  contextos and contextos[-1].get("_influencia_sabida")
                  is True,
                  f"contexto = {contextos[-1] if contextos else None}")
        comprobar("grafo frío: la etapa avanza igual",
                  fri["stage"] == 2, f"stage = {fri['stage']}")
        comprobar("grafo frío: se apunta el tiempo de la 1ª confirmación",
                  fri["first_confirm_s"] == 60,
                  f"first_confirm_s = {fri['first_confirm_s']}")
    finally:
        pr._alert_stage = alert_real
        pr.evaluate_due = evaluar_real
        infl.graph = graph_real
        infl.cache_lista = cache_real
        infl.influence = infl_real
        pr.influence = infl_real
        pr.cache_lista = cache_real
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# GRAVE 3 · signal_tracker: la liquidez desconocida no pierde contra un
#           pool muerto, y por tanto no se declara muerte.
# ─────────────────────────────────────────────────────────────────────
def prueba_grave3():
    bloque("GRAVE 3 · liquidez desconocida vs pool muerto")
    import signal_tracker as st

    def _respuesta(pares):
        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {"pairs": pares}

            @staticmethod
            def raise_for_status():
                return None
        return _R()

    par_bueno_sin_liq = {"chainId": "solana", "priceUsd": "0.00042",
                         "fdv": 250000, "marketCap": 250000,
                         "baseToken": {"symbol": "XX"}}
    par_muerto = {"chainId": "solana", "priceUsd": "0.00000001",
                  "fdv": 1, "marketCap": 1,
                  "liquidity": {"usd": 0},
                  "baseToken": {"symbol": "XX"}}
    par_vivo = {"chainId": "solana", "priceUsd": "0.00042",
                "fdv": 250000, "marketCap": 250000,
                "liquidity": {"usd": 50000},
                "baseToken": {"symbol": "XX"}}

    get_real = st.requests.get
    try:
        # (a) El caso del hallazgo: par bueno SIN campo liquidez +
        #     pool residual con liquidez 0.
        st._DEX_CACHE.clear() if hasattr(st, "_DEX_CACHE") else None
        st.requests.get = lambda *a, **k: _respuesta(
            [par_bueno_sin_liq, par_muerto])
        px, mc, muerto, liq = st._price_mc_ex("MINTA")
        comprobar("par bueno sin liquidez + pool muerto: NO se declara "
                  "muerte", muerto is False,
                  f"(px={px}, mc={mc}, muerto={muerto}, liq={liq})")
        comprobar("par bueno sin liquidez + pool muerto: gana el par con "
                  "precio real", px is not None and float(px) > 1e-6,
                  f"px = {px!r}")
        comprobar("par bueno sin liquidez + pool muerto: la liquidez del "
                  "par que da el precio se declara DESCONOCIDA (no se "
                  "cuela la del otro par al histórico)",
                  liq is None, f"liq = {liq!r}")

        # Si NINGÚN par dice la liquidez, lo mismo.
        st.requests.get = lambda *a, **k: _respuesta([par_bueno_sin_liq])
        _px2, _mc2, _m2, _liq2 = st._price_mc_ex("MINTA2")
        comprobar("ningún par con liquidez legible: liquidez desconocida",
                  _liq2 is None and _m2 is False,
                  f"(muerto={_m2}, liq={_liq2})")

        # Empate exacto en el umbral: gana el par cuya liquidez SÍ se sabe,
        # sin depender del orden en que DexScreener liste los pares.
        par_umbral = {"chainId": "solana", "priceUsd": "1.0",
                      "fdv": 1, "marketCap": 1,
                      "liquidity": {"usd": st.LIQ_MUERTO_USD},
                      "baseToken": {"symbol": "XX"}}
        par_sin = {"chainId": "solana", "priceUsd": "2.0", "fdv": 1,
                   "marketCap": 1, "baseToken": {"symbol": "XX"}}
        st.requests.get = lambda *a, **k: _respuesta([par_umbral, par_sin])
        r1 = st._price_mc_ex("MINTA3")
        st.requests.get = lambda *a, **k: _respuesta([par_sin, par_umbral])
        r2 = st._price_mc_ex("MINTA4")
        comprobar("empate en el umbral: el resultado NO depende del orden "
                  "de los pares", r1 == r2, f"{r1} vs {r2}")

        # (b) Muerte de verdad: TODOS los pares con liquidez legible y
        #     por debajo del umbral. Esto SÍ tiene que seguir dando muerte.
        st.requests.get = lambda *a, **k: _respuesta([par_muerto])
        px, mc, muerto, liq = st._price_mc_ex("MINTB")
        comprobar("solo pool muerto: SÍ se declara muerte", muerto is True,
                  f"(px={px}, muerto={muerto}, liq={liq})")

        # (c) Token vivo normal: sin cambios.
        st.requests.get = lambda *a, **k: _respuesta([par_vivo, par_muerto])
        px, mc, muerto, liq = st._price_mc_ex("MINTC")
        comprobar("token vivo: gana el pool con liquidez de verdad",
                  muerto is False and liq == 50000,
                  f"(muerto={muerto}, liq={liq})")

        # (d) Ningún par: contrato de siempre — muerte confirmada, sin
        #     precio. (Es distinto de "no pude preguntar", que deja
        #     `ultimo_fallo_precio()` puesto y muerto=False.)
        st.requests.get = lambda *a, **k: _respuesta([])
        px, mc, muerto, liq = st._price_mc_ex("MINTD")
        comprobar("sin pares: sin precio y muerte, como siempre",
                  px is None and muerto is True,
                  f"(px={px}, muerto={muerto}, liq={liq})")
        comprobar("sin pares: no queda marca de fallo de red",
                  st.ultimo_fallo_precio() is None,
                  f"fallo = {st.ultimo_fallo_precio()!r}")

        # (e) No se pudo preguntar: NO es muerte, y deja marca.
        def _revienta(*a, **k):
            raise st.requests.RequestException("timeout")
        st.requests.get = _revienta
        px, mc, muerto, liq = st._price_mc_ex("MINTE")
        comprobar("fallo de red: NO se declara muerte", muerto is False,
                  f"muerto = {muerto}")
        comprobar("fallo de red: queda la marca para el llamador",
                  st.ultimo_fallo_precio() is not None)

        # (f) El LOTE tiene que decir lo mismo que la ruta individual
        #     sobre el MISMO token. Antes decían cosas opuestas.
        def _resp_lote(pares):
            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return pares

                @staticmethod
                def raise_for_status():
                    return None
            return _R()

        bueno = dict(par_bueno_sin_liq)
        bueno["baseToken"] = {"address": "MINTF", "symbol": "XX"}
        muerto_p = dict(par_muerto)
        muerto_p["baseToken"] = {"address": "MINTF", "symbol": "XX"}
        st.requests.get = lambda *a, **k: _resp_lote(
            {"pairs": [bueno, muerto_p]})
        lote = st._prices_mc_lote(["MINTF"])
        comprobar("lote: el mint sin liquidez legible queda SIN conclusión "
                  "(lo repesca track_outcomes)", "MINTF" not in lote,
                  f"lote = {lote!r}")

        vivo = dict(par_vivo)
        vivo["baseToken"] = {"address": "MINTG", "symbol": "XX"}
        muerto_g = dict(par_muerto)
        muerto_g["baseToken"] = {"address": "MINTG", "symbol": "XX"}
        st.requests.get = lambda *a, **k: _resp_lote(
            {"pairs": [muerto_g, vivo]})
        lote = st._prices_mc_lote(["MINTG"])
        comprobar("lote: con un pool vivo y otro muerto gana el vivo",
                  lote.get("MINTG", (None, None, None, None))[2] is False,
                  f"lote = {lote!r}")

        # (g) El lote tampoco puede depender del orden de los pares en el
        #     empate exacto sobre el umbral de muerte.
        umbral_p = {"chainId": "solana", "priceUsd": "1.0", "fdv": 1,
                    "marketCap": 1,
                    "liquidity": {"usd": st.LIQ_MUERTO_USD},
                    "baseToken": {"address": "MINTH", "symbol": "XX"}}
        sin_p = {"chainId": "solana", "priceUsd": "2.0", "fdv": 1,
                 "marketCap": 1,
                 "baseToken": {"address": "MINTH", "symbol": "XX"}}
        st.requests.get = lambda *a, **k: _resp_lote({"pairs": [umbral_p,
                                                                sin_p]})
        l1 = st._prices_mc_lote(["MINTH"])
        st.requests.get = lambda *a, **k: _resp_lote({"pairs": [sin_p,
                                                                umbral_p]})
        l2 = st._prices_mc_lote(["MINTH"])
        comprobar("lote: el empate en el umbral NO depende del orden",
                  l1 == l2, f"{l1} vs {l2}")
    finally:
        st.requests.get = get_real


# ─────────────────────────────────────────────────────────────────────
# GRAVE 4 · realtime._lock_mint: exclusión mutua de verdad, con purga.
# ─────────────────────────────────────────────────────────────────────
def prueba_grave4():
    bloque("GRAVE 4 · el candado por token excluye de verdad")
    import time as _t
    import realtime as rt

    # (a) Mientras alguien lo tiene pedido, el mint comparte candado.
    rt._MINT_LOCKS.clear()
    a = rt._lock_mint("HOT")
    b = rt._lock_mint("HOT")
    comprobar("mismo mint → mismo candado por debajo",
              a._entrada[0] is b._entrada[0])
    comprobar("las reservas se cuentan", a._entrada[1] == 2,
              f"usos = {a._entrada[1]}")
    with a:
        pass
    comprobar("una salida no borra la entrada si queda otro usuario",
              "HOT" in rt._MINT_LOCKS, "la entrada desapareció demasiado pronto")
    with b:
        pass
    comprobar("cuando no queda nadie, la entrada desaparece",
              "HOT" not in rt._MINT_LOCKS,
              f"quedan {len(rt._MINT_LOCKS)} entradas")

    # (b) Estrés: varios hilos sobre el mismo mint mientras otros crean y
    #     destruyen miles de mints distintos. Si dos entran a la vez, falla.
    rt._MINT_LOCKS.clear()
    violaciones = {"n": 0}
    dentro = {"n": 0}
    vueltas = {"n": 0}
    guarda = threading.Lock()
    parar = threading.Event()

    def trabajador_hot():
        while not parar.is_set():
            with rt._lock_mint("HOT"):
                with guarda:
                    dentro["n"] += 1
                    vueltas["n"] += 1
                    if dentro["n"] > 1:
                        violaciones["n"] += 1
                _t.sleep(0.0002)
                with guarda:
                    dentro["n"] -= 1

    def trabajador_ruido(base):
        i = 0
        while not parar.is_set():
            i += 1
            with rt._lock_mint(f"{base}-{i}"):
                pass

    # Condiciones que SI destapan el fallo de las versiones 1ª-3ª: el
    # diccionario precargado por encima del umbral (para que la purga
    # entre en cada llamada), la sección crítica lo bastante larga para
    # que dos hilos se solapen, y el intervalo de cambio de hilo al mínimo
    # para abrir la ventana entre ENTREGAR el candado y TOMARLO. Con el
    # código anterior esto da violaciones; con éste, ninguna.
    _switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    for _i in range(4200):
        rt._MINT_LOCKS[f"P{_i}"] = [threading.Lock(), 1]
    hilos = [threading.Thread(target=trabajador_hot, daemon=True)
             for _ in range(8)]
    hilos += [threading.Thread(target=trabajador_ruido, args=(f"R{n}",),
                               daemon=True) for n in range(8)]
    for h in hilos:
        h.start()
    # Duración del estrés. 20 s por defecto; se puede bajar con
    # PRUEBAS_ESTRES_S para las pasadas de mutación, que corren la suite
    # entera una vez por mutante.
    try:
        _dur = float(os.getenv("PRUEBAS_ESTRES_S", "20"))
    except (TypeError, ValueError):
        _dur = 20.0
    _t.sleep(_dur)
    parar.set()
    for h in hilos:
        h.join(timeout=5)
    sys.setswitchinterval(_switch)
    for _i in range(4200):
        rt._MINT_LOCKS.pop(f"P{_i}", None)

    comprobar("8 hilos sobre el mismo mint + miles de mints de ruido: "
              "0 violaciones de exclusión mutua", violaciones["n"] == 0,
              f"{violaciones['n']} violaciones en {vueltas['n']} vueltas")
    # Umbral bajo a propósito: sirve para detectar que el estrés NO corrió
    # (0 o unas pocas vueltas), no para medir el rendimiento de la máquina
    # — con el contenedor ocupado se han visto pasadas legítimas de 495
    # vueltas donde otras hacen 150.000.
    comprobar("la prueba de verdad ejerció el candado",
              vueltas["n"] > 50 * max(1, int(_dur)),
              f"solo {vueltas['n']} vueltas en {_dur}s")
    comprobar("el diccionario NO acumula: al terminar queda vacío",
              len(rt._MINT_LOCKS) == 0,
              f"{len(rt._MINT_LOCKS)} entradas vivas")

    # (c) Reutilizar el mismo objeto tiene que SALTAR, no colarse: un
    #     segundo `with` haría un `release()` de más, dejaría la cuenta en
    #     −1 y borraría la entrada mientras otro hilo la usa — la rotura
    #     de exclusión mutua que llevan cuatro versiones persiguiendo.
    rt._MINT_LOCKS.clear()
    c = rt._lock_mint("REUSO")
    with c:
        pass
    salto = False
    try:
        with c:
            pass
    except RuntimeError:
        salto = True
    comprobar("reutilizar el mismo candado salta con RuntimeError", salto,
              "se dejó reutilizar en silencio")
    comprobar("y no deja la cuenta descuadrada",
              len(rt._MINT_LOCKS) == 0,
              f"{len(rt._MINT_LOCKS)} entradas vivas: {rt._MINT_LOCKS!r}")

    # (d) Un `__exit__` tardío no puede borrar la entrada NUEVA de otro.
    #     NOTA: con el `with` usado como toca ese estado no es alcanzable
    #     —la reserva se cuenta al pedir el candado— así que esto
    #     comprueba la REGLA, no un camino real del bot. La guarda `is`
    #     de `__exit__` se queda como cinturón de seguridad.
    rt._MINT_LOCKS.clear()
    viejo = rt._lock_mint("TARDIO")
    entrada_vieja = rt._MINT_LOCKS["TARDIO"]
    with viejo:
        pass                          # la entrada vieja se borra aquí
    nuevo = rt._lock_mint("TARDIO")
    entrada_nueva = rt._MINT_LOCKS["TARDIO"]
    comprobar("tras liberarse, el mint recibe una entrada NUEVA",
              entrada_nueva is not entrada_vieja)
    # Se simula el `__exit__` tardío del viejo sobre la entrada muerta.
    entrada_vieja[1] = 0
    with rt._MINT_LOCKS_GUARD:
        if (entrada_vieja[1] <= 0
                and rt._MINT_LOCKS.get("TARDIO") is entrada_vieja):
            rt._MINT_LOCKS.pop("TARDIO", None)
    comprobar("un __exit__ tardío NO borra la entrada nueva de otro hilo",
              rt._MINT_LOCKS.get("TARDIO") is entrada_nueva,
              "la entrada nueva desapareció")
    with nuevo:
        pass
    rt._MINT_LOCKS.clear()


# ─────────────────────────────────────────────────────────────────────
# CHOQUE 1 · el suelo de liquidez del camino caliente vuelve a aplicarse.
# ─────────────────────────────────────────────────────────────────────
def prueba_choque1():
    bloque("CHOQUE 1 · la liquidez desconocida deja de pasar en silencio")
    import io
    import contextlib
    import paper_trading as pt
    from db import get_conn, set_setting

    conn = get_conn()
    try:
        def intentar(liq, etiqueta):
            trade = {"wallet": "W" * 32, "mint": f"M-{etiqueta}",
                     "side": "compra", "ts": 1_700_000_000,
                     "signature": f"sig-{etiqueta}", "sol_amount": 1.0}
            token = {"price": 0.001, "mc": 100000, "liq": liq,
                     "symbol": "TT"}
            antes = conn.execute(
                "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                try:
                    pt.open_trade(conn, trade, token, 80)
                except Exception as e:      # pragma: no cover
                    return None, f"reventó: {e}"
            despues = conn.execute(
                "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
            return despues > antes, salida.getvalue()

        abrio, log = intentar(500, "polvo")
        comprobar("liquidez de polvo (500): NO abre", abrio is False,
                  f"abrió={abrio}")

        abrio, log = intentar(50000, "sana")
        comprobar("liquidez sana (50.000): abre", abrio is True,
                  f"abrió={abrio}")

        abrio, log = intentar(None, "desconocida")
        comprobar("liquidez desconocida: abre (no se pierde la copia)",
                  abrio is True, f"abrió={abrio}")
        comprobar("liquidez desconocida: queda dicho en el log, no pasa "
                  "en silencio", "NO comprobada" in log,
                  f"log = {log!r}")

        # Y con el interruptor del dueño en 0, se rechaza.
        set_setting(conn, "paper_liq_desconocida", "0")
        abrio, log = intentar(None, "desconocida-off")
        comprobar("con `paper_liq_desconocida = 0`: NO abre",
                  abrio is False, f"abrió={abrio}")
        set_setting(conn, "paper_liq_desconocida", "1")
        abrio, log = intentar(None, "desconocida-on")
        comprobar("con el interruptor de vuelta en 1: abre otra vez",
                  abrio is True, f"abrió={abrio}")
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# CHOQUE 2 · los paneles miden desde la marca, igual que el motor.
# ─────────────────────────────────────────────────────────────────────
def prueba_choque2():
    bloque("CHOQUE 2 · /metricas mide desde la misma marca que el motor")
    import time as _t
    import predictions as pr
    import influence as infl
    from db import get_conn, set_setting

    conn = get_conn()
    graph_real = infl.graph
    try:
        infl.graph = lambda construir=True: {"wallets": {}, "edges": {}}
        pr.graph = infl.graph

        conn.execute("DELETE FROM predictions")
        ahora = int(_t.time())
        marca = ahora - 3600
        set_setting(conn, "pred_medicion_desde", str(marca))

        lider = "L" * 32
        # 20 predicciones VIEJAS con acierto 0 (las envenenadas).
        for i in range(20):
            conn.execute(
                """INSERT INTO predictions
                   (leader, mint, created_ts, stage, confidence, meta_score,
                    predicted, arrived, alerted_stage, status, outcome_pct,
                    tier, evaluated_ts)
                   VALUES (?,?,?,2,40,40,'[]','[]',1,'evaluada',0,
                           'watchlist',?)""",
                (lider, f"VIEJA{i}", marca - 86400, ahora))
        # 6 NUEVAS con acierto 80.
        for i in range(6):
            conn.execute(
                """INSERT INTO predictions
                   (leader, mint, created_ts, stage, confidence, meta_score,
                    predicted, arrived, alerted_stage, status, outcome_pct,
                    tier, evaluated_ts)
                   VALUES (?,?,?,2,95,95,'[]','[]',1,'evaluada',80,
                           'alpha',?)""",
                (lider, f"NUEVA{i}", marca + 60, ahora))
        conn.commit()

        salud = pr._leader_health(conn, lider)
        panel = pr.metrics_text()

        comprobar("el MOTOR mide solo las nuevas",
                  salud["n"] == 6 and salud["accuracy"] == 80,
                  f"salud = {salud}")
        comprobar("el PANEL mide lo mismo que el motor",
                  "*80%* (6 evaluadas)" in panel,
                  f"panel dice: {[l for l in panel.splitlines() if 'Precisión' in l]}")
        comprobar("el panel dice cuántas quedan fuera y por qué",
                  "20 predicciones anteriores quedan fuera" in panel,
                  f"panel: {[l for l in panel.splitlines() if 'quedan fuera' in l]}")
        comprobar("el VOLUMEN sigue contando la tabla entera",
                  "26 total" in panel,
                  f"panel: {[l for l in panel.splitlines() if 'total' in l]}")
        comprobar("la calibración usa los máximos NUEVOS (95), no los "
                  "viejos", "confianza *95*" in panel,
                  f"panel: {[l for l in panel.splitlines() if 'Calibración' in l]}")
        comprobar("consultar el panel NO fija la marca por su cuenta",
                  pr.marca_medicion(conn) == marca,
                  f"marca = {pr.marca_medicion(conn)}, esperada {marca}")

        # Sin marca puesta, los paneles miden como siempre (todo).
        conn.execute("DELETE FROM settings WHERE key='pred_medicion_desde'")
        conn.commit()
        panel2 = pr.metrics_text()
        comprobar("sin marca: el panel mide TODO, como antes",
                  "(26 evaluadas)" in panel2,
                  f"panel: {[l for l in panel2.splitlines() if 'Precisión' in l]}")
        comprobar("sin marca: el panel no fija la marca al consultarlo",
                  pr.marca_medicion(conn) == 0)

        # /predicciones tiene el mismo corte que /metricas: si no, los dos
        # paneles enseñan números distintos del mismo motor.
        set_setting(conn, "pred_medicion_desde", str(marca))
        listado = pr.predictions_text(limit=3)
        comprobar("/predicciones también mide desde la marca",
                  "80% sobre 6 predicciones evaluadas" in listado,
                  f"dice: {[l for l in listado.splitlines() if 'evaluad' in l]}")
    finally:
        infl.graph = graph_real
        pr.graph = graph_real
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# CHOQUE 5 · /nota cuenta la población que de verdad pierde la estrella.
# ─────────────────────────────────────────────────────────────────────
def prueba_choque5():
    bloque("CHOQUE 5 · /nota cuenta la población correcta")
    from db import get_conn

    conn = get_conn()
    try:
        conn.execute("DELETE FROM wallets")
        filas = [("E1", "Elite"), ("E2", "Elite"), ("E3", "Elite"),
                 ("S1", "Seguimiento"),
                 ("O1", "Observación"), ("O2", "Observación"),
                 ("D1", "Descartada"),
                 ("N1", None), ("N2", None), ("N3", None)]
        for addr, grade in filas:
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, grade) "
                "VALUES (?, 1, ?)", (addr, grade))
        conn.commit()

        # La consulta que USA el comando, tal cual está en el código.
        import telegram_bot as tb
        n = tb._nota_conteos(conn)

        # La población que de verdad pierde la ⭐ (ai_analyst).
        real = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked = 1 "
            "AND grade IS NOT NULL "
            "AND grade NOT IN ('Elite', 'Seguimiento')").fetchone()["c"]

        comprobar("/nota: las que pierden la ⭐ coinciden con ai_analyst",
                  n["flojas"] == real,
                  f"/nota dice {n['flojas']}, la realidad es {real}")
        comprobar("/nota: las sin nota se cuentan aparte",
                  n["sin_nota"] == 3, f"sin_nota = {n['sin_nota']}")
        comprobar("/nota: las de nota buena son 4",
                  n["buenas"] == 4, f"buenas = {n['buenas']}")
        comprobar("/nota: buenas + flojas + sin_nota = total",
                  n["buenas"] + n["flojas"] + n["sin_nota"] == n["total"],
                  f"{n['buenas']}+{n['flojas']}+{n['sin_nota']} != {n['total']}")
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# MENOR · _recortar_tg no deja el Markdown a medias.
# ─────────────────────────────────────────────────────────────────────
def prueba_recorte():
    bloque("MENOR · el recorte no deja Markdown a medias")
    import telegram_bot as tb

    # VA LO PRIMERO: el bucle NO puede quedarse quieto. Con una rama mal puesta, `i`
    # volvía a 0 y la función se colgaba: se vio al romperla a propósito,
    # y un cuelgue en el hilo de Telegram deja el bot mudo.
    import signal as _sig

    def _corta(*_a):
        raise TimeoutError("_cerrar_markdown no terminó")

    duros = ["[a](b", "[](", "[]", "```", "``", "[a]", "\\", "[a](",
             "`[a](b`", "*[a](b*", "[" * 200, "](" * 200,
             "```" * 60, "\\[" * 100, "[a](b" * 100]
    viejo = _sig.signal(_sig.SIGALRM, _corta)
    colgados = []
    try:
        for caso in duros:
            _sig.alarm(5)
            try:
                tb._cerrar_markdown(caso)
            except (TimeoutError, RecursionError) as e:
                colgados.append((caso[:20], type(e).__name__))
            finally:
                _sig.alarm(0)
    finally:
        _sig.signal(_sig.SIGALRM, viejo)
    comprobar("ninguna entrada difícil cuelga ni desborda la pila",
              not colgados, f"{colgados}")

    base = ("• Compró *TOKEN-{i}* por 1,2 SOL entre los tempranos\n\n")
    texto = "".join(base.format(i=i) for i in range(1, 400))
    rotos = 0
    for tope in range(200, 4200, 67):
        cortado = tb._recortar_tg(texto, tope)
        cuerpo = cortado.split("_(mensaje recortado")[0]
        for marca in ("*", "_", "`"):
            if cuerpo.count(marca) % 2:
                rotos += 1
                break
    comprobar("ninguna longitud deja un marcador sin cerrar", rotos == 0,
              f"{rotos} longitudes con Markdown roto")
    largo = "x" * 9000
    comprobar("el recorte respeta el tope de Telegram",
              len(tb._recortar_tg(largo, 4096)) <= 4096)

    # Y NO se lleva por delante el contenido. Un `/ia` real es un bloque
    # entero en cursiva (`_{razon}_`): con el marcador impar al principio,
    # cerrar "cortando por el último marcador" dejaba 167 caracteres de
    # 4.017. La prueba tiene que ver la diferencia entre CERRAR el
    # marcador y BORRAR el mensaje.
    cursiva = "🧠 *Veredicto IA*\n\n_" + ("razón medida. " * 500) + "_"
    entero = tb._recortar_tg(cursiva)
    comprobar("el recorte entrega casi todo lo que cabe, no una cabecera "
              "vacía", len(entero) > 3500,
              f"solo {len(entero)} caracteres de los ~4.000 que caben")
    comprobar("…y el MENSAJE ENTERO, con el aviso de recorte pegado "
              "detrás, lo acepta Telegram",
              _valido(entero) is None, f"{_valido(entero)} · {entero[-90:]!r}")

    simbolo = ("Billetera *ABC*: nota Elite\nPEPE_2 comprado\n"
               + "linea de relleno. " * 800)
    entero2 = tb._recortar_tg(simbolo)
    comprobar("un símbolo con `_` en la línea 2 no borra el mensaje",
              len(entero2) > 3500, f"solo {len(entero2)} caracteres")

    # Corpus con code spans y enlaces, que es donde contar marcadores
    # sueltos falla: dentro de un `code span` los `_` son literales, y un
    # `[texto](url)` la paridad no lo mira siquiera. Se valida leyendo el
    # texto de izquierda a derecha, como hace Telegram.
    corpus = [
        ("code span + cursiva (/metricas)",
         "📊 *Panel*\n\n" + "".join(
             f"   `PRED_ALPHA_META` y `bot_local.env` linea {i}\n"
             for i in range(400)) + "_Acierto medido desde hoy_"),
        ("enlaces (/radar)",
         "🔭 *Radar*\n\n" + "".join(
             f"• *TOK{i}* · [DexScreener](https://x.io/t/{i}) · ok\n"
             for i in range(400))),
        ("mezcla de todo",
         "*a* `b_c` _d_ [e](u) " * 900),
        ("corchete suelto ANTES de un enlace (el caso que rompía)",
         "".join(f"El pool [{i}] estaba *fino: mira "
                 f"[DexScreener](https://d.io/{i}) y el neto*\n"
                 for i in range(300))),
        ("bloque de código cercado, como el que devuelve la IA",
         "🧠 *Post-mortem*\n\n" + "".join(
             f"Decisión {i}: entrar.\n```\nliq=120 usd\n"
             f"slippage=42%\n```\nResultado _medido_: -12%.\n"
             for i in range(200))),
    ]
    # Se valida el MENSAJE ENTERO, con el aviso de recorte incluido: el
    # cuerpo puede ser válido por su cuenta y aun así estropear lo que se
    # le pegue detrás (un `[texto](url` sin cerrar se traga el aviso).
    rotos = []
    for nombre, texto in corpus:
        for tope in range(200, 4200, 137):
            entero3 = tb._recortar_tg(texto, tope)
            motivo = _valido(entero3)
            if motivo:
                rotos.append((nombre, tope, motivo, entero3[-70:]))
    comprobar("code spans, cursivas y enlaces: ningún corte deja el "
              "Markdown mal formado", not rotos,
              f"{len(rotos)} cortes rotos, p.ej. {rotos[:2]}")

    # Fuzz: cadenas al azar con todos los caracteres que Telegram lee como
    # formato. Dos condiciones, y la segunda es la que caza las
    # regresiones: (1) la salida tiene que ser válida; (2) si la ENTRADA
    # ya era válida, la salida no puede dejar de serlo.
    import random
    rng = random.Random(20260826)
    alfabeto = "abc *_`[]()\\\n"
    # EL CONTRATO, escrito tal cual. `_cerrar_markdown` no tiene que
    # devolver algo válido a secas: tiene que devolver un cuerpo al que se
    # le pueda PEGAR TEXTO DETRÁS sin corromperlo, porque `_recortar_tg`
    # siempre le añade el aviso de recorte. Un `[texto](url` sin cerrar es
    # válido suelto y aun así se traga lo que venga después.
    AVISO = "…\n\n_(mensaje recortado: 12345 caracteres)_"
    malas = tocadas = 0
    ej_mala = ej_tocada = None
    for _ in range(30000):
        largo = rng.randint(1, 40)
        t = "".join(rng.choice(alfabeto) for _ in range(largo))
        salida = tb._cerrar_markdown(t)
        motivo = _valido(salida + AVISO)
        if motivo:
            malas += 1
            ej_mala = ej_mala or (t, salida, motivo)
        # Y no puede tocar de más: si al texto tal cual YA se le podía
        # pegar el aviso, tiene que devolverlo idéntico.
        if _valido(t + AVISO) is None and salida != t:
            tocadas += 1
            ej_tocada = ej_tocada or (t, salida)
    comprobar("fuzz de 30.000 cadenas: al cuerpo cerrado siempre se le "
              "puede pegar el aviso de recorte", malas == 0,
              f"{malas} rechazados por Telegram, p.ej. {ej_mala}")
    comprobar("fuzz: lo que ya estaba bien se devuelve INTACTO",
              tocadas == 0,
              f"{tocadas} cuerpos buenos modificados, p.ej. {ej_tocada}")

    # Fuzz de EXTREMO A EXTREMO: el mensaje tal y como sale de
    # `_recortar_tg`, con el aviso pegado. Es el contrato que de verdad
    # importa, y es el que se le escapó a la vuelta anterior.
    malas_e2e = recortados = 0
    ej_e2e = None
    for _ in range(8000):
        largo = rng.randint(60, 300)
        t = "".join(rng.choice(alfabeto) for _ in range(largo))
        salida = tb._recortar_tg(t, rng.randint(40, 120))
        if salida == t:
            continue          # no hubo recorte: se devuelve tal cual, y
        recortados += 1       # lo que entra mal, sale mal (correcto)
        motivo = _valido(salida)
        if motivo:
            malas_e2e += 1
            ej_e2e = ej_e2e or (t, salida, motivo)
    comprobar("fuzz de extremo a extremo: ningún mensaje RECORTADO sale "
              "roto", malas_e2e == 0,
              f"{malas_e2e} de {recortados} rechazados, p.ej. {ej_e2e}")
    comprobar("el fuzz de extremo a extremo recortó de verdad",
              recortados > 5000, f"solo {recortados} recortes")
    corto = "hola *mundo*"
    comprobar("un texto corto no se toca",
              tb._recortar_tg(corto, 4096) == corto)


# ─────────────────────────────────────────────────────────────────────
# MENORES · símbolos con Markdown, historial cortado y el techo del paper.
# ─────────────────────────────────────────────────────────────────────
def prueba_menores():
    bloque("MENORES")
    import io
    import contextlib
    import paper_trading as pt
    from db import get_conn, set_setting

    # (a) El símbolo del token se limpia antes de ir a Markdown.
    comprobar("el símbolo con `*` se limpia", pt._md("B*B") == "BB",
              f"salió {pt._md('B*B')!r}")
    comprobar("los corchetes se convierten, no se pierden",
              pt._md("A[B]") == "A(B)", f"salió {pt._md('A[B]')!r}")
    comprobar("un símbolo normal no se toca", pt._md("BONK") == "BONK")
    comprobar("None no revienta", pt._md(None) == "")

    conn = get_conn()
    try:
        conn.execute("DELETE FROM paper_trades")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                status)
               VALUES ('MX','B*B','W',1.0,1,1.0,'abierta')""")
        conn.commit()
        txt = pt.resumen_text()
        comprobar("/paper no saca el `*` crudo del símbolo",
                  "*B*B*" not in txt,
                  f"salió: {[l for l in txt.splitlines() if 'B' in l][:2]}")

        # Y el mensaje de APERTURA tampoco: es el que más se envía.
        enviados = []
        real_tg = pt._tg
        real_sol = pt._sol_a_usd
        real_sym = pt._symbol_rapido
        try:
            pt._tg = lambda t: enviados.append(t)
            pt._sol_a_usd = lambda *a, **k: None
            pt._symbol_rapido = lambda *a, **k: None
            with contextlib.redirect_stdout(io.StringIO()):
                pt.open_trade(
                    conn,
                    {"wallet": "W" * 32, "mint": "MY", "side": "compra",
                     "ts": 1, "signature": "sig-md", "sol_amount": 1.0},
                    {"price": 0.001, "mc": 100000, "liq": 50000,
                     "symbol": "C*C"}, 80)
        finally:
            pt._tg = real_tg
            pt._sol_a_usd = real_sol
            pt._symbol_rapido = real_sym
        comprobar("el aviso de apertura tampoco saca el `*` crudo",
                  enviados and "*C*C*" not in enviados[0],
                  f"salió: {enviados[0][:120] if enviados else 'nada'}")

        # (b) El techo de sondeo acompaña al tope configurable. Se lee la
        #     función DE PRODUCCIÓN, no se reproduce la fórmula aquí (la
        #     primera versión la copiaba y por eso no comprobaba nada).
        for valor in ("10", "400", "999999"):
            set_setting(conn, "paper_max_abiertas", valor)
            tope = pt._tope_abiertas(conn)
            techo = pt._limite_sondeo(conn)
            comprobar(f"tope {valor} → el techo de sondeo lo cubre "
                      f"(tope={tope}, techo={techo})", techo >= tope,
                      "quedarían posiciones VIVAS sin gestionar, y siempre "
                      "las mismas")
        set_setting(conn, "paper_max_abiertas", "999999")
        comprobar("el tope sigue acotado a 500 con basura dentro",
                  pt._tope_abiertas(conn) == 500,
                  f"tope = {pt._tope_abiertas(conn)}")
        set_setting(conn, "paper_max_abiertas", "10")
    finally:
        conn.close()

    # Y los dos mensajes de "no cierro todavía": IA-hold y hold-extra.
    import inspect
    fuente = inspect.getsource(pt)
    for etiqueta, marca in (
            ("IA local", '*IA local* en {_md(row[\'symbol\'])}'),
            ("Hold extra", '*Hold extra* en {_md(row[\'symbol\'])}')):
        comprobar(f"el aviso de «{etiqueta}» sanea el símbolo",
                  marca in fuente,
                  "el símbolo va crudo a Markdown en ese mensaje")
    comprobar("la razón que escribe la IA también se sanea",
              "_{_md(d.get('razon', ''))}_" in fuente,
              "la razón de la IA va cruda a Markdown")
    comprobar("el aviso de «la IA habría rechazado» también sanea",
              "compra en {_md(_sym)}: " in fuente
              and "_{_md(v.get('razon', ''))}_" in fuente,
              "el símbolo o la razón van crudos en ese mensaje")

    # /paper tiene que salir por `_send_md` (recorte + reintento en texto
    # plano); con `reply_text` a pelo, un símbolo raro dejaba el comando
    # MUDO.
    import telegram_bot as tb2
    # `cmd_paper` va decorado, así que `getsource` de la función devuelve
    # el envoltorio: se mira el módulo entero y la línea exacta.
    fuente_tb = inspect.getsource(tb2)
    comprobar("/paper sale por _send_md, no por reply_text",
              "await _send_md(update.message.chat, txt, "
              "reply_markup=kb_paper())" in fuente_tb,
              "vuelve a responder sin recorte ni reintento")

    # (c) La herramienta de la IA distingue "no pude bajarlo" de "no tiene".
    import ai_agent
    import wallet_profiler as wp
    real = wp.profile_wallet
    try:
        wp.profile_wallet = lambda a, **k: {"tx_sampled": 0,
                                            "historial_entero": False}
        roto = ai_agent._exec_read("perfil_billetera",
                                   {"address": "X" * 32})
        wp.profile_wallet = lambda a, **k: {"tx_sampled": 0,
                                            "historial_entero": True}
        vacio = ai_agent._exec_read("perfil_billetera",
                                    {"address": "X" * 32})
    finally:
        wp.profile_wallet = real
    comprobar("Helius cortado: la IA NO oye 'sin transacciones'",
              "no tenga actividad" in roto, f"dijo: {roto!r}")
    comprobar("historial entero y vacío: sigue diciendo lo de siempre",
              "Sin transacciones recuperadas" in vacio, f"dijo: {vacio!r}")


# ─────────────────────────────────────────────────────────────────────
# CHOQUES 3 y 4 · el fallo de red no castiga a la posición, y la pasada
#                 cierra la conexión pase lo que pase.
# ─────────────────────────────────────────────────────────────────────
def prueba_choques34():
    bloque("CHOQUES 3 y 4 · fallo de red vs fallo del dato, y try/finally")
    import io
    import contextlib
    import paper_trading as pt
    import signal_tracker as st
    from db import get_conn

    # (a) El CONTRATO del prefijo. `paper_trading` decide a partir de él,
    #     así que si `signal_tracker` deja de ponerlo, esto tiene que
    #     saltar.
    def _revienta_red(*a, **k):
        raise st.requests.RequestException("429 Too Many Requests")

    def _revienta_dato(*a, **k):
        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                raise ValueError("respuesta ilegible")
        return _R()

    def _revienta_json(*a, **k):
        """El caso REAL: `requests` lanza su propio JSONDecodeError, que
        hereda de RequestException ANTES que de ValueError."""
        class _R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                raise st.requests.exceptions.JSONDecodeError(
                    "Expecting value", "", 0)
        return _R()

    get_real = st.requests.get
    try:
        st.requests.get = _revienta_red
        st._price_mc_ex("MZ1")
        motivo_red = st.ultimo_fallo_precio()
        st.requests.get = _revienta_dato
        st._price_mc_ex("MZ2")
        motivo_dato = st.ultimo_fallo_precio()
    finally:
        st.requests.get = get_real

    comprobar("un fallo de transporte se marca como `red:`",
              str(motivo_red or "").startswith("red: "),
              f"motivo = {motivo_red!r}")
    comprobar("una respuesta ilegible se marca como `dato:`",
              str(motivo_dato or "").startswith("dato: "),
              f"motivo = {motivo_dato!r}")

    get_real2 = st.requests.get
    try:
        st.requests.get = _revienta_json
        st._price_mc_ex("MZ3")
        motivo_json = st.ultimo_fallo_precio()
    finally:
        st.requests.get = get_real2
    comprobar("el JSONDecodeError de requests también es `dato:` (hereda "
              "de RequestException, así que caía en la rama de red)",
              str(motivo_json or "").startswith("dato: "),
              f"motivo = {motivo_json!r}")

    # (b) Un 429 NO marca la posición; una respuesta ilegible SÍ.
    def _pasada(fallo_de):
        conn = get_conn()
        try:
            conn.execute("DELETE FROM paper_trades")
            for i, mint in enumerate(("BUENO", "MALO")):
                conn.execute(
                    """INSERT INTO paper_trades
                       (mint, symbol, wallet, entry_price, entry_ts,
                        stake_sol, status)
                       VALUES (?,?,'W',1.0,1,1.0,'abierta')""",
                    (mint, mint))
            conn.commit()
        finally:
            conn.close()

        def _falso_pmx(mint):
            if mint == "BUENO":
                st._set_fallo_precio(None)
                return (1.0, 1000.0, False, 50000.0)
            st._set_fallo_precio(f"{fallo_de}: simulado")
            return (None, None, False, 0.0)

        real_pmx = st._price_mc_ex
        try:
            st._price_mc_ex = _falso_pmx
            with contextlib.redirect_stdout(io.StringIO()):
                pt.update_open_trades()
        finally:
            st._price_mc_ex = real_pmx

        conn = get_conn()
        try:
            fila = conn.execute(
                "SELECT sin_dato_desde FROM paper_trades WHERE mint='MALO'"
            ).fetchone()
            return fila["sin_dato_desde"] if fila else "no existe"
        finally:
            conn.close()

    comprobar("un 429 NO marca la posición (si no, se hunde al final de "
              "la cola y no se vuelve a sondear)",
              _pasada("red") is None, f"marca = {_pasada('red')!r}")
    comprobar("una respuesta ilegible SÍ la marca (se repetirá igual)",
              _pasada("dato") is not None,
              f"marca = {_pasada('dato')!r}")

    # (c) try/finally: si la pasada revienta, la conexión se cierra igual.
    abiertas = []
    real_getconn = pt.get_conn

    def _get_conn_espia():
        c = real_getconn()
        abiertas.append(c)
        return c

    def _pmx_revienta(mint):
        raise KeyboardInterrupt("corte a mitad de pasada")

    conn = real_getconn()
    try:
        conn.execute("DELETE FROM paper_trades")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                status)
               VALUES ('MX','MX','W',1.0,1,1.0,'abierta')""")
        conn.commit()
    finally:
        conn.close()

    real_pmx = st._price_mc_ex
    try:
        pt.get_conn = _get_conn_espia
        st._price_mc_ex = _pmx_revienta
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pt.update_open_trades()
        except KeyboardInterrupt:
            pass
    finally:
        pt.get_conn = real_getconn
        st._price_mc_ex = real_pmx

    def _cerrada(c):
        try:
            c.execute("SELECT 1")
            return False
        except Exception:
            return True

    comprobar("la pasada abrió su conexión", len(abiertas) >= 1,
              f"conexiones = {len(abiertas)}")
    comprobar("una excepción a mitad de pasada NO deja la conexión "
              "abierta (era la fuga de la Ola 17-K)",
              all(_cerrada(c) for c in abiertas),
              "quedó alguna conexión viva")


# ─────────────────────────────────────────────────────────────────────
# MENOR · el número de seguidores ocultos de la alerta de confirmación.
# ─────────────────────────────────────────────────────────────────────
def prueba_ocultos():
    bloque("MENOR · seguidores ocultos en la confirmación")
    import json as _json
    import predictions as pr
    import influence as infl
    from db import get_conn

    conn = get_conn()
    contextos = []
    alert_real = pr._alert_stage
    eval_real = pr.evaluate_due
    infl_real = infl.influence
    cache_real = infl.cache_lista
    try:
        pr._alert_stage = lambda *a, **k: contextos.append(a[6])
        pr.evaluate_due = lambda *a, **k: None

        lider = "L" * 32
        vistos = ["V%02d" % i + "x" * 28 for i in range(2)]
        ocultos = ["O%02d" % i + "x" * 28 for i in range(3)]
        flojos = ["F%02d" % i + "x" * 28 for i in range(4)]
        pred = [{"wallet": w, "prob": 100, "shared": 12} for w in vistos]
        grafo = {"leader_score": 100,
                 "followers": ([{"wallet": w, "prob": 100, "shared": 12}
                                for w in vistos + ocultos]
                               + [{"wallet": w, "prob": 30, "shared": 3}
                                  for w in flojos])}
        infl.influence = lambda w, construir=True: dict(grafo)
        infl.cache_lista = lambda: True
        pr.influence = infl.influence
        pr.cache_lista = infl.cache_lista

        conn.execute("DELETE FROM predictions")
        conn.execute(
            """INSERT INTO predictions
               (leader, mint, created_ts, stage, confidence, meta_score,
                predicted, arrived, alerted_stage, status, tier, price0)
               VALUES (?,?,?,1,95,95,?,'[]',0,'abierta','alpha',0.001)""",
            (lider, "MOC", 1_700_000_000, _json.dumps(pred)))
        conn.commit()
        pr.on_buy(conn, vistos[0], "MOC", 1_700_000_060,
                  {"liq": 50000, "symbol": "TT", "price": 0.001},
                  vigiladas=set(vistos) | {lider}, es_estrella=False)

        comprobar("se envió la alerta de confirmación", len(contextos) == 1,
                  f"contextos = {len(contextos)}")
        if contextos:
            n = contextos[0].get("_seguidores_ocultos")
            comprobar("los «ocultos» son los que superan el umbral y NO se "
                      "ven (3), no todos los del grafo (7)", n == 3,
                      f"la alerta dice {n}")
    finally:
        pr._alert_stage = alert_real
        pr.evaluate_due = eval_real
        infl.influence = infl_real
        infl.cache_lista = cache_real
        pr.influence = infl_real
        pr.cache_lista = cache_real
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────
# OLA 18-I · en un cierre por RUG manda el neto, no el -99%.
# ─────────────────────────────────────────────────────────────────────
def prueba_rug():
    bloque("18-I · el cierre por rug enseña el dinero, no el precio")
    import io
    import contextlib
    import paper_trading as pt
    from db import get_conn

    conn = get_conn()
    enviados = []
    tg_real = pt._tg
    sol_real = pt._sol_a_usd
    try:
        pt._tg = lambda t: enviados.append(t)
        pt._sol_a_usd = lambda *a, **k: 200.0

        def cerrar(nombre, stake_usd, usd_salida, costos, frac):
            conn.execute("DELETE FROM paper_trades WHERE mint=?", (nombre,))
            conn.execute(
                """INSERT INTO paper_trades
                   (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                    stake_usd, status, fraccion_restante,
                    usd_salida_real, costos_usd)
                   VALUES (?,?,'W',1.0,1,0.5,?,'abierta',?,?,?)""",
                (nombre, nombre, stake_usd, frac, usd_salida, costos))
            conn.commit()
            row = conn.execute("SELECT * FROM paper_trades WHERE mint=?",
                               (nombre,)).fetchone()
            enviados.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                pt._close(conn, row, row["entry_price"] * 0.01,
                          "sin liquidez", "\U0001F480")
            fila = conn.execute("SELECT * FROM paper_trades WHERE mint=?",
                                (nombre,)).fetchone()
            return (enviados[0] if enviados else ""), fila

        # (a) Rug que acabó EN VERDE porque la ⭐ vendió antes (caso real
        #     Obesity: papel -97,5%, neto +61,94 $).
        txt, fila = cerrar("VERDE", 98.52, 160.56, 0.0984, 0.1)
        comprobar("rug en verde: el titular es el resultado REAL",
                  "Resultado real: *+$" in txt,
                  f"titular: {[l for l in txt.splitlines() if 'Resultado' in l or 'PnL' in l]}")
        comprobar("rug en verde: el icono del dinero es VERDE",
                  "\U0001F7E2 Resultado real" in txt,
                  f"salió: {[l for l in txt.splitlines() if 'Resultado' in l]}")
        comprobar("rug en verde: se dice cuánto se recuperó de la bolsa",
                  "recuperó" in txt, f"texto: {txt[:300]}")
        comprobar("rug en verde: el importe recuperado es el REAL, no otra "
                  "cifra cualquiera", "$160.56" in txt,
                  f"{[l for l in txt.splitlines() if 'recuper' in l]}")
        comprobar("rug en verde: el origen que se cita es el que hubo "
                  "(parciales previos), no una frase fija",
                  "(ventas parciales previas)" in txt,
                  f"{[l for l in txt.splitlines() if 'recuper' in l]}")
        comprobar("rug en verde: no se dice «de esa bolsa» cuando se "
                  "recuperó más que lo invertido",
                  "de esa bolsa" not in txt)
        comprobar("rug en verde: se explica POR QUÉ no fue pérdida total",
                  "no es una pérdida total" in txt,
                  f"{[l for l in txt.splitlines() if 'recuper' in l]}")
        comprobar("rug en verde: el % citado es el MISMO que la cabecera",
                  "El -99% de arriba" in txt,
                  f"{[l for l in txt.splitlines() if 'de arriba' in l]}")
        comprobar("rug en verde: la línea del histórico dice el PnL de "
                  "PAPEL, que es lo que se guarda",
                  "histórico: -$9.75" in txt,
                  f"{[l for l in txt.splitlines() if 'histórico' in l]}")
        comprobar("rug en verde: el −99% se explica como precio del token, "
                  "no como dinero", "no tu dinero" in txt)
        comprobar("rug en verde: el PnL de papel se sigue diciendo",
                  "PnL de papel" in txt)
        comprobar("rug en verde: lo GUARDADO no cambia (sigue el −99% en "
                  "pnl_pct, que es el precio)", round(fila["pnl_pct"]) == -99,
                  f"pnl_pct = {fila['pnl_pct']}")
        comprobar("rug en verde: el neto guardado es positivo",
                  fila["pnl_usd_neto"] > 0, f"neto = {fila['pnl_usd_neto']}")
        # Lo que de verdad hay que blindar: el cambio prometió NO tocar la
        # base. `pnl_usd` y `pnl_sol` son los que deciden el win rate de
        # `/paper` y el grading de billeteras. Escribir ahí el neto dejaba
        # las 119 pruebas verdes.
        comprobar("rug en verde: `pnl_usd` guardado sigue siendo el de "
                  "PAPEL, no el neto",
                  fila["pnl_usd"] < 0 and abs(fila["pnl_usd"] + 9.75) < 0.1,
                  f"pnl_usd = {fila['pnl_usd']} (el neto es "
                  f"{fila['pnl_usd_neto']})")
        comprobar("rug en verde: `pnl_sol` guardado no cambia de signo por "
                  "el neto", fila["pnl_sol"] < 0,
                  f"pnl_sol = {fila['pnl_sol']}")
        comprobar("rug en verde: el mensaje es Markdown válido para "
                  "Telegram", _valido(txt) is None,
                  f"{_valido(txt)} · {txt[-80:]!r}")
        comprobar("rug en verde: «invertidos» es el importe INVERTIDO, no "
                  "lo vendido", "$98.52 invertidos" in txt,
                  f"{[l for l in txt.splitlines() if 'invertidos' in l]}")
        comprobar("rug en verde: no se pierde el aviso de precio asumido",
                  "precio de salida asumido" in txt)
        comprobar("rug en verde: no se pierde el aviso de parciales",
                  "incluye ventas parciales previas" in txt)

        # (b) Rug con pérdida parcial.
        txt, fila = cerrar("PARCIAL", 94.93, 32.43, 0.0951, 0.3)
        comprobar("rug con pérdida: el titular es el resultado REAL y rojo",
                  "\U0001F534 Resultado real: *-$" in txt,
                  f"{[l for l in txt.splitlines() if 'Resultado' in l]}")
        comprobar("rug con pérdida: sigue diciendo lo que se recuperó",
                  "recuperó" in txt and "$32.43" in txt,
                  f"{[l for l in txt.splitlines() if 'recuper' in l]}")
        comprobar("rug con pérdida: Markdown válido", _valido(txt) is None,
                  f"{_valido(txt)}")

        # (c) Rug SIN ninguna venta: pérdida total y sin comisión de salida.
        txt, fila = cerrar("TOTAL", 96.34, 0.0, 0.0482, 1.0)
        comprobar("rug total: se dice que fue pérdida total",
                  "pérdida total" in txt, f"texto: {txt[:300]}")
        comprobar("rug total: se explica que NO se cobra comisión de salida",
                  "no se vende" in txt and "perder más" in txt,
                  f"texto: {txt[:400]}")
        comprobar("rug total: el neto es lo invertido más el fee de "
                  "ENTRADA, sin fee de venta",
                  abs(fila["pnl_usd_neto"] + 96.34 + 0.0482) < 0.01,
                  f"neto = {fila['pnl_usd_neto']}")
        comprobar("rug total: Markdown válido", _valido(txt) is None,
                  f"{_valido(txt)}")
        comprobar("rug total: `pnl_usd` guardado sigue siendo el de PAPEL",
                  abs(fila["pnl_usd"] + 95.38) < 0.1,
                  f"pnl_usd = {fila['pnl_usd']}")

        # (c2) Parciales SIN dólares de salida (Jupiter falló al abrir,
        #      `tokens_raw` NULL): el titular NO puede decir "no se vendió
        #      nada" sobre una operación con ventas apuntadas.
        conn.execute("DELETE FROM paper_trades WHERE mint='SINCOT'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante, pnl_realizado_usd,
                costos_usd)
               VALUES ('SINCOT','SINCOT','W',1.0,1,0.5,100.0,'abierta',
                       0.25,125.0,0.1)""")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='SINCOT'").fetchone()
        enviados.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, row, 0.01, "sin liquidez", "\U0001F480")
        txt = enviados[0] if enviados else ""
        fila = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='SINCOT'").fetchone()
        comprobar("parciales sin cotizar: NO se dice «no se vendió nada»",
                  "No se vendió nada" not in txt,
                  f"texto: {[l for l in txt.splitlines() if 'vendi' in l]}")
        comprobar("parciales sin cotizar: NO se titula «Resultado real» "
                  "con una cifra que contradice al PnL guardado",
                  not ("Resultado real" in txt and fila["pnl_usd"] > 0
                       and "-$" in txt.split("Resultado real")[-1][:20]),
                  f"pnl_usd guardado = {fila['pnl_usd']}, texto: "
                  f"{[l for l in txt.splitlines() if 'Resultado' in l]}")
        comprobar("parciales sin cotizar: el mensaje no se contradice con "
                  "la nota de parciales",
                  not ("No se vendió nada" in txt
                       and "ventas parciales previas" in txt))
        comprobar("parciales sin cotizar: Markdown válido",
                  _valido(txt) is None, f"{_valido(txt)}")

        # (c3) La guarda, término a término. En (c2) `frac < 1` y
        #      `realizado` son ciertos a la vez y cada uno tapa al otro.
        #      Aquí solo lo es `frac`: una venta parcial al MISMO precio de
        #      entrada deja un `pnl_realizado_usd` de 0 legítimo.
        for etiqueta, f_rest, real_usd, real_frac in (
                ("solo fraccion_restante", 0.5, 0.0, 0.0),
                ("solo pnl_realizado_frac", 1.0, 0.0, 0.5)):
            conn.execute("DELETE FROM paper_trades WHERE mint='TERM'")
            conn.execute(
                """INSERT INTO paper_trades
                   (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                    stake_usd, status, fraccion_restante,
                    pnl_realizado_usd, pnl_realizado_frac, costos_usd)
                   VALUES ('TERM','TERM','W',1.0,1,0.5,100.0,'abierta',
                           ?,?,?,0.1)""", (f_rest, real_usd, real_frac))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE mint='TERM'").fetchone()
            enviados.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                pt._close(conn, row, 0.01, "sin liquidez", "\U0001F480")
            txt = enviados[0] if enviados else ""
            comprobar(f"guarda · {etiqueta}: NO se dice «no se vendió nada»",
                      "No se vendió nada" not in txt,
                      f"{[l for l in txt.splitlines() if 'vendi' in l]}")

        # (c4) Rug SIN parciales pero con cotización de cierre: el texto
        #      NO puede afirmar ventas previas que no existieron.
        cot_real = None
        try:
            import ejecucion_simulada as es
            cot_real = es.cotizar_venta
            es.cotizar_venta = lambda m, t, su: {"usd_salida": 40.0}
            conn.execute("DELETE FROM paper_trades WHERE mint='SOLOCIERRE'")
            conn.execute(
                """INSERT INTO paper_trades
                   (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                    stake_usd, status, costos_usd, tokens_raw)
                   VALUES ('SOLOCIERRE','SC','W',1.0,1,0.5,100.0,'abierta',
                           0.05,1000)""")
            conn.commit()
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE mint='SOLOCIERRE'"
            ).fetchone()
            enviados.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                pt._close(conn, row, 0.01, "sin liquidez", "\U0001F480")
            txt = enviados[0] if enviados else ""
        finally:
            if cot_real is not None:
                es.cotizar_venta = cot_real
        comprobar("sin parciales: NO se afirman «ventas parciales previas»",
                  "ventas parciales previas" not in txt,
                  f"{[l for l in txt.splitlines() if 'recuper' in l]}")
        comprobar("sin parciales: se dice que el dinero vino del cierre",
                  "al cerrar" in txt,
                  f"{[l for l in txt.splitlines() if 'recuper' in l]}")

        # (c5) Polvo: recuperar menos de un céntimo NO es recuperar.
        cot_real = None
        try:
            import ejecucion_simulada as es
            cot_real = es.cotizar_venta
            es.cotizar_venta = lambda m, t, su: {"usd_salida": 0.0000002}
            conn.execute("DELETE FROM paper_trades WHERE mint='POLVO'")
            conn.execute(
                """INSERT INTO paper_trades
                   (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                    stake_usd, status, costos_usd, tokens_raw)
                   VALUES ('POLVO','POLVO','W',1.0,1,0.5,100.0,'abierta',
                           0.05,1000)""")
            conn.commit()
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE mint='POLVO'").fetchone()
            enviados.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                pt._close(conn, row, 0.01, "sin liquidez", "\U0001F480")
            txt = enviados[0] if enviados else ""
        finally:
            if cot_real is not None:
                es.cotizar_venta = cot_real
        comprobar("polvo: NO se dice «por eso no es una pérdida total»",
                  "no es una pérdida total" not in txt,
                  f"{[l for l in txt.splitlines() if 'total' in l or 'recuper' in l]}")
        comprobar("polvo: y como SÍ se cobró fee de salida, no se promete "
                  "lo contrario",
                  "NO se descuenta comisión" not in txt,
                  f"{[l for l in txt.splitlines() if 'comisión' in l]}")

        # (d) Un cierre NORMAL no cambia en nada.
        conn.execute("DELETE FROM paper_trades WHERE mint='NORMAL'")
        # Con `usd_salida_real` y `fraccion_restante < 1` para que
        # `pnl_neto` NO sea None: si no, la guarda `reason == "sin
        # liquidez"` ni se evalúa y la prueba de regresión no prueba nada.
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante, costos_usd,
                usd_salida_real)
               VALUES ('NORMAL','NORMAL','W',1.0,1,0.5,100.0,'abierta',
                       0.5,0.05,80.0)""")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='NORMAL'").fetchone()
        enviados.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, row, 1.5, "venta de la \u2b50", "\U0001F7E2")
        txt = enviados[0] if enviados else ""
        comprobar("cierre normal: sigue diciendo «PnL:», no «Resultado real»",
                  "PnL: *" in txt and "Resultado real" not in txt,
                  f"{[l for l in txt.splitlines() if 'PnL' in l or 'Resultado' in l]}")
        comprobar("cierre normal: no aparece nada del rug",
                  "pérdida total" not in txt and "no tu dinero" not in txt
                  and "de esa bolsa" not in txt)
        comprobar("cierre normal: Markdown válido", _valido(txt) is None,
                  f"{_valido(txt)}")

        # Y con OTRO motivo distinto, para que ensanchar la guarda a
        # `reason in (...)` no pase desapercibido.
        conn.execute("DELETE FROM paper_trades WHERE mint='SL'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante, costos_usd,
                usd_salida_real)
               VALUES ('SL','SL','W',1.0,1,0.5,100.0,'abierta',0.5,0.05,
                       80.0)""")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='SL'").fetchone()
        enviados.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, row, 0.5, "stop-loss", "\U0001F534")
        txt = enviados[0] if enviados else ""
        comprobar("otro motivo (stop-loss): tampoco sale el texto del rug",
                  "Resultado real" not in txt and "recuperó" not in txt,
                  f"{[l for l in txt.splitlines() if 'Resultado' in l or 'recuper' in l]}")

        # (e) Importe reconstruido: el aviso NO se puede perder al
        #     reescribir el titular.
        conn.execute("DELETE FROM paper_trades WHERE mint='APROX'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                status, fraccion_restante, costos_usd, usd_salida_real)
               VALUES ('APROX','APROX','W',1.0,1,0.5,'abierta',1.0,0.05,
                       10.0)""")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='APROX'").fetchone()
        enviados.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, row, 0.01, "sin liquidez", "\U0001F480")
        txt = enviados[0] if enviados else ""
        comprobar("importe reconstruido: sigue avisando de que los dólares "
                  "son al cambio de AHORA", "al cambio de AHORA" in txt,
                  f"texto: {txt[:400]}")
    finally:
        pt._tg = tg_real
        pt._sol_a_usd = sol_real
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# OLA 18-J · el espejo no puede dejar una posición de polvo abierta.
# ─────────────────────────────────────────────────────────────────────
def prueba_polvo():
    bloque("18-J · la posición de polvo se cierra, no se queda en bucle")
    import io
    import contextlib
    import paper_trading as pt
    from db import get_conn, set_setting

    conn = get_conn()
    enviados = []
    tg_real = pt._tg
    sol_real = pt._sol_a_usd
    try:
        pt._tg = lambda t: enviados.append(t)
        pt._sol_a_usd = lambda *a, **k: 200.0
        # Modo copia pura, que es el que tiene el dueño: sin suelo en el
        # porcentaje que vende la ⭐ y solo el 100% cuenta como total.
        set_setting(conn, "paper_parcial_min_pct", "0")
        set_setting(conn, "paper_total_pct", "100")
        set_setting(conn, "paper_polvo_usd", "0.01")
        # Copia pura de verdad: sin TP, sin SL y sin reloj. Si no, el
        # barrido de zombis parecía funcionar cuando en realidad lo que
        # cerraba la fila era el reloj de 48 h — quitar el barrido dejaba
        # la suite verde. Lo destapó la mutación.
        set_setting(conn, "paper_tp_pct", "999999")
        set_setting(conn, "paper_sl_pct", "999999")
        set_setting(conn, "paper_timeout_h", "999999")

        def nueva_posicion(mint, stake_usd=95.61, entrada=0.00156):
            conn.execute("DELETE FROM paper_trades WHERE mint=?", (mint,))
            conn.execute(
                """INSERT INTO paper_trades
                   (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                    stake_usd, status, fraccion_restante)
                   VALUES (?,?,'W',?,1,1.0,?,'abierta',1.0)""",
                (mint, mint, entrada, stake_usd))
            conn.commit()

        # SE LLAMA A LA FUNCIÓN DE PRODUCCIÓN, no a una copia de su
        # decisión: la primera versión de esta prueba reimplementaba el
        # `if polvo → cerrar` dentro del propio test, así que revertir el
        # arreglo entero dejaba la suite en verde. Lo detectó la auditoría.
        _n = [0]
        # Espía del parcial: tras cerrar por polvo NO se puede seguir y
        # llamar además a `_venta_parcial` sobre una fila ya cerrada. Hoy
        # la guarda `AND status='abierta'` lo haría inofensivo, pero
        # gastaría una cotización de Jupiter por nada y deja el código
        # diciendo una cosa y haciendo otra.
        parcial_real = pt._venta_parcial
        parciales = [0]

        def _espia_parcial(*a, **k):
            parciales[0] += 1
            return parcial_real(*a, **k)

        pt._venta_parcial = _espia_parcial

        def vender(mint, pct, price=0.00156):
            _n[0] += 1
            enviados.clear()
            parciales[0] = 0
            with contextlib.redirect_stdout(io.StringIO()):
                pt.close_on_wallet_sell(
                    conn,
                    {"mint": mint, "wallet": "W", "side": "venta",
                     "ts": 1, "signature": f"sig-{mint}-{_n[0]}"},
                    {"price": price, "symbol": mint, "liq": 50000},
                    {"known": True, "fully_sold": False, "pct_sold": pct})
            return conn.execute("SELECT * FROM paper_trades WHERE mint=?",
                                (mint,)).fetchone()

        # (a) La secuencia REAL de la posición 308 del dueño.
        nueva_posicion("BUCLE")
        secuencia = [57, 52, 40, 59, 42, 45, 48, 55, 51, 47, 53, 44, 58,
                     41, 50, 46, 54, 43, 56, 49, 52, 40, 59, 42, 45, 48,
                     55, 51, 47, 53, 44]
        cerrada_en = None
        fila = None
        for i, pct in enumerate(secuencia, 1):
            fila = vender("BUCLE", pct)
            if fila["status"] != "abierta":
                cerrada_en = i
                break
        comprobar("la posición se cierra sola en vez de encadenar 31 "
                  "parciales de $0,00", cerrada_en is not None,
                  f"sigue abierta, fracción {fila['fraccion_restante']}")
        comprobar("y se cierra pronto, no tras decenas de avisos",
                  cerrada_en is not None and cerrada_en <= 20,
                  f"tardó {cerrada_en} ventas")
        comprobar("el token vuelve a poder copiarse",
                  conn.execute(
                      "SELECT COUNT(*) c FROM paper_trades WHERE mint='BUCLE'"
                      " AND status='abierta'").fetchone()["c"] == 0)
        comprobar("el cierre se apunta como venta de la ⭐, no con un "
                  "motivo nuevo que descuadre el histórico",
                  fila["exit_reason"] == "venta de la ⭐",
                  f"motivo = {fila['exit_reason']!r}")
        comprobar("el PnL ya realizado por los trozos NO se pierde al "
                  "cerrar", fila["pnl_usd"] is not None,
                  f"pnl_usd = {fila['pnl_usd']}")
        comprobar("la fracción queda a 0 en la fila cerrada",
                  fila["fraccion_restante"] == 0,
                  f"fracción = {fila['fraccion_restante']}")
        comprobar("el mensaje de cierre explica por qué se cerró entera",
                  enviados and "no llegaba a un céntimo" in enviados[0],
                  f"mensaje: {enviados[0][:200] if enviados else 'nada'}")
        comprobar("el mensaje de cierre es Markdown válido",
                  enviados and _valido(enviados[0]) is None,
                  f"{_valido(enviados[0]) if enviados else 'sin mensaje'}")
        comprobar("se registró UN fill total, no dos",
                  conn.execute(
                      "SELECT COUNT(*) c FROM paper_fills WHERE trade_id=? "
                      "AND tipo='total'", (fila["id"],)).fetchone()["c"] == 1)
        comprobar("el fill del cierre lleva la firma de la transacción "
                  "(sin ella se pierde la idempotencia)",
                  conn.execute(
                      "SELECT firma FROM paper_fills WHERE trade_id=? AND "
                      "tipo='total'", (fila["id"],)).fetchone()["firma"],
                  "el fill se guardó sin firma")
        comprobar("el cierre manda UN solo mensaje, no el de cierre y "
                  "además el del parcial", len(enviados) == 1,
                  f"{len(enviados)} mensajes: "
                  f"{[m[:45] for m in enviados]}")
        comprobar("al cerrar por polvo NO se intenta además el parcial",
                  parciales[0] == 0,
                  f"se llamó a _venta_parcial {parciales[0]} veces")
        comprobar("el icono sigue al precio: token a la par → verde",
                  enviados and enviados[0].startswith("🟢"),
                  f"empieza por {enviados[0][:4] if enviados else ''!r}")

        # Y con el token por debajo de la entrada, rojo.
        nueva_posicion("ROJO")
        conn.execute("UPDATE paper_trades SET fraccion_restante=1e-4 "
                     "WHERE mint='ROJO'")
        conn.commit()
        fila = vender("ROJO", 50, price=0.0000156)
        comprobar("el icono sigue al precio: token caído → rojo",
                  fila["status"] == "cerrada" and enviados
                  and enviados[0].startswith("🔴"),
                  f"status={fila['status']}, "
                  f"empieza por {enviados[0][:4] if enviados else ''!r}")

        # (a2) TODA posición recién abierta tiene `fraccion_restante`
        #      NULL: `open_trade` no la escribe en el INSERT. El defecto
        #      de 1.0 está en el camino más caliente de la función y no lo
        #      tocaba ninguna prueba: cambiarlo por 0 cerraba enteras las
        #      posiciones vivas apuntando `pnl_usd = 0`, y la suite seguía
        #      en verde.
        conn.execute("DELETE FROM paper_trades WHERE mint='VIRGEN'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status)
               VALUES ('VIRGEN','VIRGEN','W',0.00156,1,1.0,100.0,
                       'abierta')""")
        conn.commit()
        fila = vender("VIRGEN", 10)
        comprobar("posición virgen (fracción NULL): una venta del 10% NO "
                  "la cierra", fila["status"] == "abierta",
                  f"status={fila['status']}, pnl_usd={fila['pnl_usd']}")
        comprobar("posición virgen: queda el 90% vivo",
                  abs((fila["fraccion_restante"] or 0) - 0.9) < 1e-9,
                  f"fracción = {fila['fraccion_restante']}")

        # (b) Una venta normal NO se convierte en cierre.
        nueva_posicion("NORMAL2")
        fila = vender("NORMAL2", 50)
        comprobar("venta del 50%: sigue siendo parcial y queda la mitad",
                  fila["status"] == "abierta"
                  and abs(fila["fraccion_restante"] - 0.5) < 1e-9,
                  f"status={fila['status']}, frac={fila['fraccion_restante']}")
        fila = vender("NORMAL2", 90)
        comprobar("venta del 90% sobre la mitad: queda un 5% que vale más "
                  "de un céntimo, sigue viva", fila["status"] == "abierta",
                  f"status={fila['status']}, frac={fila['fraccion_restante']}")

        # (c) EL PRECIO IMPORTA. La primera versión usaba siempre el mismo
        #     precio de entrada, así que el factor `price / entry_price`
        #     valía 1 y el corazón del criterio no se probaba: borrarlo
        #     dejaba la suite verde.
        nueva_posicion("CAIDO")
        fila = vender("CAIDO", 99)          # queda 1% = 0,96 $ a la par
        comprobar("token a la par: un resto de 0,96 $ sigue vivo",
                  fila["status"] == "abierta",
                  f"status={fila['status']}, frac={fila['fraccion_restante']}")
        nueva_posicion("CAIDO2")
        # Token caído 1.000×: ese mismo 1% vale 0,00096 $ → polvo.
        fila = vender("CAIDO2", 99, price=0.00000156)
        comprobar("token caído 1.000×: ese mismo 1% ya no llega a un "
                  "céntimo y se cierra", fila["status"] == "cerrada",
                  f"status={fila['status']}")
        nueva_posicion("SUBIDO")
        # Token multiplicado por 1.000: una fracción de 1e-4 vale 9,56 $.
        conn.execute("UPDATE paper_trades SET fraccion_restante=2e-4 "
                     "WHERE mint='SUBIDO'")
        conn.commit()
        fila = vender("SUBIDO", 50, price=1.56)
        comprobar("token subido 1.000×: una fracción de 1e-4 vale 9,56 $ "
                  "y NO es polvo", fila["status"] == "abierta",
                  f"status={fila['status']}, frac={fila['fraccion_restante']}")

        # (d) Fracción grande: nunca es polvo, aunque el precio se
        #     desplome. Cerrar es irreversible.
        nueva_posicion("GLITCH")
        fila = vender("GLITCH", 50, price=0.00000000156)
        comprobar("media posición NO se cierra por un precio glitcheado",
                  fila["status"] == "abierta",
                  f"status={fila['status']}")
        # La guarda está en el 5%, y eso importa en las dos direcciones.
        # Con un 10% vivo, un precio a cero NO puede cerrar la posición:
        # subir la guarda al 50% dejaría cerrable ese 10%.
        nueva_posicion("G10")
        fila = vender("G10", 90, price=0.00000000156)   # queda 10%
        comprobar("con un 10% vivo, un precio a cero NO cierra la "
                  "posición", fila["status"] == "abierta",
                  f"status={fila['status']}")
        # Y con un 4% vivo (por debajo de la guarda) el criterio de valor
        # sí manda: bajar la guarda a 0 no cambiaría esto, pero subirla sí.
        nueva_posicion("G4")
        fila = vender("G4", 96, price=0.00000000156)    # queda 4%
        comprobar("con un 4% vivo y el token a cero, sí se cierra",
                  fila["status"] == "cerrada", f"status={fila['status']}")
        # Y el suelo de fracción sin dólares no puede ser un «siempre sí».
        conn.execute("DELETE FROM paper_trades WHERE mint='SF'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                status, fraccion_restante)
               VALUES ('SF','SF','W',0.00156,1,1.0,'abierta',0.02)""")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='SF'").fetchone()
        sol_bak = pt._sol_a_usd
        pt._sol_a_usd = lambda *a, **k: None
        comprobar("sin dólares, una fracción del 1% NO es polvo por "
                  "sistema", not pt._resto_es_polvo(conn, row, 0.01, 0.00156),
                  "el suelo de fracción cerró un 1% vivo")
        pt._sol_a_usd = sol_bak

        # (e) El suelo es configurable y se acota contra basura.
        set_setting(conn, "paper_polvo_usd", "5.0")
        nueva_posicion("SUELO")
        fila = vender("SUELO", 96)          # queda 4% = 3,82 $
        comprobar("con el suelo en 5 $, un resto de 3,82 $ ya es polvo",
                  fila["status"] == "cerrada", f"status={fila['status']}")
        # Con una fracción del 50% la guarda del 5% corta antes y el
        # saneo del suelo no llega a evaluarse: la prueba parecía cubrirlo
        # y no cubría nada (quitar el saneo dejaba la suite verde). Se
        # vende el 96%, así que queda un 4% —por debajo del 5%— y el
        # camino pasa de verdad por el suelo.
        for basura in ("inf", "nan", "-3", "hola"):
            set_setting(conn, "paper_polvo_usd", basura)
            nueva_posicion("BASURA")
            fila = vender("BASURA", 96)     # queda 4% = 3,82 $ vivos
            comprobar(f"con `paper_polvo_usd` = {basura!r} no se cierra "
                      f"una posición de 3,82 $", fila["status"] == "abierta",
                      f"status={fila['status']}")
        # Y acotar la basura no puede APAGAR el corte: con el suelo
        # saneado a 1 céntimo, el polvo de verdad sigue siendo polvo.
        # (Sanearlo a 0 dejaba la suite verde y el bug de vuelta.)
        set_setting(conn, "paper_polvo_usd", "inf")
        nueva_posicion("BASURA2")
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='BASURA2'").fetchone()
        comprobar("con `paper_polvo_usd` = 'inf' saneado, el polvo de "
                  "verdad SIGUE siendo polvo",
                  pt._resto_es_polvo(conn, row, 1e-9, 0.00156),
                  "el saneo apagó el corte en vez de acotarlo")

        # Y si el ajuste NO existe, el defecto tiene que seguir cortando.
        conn.execute("DELETE FROM settings WHERE key='paper_polvo_usd'")
        conn.commit()
        nueva_posicion("DEFECTO")
        fila = vender("DEFECTO", 99.999)   # queda menos de un céntimo
        comprobar("sin el ajuste puesto, el defecto de 1 céntimo sigue "
                  "cortando la cola", fila["status"] == "cerrada",
                  f"status={fila['status']}")
        nueva_posicion("DEFECTO2")
        fila = vender("DEFECTO2", 96)      # quedan 3,82 $
        comprobar("…y con el defecto, 3,82 $ NO son polvo",
                  fila["status"] == "abierta", f"status={fila['status']}")
        set_setting(conn, "paper_polvo_usd", "0.01")

        # (f) Sin importe en dólares, se reconstruye desde SOL.
        conn.execute("DELETE FROM paper_trades WHERE mint='SINUSD'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                status, fraccion_restante)
               VALUES ('SINUSD','SINUSD','W',0.00156,1,5.0,'abierta',
                       2e-4)""")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='SINUSD'").fetchone()
        # 5 SOL × 200 $ = 1.000 $; el 1e-4 restante vale 9,5 $ con el
        # token ×100. Sin reconstruir el importe se cerraba igualmente.
        comprobar("sin `stake_usd`, el importe se reconstruye desde SOL y "
                  "un resto de 9,5 $ NO es polvo",
                  not pt._resto_es_polvo(conn, row, 9.5e-5, 0.156),
                  "cerró una posición de 9,5 $")
        pt._sol_a_usd = lambda *a, **k: None
        comprobar("y si tampoco hay precio de SOL, se cae al suelo de "
                  "fracción sin reventar",
                  pt._resto_es_polvo(conn, row, 1e-9, 0.00156))
        pt._sol_a_usd = lambda *a, **k: 200.0

        # (g) El barrido del job periódico limpia las zombis que ya
        #     existen, aunque no llegue ninguna venta nueva.
        conn.execute("DELETE FROM paper_trades WHERE mint='ZOMBI'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante)
               VALUES ('ZOMBI','ZOMBI','W',0.00156,1,1.0,95.61,'abierta',
                       6.1e-11)""")
        conn.commit()
        st_real = None
        try:
            import signal_tracker as st
            st_real = st._price_mc_ex

            def _pmx_bueno(m):
                # El doble tiene que limpiar la bandera por hilo, igual
                # que hace `_price_mc_ex` de verdad en su primera línea.
                # Sin esto arrastra el "dato: simulado" que dejó otra
                # prueba y `update_open_trades` se salta la fila.
                st._set_fallo_precio(None)
                return (0.00156, 1000.0, False, 50000.0)

            st._price_mc_ex = _pmx_bueno
            enviados.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                pt.update_open_trades()
        finally:
            if st_real is not None:
                st._price_mc_ex = st_real
        fila = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='ZOMBI'").fetchone()
        comprobar("el job periódico cierra las posiciones zombi que ya "
                  "estaban abiertas", fila["status"] == "cerrada",
                  f"status = {fila['status']}, "
                  f"frac = {fila['fraccion_restante']}")
        comprobar("el barrido apunta el cierre como venta de la ⭐, no "
                  "con un motivo nuevo que descuadre el histórico",
                  fila["exit_reason"] == "venta de la ⭐",
                  f"motivo = {fila['exit_reason']!r}")
        comprobar("el barrido manda UN solo mensaje por posición",
                  len([m for m in enviados if "ZOMBI" in m]) == 1,
                  f"{len([m for m in enviados if 'ZOMBI' in m])} mensajes "
                  f"de ZOMBI: {[m[:40] for m in enviados]}")
        comprobar("la zombi cerrada por el barrido queda con fracción 0 y "
                  "un solo fill total",
                  fila["fraccion_restante"] == 0
                  and conn.execute(
                      "SELECT COUNT(*) c FROM paper_fills WHERE trade_id=? "
                      "AND tipo='total'",
                      (fila["id"],)).fetchone()["c"] == 1,
                  f"frac={fila['fraccion_restante']}")

        # (g1b) El barrido NO puede cerrar una posición del 3% por una
        #       lectura de precio mala: cierra en firme, con una sola
        #       lectura, sin la confirmación en dos pasadas que el resto
        #       del archivo exige. Por eso pide además `_fr < 1e-3`.
        conn.execute("DELETE FROM paper_trades WHERE mint='TRESPC'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante)
               VALUES ('TRESPC','TRESPC','W',0.00156,1,1.0,95.61,'abierta',
                       0.03)""")
        conn.commit()
        st_real3 = None
        try:
            import signal_tracker as st3
            st_real3 = st3._price_mc_ex

            def _pmx_glitch(m):
                st3._set_fallo_precio(None)
                return (0.00000156, 1000.0, False, 50000.0)

            st3._price_mc_ex = _pmx_glitch
            with contextlib.redirect_stdout(io.StringIO()):
                pt.update_open_trades()
        finally:
            if st_real3 is not None:
                st3._price_mc_ex = st_real3
        fila = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='TRESPC'").fetchone()
        comprobar("el barrido NO cierra un 3% vivo por una sola lectura de "
                  "precio mala", fila["status"] == "abierta",
                  f"status = {fila['status']}")

        # Y con una fracción DENTRO del rango del barrido (5e-4 < 1e-3),
        # lo que decide es el dinero: 5 $ vivos de una posición de 10.000
        # no son polvo. Sin esta prueba, sustituir el criterio de valor
        # por `True` dejaba la suite verde.
        conn.execute("DELETE FROM paper_trades WHERE mint='GRANDE'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante)
               VALUES ('GRANDE','GRANDE','W',0.00156,1,50.0,10000.0,
                       'abierta',5e-4)""")
        conn.commit()
        st_real4 = None
        try:
            import signal_tracker as st4
            st_real4 = st4._price_mc_ex

            def _pmx_par(m):
                st4._set_fallo_precio(None)
                return (0.00156, 1000.0, False, 50000.0)

            st4._price_mc_ex = _pmx_par
            with contextlib.redirect_stdout(io.StringIO()):
                pt.update_open_trades()
        finally:
            if st_real4 is not None:
                st4._price_mc_ex = st_real4
        fila = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='GRANDE'").fetchone()
        comprobar("el barrido NO cierra 5 $ vivos aunque la fracción sea "
                  "diminuta: lo que manda es el dinero",
                  fila["status"] == "abierta", f"status = {fila['status']}")

        # (g1c) El borde exacto del suelo: valer JUSTO un céntimo no es
        #       ser polvo.
        conn.execute("DELETE FROM paper_trades WHERE mint='BORDE'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante)
               VALUES ('BORDE','BORDE','W',1.0,1,1.0,100.0,'abierta',1.0)""")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='BORDE'").fetchone()
        comprobar("un resto que vale EXACTAMENTE el suelo no es polvo",
                  not pt._resto_es_polvo(conn, row, 1e-4, 1.0),
                  "cerró un resto que valía justo el céntimo del suelo")
        comprobar("y un céntimo menos sí lo es",
                  pt._resto_es_polvo(conn, row, 0.99e-4, 1.0))

        # (g2) SIN PRECIO el barrido no puede tocar nada, y sobre todo no
        #      puede reventar: `_price_mc_ex` devuelve `price=None` sin
        #      marcar fallo en el rug, en la liquidez de polvo y en «hay
        #      pares pero ninguno con precio usable». La comparación del
        #      icono lanzaba un TypeError que se llevaba la pasada ENTERA
        #      —ni TP, ni SL, ni reloj, para todas las posiciones—, y la
        #      zombi es la fila más vieja, así que moría en la primera.
        for etiqueta, retorno in (
                ("rug (sin pares)", (None, None, True, 0.0)),
                ("liquidez de polvo", (None, None, True, 50.0)),
                ("pares vivos sin precio usable", (None, None, False, 0.0))):
            conn.execute("DELETE FROM paper_trades WHERE mint IN "
                         "('ZOMBI2','VIVA')")
            conn.execute(
                """INSERT INTO paper_trades
                   (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                    stake_usd, status, fraccion_restante)
                   VALUES ('ZOMBI2','ZOMBI2','W',0.00156,1,1.0,95.61,
                           'abierta',6.1e-11)""")
            conn.commit()
            st_real2 = None
            revento = None
            try:
                import signal_tracker as st2
                st_real2 = st2._price_mc_ex

                def _pmx_sin_precio(m, _r=retorno):
                    st2._set_fallo_precio(None)
                    return _r

                st2._price_mc_ex = _pmx_sin_precio
                with contextlib.redirect_stdout(io.StringIO()):
                    pt.update_open_trades()
            except Exception as e:
                revento = f"{type(e).__name__}: {e}"
            finally:
                if st_real2 is not None:
                    st2._price_mc_ex = st_real2
            comprobar(f"barrido sin precio ({etiqueta}): NO revienta la "
                      f"pasada", revento is None, f"reventó con {revento}")
        # (h) La carrera: un hilo cierra la posición mientras otro trae
        #     un parcial con la fila de hace un instante. El parcial NO
        #     puede escribirse encima de una fila ya cerrada: dejaría en
        #     el histórico un `pnl_realizado_usd` posterior al cierre, o
        #     sea dinero realizado que el `pnl_usd` guardado ya no cuenta.
        conn.execute("DELETE FROM paper_trades WHERE mint='CARRERA'")
        conn.execute(
            """INSERT INTO paper_trades
               (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                stake_usd, status, fraccion_restante, pnl_realizado_usd)
               VALUES ('CARRERA','CARRERA','W',0.00156,1,1.0,95.61,
                       'abierta',0.5,3.56)""")
        conn.commit()
        vieja = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='CARRERA'").fetchone()
        # El "otro hilo" cierra primero.
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, vieja, 0.002, "venta de la ⭐", "🟢")
        cerrada = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='CARRERA'").fetchone()
        # Y ahora llega el parcial con la fila VIEJA.
        enviados.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            pt._venta_parcial(conn, vieja, 0.002, 50)
        despues = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='CARRERA'").fetchone()
        comprobar("carrera: el parcial NO pisa la fila ya cerrada",
                  despues["pnl_realizado_usd"] == cerrada["pnl_realizado_usd"]
                  and despues["fraccion_restante"] ==
                  cerrada["fraccion_restante"],
                  f"realizado {cerrada['pnl_realizado_usd']} → "
                  f"{despues['pnl_realizado_usd']}, frac "
                  f"{cerrada['fraccion_restante']} → "
                  f"{despues['fraccion_restante']}")
        comprobar("carrera: tampoco manda el aviso del parcial",
                  not enviados, f"mandó {len(enviados)}")
        # Y al revés: cerrar dos veces no puede duplicar el cierre.
        enviados.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, vieja, 0.003, "tiempo", "⏰")
        recerrada = conn.execute(
            "SELECT * FROM paper_trades WHERE mint='CARRERA'").fetchone()
        comprobar("carrera: no se cierra dos veces",
                  recerrada["exit_reason"] == "venta de la ⭐"
                  and recerrada["exit_price"] == cerrada["exit_price"],
                  f"motivo {recerrada['exit_reason']!r}, "
                  f"precio {recerrada['exit_price']}")
        comprobar("carrera: el segundo cierre no manda mensaje",
                  not enviados, f"mandó {len(enviados)}")
        comprobar("carrera: no se apunta un segundo fill total",
                  conn.execute(
                      "SELECT COUNT(*) c FROM paper_fills WHERE trade_id=? "
                      "AND tipo='total'",
                      (cerrada["id"],)).fetchone()["c"] == 1)
    finally:
        pt._tg = tg_real
        pt._sol_a_usd = sol_real
        try:
            pt._venta_parcial = parcial_real
        except NameError:
            pass
        conn.close()


def _vigilante(segundos=420):
    """Si la suite se cuelga, tiene que terminar en ROJO, no quedarse muda.

    Ya pasó: al romper una rama de `_cerrar_markdown` el índice volvía a 0
    y la función no terminaba nunca. La prueba de terminación lo caza y lo
    dice, pero el proceso seguía colgado después — y en un despliegue eso
    se lee como "sin respuesta", no como "en rojo".
    """
    import threading

    def _matar():
        print("\n❌ La suite se colgó: pasaron "
              f"{segundos}s sin terminar. Mira la última línea impresa.")
        sys.stdout.flush()
        os._exit(1)

    t = threading.Timer(segundos, _matar)
    t.daemon = True
    t.start()
    return t


# ---------------------------------------------------------------------
# OLA 18-K - 1 - el "top 50" es DE VERDAD el puesto de /top.
# ---------------------------------------------------------------------
def prueba_top50():
    bloque("18-K/1 - alertar solo desde el top N REAL de /top")
    import time as _t
    import db
    from db import get_conn

    conn = get_conn()
    try:
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM positions")
        db.invalidar_copiables()
        ahora = int(_t.time())
        reciente, viejo = ahora - 600, ahora - 10 * 86400

        # 6 estrellas que ganan y operan hoy: puestos 1..6 de /top.
        for i in range(6):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "wallet_score, pnl_total, score) VALUES (?,1,0,?,?,0)",
                (f"BUENA{i}", 90 - i, 10.0))
            conn.execute(
                "INSERT INTO positions (wallet, mint, tokens, last_ts) "
                "VALUES (?,?,0,?)", (f"BUENA{i}", f"M{i}", reciente))
        # 4 estrellas que ganan pero llevan 10 dias dormidas: en /top van
        # DESPUES de las activas, pero ANTES de cualquiera que pierda.
        for i in range(4):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "wallet_score, pnl_total, score) VALUES (?,1,0,?,?,0)",
                (f"DORMIDA{i}", 99, 10.0))
            conn.execute(
                "INSERT INTO positions (wallet, mint, tokens, last_ts) "
                "VALUES (?,?,0,?)", (f"DORMIDA{i}", f"D{i}", viejo))
        # La del caso real: estrella ACTIVA pero en perdidas. En /top cae
        # por debajo de TODAS las que no pierden -> puesto 11.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, "
            "wallet_score, pnl_total, score) VALUES ('PERDEDORA',1,0,44,?,0)",
            (-3.41,))
        conn.execute(
            "INSERT INTO positions (wallet, mint, tokens, last_ts) "
            "VALUES ('PERDEDORA','MP',0,?)", (reciente,))
        # Candidata sin estrella, activa y ganadora: sale en /top pero
        # nunca puede alertar.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, "
            "wallet_score, pnl_total, score) VALUES ('CANDIDATA',0,0,95,50,0)")
        conn.execute(
            "INSERT INTO positions (wallet, mint, tokens, last_ts) "
            "VALUES ('CANDIDATA','MC',0,?)", (reciente,))
        # Un bot con estrella, activo y con la mejor nota de todas: ni
        # sale en /top ni puede alertar.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, "
            "wallet_score, pnl_total, score) VALUES ('BOT1',1,1,100,99,0)")
        conn.execute(
            "INSERT INTO positions (wallet, mint, tokens, last_ts) "
            "VALUES ('BOT1','MB',0,?)", (reciente,))
        conn.commit()

        puestos = [r["address"] for r in db.top_wallets(conn, 100)]
        pos = {a: i for i, a in enumerate(puestos, 1)}
        comprobar("montaje: la perdedora activa queda fuera del top 10 de "
                  "/top (el caso que vio el dueno)",
                  pos.get("PERDEDORA", 0) > 10,
                  f"quedo en el puesto {pos.get('PERDEDORA')}")

        op = db.top_addresses(conn, 10)
        comprobar("una estrella activa PERO fuera del top N no alerta",
                  "PERDEDORA" not in op,
                  f"puesto {pos.get('PERDEDORA')} y aun asi alerta")
        comprobar("una estrella dormida dentro del top N tampoco alerta",
                  not any(a.startswith("DORMIDA") for a in op),
                  f"conjunto = {sorted(op)}")
        comprobar("una candidata sin estrella no alerta",
                  "CANDIDATA" not in op, f"conjunto = {sorted(op)}")
        comprobar("las 6 buenas si alertan", len(op) == 6, f"{sorted(op)}")
        comprobar("un bot nunca alerta, por buena nota que tenga",
                  "BOT1" not in op, f"conjunto = {sorted(op)}")

        # Con un tope AMPLIO la candidata sin estrella si cabe dentro de
        # los N primeros de /top. Aqui es donde se ve que el filtro de la
        # estrella hace falta de verdad: con el tope pequeno la dejaba
        # fuera el LIMIT, no el filtro.
        db.invalidar_copiables()
        amplio = db.top_addresses(conn, 50)
        comprobar("montaje: con tope 50 la candidata SI esta dentro de /top",
                  0 < pos.get("CANDIDATA", 0) <= 50,
                  f"puesto {pos.get('CANDIDATA')}")
        comprobar("montaje: con tope 50 las dormidas SI estan dentro de /top",
                  all(0 < pos.get(f"DORMIDA{i}", 0) <= 50 for i in range(4)),
                  "alguna dormida quedo fuera de los 50 primeros")
        comprobar("montaje: con tope 50 la perdedora SI esta dentro de /top",
                  0 < pos.get("PERDEDORA", 0) <= 50,
                  f"puesto {pos.get('PERDEDORA')}")
        comprobar("dentro del top 50: la candidata sin estrella no alerta",
                  "CANDIDATA" not in amplio, f"conjunto = {sorted(amplio)}")
        comprobar("dentro del top 50: las dormidas no alertan",
                  not any(a.startswith("DORMIDA") for a in amplio),
                  f"conjunto = {sorted(amplio)}")
        # La perdedora SI vuelve a alertar con tope 50, y es correcto: la
        # regla es POSICIONAL, no "las que pierden nunca". Con solo 11
        # estrellas su puesto 11 cabe de sobra en 50. Lo que se arreglo es
        # que con tope 10 colaba igual.
        comprobar("dentro del top 50 alertan las 6 buenas Y la perdedora "
                  "(su puesto 11 si cabe en 50)",
                  amplio == ({f"BUENA{i}" for i in range(6)}
                             | {"PERDEDORA"}),
                  f"conjunto = {sorted(amplio)}")

        # INVARIANTE, comprobada contra la OTRA funcion (no contra una
        # copia de la misma consulta): quien alerta esta SIEMPRE entre
        # los N primeros de /top.
        for n in (1, 2, 3, 5, 8, 10, 50):
            db.invalidar_copiables()
            s = db.top_addresses(conn, n)
            primeros = [r["address"] for r in db.top_wallets(conn, n)]
            comprobar(f"top {n}: quien alerta esta entre los {n} primeros "
                      f"de /top",
                      all(a in primeros for a in s),
                      f"sobran {sorted(set(s) - set(primeros))}")

        # Contrato de tres estados.
        db.invalidar_copiables()
        comprobar("top_alertas = 0 devuelve None (sin limite, no 'nadie')",
                  db.top_addresses(conn, 0) is None,
                  f"devolvio {db.top_addresses(conn, 0)!r}")

        class _ConnRota:
            def execute(self, *a, **k):
                raise RuntimeError("base caida")
        db.invalidar_copiables()
        comprobar("si la consulta falla devuelve None (no deja el bot mudo)",
                  db.top_addresses(_ConnRota(), 5) is None,
                  "devolvio algo que no es None")

        conn.execute("DELETE FROM positions")     # nadie activo
        conn.commit()
        db.invalidar_copiables()
        vacio = db.top_addresses(conn, 10)
        comprobar("sin nadie activo devuelve un conjunto VACIO, no None",
                  vacio is not None and len(vacio) == 0, f"{vacio!r}")

        # Desempate DETERMINISTA (auditoria 18-K, hallazgo 4): /top, el
        # conjunto operativo y la posicion de las tarjetas ejecutan el
        # mismo ORDER BY por separado. Sin una ultima clave unica
        # (address), dos billeteras empatadas pueden caer a un lado u
        # otro del corte segun la ejecucion — en SQLite sale estable de
        # casualidad, en Postgres no esta garantizado. Esta prueba mira
        # el SQL porque el sintoma NO es reproducible en SQLite: aqui el
        # empate sale siempre igual y una prueba de comportamiento
        # pasaria tambien sin el desempate.
        import inspect
        import wallet_ident
        for fn, dueno in ((db.top_wallets, "top_wallets"),
                          (db._operativas, "_operativas"),
                          (wallet_ident.posicion, "wallet_ident.posicion")):
            fuente = inspect.getsource(fn)
            i_orden = fuente.find("ORDER BY")
            i_addr = fuente.find("w.address", i_orden)
            i_limit = fuente.find("LIMIT", i_orden)
            comprobar(f"{dueno}: el ORDER BY desempata por address",
                      0 <= i_orden < i_addr < i_limit,
                      "no hay w.address entre ORDER BY y LIMIT")

        # El contrato, en el unico sitio donde se decide.
        comprobar("en_top(None, x) deja pasar (sin filtro)",
                  db.en_top(None, "X") is True)
        comprobar("en_top(set(), x) NO deja pasar (filtro sin nadie)",
                  db.en_top(set(), "X") is False)
        comprobar("en_top({'X'}, 'X') deja pasar", db.en_top({"X"}, "X"))
        comprobar("en_top({'Y'}, 'X') no deja pasar",
                  db.en_top({"Y"}, "X") is False)
    finally:
        db.invalidar_copiables()
        conn.close()


# ---------------------------------------------------------------------
# OLA 18-K - 2 - creadores de mercado: vueltas al MISMO token.
# ---------------------------------------------------------------------
def prueba_creador_mercado():
    bloque("18-K/2 - sin estrella para quien da vueltas al mismo token")
    import time as _t
    import config as cfg
    import performance_review as pv
    from db import get_conn

    conn = get_conn()
    tope_previo = cfg.MM_VUELTAS_MAX
    try:
        ahora = int(_t.time())

        def sembrar_seq(wallet, mint, secuencia, ts=None):
            """Inserta señales en el ORDEN dado: 'CVCV' = compra, venta,
            compra, venta, con timestamps crecientes."""
            ts = ahora - 3600 if ts is None else ts
            for k, letra in enumerate(secuencia):
                side = "compra" if letra == "C" else "venta"
                conn.execute(
                    "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                    "side) VALUES (?,?,?,1,?,?)",
                    (f"s{wallet}{mint}{k}{ts}", wallet, mint, ts + k, side))

        def sembrar(wallet, mint, compras, ventas, ts=None):
            """Compras y ventas ALTERNADAS (el patron del caso real)."""
            pares = min(compras, ventas)
            resto = ("C" * (compras - pares)) + ("V" * (ventas - pares))
            sembrar_seq(wallet, mint, "CV" * pares + resto, ts=ts)

        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM wallets")
        for w in ("MM6", "JUSTO5", "ASIMETRICA", "DISPERSA", "ANTIGUA",
                  "LIMPIA"):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, grade) "
                "VALUES (?,1,0,'Seguimiento')", (w,))
        sembrar("MM6", "TOKEN_A", 6, 6)              # 6 vueltas -> fuera
        sembrar("JUSTO5", "TOKEN_B", 5, 5)           # 5 vueltas -> se queda
        sembrar("ASIMETRICA", "TOKEN_C", 20, 5)      # min(20,5)=5 -> se queda
        for i in range(8):                           # 8 tokens, 1 vuelta
            sembrar("DISPERSA", f"TOKEN_D{i}", 1, 1)
        sembrar("ANTIGUA", "TOKEN_E", 40, 40, ts=ahora - 400 * 86400)
        sembrar("LIMPIA", "TOKEN_F", 3, 1)
        conn.commit()

        comprobar("6 vueltas al mismo token: SIN estrella",
                  pv.creadora_de_mercado(conn, "MM6") is not None)
        comprobar("exactamente 5 vueltas: se queda (el dueno dijo MAS de 5)",
                  pv.creadora_de_mercado(conn, "JUSTO5") is None,
                  str(pv.creadora_de_mercado(conn, "JUSTO5")))
        comprobar("20 compras y 5 ventas son 5 vueltas, no 20",
                  pv.creadora_de_mercado(conn, "ASIMETRICA") is None,
                  str(pv.creadora_de_mercado(conn, "ASIMETRICA")))
        comprobar("8 tokens con una vuelta cada uno NO es creador de mercado",
                  pv.creadora_de_mercado(conn, "DISPERSA") is None,
                  str(pv.creadora_de_mercado(conn, "DISPERSA")))
        comprobar("40 vueltas hace mas de un ano no cuentan (fuera de la "
                  "ventana)",
                  pv.creadora_de_mercado(conn, "ANTIGUA") is None,
                  str(pv.creadora_de_mercado(conn, "ANTIGUA")))
        comprobar("una billetera normal no se toca",
                  pv.creadora_de_mercado(conn, "LIMPIA") is None)

        # El falso positivo que destapo la auditoria: 6 compras
        # escalonadas y LUEGO 6 ventas parciales es UNA posicion, no 6
        # vueltas. La metrica del minimo la condenaba; la de alternancias
        # compra->venta la deja en paz.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, grade) "
            "VALUES ('ESCALONADA',1,0,'Seguimiento')")
        sembrar_seq("ESCALONADA", "TOKEN_G", "CCCCCCVVVVVV")
        conn.commit()
        comprobar("6 compras escalonadas + 6 ventas parciales = 1 vuelta, "
                  "conserva la estrella",
                  pv.creadora_de_mercado(conn, "ESCALONADA") is None,
                  str(pv.creadora_de_mercado(conn, "ESCALONADA")))
        # Ronda 2: dos señales en el MISMO segundo se ordenan por la
        # FIRMA en Python (por puntos de codigo), no por el motor: un
        # Postgres con colacion de idioma ordenaria 'a' antes que 'B' y
        # contaria distinto que SQLite. Se insertan al reves del orden
        # canonico: compra con firma 'zz', venta con firma 'aa'. En orden
        # de insercion seria compra->venta (1 vuelta); por firma es
        # venta->compra (0). La cuenta correcta es 0.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, grade) "
            "VALUES ('EMPATADA',1,0,'Seguimiento')")
        _ts_e = ahora - 3600
        conn.execute(
            "INSERT INTO signals (signature, wallet, mint, sol, ts, side) "
            "VALUES ('zz_empate','EMPATADA','TOKEN_I',1,?, 'compra')",
            (_ts_e,))
        conn.execute(
            "INSERT INTO signals (signature, wallet, mint, sol, ts, side) "
            "VALUES ('aa_empate','EMPATADA','TOKEN_I',1,?, 'venta')",
            (_ts_e,))
        conn.commit()
        comprobar("empate de segundo: manda el orden por firma (0 vueltas), "
                  "no el orden de insercion",
                  pv._vueltas_max(conn, 30, "EMPATADA")
                  .get("EMPATADA", (0, None))[0] == 0,
                  str(pv._vueltas_max(conn, 30, "EMPATADA")))

        # Y el mismo volumen ALTERNADO si cae: es el patron real.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, grade) "
            "VALUES ('ALTERNADA',1,0,'Seguimiento')")
        sembrar_seq("ALTERNADA", "TOKEN_H", "CV" * 6)
        conn.commit()
        comprobar("el mismo volumen alternado (CVCVCV...) SI cae",
                  pv.creadora_de_mercado(conn, "ALTERNADA") is not None)

        motivo = pv.creadora_de_mercado(conn, "MM6")
        comprobar("el motivo dice cuantas vueltas y a que token",
                  motivo and "6" in motivo and "TOKEN_A"[:8] in motivo,
                  str(motivo))

        # El lote y el de una en una tienen que coincidir SIEMPRE.
        lote = pv.creadoras_de_mercado(conn)
        una = {w for w in ("MM6", "JUSTO5", "ASIMETRICA", "DISPERSA",
                           "ANTIGUA", "LIMPIA", "ESCALONADA", "ALTERNADA",
                           "EMPATADA")
               if pv.creadora_de_mercado(conn, w)}
        comprobar("el calculo en lote da lo mismo que el de una en una",
                  set(lote) == una, f"lote={sorted(lote)} una={sorted(una)}")

        # El lote solo mira estrellas.
        conn.execute("UPDATE wallets SET is_tracked = 0 WHERE address='MM6'")
        conn.commit()
        comprobar("el lote ignora a las que ya no tienen estrella",
                  "MM6" not in pv.creadoras_de_mercado(conn))
        conn.execute("UPDATE wallets SET is_tracked = 1 WHERE address='MM6'")
        conn.commit()

        # Interruptor de apagado.
        cfg.MM_VUELTAS_MAX = 0
        comprobar("MM_VUELTAS_MAX = 0 apaga la regla",
                  pv.creadora_de_mercado(conn, "MM6") is None
                  and pv.creadoras_de_mercado(conn) == {})
        cfg.MM_VUELTAS_MAX = tope_previo

        # Y la depuracion le quita la estrella de verdad.
        import ai_analyst as aa
        res = aa.depurar_estrellas(conn)
        fila = conn.execute(
            "SELECT is_tracked, ai_follow, ai_reason FROM wallets "
            "WHERE address='MM6'").fetchone()
        comprobar("depurar_estrellas le quita la estrella",
                  fila["is_tracked"] == 0, f"is_tracked = {fila['is_tracked']}")
        comprobar("y deja de seguirla", not fila["ai_follow"])
        comprobar("y escribe el motivo en la ficha",
                  "creadora de mercado" in (fila["ai_reason"] or ""),
                  str(fila["ai_reason"])[:90])
        comprobar("y lo cuenta en el resumen (MM6 y ALTERNADA)",
                  res.get("creadoras_mercado") == 2,
                  f"resumen = {res}")
        comprobar("a la escalonada no la toca",
                  conn.execute("SELECT is_tracked FROM wallets WHERE "
                               "address='ESCALONADA'").fetchone()
                  ["is_tracked"] == 1)
        intacta = conn.execute(
            "SELECT is_tracked FROM wallets WHERE address='JUSTO5'"
        ).fetchone()
        comprobar("a la de 5 vueltas no la toca", intacta["is_tracked"] == 1)
    finally:
        cfg.MM_VUELTAS_MAX = tope_previo
        conn.close()


# ---------------------------------------------------------------------
# OLA 18-K - 3 - /reentrada: enfriamiento por token desde el bot.
# ---------------------------------------------------------------------
def prueba_reentrada():
    bloque("18-K/3 - /reentrada cambia el enfriamiento por token")
    import asyncio as _aio
    import telegram_bot as tb
    from db import get_conn, get_setting, set_setting

    conn = get_conn()
    try:
        comprobar("_f_setting: None -> por defecto",
                  tb._f_setting(None, 24.0) == 24.0)
        comprobar("_f_setting: '' -> por defecto",
                  tb._f_setting("", 24.0) == 24.0)
        comprobar("_f_setting: 'hola' -> por defecto",
                  tb._f_setting("hola", 24.0) == 24.0)
        comprobar("_f_setting: 'nan' -> por defecto (NaN pasa cualquier "
                  "comparacion)",
                  tb._f_setting("nan", 24.0) == 24.0)
        comprobar("_f_setting: '6' -> 6.0", tb._f_setting("6", 24.0) == 6.0)
        comprobar("_f_setting: '0' -> 0.0 (no cae al defecto)",
                  tb._f_setting("0", 24.0) == 0.0)

        dichos = []

        class _Msg:
            chat = None
            async def reply_text(self, txt, **k):
                dichos.append(txt)

        upd = types.SimpleNamespace(message=_Msg(),
                                    effective_user=types.SimpleNamespace(id=1))

        def correr(*args):
            dichos.clear()
            ctx = types.SimpleNamespace(args=list(args))
            _aio.run(tb.cmd_reentrada(upd, ctx))
            return dichos[-1] if dichos else ""

        set_setting(conn, "paper_reentrada_h", "0")
        correr("6")
        comprobar("/reentrada 6 lo guarda",
                  float(get_setting(conn, "paper_reentrada_h", "x")) == 6.0,
                  str(get_setting(conn, "paper_reentrada_h", None)))
        correr("0")
        comprobar("/reentrada 0 lo apaga",
                  float(get_setting(conn, "paper_reentrada_h", "x")) == 0.0)

        set_setting(conn, "paper_reentrada_h", "6")
        for malo in ("-1", "abc", "nan", "inf", "99999"):
            txt = correr(malo)
            comprobar(f"/reentrada {malo} se rechaza y no cambia nada",
                      "Uso:" in txt
                      and float(get_setting(conn, "paper_reentrada_h",
                                            "x")) == 6.0,
                      f"respuesta = {txt[:50]!r}")

        txt = correr()
        comprobar("/reentrada sin argumentos informa del valor actual",
                  "6" in txt, f"respuesta = {txt[:60]!r}")

        # Y el enfriamiento hace su trabajo en paper_trading.
        import paper_trading as pt
        comprobar("paper_trading lee el ajuste",
                  pt._f(conn, "paper_reentrada_h", 24.0) == 6.0,
                  str(pt._f(conn, "paper_reentrada_h", 24.0)))

        # /copiapura ya no gobierna el enfriamiento (auditoria 18-K):
        # antes `on` lo ponia a 0 en silencio, deshaciendo un /reentrada
        # recien dado, y `off` restauraba el valor viejo pisando el nuevo.
        comprobar("el preset de copia pura ya no toca paper_reentrada_h",
                  "paper_reentrada_h" not in tb._COPIA_PURA,
                  str(sorted(tb._COPIA_PURA)))
        import json as _json
        set_setting(conn, "paper_reentrada_h", "6")
        # foto vieja guardada por un `on` de ANTES del cambio
        set_setting(conn, "copia_pura_previo",
                    _json.dumps({"paper_reentrada_h": "24",
                                 "paper_tp_pct": "100"}))
        conn.commit()

        class _Chat:
            async def send_message(self, *a, **k):
                return None
        upd2 = types.SimpleNamespace(
            message=types.SimpleNamespace(chat=_Chat(),
                                          reply_text=_Msg().reply_text),
            effective_user=types.SimpleNamespace(id=1))
        ctx2 = types.SimpleNamespace(args=["off"])
        _aio.run(tb.cmd_copia_pura(upd2, ctx2))
        comprobar("una foto VIEJA de copia pura no pisa el /reentrada nuevo",
                  float(get_setting(conn, "paper_reentrada_h", "x")) == 6.0,
                  str(get_setting(conn, "paper_reentrada_h", None)))
        comprobar("pero el resto de la foto vieja SI se restaura",
                  get_setting(conn, "paper_tp_pct", None) == "100",
                  str(get_setting(conn, "paper_tp_pct", None)))

        # Ronda 2: el residuo del preset VIEJO. Si el `on` antiguo dejo
        # el enfriamiento a 0 y el dueño NUNCA dio /reentrada, el `off`
        # debe restaurar la foto en vez de dejar el 0 huerfano (que
        # perpetuaria las 24 reaperturas del mismo token).
        set_setting(conn, "paper_reentrada_h", "0")
        set_setting(conn, "copia_pura_previo",
                    _json.dumps({"paper_reentrada_h": "24",
                                 "paper_tp_pct": "100"}))
        conn.commit()
        _aio.run(tb.cmd_copia_pura(upd2, types.SimpleNamespace(args=["off"])))
        comprobar("el 0 huerfano del preset viejo SI se restaura en el off",
                  float(get_setting(conn, "paper_reentrada_h", "x")) == 24.0,
                  str(get_setting(conn, "paper_reentrada_h", None)))

        # Ronda 3: un /reentrada 0 DELIBERADO (se guarda como "0.0", no
        # como el "0" literal del preset viejo) se respeta en el off.
        correr("0")
        set_setting(conn, "copia_pura_previo",
                    _json.dumps({"paper_reentrada_h": "24",
                                 "paper_tp_pct": "100"}))
        conn.commit()
        _aio.run(tb.cmd_copia_pura(upd2, types.SimpleNamespace(args=["off"])))
        comprobar("un /reentrada 0 deliberado NO se pisa en el off",
                  float(get_setting(conn, "paper_reentrada_h", "x")) == 0.0,
                  str(get_setting(conn, "paper_reentrada_h", None)))
    finally:
        conn.close()


def main():
    _vigilante()
    prueba_grave1()
    prueba_grave2()
    prueba_grave3()
    prueba_grave4()
    prueba_choque1()
    prueba_choque2()
    prueba_choques34()
    prueba_choque5()
    prueba_recorte()
    prueba_menores()
    prueba_ocultos()
    prueba_rug()
    prueba_polvo()
    prueba_top50()
    prueba_creador_mercado()
    prueba_reentrada()

    print("\n" + "─" * 60)
    if _FALLOS:
        print(f"❌ {len(_FALLOS)} fallos de {_OK + len(_FALLOS)} pruebas:")
        for f in _FALLOS:
            print(f"   · {f}")
        return 1
    print(f"✅ {_OK} pruebas, todas verdes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


