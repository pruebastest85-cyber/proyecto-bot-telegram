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
# GUARDIA (18-M): la suite hace DELETE FROM wallets/signals/trades. Con
# DATABASE_URL puesta, db.py hablaria con ese Postgres y los borrados
# irian contra una base REAL. Se quita SIEMPRE: las pruebas son de
# SQLite temporal, sin excepciones.
os.environ.pop("DATABASE_URL", None)
# (18-P) Y RADAR_SILENCIOSO: la suite fija que el radar viene DE FÁBRICA
# en modo oculto, y eso solo se puede comprobar si el entorno de quien
# ejecuta las pruebas no lo ha cambiado.
os.environ.pop("RADAR_SILENCIOSO", None)
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
        # (19-C) Este cierre lo hace el barrido de mantenimiento: NADIE
        # vendió. Apuntarlo como "venta de la ⭐" mezclaba en el desglose
        # de /paper las copias reales con las tareas de limpieza, y ese
        # desglose es justo lo que el dueño mira para saber qué cierra
        # sus posiciones. El motivo del ESPEJO (el que sí dispara una
        # venta de verdad, caso (a) de esta misma prueba) NO cambia.
        #
        # El miedo de la versión anterior —"un motivo nuevo descuadra el
        # histórico"— no se sostiene contra el código: `resumen_text`
        # hace `GROUP BY exit_reason` sin lista fija de motivos, así que
        # un valor nuevo sale como una línea más y ningún total se
        # calcula a partir de un catálogo cerrado. Se comprueba abajo en
        # vez de darlo por supuesto.
        comprobar("el barrido apunta el cierre con motivo PROPIO, no "
                  "como una venta que nadie hizo",
                  fila["exit_reason"] == "resto de polvo",
                  f"motivo = {fila['exit_reason']!r}")
        _desglose = conn.execute(
            "SELECT exit_reason r, COUNT(*) n FROM paper_trades "
            "WHERE status='cerrada' GROUP BY exit_reason").fetchall()
        _motivos = {r["r"]: r["n"] for r in _desglose}
        _total_cerradas = conn.execute(
            "SELECT COUNT(*) c FROM paper_trades "
            "WHERE status='cerrada'").fetchone()["c"]
        comprobar("y el motivo nuevo aparece en el desglose por su cuenta",
                  _motivos.get("resto de polvo", 0) >= 1, str(_motivos))
        comprobar("sin descuadrar el total: la suma del desglose sigue "
                  "siendo el número de cerradas",
                  sum(_motivos.values()) == _total_cerradas,
                  f"desglose suma {sum(_motivos.values())}, "
                  f"cerradas {_total_cerradas}")
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
                "confirmada, wallet_score, pnl_total, score) "
                "VALUES (?,1,0,1,?,?,0)",
                (f"BUENA{i}", 90 - i, 10.0))
            conn.execute(
                "INSERT INTO positions (wallet, mint, tokens, last_ts) "
                "VALUES (?,?,0,?)", (f"BUENA{i}", f"M{i}", reciente))
        # 4 estrellas que ganan pero llevan 10 dias dormidas: en /top van
        # DESPUES de las activas, pero ANTES de cualquiera que pierda.
        for i in range(4):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "confirmada, wallet_score, pnl_total, score) "
                "VALUES (?,1,0,1,?,?,0)",
                (f"DORMIDA{i}", 99, 10.0))
            conn.execute(
                "INSERT INTO positions (wallet, mint, tokens, last_ts) "
                "VALUES (?,?,0,?)", (f"DORMIDA{i}", f"D{i}", viejo))
        # La del caso real: estrella ACTIVA pero en perdidas. En /top cae
        # por debajo de TODAS las que no pierden -> puesto 11.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, confirmada, "
            "wallet_score, pnl_total, score) "
            "VALUES ('PERDEDORA',1,0,1,44,?,0)",
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
            "INSERT INTO wallets (address, is_tracked, is_bot, confirmada, "
            "wallet_score, pnl_total, score) "
            "VALUES ('BOT1',1,1,1,100,99,0)")
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

        # 18-L: una ⭐ EN PRUEBA (confirmada=0), activa, ganadora y dentro
        # del top, NO alerta: se mide en silencio hasta confirmarse.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, confirmada, "
            "wallet_score, pnl_total, score) "
            "VALUES ('ENPRUEBA',1,0,0,95,60,0)")
        conn.execute(
            "INSERT INTO positions (wallet, mint, tokens, last_ts) "
            "VALUES ('ENPRUEBA','ME',0,?)", (reciente,))
        conn.commit()
        db.invalidar_copiables()
        op2 = db.top_addresses(conn, 10)
        comprobar("una estrella EN PRUEBA no alerta aunque este en el top",
                  "ENPRUEBA" not in op2, f"conjunto = {sorted(op2)}")

        # Ronda 3: las confirmadas van POR DELANTE de las que estan en
        # prueba en el ranking. Sin eso, 60 en-prueba de nota alta
        # empujaban a la unica confirmada fuera del corte y el bot
        # quedaba mudo teniendo calidad disponible.
        for i in range(60):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "confirmada, wallet_score, pnl_total, score) "
                "VALUES (?,1,0,0,99,50,0)", (f"PRUEBA{i:02d}",))
            conn.execute(
                "INSERT INTO positions (wallet, mint, tokens, last_ts) "
                "VALUES (?,?,0,?)", (f"PRUEBA{i:02d}", f"MP{i}", reciente))
        conn.commit()
        db.invalidar_copiables()
        op3 = db.top_addresses(conn, 10)
        comprobar("60 en-prueba de nota alta NO desplazan a las "
                  "confirmadas del corte",
                  all(f"BUENA{i}" in op3 for i in range(6)),
                  f"conjunto = {sorted(op3)}")
        primeros3 = [r["address"] for r in db.top_wallets(conn, 10)]
        comprobar("y /top enseña a las confirmadas primero (espejo)",
                  all(f"BUENA{i}" in primeros3 for i in range(6)),
                  f"top10 = {primeros3}")
        conn.execute("DELETE FROM wallets WHERE address LIKE 'PRUEBA%'")
        conn.execute("DELETE FROM positions WHERE wallet LIKE 'PRUEBA%'")
        conn.commit()
        db.invalidar_copiables()
        conn.execute("DELETE FROM wallets WHERE address='ENPRUEBA'")
        conn.execute("DELETE FROM positions WHERE wallet='ENPRUEBA'")
        conn.commit()
        db.invalidar_copiables()

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
            i_conf = fuente.find("confirmada", i_orden)
            comprobar(f"{dueno}: las confirmadas van primero en el orden",
                      0 <= i_orden < i_conf < i_limit,
                      "no hay confirmada entre ORDER BY y LIMIT")

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

        # Sin red: `familia` llama a Helius de verdad y la cabecera de
        # la suite promete "no necesitan red".
        import wallet_funding as _wf
        _familia_real = _wf.familia
        _wf.familia = lambda a: []
        # Y la depuracion le quita la estrella de verdad. MM6 entra
        # CONFIRMADA para comprobar que la democion tambien limpia la
        # fase: sin eso, una re-promocion futura alertaria al instante
        # con la confirmacion rancia (auditoria 18-L, M1).
        conn.execute("UPDATE wallets SET confirmada=1, prueba_desde=1000 "
                     "WHERE address='MM6'")
        conn.commit()
        import ai_analyst as aa
        res = aa.depurar_estrellas(conn)
        fila = conn.execute(
            "SELECT is_tracked, ai_follow, ai_reason FROM wallets "
            "WHERE address='MM6'").fetchone()
        comprobar("depurar_estrellas le quita la estrella",
                  fila["is_tracked"] == 0, f"is_tracked = {fila['is_tracked']}")
        _fmm = conn.execute("SELECT confirmada, prueba_desde FROM wallets "
                            "WHERE address='MM6'").fetchone()
        comprobar("y le limpia la confirmacion (sin fase rancia)",
                  not _fmm["confirmada"], str(_fmm["confirmada"]))
        comprobar("y el reloj de la prueba (si volviera, ventana fresca)",
                  _fmm["prueba_desde"] is None, str(_fmm["prueba_desde"]))
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
        _wf.familia = _familia_real
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



# ---------------------------------------------------------------------
# OLA 18-L - las tres puertas: la estrella se GANA, no se regala.
# ---------------------------------------------------------------------
def prueba_filtro():
    bloque("18-L - filtro de tres puertas")
    import time as _t
    import config as cfg
    import filtro_calidad as fc
    from db import get_conn

    conn = get_conn()
    ahora = int(_t.time())
    act_previo = cfg.FILTRO_TRES_PUERTAS
    prov_previo = cfg.FILTRO_PROVISIONAL
    cfg.FILTRO_PROVISIONAL = 0        # el grueso se prueba en modo estricto
    try:
        # La tabla `trades` la crea trades_store bajo demanda; en esta
        # base temporal aun no existe.
        from trades_store import _ensure
        _ensure(conn)
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trades")
        conn.commit()

        def op(w, m, side, sol, ts, tokens=100):
            conn.execute(
                "INSERT INTO trades (wallet, signature, mint, side, sol, "
                "tokens, ts) VALUES (?,?,?,?,?,?,?)",
                (w, f"{w}{m}{side}{ts}{sol}", m, side, sol, tokens, ts))

        def posicion(w, m, gana, hold_min=60, hace_dias=10):
            t0 = ahora - hace_dias * 86400
            op(w, m, "compra", 1.0, t0)
            op(w, m, "venta", 1.5 if gana else 0.5,
               t0 + int(hold_min * 60))

        def estrella(w, confirmada=0, prueba_desde=None):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "confirmada, prueba_desde, grade) "
                "VALUES (?,1,0,?,?,'Seguimiento')",
                (w, confirmada, prueba_desde))

        def senal(w, k, chg):
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side, chg_24h) VALUES (?,?,?,1,?,'compra',?)",
                (f"s{w}{k}", w, f"MINT{k}", ahora - 3600 - k, chg))

        # COMPLETA: 12 cerradas, 9 ganadas (75%), hold 60 min, 12 tokens,
        # 6 señales medidas con 4 en positivo (67%). Pasa TODO.
        estrella("COMPLETA")
        for i in range(12):
            posicion("COMPLETA", f"TA{i}", gana=(i < 9), hace_dias=10 + i)
        for i in range(6):
            senal("COMPLETA", i, 50 if i < 4 else -50)

        # SIN_MEDIDAS: mismo historial impecable, pero solo 2 señales
        # medidas -> en prueba (puerta 3).
        estrella("SIN_MEDIDAS")
        for i in range(12):
            posicion("SIN_MEDIDAS", f"TB{i}", gana=(i < 9), hace_dias=10 + i)
        for i in range(2):
            senal("SIN_MEDIDAS", i, 50)

        # WR_BAJO: 12 cerradas pero 5 ganadas (42%) -> puerta 1.
        estrella("WR_BAJO")
        for i in range(12):
            posicion("WR_BAJO", f"TC{i}", gana=(i < 5), hace_dias=10 + i)

        # POCAS: 4 cerradas al 100% -> puerta 1 (historial corto).
        estrella("POCAS")
        for i in range(4):
            posicion("POCAS", f"TD{i}", gana=True, hace_dias=10 + i)

        # SCALPER: WR 75% en 12 cerradas pero hold de 5 min -> puerta 2.
        estrella("SCALPER")
        for i in range(12):
            posicion("SCALPER", f"TE{i}", gana=(i < 9), hold_min=5,
                     hace_dias=10 + i)

        # CONCENTRADA: WR alto, hold 60 min, pero solo 4 tokens (varias
        # vueltas ganadoras a cada uno) -> con el minimo bajado cae por
        # diversificacion.
        estrella("CONCENTRADA")
        for i in range(12):
            posicion("CONCENTRADA", f"TF{i % 4}", gana=(i < 9),
                     hace_dias=10 + i)

        # VENTA_PARCIAL: vende solo el 10% de lo comprado -> NO cuenta
        # como cerrada -> historial corto.
        estrella("VENTA_PARCIAL")
        for i in range(12):
            t0 = ahora - (10 + i) * 86400
            op("VENTA_PARCIAL", f"TG{i}", "compra", 1.0, t0, tokens=100)
            op("VENTA_PARCIAL", f"TG{i}", "venta", 1.5,
               t0 + 3600, tokens=10)

        # ANTIGUA: historial perfecto pero de hace 200 dias (fuera de la
        # ventana de 90) -> historial corto.
        estrella("ANTIGUA")
        for i in range(12):
            posicion("ANTIGUA", f"TH{i}", gana=True, hace_dias=200 + i)

        # NETO_NEGATIVO (18-N, cazado por el dueño): 62% de winrate en
        # cerradas pequeñas... y una bolsa SIN cerrar que lo hunde. El
        # neto de la ventana manda.
        estrella("NETO_NEGATIVO")
        for i in range(12):
            posicion("NETO_NEGATIVO", f"TN{i}", gana=(i < 9),
                     hace_dias=10 + i)      # neto de cerradas: +3.0
        op("NETO_NEGATIVO", "TNX", "compra", 25.0, ahora - 5 * 86400)
        # vende solo el 10%: no cuenta como cerrada, pero el neto la ve
        op("NETO_NEGATIVO", "TNX", "venta", 2.0, ahora - 4 * 86400,
           tokens=10)

        # AIRDROP_MAQUILLA: trading neto -2.5, pero vendio airdrops por
        # +10. Los mints sin compra NO maquillan el neto.
        estrella("AIRDROP_MAQUILLA")
        for i in range(12):
            posicion("AIRDROP_MAQUILLA", f"TO{i}", gana=(i < 9),
                     hace_dias=10 + i)
        op("AIRDROP_MAQUILLA", "TOX", "compra", 6.0, ahora - 5 * 86400)
        op("AIRDROP_MAQUILLA", "TOX", "venta", 0.5, ahora - 4 * 86400,
           tokens=10)          # trading: +3.0 - 5.5 = -2.5
        for i in range(4):     # airdrops vendidos: +10 SOL regalados
            op("AIRDROP_MAQUILLA", f"TOA{i}", "venta", 2.5,
               ahora - 3 * 86400 + i)

        # MEDIDAS_MALAS: historial perfecto pero sus señales medidas
        # pierden (acierto 17%, mediana negativa) -> puerta 3.
        estrella("MEDIDAS_MALAS")
        for i in range(12):
            posicion("MEDIDAS_MALAS", f"TI{i}", gana=(i < 9),
                     hace_dias=10 + i)
        for i in range(6):
            senal("MEDIDAS_MALAS", i, 50 if i < 1 else -60)
        conn.commit()

        # -- puertas() sobre cada caso --
        hist = fc.historial(conn)
        med = fc.medidas(conn)
        casos = {
            "COMPLETA": True, "SIN_MEDIDAS": False, "WR_BAJO": False,
            "POCAS": False, "SCALPER": False, "CONCENTRADA": False,
            "VENTA_PARCIAL": False, "ANTIGUA": False,
            "MEDIDAS_MALAS": False, "NETO_NEGATIVO": False,
            "AIRDROP_MAQUILLA": False,
        }
        for w, esperado in casos.items():
            ok, motivo = fc.puertas(hist.get(w), med.get(w))
            comprobar(f"puertas({w}) = {'pasa' if esperado else 'NO pasa'}",
                      ok is esperado, f"motivo: {motivo}")
        # los motivos señalan la puerta correcta
        comprobar("el motivo de POCAS es el historial corto",
                  "historial corto" in fc.puertas(hist.get("POCAS"),
                                                  None)[1])
        # Estos dos motivos cazan mutaciones que el veredicto global no ve
        # (sin señales medidas, la puerta 3 tapaba el resultado final):
        comprobar("VENTA_PARCIAL cae por historial corto (vender el 10% "
                  "no cierra la posicion)",
                  "historial corto" in fc.puertas(hist.get("VENTA_PARCIAL"),
                                                  None)[1],
                  fc.puertas(hist.get("VENTA_PARCIAL"), None)[1])
        comprobar("ANTIGUA cae por historial corto (fuera de la ventana "
                  "de 90 dias)",
                  "historial corto" in fc.puertas(hist.get("ANTIGUA"),
                                                  None)[1],
                  fc.puertas(hist.get("ANTIGUA"), None)[1])

        # Ronda 2 de la auditoria — los sesgos de la retencion y las
        # transferencias:
        # Su UNICA posicion: acumula 6 dias y vende TODO en 5 minutos.
        estrella("ACUMULADORA")
        base = ahora - 6 * 86400
        for d in range(6):
            op("ACUMULADORA", "TJX", "compra", 1.0, base + d * 86400,
               tokens=100)
        op("ACUMULADORA", "TJX", "venta", 9.0,
           base + 5 * 86400 + 300, tokens=600)
        conn.commit()
        h_ac = fc.historial(conn, "ACUMULADORA").get("ACUMULADORA")
        comprobar("acumular 6 dias y soltar en 5 min cuenta como "
                  "retencion de 5 min, no de dias",
                  h_ac and h_ac["hold_min"] is not None
                  and h_ac["hold_min"] < 10,
                  str(h_ac))
        h_ac2 = fc.historial(conn).get("ACUMULADORA")
        comprobar("y el calculo en LOTE dice lo mismo (misma consulta "
                  "en las dos ramas)",
                  h_ac2 and h_ac2["hold_min"] is not None
                  and h_ac2["hold_min"] < 10,
                  str(h_ac2))

        # TRANSFERIDA: compra 100 fichas con 1 SOL, recibe 400 por
        # transferencia y vende 500 por 3 SOL. Eso NO es un trade
        # medible: no cuenta como cerrada (ni ganada).
        estrella("TRANSFERIDA")
        for i in range(10):
            posicion("TRANSFERIDA", f"TK{i}", gana=True, hace_dias=30 + i)
        t0 = ahora - 5 * 86400
        op("TRANSFERIDA", "TKX", "compra", 1.0, t0, tokens=100)
        op("TRANSFERIDA", "TKX", "venta", 3.0, t0 + 3600, tokens=500)
        conn.commit()
        h_tr = fc.historial(conn, "TRANSFERIDA").get("TRANSFERIDA")
        comprobar("vender 5x lo comprado (transferencias) no cuenta "
                  "como posicion cerrada",
                  h_tr["cerradas"] == 10, str(h_tr))

        # SOLO_VENTAS: un mint sin ninguna compra (airdrop vendido) no
        # cuenta para la diversificacion.
        estrella("SOLO_VENTAS")
        for i in range(3):
            posicion("SOLO_VENTAS", f"TL{i}", gana=True, hace_dias=30 + i)
        for i in range(9):      # 9 airdrops vendidos, sin compra
            op("SOLO_VENTAS", f"TLA{i}", "venta", 0.5,
               ahora - 10 * 86400 + i, tokens=100)
        conn.commit()
        h_sv = fc.historial(conn, "SOLO_VENTAS").get("SOLO_VENTAS")
        comprobar("los airdrops vendidos no cuentan como diversificacion",
                  h_sv["tokens"] == 3, str(h_sv))

        comprobar("el motivo de WR_BAJO es el winrate",
                  "winrate" in fc.puertas(hist.get("WR_BAJO"), None)[1])
        comprobar("el motivo de SCALPER es la retencion",
                  "retención" in fc.puertas(hist.get("SCALPER"), None)[1])
        comprobar("NETO_NEGATIVO cae por perder dinero (la bolsa sin "
                  "cerrar cuenta)",
                  "pierde dinero" in fc.puertas(hist.get("NETO_NEGATIVO"),
                                                None)[1],
                  fc.puertas(hist.get("NETO_NEGATIVO"), None)[1])
        comprobar("los airdrops vendidos NO maquillan el neto",
                  "pierde dinero" in fc.puertas(
                      hist.get("AIRDROP_MAQUILLA"), None)[1],
                  fc.puertas(hist.get("AIRDROP_MAQUILLA"), None)[1])
        # Con un minimo de neto SUBIDO, una que gana poco no "pierde":
        # el motivo tiene que decir la verdad (ronda de auditoria 18-N).
        _neto_previo = cfg.FILTRO_NETO_MIN
        cfg.FILTRO_NETO_MIN = 10.0
        _m_poco = fc.puertas(hist.get("COMPLETA"), None)[1]
        comprobar("con el minimo subido, ganar poco dice 'no gana lo "
                  "suficiente', no 'pierde'",
                  "no gana lo suficiente" in _m_poco
                  and "pierde" not in _m_poco, _m_poco)
        cfg.FILTRO_NETO_MIN = _neto_previo
        # Las posiciones cerradas se cuentan POR TOKEN, asi que con el
        # minimo por defecto (10 cerradas) la puerta de diversificacion
        # solo puede hablar si el dueño baja ese minimo: 10 tokens
        # cerrados ya son mas de 8 distintos. Con los valores por defecto
        # CONCENTRADA cae por historial corto (4 tokens = 4 cerradas), y
        # con el minimo bajado cae por diversificacion. Se prueban los dos.
        comprobar("CONCENTRADA con umbral por defecto cae por historial",
                  "historial corto" in fc.puertas(hist.get("CONCENTRADA"),
                                                  None)[1],
                  fc.puertas(hist.get("CONCENTRADA"), None)[1])
        _cer_previo = cfg.FILTRO_MIN_CERRADAS
        cfg.FILTRO_MIN_CERRADAS = 3
        comprobar("CONCENTRADA con minimo bajado cae por diversificacion",
                  "tokens operados" in fc.puertas(hist.get("CONCENTRADA"),
                                                  None)[1],
                  fc.puertas(hist.get("CONCENTRADA"), None)[1])
        cfg.FILTRO_MIN_CERRADAS = _cer_previo
        comprobar("el motivo de SIN_MEDIDAS es la prueba",
                  "en prueba" in fc.puertas(hist.get("SIN_MEDIDAS"),
                                            med.get("SIN_MEDIDAS"))[1])
        comprobar("el motivo de MEDIDAS_MALAS son las señales",
                  "señales medidas malas" in fc.puertas(
                      hist.get("MEDIDAS_MALAS"),
                      med.get("MEDIDAS_MALAS"))[1])
        # historial(conn, wallet) coincide con el lote
        comprobar("historial de una en una = lote",
                  fc.historial(conn, "COMPLETA").get("COMPLETA")
                  == hist.get("COMPLETA"),
                  f"{fc.historial(conn, 'COMPLETA')}")

        # -- clasificar() escribe la fase --
        res = fc.clasificar(conn)
        f = {r["address"]: r for r in conn.execute(
            "SELECT address, is_tracked, confirmada, prueba_desde, "
            "ai_reason FROM wallets").fetchall()}
        comprobar("clasificar confirma SOLO a la completa",
                  res["confirmadas"] == 1
                  and f["COMPLETA"]["confirmada"] == 1,
                  f"resumen = {res}")
        comprobar("y escribe el motivo de la confirmacion",
                  "confirmada" in (f["COMPLETA"]["ai_reason"] or ""),
                  str(f["COMPLETA"]["ai_reason"])[:80])
        # Con la ficha LLENA (500 caracteres) el motivo nuevo tiene que
        # seguir siendo visible: va al principio, no al final (ronda 2).
        conn.execute("UPDATE wallets SET confirmada=0, ai_reason=? "
                     "WHERE address='COMPLETA'", ("x" * 500,))
        conn.commit()
        fc.clasificar(conn)
        _lleno = conn.execute("SELECT ai_reason FROM wallets "
                              "WHERE address='COMPLETA'").fetchone()
        comprobar("el motivo nuevo es visible aunque la ficha este llena",
                  (_lleno["ai_reason"] or "").startswith("✅ confirmada"),
                  (_lleno["ai_reason"] or "")[:60])
        f = {r["address"]: r for r in conn.execute(
            "SELECT address, is_tracked, confirmada, prueba_desde "
            "FROM wallets").fetchall()}
        comprobar("las demas quedan en prueba con el reloj corriendo",
                  all(f[w]["confirmada"] == 0
                      and f[w]["prueba_desde"] is not None
                      for w in casos if w != "COMPLETA"),
                  str({w: (f[w]["confirmada"], f[w]["prueba_desde"])
                       for w in casos if w != "COMPLETA"}))
        comprobar("ninguna pierde la estrella el primer dia de prueba",
                  all(f[w]["is_tracked"] == 1 for w in casos))

        # -- una confirmada que deja de cumplir vuelve a prueba --
        conn.execute("DELETE FROM signals WHERE wallet='COMPLETA'")
        conn.commit()
        fc.clasificar(conn)
        f2 = conn.execute(
            "SELECT confirmada, ai_reason FROM wallets "
            "WHERE address='COMPLETA'").fetchone()
        comprobar("una confirmada que pierde sus medidas vuelve a prueba",
                  f2["confirmada"] == 0,
                  str(f2["ai_reason"])[:80])
        comprobar("y el motivo queda en la ficha",
                  "vuelve a prueba" in (f2["ai_reason"] or ""),
                  str(f2["ai_reason"])[:80])

        # -- retiro por inactividad: prueba vieja y sin señales --
        estrella("DORMIDA_PRUEBA", confirmada=0,
                 prueba_desde=ahora - 20 * 86400)
        estrella("VIVA_PRUEBA", confirmada=0,
                 prueba_desde=ahora - 20 * 86400)
        senal("VIVA_PRUEBA", 99, 10)     # opero ayer: se queda
        conn.commit()
        fc.clasificar(conn)
        f3 = {r["address"]: r for r in conn.execute(
            "SELECT address, is_tracked, ai_reason FROM wallets "
            "WHERE address IN ('DORMIDA_PRUEBA','VIVA_PRUEBA')").fetchall()}
        comprobar("en prueba 20 dias sin operar: pierde la estrella",
                  f3["DORMIDA_PRUEBA"]["is_tracked"] == 0,
                  str(f3["DORMIDA_PRUEBA"]["ai_reason"])[:70])
        _rd = conn.execute("SELECT prueba_desde FROM wallets "
                           "WHERE address='DORMIDA_PRUEBA'").fetchone()
        comprobar("y el retiro limpia el reloj (si vuelve, ventana fresca)",
                  _rd["prueba_desde"] is None, str(_rd["prueba_desde"]))
        comprobar("en prueba pero operando: conserva la estrella",
                  f3["VIVA_PRUEBA"]["is_tracked"] == 1)

        # -- interruptor maestro --
        cfg.FILTRO_TRES_PUERTAS = 0
        res_off = fc.clasificar(conn)
        n_conf = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1 "
            "AND confirmada=1").fetchone()["c"]
        n_est = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1"
        ).fetchone()["c"]
        comprobar("interruptor apagado: TODAS las estrellas confirman",
                  n_conf == n_est and res_off["interruptor"] == "apagado",
                  f"{n_conf}/{n_est}")
        cfg.FILTRO_TRES_PUERTAS = act_previo

        # -- /filtro no revienta y dice los numeros --
        txt = fc.resumen(conn)
        comprobar("el resumen de /filtro menciona las tres puertas",
                  "1️⃣" in txt and "2️⃣" in txt and "3️⃣" in txt,
                  txt[:80])
        # El CONTADOR de la puerta 1 tambien se vigila (auditoria 18-N):
        # era el unico de los cuatro sitios con la condicion del neto sin
        # prueba. Con las sembradas de este bloque, pasan cerradas+WR+neto
        # exactamente 5: COMPLETA, SIN_MEDIDAS, SCALPER, MEDIDAS_MALAS y
        # TRANSFERIDA (NETO_NEGATIVO y AIRDROP_MAQUILLA caen por el neto;
        # si cambias las sembradas, recalcula este numero a mano).
        _linea1 = [l for l in txt.split("\n") if l.startswith("1️⃣")][0]
        comprobar("el contador de la puerta 1 descuenta a las del neto "
                  "negativo (pasan 5)",
                  "pasan 5" in _linea1, _linea1)

        # -- el consenso del camino caliente exige confirmada --
        import inspect, realtime
        fuente = inspect.getsource(realtime._proc)
        comprobar("el consenso solo cuenta billeteras confirmadas",
                  "COALESCE(w.confirmada, 0) = 1" in fuente,
                  "falta el filtro en la consulta del consenso")

        # -- modo PROVISIONAL (el arranque que eligio el dueño) --
        cfg.FILTRO_PROVISIONAL = 1
        ok_p, m_p = fc.puertas(hist.get("SIN_MEDIDAS"),
                               med.get("SIN_MEDIDAS"))
        comprobar("provisional: historial+copiable sin medidas CONFIRMA",
                  ok_p is True and m_p.startswith("provisional"),
                  f"{ok_p} / {m_p}")
        comprobar("provisional: las medidas MALAS siguen tumbando",
                  fc.puertas(hist.get("MEDIDAS_MALAS"),
                             med.get("MEDIDAS_MALAS"))[0] is False)
        comprobar("provisional: el historial flojo NO confirma",
                  fc.puertas(hist.get("WR_BAJO"),
                             med.get("WR_BAJO"))[0] is False)
        comprobar("provisional: el scalper NO confirma",
                  fc.puertas(hist.get("SCALPER"),
                             med.get("SCALPER"))[0] is False)
        # Y el historial CORTO tampoco, aunque lo demas parezca perfecto:
        # 4 cerradas al 100% con buen hold y 9 tokens comprados. El
        # provisional solo salta la puerta 3 — nunca la 1 ni la 2.
        estrella("CORTA_PROV")
        for i in range(4):
            posicion("CORTA_PROV", f"TM{i}", gana=True, hace_dias=20 + i)
        for i in range(5):      # 5 compras abiertas: diversifica sin cerrar
            op("CORTA_PROV", f"TMA{i}", "compra", 1.0,
               ahora - 8 * 86400 + i)
        conn.commit()
        _h_cp = fc.historial(conn, "CORTA_PROV").get("CORTA_PROV")
        ok_cp, m_cp = fc.puertas(_h_cp, None)
        comprobar("provisional: el historial corto NO confirma "
                  "(la puerta 1 no se salta)",
                  ok_cp is False and "historial corto" in m_cp,
                  f"{ok_cp} / {m_cp} / {_h_cp}")
        # El bloque del interruptor maestro acaba de confirmar a TODAS en
        # bloque; se resetea SIN_MEDIDAS para ver la transicion con motivo.
        conn.execute("UPDATE wallets SET confirmada=0, ai_reason=NULL "
                     "WHERE address='SIN_MEDIDAS'")
        conn.commit()
        fc.clasificar(conn)
        f4 = conn.execute(
            "SELECT confirmada, ai_reason FROM wallets "
            "WHERE address='SIN_MEDIDAS'").fetchone()
        comprobar("provisional: clasificar la marca confirmada",
                  f4["confirmada"] == 1, str(f4["ai_reason"])[:70])
        comprobar("provisional: el motivo dice que es provisional",
                  "provisional" in (f4["ai_reason"] or ""),
                  str(f4["ai_reason"])[:70])
        # y cuando llegan las medidas y son malas, la pierde
        for i in range(6):
            senal("SIN_MEDIDAS", 100 + i, -60)
        conn.commit()
        fc.clasificar(conn)
        f5 = conn.execute(
            "SELECT confirmada FROM wallets "
            "WHERE address='SIN_MEDIDAS'").fetchone()
        comprobar("provisional: 6 medidas malas le quitan la confirmacion",
                  f5["confirmada"] == 0, str(f5["confirmada"]))

        # -- base sin la tabla `trades` (recien nacida): nada revienta --
        conn.execute("DROP TABLE trades")
        conn.commit()
        try:
            h_frio = fc.historial(conn, "COMPLETA")
            r_frio = fc.resumen(conn)
            c_frio = fc.clasificar(conn)
            ok_frio = isinstance(h_frio, dict) and "🚪" in r_frio \
                and isinstance(c_frio, dict)
        except Exception as e:
            ok_frio = False
            r_frio = str(e)
        comprobar("sin tabla trades: historial/resumen/clasificar "
                  "funcionan (la crean ellos)",
                  ok_frio, str(r_frio)[:80])

        # -- contrato: la migracion de `confirmada` corre AL ARRANCAR --
        # No se puede re-arrancar db.py dentro de la suite (la conexion
        # global ya esta preparada), asi que se mira la fuente: si la
        # columna sale de las listas de migracion, una base ya desplegada
        # se quedaria sin ella hasta el primer ciclo de la IA y
        # `top_addresses` devolveria None = ALERTAN TODAS (fail-open que
        # cazo la auditoria).
        import inspect
        import db as _dbm
        _f_sq = inspect.getsource(_dbm._preparar_sqlite)
        _f_pg = inspect.getsource(_dbm._preparar_pg)
        comprobar("la migracion de arranque de SQLite incluye confirmada "
                  "y prueba_desde",
                  '"confirmada"' in _f_sq and '"prueba_desde"' in _f_sq)
        comprobar("la de Postgres tambien",
                  '"confirmada"' in _f_pg and '"prueba_desde"' in _f_pg)
        # Y la clasificacion inicial corre AL ARRANCAR en los dos
        # motores: sin ella, entre el deploy y el primer ciclo todas las
        # ⭐ tienen confirmada NULL y el bot queda MUDO (ronda 2).
        comprobar("el arranque de SQLite clasifica al preparar la base",
                  "clasificar(conn)" in _f_sq)
        comprobar("el de Postgres tambien",
                  "clasificar(pg)" in _f_pg)
    finally:
        cfg.FILTRO_TRES_PUERTAS = act_previo
        cfg.FILTRO_PROVISIONAL = prov_previo
        conn.close()


# ---------------------------------------------------------------------
# OLA 18-M - estabilidad: seis grietas pequeñas, cerradas.
# ---------------------------------------------------------------------
def prueba_18m():
    bloque("18-M - estabilidad")
    import io as _io
    import inspect
    import realtime
    import signal_tracker as st
    import db as _dbm

    # 1) simbolo saneado para la tarjeta
    comprobar("_sym_md limpia * _ ` [ ]",
              realtime._sym_md("A*B_C`D[E]") == "AB CD(E)",
              repr(realtime._sym_md("A*B_C`D[E]")))
    comprobar("_sym_md con None devuelve ?",
              realtime._sym_md(None) == "?")
    comprobar("_sym_md con solo basura devuelve ?",
              realtime._sym_md("*`*") == "?",
              repr(realtime._sym_md("*`*")))
    comprobar("_sym_md deja en paz un simbolo normal",
              realtime._sym_md("BONK") == "BONK")
    fuente_proc = inspect.getsource(realtime._proc)
    comprobar("la tarjeta usa el simbolo SANEADO (sym), no el crudo",
              "💎 *{sym}*" in fuente_proc,
              "la linea de la tarjeta no usa {sym}")

    # 2) track_outcomes cierra la conexion AUNQUE la pasada reviente
    real_inner = st._track_outcomes
    conexiones = []
    real_get = st.get_conn

    def _get_espia():
        c = real_get()
        conexiones.append(c)
        return c

    def _revienta(conn):
        raise RuntimeError("pasada rota a proposito")
    st.get_conn = _get_espia
    st._track_outcomes = _revienta
    try:
        try:
            st.track_outcomes()
            exploto = False
        except RuntimeError:
            exploto = True
        cerrada = False
        if conexiones:
            try:
                conexiones[0].execute("SELECT 1")
            except Exception:
                cerrada = True
        comprobar("una pasada rota propaga el error (no lo esconde)",
                  exploto)
        comprobar("y la conexion queda CERRADA igualmente",
                  cerrada, "la conexion seguia abierta")
    finally:
        st._track_outcomes = real_inner
        st.get_conn = real_get

    # 3) el arranque dice la verdad sobre el webhook segun PUBLIC_URL
    _f_tg = _io.open("telegram_bot.py", encoding="utf-8").read()
    comprobar("el arranque distingue con y sin PUBLIC_URL",
              "sin PUBLIC_URL no hay webhook" in _f_tg
              and _f_tg.count('print("📡 LaserStream activo') == 2,
              f"ramas: {_f_tg.count(chr(39)+chr(34)+'LaserStream')}")
    comprobar("el mensaje viejo (mentira en el PC) ya no existe",
              "webhook sigue como respaldo" not in _f_tg)

    # 4) el supervisor usa el reloj monotonico, no la hora de pared
    _f_sup = _io.open("supervisor.py", encoding="utf-8").read()
    comprobar("supervisor sin time.time() (solo monotonico)",
              "time.time()" not in _f_sup
              and _f_sup.count("time.monotonic()") >= 8,
              f"time(): {_f_sup.count('time.time()')} · "
              f"monotonic: {_f_sup.count('time.monotonic()')}")

    # 5) lock_timeout en los indices tardios (solo Postgres)
    _f_idx = inspect.getsource(_dbm._crear_indices_tardios)
    comprobar("los indices tardios ponen lock_timeout en Postgres",
              "lock_timeout = '5s'" in _f_idx
              and "lock_timeout = DEFAULT" in _f_idx
              and "USE_PG" in _f_idx,
              "falta el SET '5s', el DEFAULT o la guarda USE_PG")

    # 6) el alias va saneado en los dos mensajes del paper
    _f_pt = _io.open("paper_trading.py", encoding="utf-8").read()
    comprobar("el paper sanea el alias en 'Copiando a' y en la venta "
              "parcial",
              _f_pt.count("_md(_nom)") >= 2,
              f"_md(_nom) aparece {_f_pt.count('_md(_nom)')} veces")


# ---------------------------------------------------------------------
# OLA 18-M - /reembudo: re-evaluar TODAS con el embudo, de un golpe.
# ---------------------------------------------------------------------
def prueba_reembudo():
    bloque("18-M - /reembudo re-evalua todas de un golpe")
    import asyncio as _aio
    import time as _t
    import config as cfg
    import filtro_calidad as fc
    from db import get_conn

    conn = get_conn()
    ahora = int(_t.time())
    act_previo = cfg.FILTRO_TRES_PUERTAS
    prov_previo = cfg.FILTRO_PROVISIONAL
    cfg.FILTRO_TRES_PUERTAS = 1
    cfg.FILTRO_PROVISIONAL = 1
    try:
        from trades_store import _ensure
        _ensure(conn)
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trades")
        conn.commit()

        def op(w, m, side, sol, ts, tokens=100):
            conn.execute(
                "INSERT INTO trades (wallet, signature, mint, side, sol, "
                "tokens, ts) VALUES (?,?,?,?,?,?,?)",
                (w, f"{w}{m}{side}{ts}{sol}", m, side, sol, tokens, ts))

        def posicion(w, m, gana, hold_min=60, hace_dias=10):
            t0 = ahora - hace_dias * 86400
            op(w, m, "compra", 1.0, t0)
            op(w, m, "venta", 1.5 if gana else 0.5,
               t0 + int(hold_min * 60))

        def estrella(w):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "confirmada, grade) VALUES (?,1,0,0,'Seguimiento')", (w,))

        # PASA_TODO: historial impecable + 6 medidas buenas -> confirmada
        estrella("PASA_TODO")
        for i in range(12):
            posicion("PASA_TODO", f"RA{i}", gana=(i < 9), hace_dias=10 + i)
        for i in range(6):
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side, chg_24h) VALUES (?,?,?,1,?,'compra',?)",
                (f"rs{i}", "PASA_TODO", f"RM{i}", ahora - 3600 - i,
                 50 if i < 4 else -50))
        # PASA_12: historial impecable sin medidas -> provisional
        estrella("PASA_12")
        for i in range(12):
            posicion("PASA_12", f"RB{i}", gana=(i < 9), hace_dias=10 + i)
        # FALLA_WR / FALLA_HOLD / FALLA_POCAS: no pasan el historial
        estrella("FALLA_WR")
        for i in range(12):
            posicion("FALLA_WR", f"RC{i}", gana=(i < 5), hace_dias=10 + i)
        estrella("FALLA_HOLD")
        for i in range(12):
            posicion("FALLA_HOLD", f"RD{i}", gana=(i < 9), hold_min=5,
                     hace_dias=10 + i)
        estrella("FALLA_POCAS")
        for i in range(3):
            posicion("FALLA_POCAS", f"RE{i}", gana=True, hace_dias=10 + i)
        # FALLA_NETO (18-N): winrate alto en cerradas pero una bolsa sin
        # cerrar la deja en negativo -> el corte del reembudo la tira.
        estrella("FALLA_NETO")
        for i in range(12):
            posicion("FALLA_NETO", f"RN{i}", gana=(i < 9),
                     hace_dias=10 + i)
        op("FALLA_NETO", "RNX", "compra", 25.0, ahora - 5 * 86400)
        op("FALLA_NETO", "RNX", "venta", 2.0, ahora - 4 * 86400,
           tokens=10)
        conn.commit()

        # -- ensayo: cuenta bien y NO toca nada --
        res = fc.reevaluacion(conn, ejecutar=False)
        comprobar("ensayo: 4 caerian y 2 sobreviven",
                  res["caen"] == 4 and res["sobreviven"] == 2
                  and not res["ejecutado"], str(res)[:120])
        _mot_neto = {w: m for w, _a, m in res["detalle_caen"]}.get(
            "FALLA_NETO", "")
        comprobar("la del neto negativo cae por 'pierde dinero'",
                  "pierde dinero" in _mot_neto, _mot_neto)
        n_est = conn.execute("SELECT COUNT(*) c FROM wallets WHERE "
                             "is_tracked=1").fetchone()["c"]
        comprobar("el ensayo no quita ninguna estrella", n_est == 6,
                  f"quedan {n_est}")
        comprobar("el ensayo lista a las supervivientes",
                  {w for w, _a in res["detalle_viven"]}
                  == {"PASA_TODO", "PASA_12"},
                  str(res["detalle_viven"]))
        motivos = {w: m for w, _a, m in res["detalle_caen"]}
        comprobar("cada caida lleva su motivo del historial",
                  "winrate" in motivos["FALLA_WR"]
                  and "retención" in motivos["FALLA_HOLD"]
                  and "historial corto" in motivos["FALLA_POCAS"],
                  str(motivos))

        # -- ejecutar: descarta, conserva y clasifica --
        res2 = fc.reevaluacion(conn, ejecutar=True)
        comprobar("ejecutar lo marca como ejecutado", res2["ejecutado"])
        f = {r["address"]: r for r in conn.execute(
            "SELECT address, is_tracked, confirmada, ai_reason "
            "FROM wallets").fetchall()}
        comprobar("las 4 que no pasan el historial pierden la estrella",
                  all(f[w]["is_tracked"] == 0 for w in
                      ("FALLA_WR", "FALLA_HOLD", "FALLA_POCAS",
                       "FALLA_NETO")),
                  str({w: f[w]["is_tracked"] for w in motivos}))
        comprobar("y el motivo queda al principio de la ficha",
                  (f["FALLA_WR"]["ai_reason"] or "")
                  .startswith("🧹 re-evaluación del embudo"),
                  str(f["FALLA_WR"]["ai_reason"])[:60])
        comprobar("las 2 supervivientes conservan la estrella",
                  f["PASA_TODO"]["is_tracked"] == 1
                  and f["PASA_12"]["is_tracked"] == 1)
        comprobar("y quedan clasificadas: la completa confirmada",
                  f["PASA_TODO"]["confirmada"] == 1,
                  str(f["PASA_TODO"]["ai_reason"])[:60])
        comprobar("y la sin-medidas confirmada provisional",
                  f["PASA_12"]["confirmada"] == 1
                  and "provisional" in (f["PASA_12"]["ai_reason"] or ""),
                  str(f["PASA_12"]["ai_reason"])[:60])

        # -- con el interruptor apagado se niega --
        cfg.FILTRO_TRES_PUERTAS = 0
        res3 = fc.reevaluacion(conn, ejecutar=True)
        comprobar("con el filtro apagado devuelve error y no actua",
                  bool(res3.get("error")), str(res3)[:80])
        cfg.FILTRO_TRES_PUERTAS = 1

        # -- el mando /reembudo: ensayo y ejecucion --
        # Sin red: la depuracion previa del "si" pasa por `familia`.
        import wallet_funding as _wf
        _familia_real = _wf.familia
        _wf.familia = lambda a: []
        import types as _types
        import telegram_bot as tb
        # reponer una que cae, para que la ejecucion del mando tenga algo
        conn.execute("UPDATE wallets SET is_tracked=1 "
                     "WHERE address='FALLA_WR'")
        conn.commit()
        mensajes = []

        class _Chat:
            async def send_message(self, txt, **k):
                mensajes.append(txt)
        upd = _types.SimpleNamespace(
            message=_types.SimpleNamespace(chat=_Chat()),
            effective_user=_types.SimpleNamespace(id=1))
        _aio.run(tb.cmd_reembudo(upd, _types.SimpleNamespace(args=[])))
        comprobar("/reembudo (ensayo) responde con numeros y sin tocar",
                  mensajes and "Ensayo" in mensajes[-1]
                  and conn.execute(
                      "SELECT is_tracked FROM wallets WHERE "
                      "address='FALLA_WR'").fetchone()["is_tracked"] == 1,
                  (mensajes[-1][:70] if mensajes else "sin mensaje"))
        _aio.run(tb.cmd_reembudo(upd, _types.SimpleNamespace(args=["si"])))
        comprobar("/reembudo si ejecuta de verdad",
                  "EJECUTADA" in mensajes[-1]
                  and conn.execute(
                      "SELECT is_tracked FROM wallets WHERE "
                      "address='FALLA_WR'").fetchone()["is_tracked"] == 0,
                  mensajes[-1][:70])
        comprobar("y el mensaje informa del estado FINAL (quedan/confirmadas)",
                  "quedan" in mensajes[-1] and "confirmadas" in mensajes[-1],
                  mensajes[-1][:90])

        # Hallazgo de la auditoria: una superviviente del corte que cae
        # justo despues por inactividad NO debe contarse como "queda".
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trades")
        conn.commit()
        estrella("VIEJA_MALA")          # pasa 1-2, medidas malas, dormida
        for i in range(12):
            posicion("VIEJA_MALA", f"RF{i}", gana=(i < 9),
                     hace_dias=30 + i)
        for i in range(6):
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side, chg_24h) VALUES (?,?,?,1,?,'compra',?)",
                (f"vm{i}", "VIEJA_MALA", f"VM{i}",
                 ahora - 20 * 86400 - i, -60))
        conn.execute("UPDATE wallets SET prueba_desde=? "
                     "WHERE address='VIEJA_MALA'", (ahora - 20 * 86400,))
        conn.commit()
        r4 = fc.reevaluacion(conn, ejecutar=True)
        comprobar("el corte la da por superviviente (pasa 1-2)...",
                  r4["sobreviven"] == 1, str(r4)[:100])
        comprobar("...pero 'quedan' dice la verdad tras clasificar "
                  "(la retiro la inactividad)",
                  r4.get("quedan") == 0,
                  f"quedan={r4.get('quedan')}")

        # La guardia de DATABASE_URL de la cabecera: sin ella, correr la
        # suite con esa variable puesta lanzaria los DELETE contra un
        # Postgres REAL. Se comprueba sobre el propio fuente porque en
        # el entorno de pruebas la variable no existe y el sintoma no es
        # observable.
        import io as _io2
        _fuente_suite = _io2.open(__file__, encoding="utf-8").read()
        # Se mira solo la CABECERA (2.000 primeros caracteres): la
        # propia cadena de esta prueba no cuenta como guardia. Y ademas
        # se comprueba el EFECTO en runtime: comentar la linea dejaria
        # el texto presente pero la variable viva.
        import os as _os3
        comprobar("la suite quita DATABASE_URL antes de tocar nada",
                  'os.environ.pop("DATABASE_URL"' in _fuente_suite[:2000]
                  and "DATABASE_URL" not in _os3.environ)

        # Ensayo con MAS de 50 supervivientes: la lista se corta y el
        # mensaje lo dice ("y N más") en vez de callarselo.
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM trades")
        conn.commit()
        for k in range(52):
            estrella(f"MASIVA{k:02d}")
            for i in range(12):
                posicion(f"MASIVA{k:02d}", f"MM{k}_{i}", gana=(i < 9),
                         hace_dias=10 + i)
        conn.commit()
        mensajes.clear()
        _aio.run(tb.cmd_reembudo(upd, _types.SimpleNamespace(args=[])))
        comprobar("con 52 supervivientes el ensayo avisa 'y 2 más'",
                  "y 2 más" in mensajes[-1],
                  mensajes[-1][-120:])
    finally:
        try:
            _wf.familia = _familia_real
        except Exception:
            pass
        cfg.FILTRO_TRES_PUERTAS = act_previo
        cfg.FILTRO_PROVISIONAL = prov_previo
        conn.close()


# ---------------------------------------------------------------------
# OLA 18-O - los 5 hallazgos de la auditoria transversal del 28/8.
# ---------------------------------------------------------------------
def prueba_18o_rastrear():
    bloque("18-O/1 - /rastrear deja de deshacerse solo")
    import time as _t
    import signal_tracker as st
    import wallet_admin as wa
    import realtime as rt
    from db import get_conn

    conn = get_conn()
    ahora = int(_t.time())
    _hook_real = wa.sync_helius_webhook
    _tg_real = getattr(rt, "tg_send", None)
    _hook_rt_real = rt.sync_helius_webhook
    wa.sync_helius_webhook = lambda: "(webhook de prueba)"
    rt.tg_send = lambda *a, **k: None
    rt.sync_helius_webhook = lambda *a, **k: None
    try:
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trades")
        conn.commit()

        def senal(w, i, chg, ts):
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side, chg_24h) VALUES (?,?,?,1,?,'compra',?)",
                (f"o{w}{i}", w, f"M{w}{i}", ts, chg))

        # VIEJA: 4 señales rojas ANTIGUAS y ningun turno nuevo.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, pnl_30d, "
            "ai_reason) VALUES ('VIEJA',1,0,-5,'ficha previa')")
        for i in range(4):
            senal("VIEJA", i, -30, ahora - 10 * 86400 + i)
        conn.commit()
        st._check_streaks(conn)
        f = conn.execute("SELECT is_tracked, ai_reason FROM wallets "
                         "WHERE address='VIEJA'").fetchone()
        comprobar("sin turno nuevo, la racha SIGUE degradando (no se "
                  "afloja nada)", f["is_tracked"] == 0,
                  str(f["is_tracked"]))
        comprobar("y el motivo se antepone SIN borrar la ficha anterior",
                  (f["ai_reason"] or "").startswith("📉 sin ⭐")
                  and "ficha previa" in (f["ai_reason"] or ""),
                  str(f["ai_reason"])[:120])
        comprobar("y NO dice 'de este turno' cuando no hay turno (esa se "
                  "juzgo por toda su vida)",
                  "de este turno" not in (f["ai_reason"] or ""),
                  str(f["ai_reason"])[:120])

        # RESTAURADA: mismas señales viejas, pero el dueño la restauro.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, pnl_30d, "
            "grade) VALUES ('RESTAURADA',0,1,-5,'Descartada')")
        for i in range(4):
            senal("RESTAURADA", i, -30, ahora - 10 * 86400 + i)
        conn.commit()
        msg = wa.restore_wallet("RESTAURADA")
        fr = conn.execute(
            "SELECT is_tracked, is_bot, confirmada, prueba_desde, grade "
            "FROM wallets WHERE address='RESTAURADA'").fetchone()
        comprobar("/rastrear devuelve la estrella", fr["is_tracked"] == 1)
        comprobar("y NO el altavoz (entra en prueba)",
                  not fr["confirmada"] and fr["prueba_desde"],
                  f"confirmada={fr['confirmada']} desde={fr['prueba_desde']}")
        comprobar("y borra la nota vieja ('Descartada' ya no la tumba)",
                  fr["grade"] is None, str(fr["grade"]))
        comprobar("y avisa de que hoy no pasa el embudo",
                  "⚠️" in msg and "historial corto" in msg, msg[-160:])

        # El aviso mira las TRES puertas: el caso mas frecuente es una
        # billetera con buen historial degradada por su rendimiento
        # MEDIDO, y esas señales no caducan.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot) "
            "VALUES ('REST3',0,1)")
        for i in range(12):    # historial que SI pasa las puertas 1-2
            t0 = ahora - (10 + i) * 86400
            for side, sol, ts in (("compra", 1.0, t0),
                                  ("venta", 1.6 if i < 9 else 0.4,
                                   t0 + 90 * 60)):
                conn.execute(
                    "INSERT INTO trades (wallet, signature, mint, side, "
                    "sol, tokens, ts) VALUES ('REST3',?,?,?,?,100,?)",
                    (f"r3{i}{side}", f"R3M{i}", side, sol, ts))
        for i in range(8):     # pero 8 medidas malas del bot
            senal("REST3", i, -70, ahora - 30 * 86400 + i)
        conn.commit()
        msg3 = wa.restore_wallet("REST3")
        comprobar("el aviso llega tambien cuando lo que falla es la "
                  "puerta 3 (señales medidas)",
                  "⚠️" in msg3 and "medidas malas" in msg3, msg3[-200:])
        comprobar("y NO le promete que /reembudo se la llevara (solo "
                  "retira a quien falla el historial)",
                  "/reembudo NO se la llevaría" in msg3, msg3[-200:])
        comprobar("mientras que a la de historial corto si se lo dice",
                  "/reembudo se la llevaría" in msg
                  and "NO se la llevaría" not in msg, msg[-200:])

        # La retención vieja tampoco puede tumbarla: el bloque 1 de la
        # depuración lee `hold_median_min`, del perfilado ANTERIOR.
        # Se limpia SOLO al restaurar de verdad (estando fuera).
        conn.execute("UPDATE wallets SET hold_median_min=2.0, "
                     "grade='Descartada', is_tracked=0 "
                     "WHERE address='REST3'")
        conn.commit()
        wa.restore_wallet("REST3")
        _fh = conn.execute(
            "SELECT hold_median_min, grade FROM wallets "
            "WHERE address='REST3'").fetchone()
        comprobar("/rastrear borra la retencion y la nota viejas",
                  _fh["hold_median_min"] is None and _fh["grade"] is None,
                  str(dict(_fh)))

        # Pero sobre una ⭐ VIVA no toca nada suyo: quitarle la
        # confirmacion la dejaria muda hasta 2 h, y la nota y la
        # retencion solo vuelven si entra otra vez en la cola de
        # perfilado, cosa que no esta garantizada.
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=1, "
                     "grade='Elite', hold_median_min=95.0 "
                     "WHERE address='REST3'")
        conn.commit()
        msg_viva = wa.restore_wallet("REST3")
        _fv = conn.execute(
            "SELECT confirmada, grade, hold_median_min FROM wallets "
            "WHERE address='REST3'").fetchone()
        comprobar("/rastrear sobre una ⭐ viva no la desconfirma ni le "
                  "borra la nota ni la retencion",
                  _fv["confirmada"] == 1 and _fv["grade"] == "Elite"
                  and _fv["hold_median_min"] == 95.0, str(dict(_fv)))
        comprobar("y se lo dice al dueño, nombrando lo que conserva",
                  "Ya la tenías" in msg_viva
                  and "la confirmación" in msg_viva
                  and "Elite" in msg_viva, msg_viva[-260:])
        comprobar("y la enumeracion se lee como español (a, b y c)",
                  "la confirmación, la nota (Elite) y la retención"
                  in msg_viva, msg_viva[-260:])

        # Y si esa ⭐ viva viene CONFIRMADA pero no pasa el embudo, el
        # aviso no puede decirle que esta muda: lo esta alertando.
        conn.execute("DELETE FROM trades WHERE wallet='REST3'")
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=1 "
                     "WHERE address='REST3'")
        conn.commit()
        print("DEBUG estado:", dict(conn.execute(
            "SELECT is_tracked, confirmada FROM wallets "
            "WHERE address='REST3'").fetchone()))
        msg_conf = wa.restore_wallet("REST3")
        print("DEBUG msg:", repr(msg_conf))
        comprobar("a una confirmada el aviso NO le dice que esta muda",
                  "puede alertar y copiarse" in msg_conf
                  and "no alerta ni se copia" not in msg_conf,
                  msg_conf[-260:])

        # EL CASO DE HOY: ⭐ viva con el reloj del turno en NULL (todas
        # las heredadas de antes de esta ola). /rastrear le estrena reloj
        # IGUAL que a una descartada — si no, el mando no protegeria de
        # nada justo en la poblacion real — y el mensaje lo dice.
        conn.execute("DELETE FROM signals WHERE wallet='REST3'")
        conn.execute("UPDATE wallets SET is_tracked=1, turno_desde=NULL, "
                     "prueba_desde=?, ai_reason='ficha valiosa' "
                     "WHERE address='REST3'", (ahora - 9 * 86400,))
        for i in range(10):
            senal("REST3", 50 + i, -40, ahora - 3600 + i)
        conn.commit()
        import performance_review as pv_r3
        _antes_g = pv_r3.perdedora_confirmada(conn, "REST3")
        msg_null = wa.restore_wallet("REST3")
        _fnull = conn.execute(
            "SELECT turno_desde, ai_reason FROM wallets "
            "WHERE address='REST3'").fetchone()
        comprobar("a una ⭐ viva SIN reloj de turno se le estrena uno",
                  _fnull["turno_desde"] is not None,
                  str(_fnull["turno_desde"]))
        comprobar("y con el la guarda anti-re-promocion deja de verle el "
                  "expediente viejo (para eso sirve el mando)",
                  (_antes_g is not None)
                  and pv_r3.perdedora_confirmada(conn, "REST3") is None,
                  f"antes={_antes_g} ahora="
                  f"{pv_r3.perdedora_confirmada(conn, 'REST3')}")
        comprobar("y el mensaje avisa de que su expediente deja de contar",
                  "dejan de contar en su contra" in msg_null,
                  msg_null[:220])
        comprobar("y la ficha se antepone en vez de borrarse",
                  "ficha valiosa" in (_fnull["ai_reason"] or ""),
                  str(_fnull["ai_reason"])[:120])

        # Y si arrastra una nota o una retencion que la van a tumbar, se
        # avisa (a la viva no se le borran, asi que el ciclo las lee).
        conn.execute("UPDATE wallets SET grade='Descartada', "
                     "hold_median_min=1.0 WHERE address='REST3'")
        conn.commit()
        msg_her = wa.restore_wallet("REST3")
        comprobar("y se avisa de la nota y la retencion heredadas",
                  "Arrastra del perfilado anterior" in msg_her
                  and "Descartada" in msg_her, msg_her[-260:])
        comprobar("y NO se le promete que empezara a alertar (esos "
                  "bloques corren ANTES de la clasificacion)",
                  "empezará a alertar" not in msg_her, msg_her[-260:])
        comprobar("el aviso nombra tambien las reglas que la re-evaluacion "
                  "puede volver a aplicarle (bot/entidad)",
                  "como bot" in msg_her, msg_her[-260:])
        comprobar("y el mensaje va en texto plano, sin marcas de formato "
                  "sueltas (/rastrear no manda parse_mode)",
                  "_(" not in msg_her and ")_" not in msg_her,
                  msg_her[-160:])
        conn.execute("DELETE FROM signals WHERE wallet='REST3'")
        conn.execute("UPDATE wallets SET grade=NULL, hold_median_min=NULL "
                     "WHERE address='REST3'")
        conn.commit()
        conn.execute("UPDATE wallets SET is_tracked=0, confirmada=0 "
                     "WHERE address='REST3'")
        conn.commit()

        # Y si es CREADORA DE MERCADO, se le dice claro: esa regla del
        # dueño no la levanta /rastrear.
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot) "
            "VALUES ('RESTMM',0,1)")
        for k in range(24):        # 12 vueltas compra→venta al mismo token
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side) VALUES (?, 'RESTMM','TOKMM',1,?,?)",
                (f"mm{k}", ahora - 3600 + k,
                 "compra" if k % 2 == 0 else "venta"))
        conn.commit()
        msgmm = wa.restore_wallet("RESTMM")
        comprobar("a la creadora de mercado se le avisa de que el ciclo "
                  "se la va a quitar igual",
                  "CREADORA DE MERCADO" in msgmm, msgmm[-200:])

        # Con el embudo APAGADO no hay aviso: todas confirman y
        # /reembudo se niega a correr, asi que avisar seria mentir.
        cfg_ra = __import__("config")
        _act_prev = cfg_ra.FILTRO_TRES_PUERTAS
        cfg_ra.FILTRO_TRES_PUERTAS = 0
        try:
            conn.execute("UPDATE wallets SET is_tracked=0 "
                         "WHERE address='RESTAURADA'")
            conn.commit()
            msg_ap = wa.restore_wallet("RESTAURADA")
        finally:
            cfg_ra.FILTRO_TRES_PUERTAS = _act_prev
        comprobar("con el embudo apagado /rastrear no avisa de puertas",
                  "⚠️" not in msg_ap, msg_ap[-160:])
        comprobar("ni promete que 'pasa el embudo' cuando no hay embudo",
                  "Pasa el embudo" not in msg_ap, msg_ap[-160:])

        # A la que PASA el embudo y venia de fuera se le dice que
        # empezara a alertar en cuanto la clasifiquen: antes se quedaba
        # muda hasta 2 h sin que el mensaje dijera nada.
        conn.execute("DELETE FROM signals WHERE wallet='REST3'")
        conn.execute("UPDATE wallets SET is_tracked=0, confirmada=0 "
                     "WHERE address='REST3'")
        for i in range(12):
            t0 = ahora - (10 + i) * 86400
            for side, sol, ts in (("compra", 1.0, t0),
                                  ("venta", 1.6, t0 + 90 * 60)):
                conn.execute(
                    "INSERT INTO trades (wallet, signature, mint, side, "
                    "sol, tokens, ts) VALUES ('REST3',?,?,?,?,100,?)",
                    (f"ok{i}{side}", f"OKM{i}", side, sol, ts))
        conn.commit()
        msg_ok = wa.restore_wallet("REST3")
        comprobar("a la que SI pasa el embudo se le avisa de que hablara "
                  "en cuanto la clasifiquen",
                  "empezará a alertar" in msg_ok and "⚠️" not in msg_ok,
                  msg_ok[-260:])
        # Y si ademas es creadora de mercado, esa promesa NO puede salir.
        conn.execute("DELETE FROM signals WHERE wallet='REST3'")
        for k in range(24):
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side) VALUES (?, 'REST3','TOKMM2',1,?,?)",
                (f"mm2x{k}", ahora - 7200 + k,
                 "compra" if k % 2 == 0 else "venta"))
        conn.execute("UPDATE wallets SET is_tracked=0, confirmada=0 "
                     "WHERE address='REST3'")
        conn.commit()
        msg_ok_mm = wa.restore_wallet("REST3")
        comprobar("pero si es creadora de mercado esa promesa NO sale "
                  "(la depuracion corre antes que la clasificacion)",
                  "empezará a alertar" not in msg_ok_mm
                  and "CREADORA DE MERCADO" in msg_ok_mm,
                  msg_ok_mm[-260:])
        conn.execute("DELETE FROM signals WHERE wallet='REST3'")
        conn.commit()
        conn.execute("DELETE FROM trades WHERE wallet='REST3'")
        conn.commit()

        # Y a la que estaba fuera, lo mismo.
        conn.execute("UPDATE wallets SET is_tracked=0, turno_desde=NULL "
                     "WHERE address='REST3'")
        conn.commit()
        wa.restore_wallet("REST3")
        # El plazo de inactividad tambien se reinicia SIEMPRE, incluida
        # una ⭐ VIVA: si no, una que llevara 14 dias parada la retiraria
        # la clasificacion justo despues de que el dueño la pidiera.
        conn.execute("UPDATE wallets SET is_tracked=1, "
                     "prueba_desde=? WHERE address='REST3'",
                     (ahora - 40 * 86400,))
        conn.commit()
        wa.restore_wallet("REST3")
        comprobar("/rastrear reinicia el plazo de inactividad tambien a "
                  "una ⭐ viva (si no, la clasificacion la retiraria)",
                  conn.execute("SELECT prueba_desde FROM wallets WHERE "
                               "address='REST3'").fetchone()["prueba_desde"]
                  >= ahora - 60, "reloj de prueba viejo")
        # Y `confirmada` nunca se queda en NULL (defensa en profundidad:
        # el conjunto operativo la evalua como falsa, pero un NULL ahi es
        # el estado huerfano que la reparacion de arranque limpia).
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=NULL "
                     "WHERE address='REST3'")
        conn.commit()
        wa.restore_wallet("REST3")
        comprobar("y `confirmada` nunca se queda en NULL",
                  conn.execute("SELECT confirmada FROM wallets WHERE "
                               "address='REST3'").fetchone()["confirmada"]
                  == 0, "quedo NULL")
        comprobar("y a la que estaba fuera tambien se le pone",
                  conn.execute("SELECT turno_desde FROM wallets WHERE "
                               "address='REST3'").fetchone()["turno_desde"],
                  "sigue NULL")

        st._check_streaks(conn)
        f = conn.execute("SELECT is_tracked FROM wallets "
                         "WHERE address='RESTAURADA'").fetchone()
        comprobar("la racha VIEJA ya no deshace el /rastrear del dueño",
                  f["is_tracked"] == 1, str(f["is_tracked"]))

        # Pero si vuelve a fallar EN SU TURNO NUEVO, cae igual.
        # (el `or ahora` es para que una regresión que borre el reloj no
        # tumbe la suite con un TypeError y esconda los demás fallos)
        _desde = conn.execute("SELECT prueba_desde FROM wallets WHERE "
                              "address='RESTAURADA'").fetchone()[0] or ahora
        conn.execute("UPDATE wallets SET is_tracked=1 WHERE "
                     "address='RESTAURADA'")
        for i in range(4):
            senal("RESTAURADA", 10 + i, -30, int(_desde) + 1 + i)
        conn.commit()
        st._check_streaks(conn)
        f = conn.execute("SELECT is_tracked, ai_reason FROM wallets "
                         "WHERE address='RESTAURADA'").fetchone()
        comprobar("y si falla otra vez con señales NUEVAS, cae igual",
                  f["is_tracked"] == 0, str(f["is_tracked"]))
        comprobar("con un motivo que habla de ESTE turno",
                  "de este turno" in (f["ai_reason"] or ""),
                  str(f["ai_reason"])[:120])

        # La revision de rendimiento respeta el mismo turno.
        import performance_review as pv
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, alias, "
            "turno_desde, ai_reason) VALUES "
            "('REVISADA',1,0,'Revisada',?,'ficha previa')",
            (ahora - 300,))
        for i in range(10):        # 10 medidas MALAS, todas del turno viejo
            senal("REVISADA", i, -40, ahora - 5 * 86400 + i)
        conn.commit()
        r = pv.review_tracked(notify=False)
        f = conn.execute("SELECT is_tracked FROM wallets "
                         "WHERE address='REVISADA'").fetchone()
        comprobar("la revisión de rendimiento tampoco juzga con el "
                  "expediente del turno anterior",
                  f["is_tracked"] == 1 and r["degradadas"] == 0, str(r))
        for i in range(10):        # ahora 10 medidas malas de ESTE turno
            senal("REVISADA", 20 + i, -40, ahora - 100 + i)
        conn.commit()
        r = pv.review_tracked(notify=False)
        f = conn.execute("SELECT is_tracked, ai_reason FROM wallets "
                         "WHERE address='REVISADA'").fetchone()
        comprobar("pero con señales malas de SU turno sí degrada",
                  f["is_tracked"] == 0 and r["degradadas"] == 1, str(r))
        comprobar("y el motivo se antepone sin borrar la ficha",
                  (f["ai_reason"] or "").startswith("📉 sin ⭐")
                  and "ficha previa" in (f["ai_reason"] or ""),
                  str(f["ai_reason"])[:120])

        # Si otro hilo se adelanta y le quita la ⭐ a MITAD de la
        # comprobacion, ni la racha ni la revision pueden anunciar una
        # degradacion que no hicieron (el UPDATE lleva is_tracked=1 y se
        # mira el rowcount).
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, alias, "
            "pnl_30d) VALUES ('ROBADA',1,0,'Robada',-5)")
        for i in range(10):
            senal("ROBADA", i, -40, ahora - 3600 + i)
        conn.commit()
        avisos_tg = []
        _tg_prev = rt.tg_send
        rt.tg_send = lambda *a, **k: avisos_tg.append(a[0] if a else "")
        _getconn_prev = pv.get_conn
        try:
            st._check_streaks(_ConnRoba(
                conn, "ROBADA", "is_tracked, alias, turno_desde"))
            comprobar("la racha no avisa de una degradacion que no hizo",
                      avisos_tg == [], str(avisos_tg))
            conn.execute("UPDATE wallets SET is_tracked=1 "
                         "WHERE address='ROBADA'")
            conn.commit()
            pv.get_conn = lambda: _ConnRoba(
                conn, "ROBADA", "address, alias, turno_desde")
            r_ya = pv.review_tracked(notify=False)
            comprobar("y la revision no la cuenta como degradada",
                      r_ya["degradadas"] == 0, str(r_ya))
        finally:
            rt.tg_send = _tg_prev
            pv.get_conn = _getconn_prev

        # Las dos degradaciones tienen que refrescar el conjunto
        # operativo YA: sin esto la ⭐ degradada seguia alertando y
        # copiandose hasta 60 s (el TTL de la cache).
        import db as _db
        avisos = {"copiables": 0, "vigiladas": 0}
        _ic_real, _iv_real = _db.invalidar_copiables, rt.invalidar_vigiladas

        def _ic():
            avisos["copiables"] += 1

        def _iv():
            avisos["vigiladas"] += 1

        _db.invalidar_copiables = _ic
        rt.invalidar_vigiladas = _iv
        try:
            conn.execute("DELETE FROM wallets")
            conn.execute("DELETE FROM signals")
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "pnl_30d) VALUES ('CACHE',1,0,-5)")
            for i in range(4):
                senal("CACHE", i, -30, ahora - 3600 + i)
            conn.commit()
            st._check_streaks(conn)
            comprobar("la racha refresca el conjunto operativo",
                      avisos["copiables"] >= 1 and avisos["vigiladas"] >= 1,
                      str(avisos))
            avisos["copiables"] = avisos["vigiladas"] = 0
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, alias, "
                "pnl_30d) VALUES ('CACHE2',1,0,'C2',-5)")
            for i in range(10):
                senal("CACHE2", i, -40, ahora - 3600 + i)
            conn.commit()
            pv.review_tracked(notify=False)
            comprobar("y la revisión de rendimiento también",
                      avisos["copiables"] >= 1 and avisos["vigiladas"] >= 1,
                      str(avisos))
        finally:
            _db.invalidar_copiables = _ic_real
            rt.invalidar_vigiladas = _iv_real
    finally:
        # Se devuelven TODOS los dobles: si `realtime.sync_helius_webhook`
        # se queda anulado, cualquier prueba posterior corre con el
        # webhook desactivado en silencio.
        wa.sync_helius_webhook = _hook_real
        rt.sync_helius_webhook = _hook_rt_real
        rt.tg_send = _tg_real
        conn.close()


def prueba_18o_promocion():
    bloque("18-O/2 - la IA no puede dar la estrella saltandose el embudo")
    import time as _t
    import config as cfg
    import ai_analyst as aa
    import wallet_funding as wf
    import wallet_identity as wi
    import wallet_score as ws
    from db import get_conn

    conn = get_conn()
    ahora = int(_t.time())
    previos = {
        "perfil": aa.profile_wallet, "bot": aa._hard_bot_reason,
        "veredicto": aa.ai_verdict, "nota": aa.nota_bloquea,
        "id": wi.identificar, "mot": wi.motivo_exclusion,
        "nueva": wf.recien_creada, "herm": wf.hermanas,
        "jefa": wf.hermana_con_estrella, "destr": wf.destronar_hermanas,
        "score": ws.compute_score,
        "puerta": cfg.FILTRO_PUERTA_PROMOCION,
        "cerradas": cfg.FILTRO_MIN_CERRADAS,
    }
    import trades_store as _ts
    previos["tope_trades"] = _ts.MAX_TRADES_TOTAL
    try:
        aa.profile_wallet = lambda a: {
            "tx_sampled": 200, "pnl_30d_sol": 9.0, "pnl_total_sol": 30.0,
            "unrealized_sol": 0.0, "net_pnl_sol": 30.0,
            "hold_median_min": 120.0, "metrics": {"roi_median": 1.5},
            "tokens": {}}
        aa._hard_bot_reason = lambda p: None
        aa.ai_verdict = lambda *a, **k: {
            "clasificacion": "trader", "seguir": True, "confianza": 90,
            "alias": None, "razon": "veredicto de prueba",
            "modelo": "doble"}
        aa.nota_bloquea = lambda conn_, tier: False
        wi.identificar = lambda dirs: {}
        wi.motivo_exclusion = lambda x: None
        wf.recien_creada = lambda a: (False, 999.0, "")
        wf.hermanas = lambda a: []
        wf.hermana_con_estrella = lambda c, a, s: None
        wf.destronar_hermanas = lambda c, a: []
        ws.compute_score = lambda p, t: {"score": 70.0}
        cfg.FILTRO_MIN_CERRADAS = 10

        from trades_store import _ensure
        _ensure(conn)
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM appearances")
        conn.commit()

        def candidata(w):
            conn.execute(
                "INSERT INTO wallets (address, winning_tokens_count, "
                "is_tracked, is_bot, score) VALUES (?,3,0,0,10)", (w,))
            conn.execute(
                "INSERT INTO appearances (wallet, mint, reason, buy_sol, "
                "entry_multiple) VALUES (?,?,'prueba',5.0,9.0)",
                (w, f"AP{w}"))

        def posicion(w, m, gana, hold_min=90, hace_dias=10):
            t0 = ahora - hace_dias * 86400
            for side, sol, ts in (("compra", 1.0, t0),
                                  ("venta", 1.6 if gana else 0.4,
                                   t0 + int(hold_min * 60))):
                conn.execute(
                    "INSERT INTO trades (wallet, signature, mint, side, "
                    "sol, tokens, ts) VALUES (?,?,?,?,?,100,?)",
                    (w, f"{w}{m}{side}", m, side, sol, ts))

        # BUENA: 12 cerradas, 9 ganadas (75%), neto positivo, 12 tokens.
        candidata("PROMO_BUENA")
        for i in range(12):
            posicion("PROMO_BUENA", f"B{i}", gana=(i < 9), hace_dias=10 + i)
        # MALA: 12 cerradas pero solo 3 ganadas (25%) y neto negativo.
        candidata("PROMO_MALA")
        for i in range(12):
            posicion("PROMO_MALA", f"L{i}", gana=(i < 3), hace_dias=10 + i)
        # CORTA: historial de 4 posiciones, impecable pero insuficiente.
        candidata("PROMO_CORTA")
        for i in range(4):
            posicion("PROMO_CORTA", f"C{i}", gana=True, hace_dias=10 + i)
        conn.commit()

        aa.evaluate_tracked(conn)
        est = {r["address"]: r for r in conn.execute(
            "SELECT address, is_tracked, confirmada, prueba_desde, "
            "turno_desde, ai_reason FROM wallets")}
        comprobar("la que pasa el historial SÍ recibe la estrella",
                  est["PROMO_BUENA"]["is_tracked"] == 1,
                  str(est["PROMO_BUENA"]["ai_reason"])[:120])
        comprobar("y entra EN PRUEBA, no confirmada",
                  not est["PROMO_BUENA"]["confirmada"]
                  and est["PROMO_BUENA"]["prueba_desde"],
                  str(dict(est["PROMO_BUENA"])))
        comprobar("y con el reloj del turno en marcha (desde ahora "
                  "cuentan sus señales)",
                  est["PROMO_BUENA"]["turno_desde"],
                  str(est["PROMO_BUENA"]["turno_desde"]))
        comprobar("a la rechazada se le borra el reloj del turno (si "
                  "volviera, se la juzgaria por todo su historial)",
                  est["PROMO_MALA"]["turno_desde"] is None,
                  str(est["PROMO_MALA"]["turno_desde"]))
        comprobar("la de winrate 25% NO recibe estrella aunque la IA "
                  "diga que sí", est["PROMO_MALA"]["is_tracked"] == 0,
                  str(est["PROMO_MALA"]["ai_reason"])[:120])
        comprobar("y la ficha dice en qué puerta se quedó",
                  "winrate" in (est["PROMO_MALA"]["ai_reason"] or ""),
                  str(est["PROMO_MALA"]["ai_reason"])[:120])
        comprobar("la de historial corto tampoco",
                  est["PROMO_CORTA"]["is_tracked"] == 0
                  and "historial corto" in
                  (est["PROMO_CORTA"]["ai_reason"] or ""),
                  str(est["PROMO_CORTA"]["ai_reason"])[:120])

        # A una ⭐ QUE YA TIENE la estrella la guarda no la toca: es lo
        # que hace que /rastrear del dueño valga para algo (si no, el
        # ciclo se la quitaba a las 2 h y el mando volvia a ser inutil).
        conn.execute("UPDATE wallets SET is_tracked=1, ai_class=NULL, "
                     "pnl_updated=NULL WHERE address='PROMO_MALA'")
        conn.commit()
        aa.evaluate_tracked(conn)
        _fya = conn.execute(
            "SELECT is_tracked, turno_desde FROM wallets "
            "WHERE address='PROMO_MALA'").fetchone()
        comprobar("a la ⭐ ya puesta (p. ej. restaurada a mano) la guarda "
                  "NO se la quita", _fya["is_tracked"] == 1)
        comprobar("y a una ⭐ HEREDADA (sin reloj de turno) la IA no le "
                  "inventa uno: nada de amnistia al desplegar",
                  _fya["turno_desde"] is None, str(_fya["turno_desde"]))
        conn.execute("UPDATE wallets SET is_tracked=0, ai_class=NULL, "
                     "pnl_updated=NULL WHERE address='PROMO_MALA'")
        conn.commit()

        # Una fila con la confirmacion puesta pero SIN estrella (estado
        # que existio antes de 18-O) no puede entrar al altavoz en el
        # momento de la promocion: la ⭐ nueva empieza siempre en prueba.
        conn.execute("UPDATE wallets SET is_tracked=0, confirmada=1, "
                     "turno_desde=NULL, prueba_desde=NULL, "
                     "ai_class=NULL, pnl_updated=NULL "
                     "WHERE address='PROMO_BUENA'")
        conn.commit()
        aa.evaluate_tracked(conn)
        _fhu = conn.execute(
            "SELECT is_tracked, confirmada FROM wallets "
            "WHERE address='PROMO_BUENA'").fetchone()
        comprobar("una fila con confirmacion huerfana no alerta al "
                  "recibir la ⭐: entra en prueba",
                  _fhu["is_tracked"] == 1 and not _fhu["confirmada"],
                  str(dict(_fhu)))

        # Si otro hilo la degrada JUSTO despues del UPDATE grande de la
        # promocion, el reloj del turno no puede quedarse puesto: seria
        # una ⭐ degradada con reloj, y con el la guarda anti-re-promocion
        # se queda ciega.
        conn.execute("UPDATE wallets SET is_tracked=0, turno_desde=NULL, "
                     "prueba_desde=NULL, ai_class=NULL, pnl_updated=NULL "
                     "WHERE address='PROMO_BUENA'")
        conn.commit()
        aa.evaluate_tracked(_ConnRoba(
            conn, "PROMO_BUENA", "UPDATE wallets SET ai_class=?"))
        _fcar = conn.execute(
            "SELECT is_tracked, turno_desde, prueba_desde FROM wallets "
            "WHERE address='PROMO_BUENA'").fetchone()
        comprobar("una degradacion a mitad de la promocion no deja reloj "
                  "de turno puesto",
                  not (_fcar["turno_desde"] and not _fcar["is_tracked"]),
                  str(dict(_fcar)))
        comprobar("ni el reloj de la prueba (si volviera, le regalaria "
                  "el plazo entero y un motivo de retiro falso)",
                  not (_fcar["prueba_desde"] and not _fcar["is_tracked"]),
                  str(dict(_fcar)))
        conn.execute("UPDATE wallets SET is_tracked=1 "
                     "WHERE address='PROMO_BUENA'")
        conn.commit()

        # Y al reves: si el dueño hace /rastrear JUSTO cuando la IA la
        # esta degradando, el borrado de fase no puede pisar la estrella
        # recien restaurada (en Postgres las dos escrituras no son
        # atomicas).
        conn.execute("UPDATE wallets SET is_tracked=1, turno_desde=NULL, "
                     "prueba_desde=NULL, ai_class=NULL, pnl_updated=NULL "
                     "WHERE address='PROMO_MALA'")
        conn.commit()
        _ver_prev2 = aa.ai_verdict
        aa.ai_verdict = lambda *a, **k: {
            "clasificacion": "indeterminado", "seguir": False,
            "confianza": 80, "alias": None, "razon": "no",
            "modelo": "doble"}
        try:
            aa.evaluate_tracked(_ConnRastreo(
                conn, "PROMO_MALA", ahora, "UPDATE wallets SET ai_class=?"))
        finally:
            aa.ai_verdict = _ver_prev2
        _fres = conn.execute(
            "SELECT is_tracked, turno_desde FROM wallets "
            "WHERE address='PROMO_MALA'").fetchone()
        comprobar("un /rastrear a mitad de la degradacion no se queda sin "
                  "reloj de turno",
                  not (_fres["is_tracked"] and not _fres["turno_desde"]),
                  str(dict(_fres)))
        conn.execute("UPDATE wallets SET is_tracked=0, turno_desde=NULL, "
                     "ai_class=NULL, pnl_updated=NULL "
                     "WHERE address='PROMO_MALA'")
        conn.commit()

        # Al QUITAR la estrella se borra el reloj del turno: si volviera
        # mas adelante, se la juzgaria por TODO su historial otra vez y
        # no con media pagina en blanco.
        conn.execute("UPDATE wallets SET is_tracked=1, turno_desde=?, "
                     "ai_class=NULL, pnl_updated=NULL "
                     "WHERE address='PROMO_MALA'", (ahora - 86400,))
        conn.commit()
        _ver_prev = aa.ai_verdict
        aa.ai_verdict = lambda *a, **k: {
            "clasificacion": "indeterminado", "seguir": False,
            "confianza": 80, "alias": None, "razon": "la IA dice que no",
            "modelo": "doble"}
        aa.evaluate_tracked(conn)
        aa.ai_verdict = _ver_prev
        _fdeg = conn.execute(
            "SELECT is_tracked, turno_desde FROM wallets "
            "WHERE address='PROMO_MALA'").fetchone()
        comprobar("al quitar la ⭐ se borra el reloj del turno",
                  _fdeg["is_tracked"] == 0
                  and _fdeg["turno_desde"] is None, str(dict(_fdeg)))
        conn.execute("UPDATE wallets SET is_tracked=0, ai_class=NULL, "
                     "pnl_updated=NULL WHERE address='PROMO_MALA'")
        conn.commit()

        # Con la tabla `trades` en su tope, la guarda NO se apaga: la
        # poda deja la tabla clavada EN el tope, asi que apagarse ahi
        # seria apagarse para siempre. Se AVISA y se sigue juzgando.
        import io as _io
        import contextlib as _ctx
        _tope_prev = _ts.MAX_TRADES_TOTAL
        _ts.MAX_TRADES_TOTAL = 1
        _salida = _io.StringIO()
        try:
            with _ctx.redirect_stdout(_salida):
                aa.evaluate_tracked(conn)
        finally:
            _ts.MAX_TRADES_TOTAL = _tope_prev
        comprobar("con `trades` en su tope la guarda SIGUE aplicandose",
                  conn.execute("SELECT is_tracked FROM wallets WHERE "
                               "address='PROMO_MALA'").fetchone()
                  ["is_tracked"] == 0)
        comprobar("pero avisa al dueño de que hay que subir el tope",
                  "MAX_TRADES_TOTAL" in _salida.getvalue(),
                  _salida.getvalue()[-200:])
        conn.execute("UPDATE wallets SET is_tracked=0, ai_class=NULL, "
                     "pnl_updated=NULL WHERE address='PROMO_MALA'")
        conn.commit()

        # El interruptor de apagado devuelve el comportamiento anterior.
        cfg.FILTRO_PUERTA_PROMOCION = 0
        conn.execute("UPDATE wallets SET ai_class=NULL, pnl_updated=NULL")
        conn.commit()
        aa.evaluate_tracked(conn)
        comprobar("FILTRO_PUERTA_PROMOCION=0 apaga la guarda",
                  conn.execute("SELECT is_tracked FROM wallets WHERE "
                               "address='PROMO_MALA'").fetchone()
                  ["is_tracked"] == 1)
    finally:
        aa.profile_wallet = previos["perfil"]
        aa._hard_bot_reason = previos["bot"]
        aa.ai_verdict = previos["veredicto"]
        aa.nota_bloquea = previos["nota"]
        wi.identificar = previos["id"]
        wi.motivo_exclusion = previos["mot"]
        wf.recien_creada = previos["nueva"]
        wf.hermanas = previos["herm"]
        wf.hermana_con_estrella = previos["jefa"]
        wf.destronar_hermanas = previos["destr"]
        ws.compute_score = previos["score"]
        cfg.FILTRO_PUERTA_PROMOCION = previos["puerta"]
        cfg.FILTRO_MIN_CERRADAS = previos["cerradas"]
        _ts.MAX_TRADES_TOTAL = previos["tope_trades"]
        conn.close()


class _CursorFalso:
    """Cursor con filas ya materializadas (para simular una carrera)."""

    def __init__(self, filas):
        self._f = filas

    def fetchall(self):
        return self._f

    def fetchone(self):
        return self._f[0] if self._f else None


class _ConnRoba:
    """Conexión que le quita la ⭐ a una billetera JUSTO despues de que
    el código lea su fila: sirve para probar los candados
    `AND is_tracked = 1` + `rowcount` de la racha y de la revisión."""

    def __init__(self, real, victima, fragmento):
        self._real = real
        self._victima = victima
        self._frag = fragmento
        self.robada = False

    def execute(self, sql, params=()):
        cur = self._real.execute(sql, params)
        if not self.robada and self._frag in " ".join(sql.split()):
            filas = cur.fetchall()
            self._real.execute(
                "UPDATE wallets SET is_tracked = 0 WHERE address = ?",
                (self._victima,))
            self._real.commit()
            self.robada = True
            return _CursorFalso(filas)
        return cur

    def commit(self):
        return self._real.commit()

    def close(self):
        pass            # la conexión real la cierra quien la abrió


class _ConnRastreo:
    """Conexión que simula un /rastrear del dueño JUSTO en medio de la
    clasificación: le pone reloj nuevo a la billetera despues de que la
    pasada leyera el viejo."""

    def __init__(self, real, victima, nuevo_reloj, fragmento=None):
        self._real = real
        self._victima = victima
        self._reloj = nuevo_reloj
        self._frag = fragmento or "MAX(s.ts) AS ult"
        self.disparado = False

    def execute(self, sql, params=()):
        cur = self._real.execute(sql, params)
        if not self.disparado and self._frag in " ".join(sql.split()):
            filas = cur.fetchall()
            # Un /rastrear completo: estrella y reloj de turno nuevos.
            self._real.execute(
                "UPDATE wallets SET prueba_desde = ?, is_tracked = 1, "
                "turno_desde = ? WHERE address = ?",
                (self._reloj, self._reloj, self._victima))
            self._real.commit()
            self.disparado = True
            return _CursorFalso(filas)
        return cur

    def commit(self):
        return self._real.commit()

    def close(self):
        pass


class _ConnCarrera:
    """Conexión que, justo DESPUÉS de que `clasificar` lea la lista de
    estrellas, le quita la ⭐ a una de ellas — la carrera real entre el
    hilo del ciclo y el que degrada (racha, /descartar, revisión)."""

    def __init__(self, real, victima):
        self._real = real
        self._victima = victima
        self.robada = False

    def execute(self, sql, params=()):
        cur = self._real.execute(sql, params)
        # Se roba la ⭐ en la ÚLTIMA consulta previa al bucle (la de la
        # última señal): así el historial ya está calculado y la
        # billetera llega a la línea de la confirmación, que es
        # justamente lo que hay que probar.
        if not self.robada and "MAX(s.ts) AS ult" in " ".join(sql.split()):
            filas = cur.fetchall()
            self._real.execute(
                "UPDATE wallets SET is_tracked = 0 WHERE address = ?",
                (self._victima,))
            self._real.commit()
            self.robada = True
            return _CursorFalso(filas)
        return cur

    def commit(self):
        return self._real.commit()


def prueba_18o_carrera():
    bloque("18-O/3 - la confirmacion no resucita a una ⭐ ya degradada")
    import time as _t
    import config as cfg
    import filtro_calidad as fc
    from db import get_conn

    conn = get_conn()
    ahora = int(_t.time())
    act_previo = cfg.FILTRO_TRES_PUERTAS
    cer_previo = cfg.FILTRO_MIN_CERRADAS
    cfg.FILTRO_TRES_PUERTAS = 1
    cfg.FILTRO_MIN_CERRADAS = 10
    try:
        from trades_store import _ensure
        _ensure(conn)
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trades")
        conn.commit()
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, confirmada) "
            "VALUES ('CARRERA',1,0,0)")
        for i in range(12):
            t0 = ahora - (10 + i) * 86400
            for side, sol, ts in (("compra", 1.0, t0),
                                  ("venta", 1.6 if i < 9 else 0.4,
                                   t0 + 90 * 60)):
                conn.execute(
                    "INSERT INTO trades (wallet, signature, mint, side, "
                    "sol, tokens, ts) VALUES ('CARRERA',?,?,?,?,100,?)",
                    (f"c{i}{side}", f"CM{i}", side, sol, ts))
        conn.commit()

        proxy = _ConnCarrera(conn, "CARRERA")
        fc.clasificar(proxy)
        comprobar("la carrera se disparó (la ⭐ se pierde a mitad)",
                  proxy.robada)
        fcar = conn.execute("SELECT is_tracked, confirmada FROM wallets "
                            "WHERE address='CARRERA'").fetchone()
        comprobar("no queda el estado imposible is_tracked=0 + "
                  "confirmada=1", not (fcar["confirmada"] and
                                       not fcar["is_tracked"]),
                  f"is_tracked={fcar['is_tracked']} "
                  f"confirmada={fcar['confirmada']}")

        # Sin carrera, la misma billetera SÍ se confirma (la condición
        # nueva no rompe el camino normal).
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=0 "
                     "WHERE address='CARRERA'")
        conn.commit()
        fc.clasificar(conn)
        fok = conn.execute("SELECT confirmada FROM wallets "
                           "WHERE address='CARRERA'").fetchone()
        comprobar("y sin carrera se confirma como siempre",
                  fok["confirmada"] == 1, str(fok["confirmada"]))

        # DOS RELOJES, DOS COSAS (ronda 4). `prueba_desde` es el plazo
        # de inactividad y la clasificacion lo reinicia; `turno_desde`
        # dice desde cuando cuentan las señales y NADIE de aqui lo toca.
        # Cuando eran la misma columna, la ⭐ que empezaba a fallar se
        # blindaba: al volver a prueba se le renovaba el reloj y con el
        # se le borraba el expediente medido.
        conn.execute("DELETE FROM signals")
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=1, "
                     "prueba_desde=?, turno_desde=? "
                     "WHERE address='CARRERA'",
                     (ahora - 30 * 86400, ahora - 30 * 86400))
        for i in range(9):     # empieza a fallar la puerta 3
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side, chg_24h) VALUES (?, 'CARRERA', ?, 1, ?, "
                "'compra', -70)",
                (f"cn{i}", f"CN{i}", ahora - 3600 + i))
        conn.commit()
        fc.clasificar(conn)
        fnull2 = conn.execute(
            "SELECT confirmada, prueba_desde, turno_desde FROM wallets "
            "WHERE address='CARRERA'").fetchone()
        comprobar("la confirmada que falla la puerta 3 vuelve a prueba",
                  not fnull2["confirmada"], str(dict(fnull2)))
        comprobar("y se le renueva el plazo de prueba (como en 18-L)",
                  fnull2["prueba_desde"] > ahora - 86400,
                  str(fnull2["prueba_desde"]))
        comprobar("pero NO el reloj del turno: sus señales malas siguen "
                  "contando contra ella",
                  fnull2["turno_desde"] == ahora - 30 * 86400,
                  str(fnull2["turno_desde"]))
        import performance_review as pv3
        comprobar("y la guarda anti-re-promocion las ve",
                  pv3.perdedora_confirmada(conn, "CARRERA") is not None,
                  str(pv3.perdedora_confirmada(conn, "CARRERA")))

        # Una ⭐ vieja SIN reloj de turno (base migrada) no recibe uno de
        # la clasificacion: NULL significa "se la juzga por todo", que es
        # la direccion segura. Si la clasificacion se lo pusiera, el dia
        # del despliegue amnistiaria a toda la poblacion de golpe.
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=0, "
                     "prueba_desde=NULL, turno_desde=NULL "
                     "WHERE address='CARRERA'")
        conn.commit()
        fc.clasificar(conn)
        fmig = conn.execute(
            "SELECT turno_desde FROM wallets "
            "WHERE address='CARRERA'").fetchone()
        comprobar("a la ⭐ heredada sin reloj de turno no se le inventa "
                  "uno (nada de amnistia al desplegar)",
                  fmig["turno_desde"] is None, str(fmig["turno_desde"]))
        comprobar("y por eso sus señales viejas siguen contando",
                  pv3.perdedora_confirmada(conn, "CARRERA") is not None,
                  str(pv3.perdedora_confirmada(conn, "CARRERA")))

        # Lo mismo con el interruptor maestro apagado.
        conn.execute("UPDATE wallets SET confirmada=0 "
                     "WHERE address='CARRERA'")
        conn.commit()
        cfg.FILTRO_TRES_PUERTAS = 0
        fc.clasificar(conn)
        cfg.FILTRO_TRES_PUERTAS = 1
        fapag = conn.execute(
            "SELECT confirmada, turno_desde FROM wallets "
            "WHERE address='CARRERA'").fetchone()
        comprobar("con el embudo apagado tampoco se inventa reloj",
                  fapag["confirmada"] == 1
                  and fapag["turno_desde"] is None, str(dict(fapag)))

        # El retiro por inactividad dice lo que mide, y no pisa un
        # /rastrear que haya ocurrido a mitad de la pasada.
        conn.execute("DELETE FROM signals")
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=0, "
                     "prueba_desde=? WHERE address='CARRERA'",
                     (ahora - 40 * 86400,))
        conn.execute("DELETE FROM trades WHERE wallet='CARRERA'")
        conn.commit()
        fc.clasificar(conn)
        fret = conn.execute(
            "SELECT is_tracked, ai_reason FROM wallets "
            "WHERE address='CARRERA'").fetchone()
        comprobar("la inactiva pierde la ⭐ con un motivo que dice lo que "
                  "de verdad se midio",
                  fret["is_tracked"] == 0
                  and "con ⭐ sin operar" in (fret["ai_reason"] or ""),
                  str(fret["ai_reason"])[:120])

        # Y si el dueño hace /rastrear a mitad de la pasada, el retiro
        # por inactividad NO puede aplicarse con el reloj viejo.
        conn.execute("UPDATE wallets SET is_tracked=1, confirmada=0, "
                     "prueba_desde=? WHERE address='CARRERA'",
                     (ahora - 40 * 86400,))
        conn.commit()
        proxy3 = _ConnRastreo(conn, "CARRERA", ahora)
        fc.clasificar(proxy3)
        comprobar("el /rastrear a mitad de pasada se disparó",
                  proxy3.disparado)
        frast = conn.execute(
            "SELECT is_tracked, prueba_desde FROM wallets "
            "WHERE address='CARRERA'").fetchone()
        comprobar("un /rastrear a mitad de pasada no lo pisa el retiro "
                  "por inactividad con el reloj viejo",
                  frast["is_tracked"] == 1, str(dict(frast)))

        # G2: la carrera tampoco debe dejar reloj puesto sin estrella
        # (ese reloj falso cegaria a la guarda de arriba). Se prueban los
        # DOS caminos que ponen el reloj: el de la confirmada que vuelve
        # a prueba y el de la que entra en prueba por primera vez.
        for etiqueta, conf in (("volviendo de confirmada", 1),
                               ("entrando en prueba por primera vez", 0)):
            conn.execute("UPDATE wallets SET is_tracked=1, confirmada=?, "
                         "prueba_desde=NULL WHERE address='CARRERA'",
                         (conf,))
            conn.commit()
            proxy2 = _ConnCarrera(conn, "CARRERA")
            fc.clasificar(proxy2)
            fg2 = conn.execute(
                "SELECT is_tracked, prueba_desde FROM wallets "
                "WHERE address='CARRERA'").fetchone()
            comprobar(f"degradada a mitad ({etiqueta}) no queda con "
                      f"reloj puesto",
                      not (fg2["prueba_desde"] and not fg2["is_tracked"]),
                      f"is_tracked={fg2['is_tracked']} "
                      f"prueba_desde={fg2['prueba_desde']}")
    finally:
        cfg.FILTRO_TRES_PUERTAS = act_previo
        cfg.FILTRO_MIN_CERRADAS = cer_previo
        conn.close()


def prueba_18o_puertas12():
    bloque("18-O/4 - una sola version de las puertas 1-2")
    import config as cfg
    import filtro_calidad as fc

    cer_previo = cfg.FILTRO_MIN_CERRADAS
    cfg.FILTRO_MIN_CERRADAS = 10
    try:
        buena = {"cerradas": 12, "wr": 70.0, "tokens": 12,
                 "hold_min": 90.0, "neto": 5.0}
        comprobar("historial bueno pasa las puertas 1-2",
                  fc.puertas_historial(buena)[0])
        comprobar("sin historial NO pasa (y lo dice)",
                  fc.puertas_historial(None)[0] is False
                  and "historial corto" in fc.puertas_historial(None)[1])
        for campo, valor, texto in (
                ("cerradas", 4, "historial corto"),
                ("wr", 30.0, "winrate"),
                ("neto", -3.0, "pierde dinero"),
                ("hold_min", 5.0, "retención"),
                ("tokens", 2, "tokens operados")):
            malo = dict(buena, **{campo: valor})
            ok, motivo = fc.puertas_historial(malo)
            comprobar(f"puertas 1-2: cae por {campo}",
                      (not ok) and texto in motivo, f"{ok} {motivo}")
        # /filtro deduce "se quedó en la puerta 2" leyendo el TEXTO del
        # motivo. Si alguien cambia esas cadenas sin mirar, el mando le
        # enseñaría al dueño un embudo distinto del real: se fijan aquí.
        p2 = [fc.puertas_historial(dict(buena, hold_min=5.0))[1],
              fc.puertas_historial(dict(buena, tokens=2))[1]]
        comprobar("los motivos de la puerta 2 llevan las palabras que "
                  "/filtro busca (la constante, no una copia)",
                  all(any(p in m for p in fc._PALABRAS_PUERTA2)
                      for m in p2), f"{p2} vs {fc._PALABRAS_PUERTA2}")
        p1 = [fc.puertas_historial(dict(buena, cerradas=4))[1],
              fc.puertas_historial(dict(buena, wr=30.0))[1],
              fc.puertas_historial(dict(buena, neto=-3.0))[1]]
        comprobar("y los de la puerta 1 NO las llevan (si no, /filtro "
                  "contaría a un suspenso de la 1 como aprobado)",
                  not any(any(p in m for p in fc._PALABRAS_PUERTA2)
                          for m in p1), str(p1))
        # El indice por billetera de `signals` tiene que crearse al
        # arrancar: las tres consultas nuevas por billetera (racha,
        # rendimiento medido, puerta 3) escanean la tabla entera sin el.
        import db as _dbi
        comprobar("el arranque crea el indice signals(wallet, ts)",
                  any("idx_signals_wallet_ts" in q
                      for q in _dbi._INDICES_TARDIOS),
                  str(_dbi._INDICES_TARDIOS))
    finally:
        cfg.FILTRO_MIN_CERRADAS = cer_previo


def prueba_18o_turno():
    bloque("18-O/7 - el reloj del turno se borra en TODA degradacion")
    import re as _re
    import time as _t
    import config as cfg
    import ai_analyst as aa
    import filtro_calidad as fc
    import performance_review as pv
    from db import get_conn

    conn = get_conn()
    ahora = int(_t.time())
    cer_previo = cfg.FILTRO_MIN_CERRADAS
    cfg.FILTRO_MIN_CERRADAS = 10
    try:
        # 1) INVARIANTE ESTRUCTURAL. Cualquier UPDATE del repo que quite
        # la estrella tiene que borrar tambien el reloj del turno; si se
        # queda puesto sin estrella, la guarda que impide a la IA
        # re-promover a una perdedora medida se queda ciega. Se
        # comprueba sobre el CODIGO porque los sitios son once y estan
        # repartidos por siete ficheros.
        import glob as _glob
        _raiz = os.path.dirname(os.path.abspath(__file__))
        huerfanos = []
        for ruta in _glob.glob(os.path.join(_raiz, "*.py")):
            arch = os.path.basename(ruta)
            if arch.startswith("pruebas_"):
                continue
            # Los comentarios se quitan: varios EXPLICAN el estado
            # is_tracked=0 en prosa y no son consultas.
            texto = "\n".join(
                "" if l.lstrip().startswith("#") else l
                for l in open(ruta, encoding="utf-8").read().splitlines())
            for m in _re.finditer(r"is_tracked\s*=\s*0", texto):
                trozo = texto[max(0, m.start() - 500):m.start() + 500]
                if "UPDATE wallets" not in trozo:
                    continue        # no es una degradacion
                if "turno_desde" not in trozo:
                    linea = texto[:m.start()].count("\n") + 1
                    huerfanos.append(f"{arch}:{linea}")
        comprobar("ningun UPDATE con `is_tracked = 0` literal deja el "
                  "reloj del turno puesto (los de `is_tracked = ?` los "
                  "cubren las pruebas de comportamiento de 18-O/2)",
                  not huerfanos, ", ".join(huerfanos))

        # Y al arrancar se REPARAN las filas viejas que quedaron con la
        # confirmacion puesta sin estrella: sin eso, la primera
        # re-promocion de la IA las metia al altavoz sin puertas.
        import db as _dbrep
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot, confirmada, "
            "prueba_desde, turno_desde) VALUES ('HUERFANA',0,0,1,111,222)")
        conn.commit()
        _dbrep._preparar_sqlite(conn)
        _fh = conn.execute(
            "SELECT confirmada, prueba_desde, turno_desde FROM wallets "
            "WHERE address='HUERFANA'").fetchone()
        comprobar("el arranque limpia la confirmacion huerfana",
                  not _fh["confirmada"] and _fh["prueba_desde"] is None
                  and _fh["turno_desde"] is None, str(dict(_fh)))

        # El export es lo que el dueño (o quien le ayude) analiza fuera:
        # sin las columnas de fase no se puede saber que ⭐ alertaban ni
        # sobre que ventana se las juzgaba, y cualquier recalculo daria
        # numeros distintos a los del bot.
        _exp = open(os.path.join(_raiz, "exportar.py"),
                    encoding="utf-8").read()
        _faltan = [c for c in ("confirmada", "prueba_desde", "turno_desde")
                   if c not in _exp]
        comprobar("el export lleva las columnas de fase de la ⭐",
                  not _faltan, str(_faltan))

        # 2) Y tres caminos de degradacion, ejercitados de verdad.
        from trades_store import _ensure
        _ensure(conn)
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM trades")
        conn.commit()

        def estrella(w, grade=None):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "confirmada, turno_desde, grade) VALUES (?,1,0,0,?,?)",
                (w, ahora - 5 * 86400, grade))

        estrella("DEG_REEMBUDO")          # sin historial: /reembudo la echa
        estrella("DEG_GRADO", "Descartada")
        conn.commit()

        import wallet_funding as _wf
        _fam_real = _wf.familia
        _wf.familia = lambda a: []
        try:
            aa.depurar_estrellas(conn)
        finally:
            _wf.familia = _fam_real
        fg = conn.execute(
            "SELECT is_tracked, turno_desde FROM wallets "
            "WHERE address='DEG_GRADO'").fetchone()
        comprobar("la depuracion por nota borra el reloj del turno",
                  fg["is_tracked"] == 0 and fg["turno_desde"] is None,
                  str(dict(fg)))

        conn.execute("UPDATE wallets SET is_tracked=1, turno_desde=? "
                     "WHERE address='DEG_REEMBUDO'", (ahora - 5 * 86400,))
        conn.commit()
        fc.reevaluacion(conn, ejecutar=True)
        fr = conn.execute(
            "SELECT is_tracked, turno_desde FROM wallets "
            "WHERE address='DEG_REEMBUDO'").fetchone()
        comprobar("/reembudo borra el reloj del turno",
                  fr["is_tracked"] == 0 and fr["turno_desde"] is None,
                  str(dict(fr)))

        # 3) La guarda anti-re-promocion SI corta por el turno.
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        estrella("GUARDA")
        for i in range(12):        # 12 medidas malas ANTERIORES al turno
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side, chg_24h) VALUES (?, 'GUARDA', ?, 1, ?, "
                "'compra', -50)",
                (f"g{i}", f"GM{i}", ahora - 30 * 86400 + i))
        conn.commit()
        comprobar("con la ⭐ puesta, la guarda no le saca el expediente "
                  "de su turno anterior",
                  pv.perdedora_confirmada(conn, "GUARDA") is None,
                  str(pv.perdedora_confirmada(conn, "GUARDA")))
        conn.execute("UPDATE wallets SET is_tracked=0, turno_desde=NULL "
                     "WHERE address='GUARDA'")
        conn.commit()
        comprobar("pero degradada (sin reloj) la guarda las ve TODAS: la "
                  "IA no puede re-promoverla",
                  pv.perdedora_confirmada(conn, "GUARDA") is not None,
                  str(pv.perdedora_confirmada(conn, "GUARDA")))
    finally:
        cfg.FILTRO_MIN_CERRADAS = cer_previo
        conn.close()


def prueba_18o_quorum():
    bloque("18-O/5 - el quorum de salida cuenta solo ⭐ confirmadas")
    import time as _t
    import paper_trading as pt
    from db import get_conn, get_setting, set_setting

    conn = get_conn()
    ahora = int(_t.time())
    _fill_real = pt._fill_nuevo
    _quorum_previo = str(get_setting(conn, "consenso_salida_n", "2") or "2")
    llamadas = []
    try:
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM paper_fills")
        set_setting(conn, "consenso_salida_n", "2")
        ent = ahora - 600
        # LIDER y Q2 confirmadas; Q3 EN PRUEBA (medida en silencio).
        for w, conf in (("LIDER", 1), ("Q2", 1), ("Q3", 0)):
            conn.execute(
                "INSERT INTO wallets (address, is_tracked, is_bot, "
                "confirmada) VALUES (?,1,0,?)", (w, conf))
            conn.execute(       # las tres compraron dentro de la ventana
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side) VALUES (?,?,'QM',1,?,'compra')",
                (f"qc{w}", w, ent - 600))
        # Posicion viva abierta por CONSENSO, a nombre de la lider.
        conn.execute(
            "INSERT INTO paper_trades (id, signature, wallet, mint, "
            "symbol, stake_sol, entry_price, entry_ts, status, origen) "
            "VALUES (1,'sig0','LIDER','QM','QM',1,1.0,?, 'abierta', "
            "'consenso')", (ent,))
        conn.commit()

        def _fill_espia(conn_, trade_id, firma):
            llamadas.append((trade_id, firma))
            return False        # corta ahi: solo interesa si SE LLEGO
        pt._fill_nuevo = _fill_espia

        def vende(w, k):
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side) VALUES (?,?,'QM',1,?,'venta')",
                (f"qv{w}{k}", w, ent + 60 + k))
            conn.commit()

        # Vende SOLO la que esta en prueba: no debe contar para nada.
        vende("Q3", 1)
        pt.close_on_wallet_sell(
            conn, {"mint": "QM", "wallet": "Q3", "signature": "v1"},
            {"price": 2.0})
        comprobar("la ⭐ EN PRUEBA sola no alcanza el quorum de salida",
                  llamadas == [], str(llamadas))
        comprobar("y la posicion sigue abierta",
                  conn.execute("SELECT status FROM paper_trades WHERE id=1")
                  .fetchone()["status"] == "abierta")

        # Ahora vende una confirmada: 1 confirmada < quorum 2.
        vende("Q2", 2)
        pt.close_on_wallet_sell(
            conn, {"mint": "QM", "wallet": "Q3", "signature": "v2"},
            {"price": 2.0})
        comprobar("una sola confirmada tampoco (el quorum es 2)",
                  llamadas == [], str(llamadas))

        # Y con dos confirmadas vendiendo, si se llega a la salida.
        vende("LIDER", 3)
        pt.close_on_wallet_sell(
            conn, {"mint": "QM", "wallet": "Q3", "signature": "v3"},
            {"price": 2.0})
        comprobar("con DOS confirmadas vendiendo si se llega a la salida",
                  len(llamadas) == 1, str(llamadas))

        # Si una de la manada pierde la confirmacion, deja de sumar al
        # quorum. Eso es lo buscado, y NO atrapa la posicion: la venta de
        # la LIDER (la que abrio) cierra por su propia via, sin quorum.
        llamadas.clear()
        conn.execute("DELETE FROM paper_fills")
        conn.execute("UPDATE wallets SET confirmada=0 WHERE address='Q2'")
        conn.commit()
        pt.close_on_wallet_sell(
            conn, {"mint": "QM", "wallet": "Q3", "signature": "v4"},
            {"price": 2.0})
        comprobar("si una de la manada se desconfirma, deja de sumar",
                  llamadas == [], str(llamadas))
        pt.close_on_wallet_sell(
            conn, {"mint": "QM", "wallet": "LIDER", "signature": "v5"},
            {"price": 2.0})
        comprobar("pero la venta de la LIDER cierra igual: la posicion "
                  "nunca queda atrapada por el quorum",
                  len(llamadas) == 1, str(llamadas))
        conn.execute("UPDATE wallets SET confirmada=1 WHERE address='Q2'")
        conn.commit()
    finally:
        pt._fill_nuevo = _fill_real
        set_setting(conn, "consenso_salida_n", _quorum_previo)
        conn.close()


def prueba_18o_medidas():
    bloque("18-O/6 - la puerta 3 NO caduca (olvidar premiaria a la mala)")
    import time as _t
    import config as cfg
    import filtro_calidad as fc
    from db import get_conn

    conn = get_conn()
    ahora = int(_t.time())
    ven_previo = cfg.FILTRO_VENTANA_DIAS
    prov_previo = cfg.FILTRO_PROVISIONAL
    cfg.FILTRO_VENTANA_DIAS = 90
    cfg.FILTRO_PROVISIONAL = 1
    try:
        conn.execute("DELETE FROM wallets")
        conn.execute("DELETE FROM signals")
        conn.execute(
            "INSERT INTO wallets (address, is_tracked, is_bot) "
            "VALUES ('VENTANA',1,0)")
        for i in range(8):         # 8 medidas MALAS y viejas
            conn.execute(
                "INSERT INTO signals (signature, wallet, mint, sol, ts, "
                "side, chg_24h) VALUES (?, 'VENTANA', ?, 1, ?, "
                "'compra', -60)",
                (f"vv{i}", f"VM{i}", ahora - 200 * 86400 + i))
        conn.commit()
        m = fc.medidas(conn).get("VENTANA") or {}
        comprobar("las medidas malas NO caducan (si caducaran, la "
                  "billetera volveria al altavoz como 'provisional')",
                  m.get("n") == 8, str(m))
        m1 = fc.medidas(conn, "VENTANA").get("VENTANA") or {}
        comprobar("ni pidiendo la billetera suelta (las dos consultas "
                  "tienen que decir lo mismo)",
                  m1.get("n") == 8, str(m1))
        buena = {"cerradas": 12, "wr": 70.0, "tokens": 12,
                 "hold_min": 90.0, "neto": 5.0}
        ok, motivo = fc.puertas(buena, m)
        comprobar("y con ellas la puerta 3 la sigue suspendiendo",
                  (not ok) and "medidas malas" in motivo, f"{ok} {motivo}")
        comprobar("mientras que sin medidas pasaria como provisional "
                  "(por eso olvidar seria peligroso)",
                  fc.puertas(buena, None)[0] is True)
    finally:
        cfg.FILTRO_VENTANA_DIAS = ven_previo
        cfg.FILTRO_PROVISIONAL = prov_previo
        conn.close()


# ---------------------------------------------------------------------
# OLA 18-P - el radar trabaja en silencio (pedido del dueno 29/8).
# ---------------------------------------------------------------------
def prueba_18p_radar():
    bloque("18-P - el radar trabaja oculto: ni un mensaje, mismo trabajo")
    import asyncio as _aio
    import time as _t
    import types as _ty
    import radar as rd
    import realtime as rt
    import telegram_bot as tb
    from db import get_conn, get_setting, set_setting

    conn = get_conn()
    ahora = int(_t.time())
    enviados = []
    _tg_prev = rt.tg_send
    rt.tg_send = lambda *a, **k: enviados.append(a[0] if a else "")
    _sil_prev = get_setting(conn, "radar_silencioso", None)
    _def_prev = rd.SILENCIOSO_DEF
    try:
        conn.execute("DELETE FROM radar_tokens")
        conn.commit()

        # 1) El interruptor: por defecto MUDO, y el ajuste manda sobre el
        # valor de arranque (para que /radarsilencio no pida reinicio).
        set_setting(conn, "radar_silencioso", "")
        conn.commit()
        comprobar("de fabrica (sin RADAR_SILENCIOSO en el entorno) el "
                  "radar viene en modo oculto",
                  _def_prev == 1, str(_def_prev))
        rd.SILENCIOSO_DEF = 1
        comprobar("por defecto el radar esta en modo oculto",
                  rd.silencioso(conn) is True)
        rd.SILENCIOSO_DEF = 0
        comprobar("con RADAR_SILENCIOSO=0 de arranque, avisa",
                  rd.silencioso(conn) is False)
        set_setting(conn, "radar_silencioso", "1")
        conn.commit()
        comprobar("el ajuste guardado manda sobre el valor de arranque",
                  rd.silencioso(conn) is True)
        set_setting(conn, "radar_silencioso", "0")
        conn.commit()
        comprobar("y tambien para volver a encenderlo",
                  rd.silencioso(conn) is False)
        set_setting(conn, "radar_silencioso", "no-es-un-numero")
        conn.commit()
        rd.SILENCIOSO_DEF = 1
        comprobar("un ajuste ilegible cae al valor de arranque, no revienta",
                  rd.silencioso(conn) is True)

        # 2) El mando /radarsilencio cambia el ajuste EN CALIENTE.
        mensajes = []

        class _Chat:
            async def send_message(self, text, **kw):
                mensajes.append(text)

        class _Msg:
            chat = _Chat()

            async def reply_text(self, text, **kw):
                mensajes.append(text)

        upd = _ty.SimpleNamespace(message=_Msg(),
                                  effective_user=_ty.SimpleNamespace(id=1))
        set_setting(conn, "radar_silencioso", "0")
        conn.commit()
        _aio.run(tb.cmd_radar_silencio(upd, _ty.SimpleNamespace(args=["on"])))
        comprobar("/radarsilencio on lo deja mudo al instante",
                  rd.silencioso(conn) is True, str(mensajes[-1])[:80])
        mensajes.clear()
        _aio.run(tb.cmd_radar_silencio(upd, _ty.SimpleNamespace(args=["off"])))
        comprobar("/radarsilencio off le devuelve la voz",
                  rd.silencioso(conn) is False, str(mensajes[-1])[:80])
        mensajes.clear()
        _aio.run(tb.cmd_radar_silencio(upd, _ty.SimpleNamespace(args=[])))
        comprobar("/radarsilencio a secas dice en que modo esta",
                  "Radar con avisos" in mensajes[-1]
                  and "modo oculto" not in mensajes[-1].lower(),
                  str(mensajes[-1])[:110])
        mensajes.clear()
        _aio.run(tb.cmd_radar_silencio(upd, _ty.SimpleNamespace(args=["on"])))
        mensajes.clear()
        _aio.run(tb.cmd_radar_silencio(upd, _ty.SimpleNamespace(args=[])))
        comprobar("y lo dice distinto cuando esta callado",
                  "modo oculto" in mensajes[-1].lower()
                  and "Radar con avisos" not in mensajes[-1],
                  str(mensajes[-1])[:110])
        _aio.run(tb.cmd_radar_silencio(upd, _ty.SimpleNamespace(args=["off"])))
        mensajes.clear()
        _aio.run(tb.cmd_radar_silencio(upd, _ty.SimpleNamespace(args=["xy"])))
        comprobar("y un argumento raro solo explica el uso",
                  "Uso:" in mensajes[-1], str(mensajes[-1])[:90])

        # 3) La promocion al embudo SE HACE igual con el radar mudo: es lo
        # valioso: mete al ciclo a los compradores tempranos del token.
        def _sembrar():
            conn.execute("DELETE FROM radar_tokens")
            conn.execute(
                "INSERT INTO radar_tokens (mint, ts, symbol, liq, "
                "resultado, price0) VALUES ('RMINT',?,'RSYM',9000,"
                "'sin_conocidas',1.0)", (ahora - 7200,))
            conn.execute("DELETE FROM winning_tokens")
            conn.commit()

        # Sin red: se sustituyen las dos fuentes que consulta el
        # seguimiento (precio y ficha del token) por dobles que dicen
        # "x5 y con talla de ganador".
        import signal_tracker as _st
        import token_check as _tc
        _px_prev, _an_prev = _st._price_mc_ex, _tc.analyze_token
        _st._price_mc_ex = lambda m: (5.0, 900000.0, False, 90000.0)
        _tc.analyze_token = lambda m: {"liq": 90000.0, "mc": 900000.0,
                                       "vol24": 300000.0, "symbol": "RSYM",
                                       "pair": "P1"}
        try:
          for mudo, etiqueta in ((True, "mudo"), (False, "con voz")):
            _sembrar()
            enviados.clear()
            set_setting(conn, "radar_silencioso", "1" if mudo else "0")
            conn.commit()
            rd._seguimiento(conn)
            fila = conn.execute(
                "SELECT resultado FROM radar_tokens "
                "WHERE mint='RMINT'").fetchone()
            gan = conn.execute(
                "SELECT COUNT(*) AS c FROM winning_tokens "
                "WHERE mint='RMINT'").fetchone()["c"]
            comprobar(f"({etiqueta}) el ganador se promueve al embudo igual",
                      fila["resultado"] == "ganador_promovido" and gan == 1,
                      f"{dict(fila)} winning={gan}")
            if mudo:
                comprobar("(mudo) y NO se manda ningun mensaje",
                          enviados == [], str(enviados)[:120])
            else:
                comprobar("(con voz) si se manda el aviso",
                          any("Radar" in m for m in enviados),
                          str(enviados)[:120])
        finally:
            _st._price_mc_ex, _tc.analyze_token = _px_prev, _an_prev

        # 3b) LO IMPORTANTE: la alerta de smart money (53 al dia en la
        # base del dueño) se calla, pero el hallazgo se REGISTRA igual —
        # /radar y el resumen tienen que seguir contandolo.
        import helius_budget as _hb
        _puede_prev = _hb.puede_llamar
        _hb.puede_llamar = lambda *a, **k: True
        _fr_prev, _cp_prev, _co_prev = (rd._frescos, rd._compradores,
                                        rd._conocidas)
        rd._frescos = lambda: [{"mint": "SMART1", "symbol": "SM",
                                "liq": 20000.0, "edad_min": 10}]
        rd._compradores = lambda m: {"W1"}
        _conocidas_hay = [{"address": "W1", "alias": "Conocida",
                           "is_tracked": 1, "grade": "Elite"}]
        rd._conocidas = lambda conn_, buyers: _conocidas_hay
        _tc.analyze_token = lambda m: {"price": 1.0, "liq": 20000.0,
                                       "mc": 500000.0, "vol24": 100000.0,
                                       "symbol": "SM", "rug_ok": True,
                                       "score": 90}
        _sem_prev = rd._semaforo
        rd._semaforo = lambda t: (True, "seguridad OK")
        # `escanear` llama a `_seguimiento` al final: se le pone tambien
        # el doble del precio para que la suite no dependa NUNCA de que
        # la fila sembrada caiga fuera de su ventana.
        _px_prev2 = _st._price_mc_ex
        _st._price_mc_ex = lambda m: (1.0, 100.0, False, 100.0)
        try:
            for mudo, etiqueta in ((True, "mudo"), (False, "con voz")):
                conn.execute("DELETE FROM radar_tokens")
                conn.commit()
                enviados.clear()
                set_setting(conn, "radar_silencioso", "1" if mudo else "0")
                conn.commit()
                n = rd.escanear()
                fila = conn.execute(
                    "SELECT resultado, smart FROM radar_tokens "
                    "WHERE mint='SMART1'").fetchone()
                comprobar(f"({etiqueta}) el hallazgo se registra igual",
                          fila is not None
                          and str(fila["resultado"]).startswith("alertado")
                          and fila["smart"] == 1 and n == 1,
                          f"{dict(fila) if fila else None} n={n}")
                if mudo:
                    comprobar("(mudo) y NO llega ni un mensaje al chat",
                              enviados == [], str(enviados)[:150])
                else:
                    comprobar("(con voz) el aviso si sale",
                              any("RADAR" in m for m in enviados),
                              str(enviados)[:150])
            # Y un token SIN conocidas deja `smart` en 0, no en NULL:
            # NULL tiene que significar solo "fila anterior a 18-P".
            conn.execute("DELETE FROM radar_tokens")
            conn.commit()
            _conocidas_hay.clear()
            rd.escanear()
            _fsc = conn.execute(
                "SELECT resultado, smart FROM radar_tokens "
                "WHERE mint='SMART1'").fetchone()
            comprobar("un token sin conocidas queda con smart=0, no NULL",
                      _fsc is not None and _fsc["smart"] == 0
                      and str(_fsc["resultado"]).startswith("sin_conocidas"),
                      str(dict(_fsc)) if _fsc else "sin fila")
        finally:
            _hb.puede_llamar = _puede_prev
            rd._frescos, rd._compradores = _fr_prev, _cp_prev
            rd._conocidas, rd._semaforo = _co_prev, _sem_prev
            _tc.analyze_token = _an_prev      # se re-parcheo aqui arriba
            _st._price_mc_ex = _px_prev2

        # 4) La linea del resumen diario cuenta lo que paso.
        conn.execute("DELETE FROM radar_tokens")
        for k, (res, sm) in enumerate((("murio", 2), ("alertado:2", 2),
                                       ("alertado:1", 1),
                                       ("ganador_promovido", 3),
                                       ("sin_conocidas", None))):
            conn.execute(
                "INSERT INTO radar_tokens (mint, ts, symbol, liq, "
                "resultado, smart) VALUES (?,?,?,1000,?,?)",
                (f"M{k}", ahora - 600, f"S{k}", res, sm))
        conn.commit()
        linea = rd.resumen_linea(conn)
        comprobar("la linea del resumen cuenta examinados, con smart money "
                  "y promovidos",
                  linea and "5 tokens" in linea and "4 con" in linea
                  and "1 promovido al embudo" in linea, str(linea))
        conn.execute("DELETE FROM radar_tokens")
        conn.commit()
        comprobar("si el radar esta encendido y no examino NADA en 24 h, "
                  "la linea AVISA (es la unica alarma que queda al "
                  "quitarle la voz)",
                  "0 tokens nuevos" in (rd.resumen_linea(conn) or "")
                  and "revisa" in (rd.resumen_linea(conn) or ""),
                  str(rd.resumen_linea(conn)))

        # 4b) Y esa linea tiene que llegar de verdad al resumen diario:
        # con el radar mudo es lo unico que el dueño ve sin preguntar.
        conn.execute("DELETE FROM radar_tokens")
        for k, (res, sm) in enumerate((("alertado:3", 3), ("murio", None))):
            conn.execute(
                "INSERT INTO radar_tokens (mint, ts, symbol, liq, "
                "resultado, smart) VALUES (?,?,?,1000,?,?)",
                (f"D{k}", ahora - 600, f"DS{k}", res, sm))
        conn.commit()
        import digest as _dg
        _txt_res = _dg.resumen_text()
        comprobar("el resumen diario lleva la linea del radar",
                  "Radar de tokens recién nacidos" in _txt_res
                  and "2 tokens nuevos examinados" in _txt_res,
                  _txt_res[-260:])

        # 4b-bis) Con el radar APAGADO del todo, el resumen no habla de
        # el en presente, y /radarsilencio lo dice.
        _act_prev = rd.ACTIVO
        rd.ACTIVO = 0
        try:
            comprobar("con el radar apagado no sale linea en el resumen",
                      rd.resumen_linea(conn) is None)
            mensajes.clear()
            _aio.run(tb.cmd_radar_silencio(upd,
                                           _ty.SimpleNamespace(args=[])))
            comprobar("y /radarsilencio avisa de que esta apagado",
                      "APAGADO" in mensajes[-1], str(mensajes[-1])[-160:])
        finally:
            rd.ACTIVO = _act_prev

        # 4c) La coletilla del resumen dice la verdad en los dos modos.
        set_setting(conn, "radar_silencioso", "0")
        conn.commit()
        comprobar("con el radar avisando, el resumen NO dice 'en silencio'",
                  "trabaja en silencio" not in _dg.resumen_text(),
                  _dg.resumen_text()[-200:])
        set_setting(conn, "radar_silencioso", "1")
        conn.commit()
        comprobar("y callado si lo dice",
                  "trabaja en silencio" in _dg.resumen_text(),
                  _dg.resumen_text()[-200:])

        # 4d) El hallazgo que luego MURIO sigue saliendo en /radar: la
        # lista va por `smart`, no por el estado (y sin LIKE '...%' con
        # parametros, que en Postgres revienta la consulta).
        conn.execute("DELETE FROM radar_tokens")
        conn.execute(
            "INSERT INTO radar_tokens (mint, ts, symbol, liq, resultado, "
            "smart) VALUES ('MUERTO1',?,'MSYM',5000,'murio',4)",
            (ahora - 3600,))
        conn.commit()
        _rt = rd.radar_text()
        comprobar("el hallazgo que murio despues sigue en /radar",
                  "MSYM" in _rt and "4 conocida" in _rt, _rt[-260:])
        comprobar("y se ve QUE murio (si no, el dueño copiaria el mint de "
                  "un token muerto)",
                  "murió" in _rt, _rt[-260:])
        # Una fila vieja con `smart` en NULL (relleno no llegado) no puede
        # descuadrar el total con el desglose de arriba.
        conn.execute("DELETE FROM radar_tokens")
        conn.execute(
            "INSERT INTO radar_tokens (mint, ts, symbol, liq, resultado) "
            "VALUES ('VIEJA1',?,'VSYM',5000,'alertado:2')", (ahora - 600,))
        conn.commit()
        _rtv = rd.radar_text()
        comprobar("una fila sin `smart` cuenta igual en el total de /radar",
                  "Hallazgos de smart money en 24 h: 1" in _rtv, _rtv[:400])
        comprobar("y la lista saca su numero del `resultado`, no un None",
                  "2 conocida(s)" in _rtv and "None conocida" not in _rtv,
                  _rtv[-260:])
        comprobar("a un hallazgo aun sin comprobar no se le llama 'vivo' "
                  "(el seguimiento mira 15 al azar por pasada)",
                  "en seguimiento" in _rtv and "🟢 vivo" not in _rtv,
                  _rtv[-260:])
        comprobar("y con un solo token la linea del resumen va en "
                  "singular",
                  "1 token nuevo examinado ·" in (rd.resumen_linea(conn)
                                                  or ""),
                  str(rd.resumen_linea(conn)))
        comprobar("y tambien en la linea del resumen",
                  "1 con billeteras" in (rd.resumen_linea(conn) or ""),
                  str(rd.resumen_linea(conn)))

        # 4e) La columna `smart` se migra —y se RELLENA— en una base que
        # ya existía. Va sobre una base temporal propia: `_preparar_sqlite`
        # hace mucho más que el ALTER (repara fases y clasifica), y sobre
        # la base compartida de la suite dejaría a las billeteras de otras
        # pruebas reclasificadas.
        import sqlite3 as _sq
        import db as _dbm
        _tmp = os.path.join(_TMP, "migra18p.db")
        _cx = _sq.connect(_tmp)
        _cx.row_factory = _sq.Row
        _cx.executescript(
            "CREATE TABLE radar_tokens (mint TEXT PRIMARY KEY, ts INTEGER, "
            "symbol TEXT, liq REAL, resultado TEXT);"
            "INSERT INTO radar_tokens VALUES "
            "('V1', 1, 'VS1', 100, 'alertado:4'),"
            "('V2', 1, 'VS2', 100, 'murio'),"
            "('V3', 1, 'VS3', 100, 'alertado:');")
        _cx.commit()
        try:
            _dbm._preparar_sqlite(_cx)
            _cols = [r[1] for r in _cx.execute(
                "PRAGMA table_info(radar_tokens)").fetchall()]
            comprobar("una base vieja recibe la columna `smart` al arrancar",
                      "smart" in _cols, str(_cols))
            _v = {r["mint"]: r["smart"] for r in _cx.execute(
                "SELECT mint, smart FROM radar_tokens").fetchall()}
            comprobar("y las filas que ya decian 'alertado:N' recuperan el "
                      "numero (si no, el dia del despliegue el resumen "
                      "diria 0 justo cuando el radar deja de avisar)",
                      _v.get("V1") == 4, str(_v))
            comprobar("las que no lo dicen se quedan sin dato, no en 0 "
                      "falso", _v.get("V2") is None, str(_v))
            comprobar("y un 'alertado:' sin numero no revienta la "
                      "migracion", _v.get("V3") is None, str(_v))
        finally:
            _cx.close()

        # 4f) /radar: el desglose por estado y el TOTAL de hallazgos son
        # dos numeros distintos y ninguno puede mentir. Antes "alertados"
        # salia del estado y encogia solo cuando el token moria.
        conn.execute("DELETE FROM radar_tokens")
        for mint, res, sm in (("R1", "alertado:2", 2), ("R2", "murio", 3),
                              ("R3", "ganador_promovido", 1),
                              ("R4", "sin_conocidas", 0)):
            conn.execute(
                "INSERT INTO radar_tokens (mint, ts, symbol, liq, "
                "resultado, smart) VALUES (?,?,?,1000,?,?)",
                (mint, ahora - 3600, mint + "S", res, sm))
        conn.commit()
        _rt2 = rd.radar_text()
        comprobar("/radar da el TOTAL de hallazgos por `smart`, no el "
                  "estado (3, no 1)",
                  "Hallazgos de smart money en 24 h: 3" in _rt2,
                  _rt2[:400])
        comprobar("y el desglose por estado ya no se llama 'alertados'",
                  "aún en seguimiento: 1" in _rt2 and "🎯 alertados" not in _rt2,
                  _rt2[:400])

        # 5) /radar dice en que modo esta (es como el dueño lo comprueba).
        set_setting(conn, "radar_silencioso", "1")
        conn.commit()
        comprobar("/radar avisa de que esta en modo oculto",
                  "Modo oculto" in rd.radar_text())
        set_setting(conn, "radar_silencioso", "0")
        conn.commit()
        comprobar("y de que esta avisando cuando lo esta",
                  "Avisa por Telegram" in rd.radar_text())
    finally:
        rt.tg_send = _tg_prev
        rd.SILENCIOSO_DEF = _def_prev
        try:                    # no dejar rastro para las que vengan
            conn.execute("DELETE FROM radar_tokens")
            conn.execute("DELETE FROM winning_tokens")
            conn.commit()
        except Exception:
            pass
        try:
            set_setting(conn, "radar_silencioso",
                        _sil_prev if _sil_prev is not None else "")
            conn.commit()
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------
# OLA 18-Q - /salud deja de dar rojo por el tamaño de la base.
# ---------------------------------------------------------------------
def prueba_18q_salud_base():
    bloque("18-Q - el aviso de la base mira el DISCO, no los MB")
    import os as _os
    import shutil as _sh
    import tempfile as _tf
    import time as _t
    import salud as sd
    from db import get_conn

    conn = get_conn()
    _du_prev = _sh.disk_usage
    _gs_prev = _os.path.getsize
    _st_prev = _os.stat
    _ts_prev = None
    _tsm_prev = None
    from db import set_setting as _sset
    _cfg = __import__("config")
    _ruta_prev = _cfg.DB_PATH
    _d = _tf.mkdtemp(prefix="salud18q_")
    try:
        class _DU:
            def __init__(self, free):
                self.total, self.used, self.free = free * 3, free * 2, free

        # Base FALSA de 400 MB. El tamaño se finge en `getsize` en vez de
        # escribir 400 MB de verdad: en Windows —donde corre el bot del
        # dueño— `truncate` rellena de ceros y la suite escribiría medio
        # giga justo para probar la presión de disco.
        _base = _os.path.join(_d, "wallets.db")
        open(_base, "wb").close()
        _MB400 = 400 * 10**6
        _tam = {}                      # tamaños fingidos, por ruta

        def _getsize(p):
            _a = _os.path.abspath(p)
            if _a in _tam:
                return _tam[_a]
            return _MB400 if _a == _os.path.abspath(_base) else _gs_prev(p)
        _os.path.getsize = _getsize
        _cfg.DB_PATH = _base

        class _StDev:
            """stat de verdad, con el volumen cambiado."""

            def __init__(self, r, dev):
                self._r, self.st_dev = r, dev

            def __getattr__(self, n):
                return getattr(self._r, n)

        def _libres(base_gb, tmp_gb=None):
            """Simula el disco: uno para la carpeta de la base y otro
            (opcional) para el temporal. Cuando se pide un temporal
            aparte se le da tambien OTRO volumen (`st_dev`), que es lo
            que mira `_c_base_datos` para decidir si son dos sitios o
            uno."""
            _tmp = _tf.gettempdir()

            def _du(p):
                if tmp_gb is not None and _os.path.abspath(p) \
                        == _os.path.abspath(_tmp):
                    return _DU(int(tmp_gb * 10**9))
                return _DU(int(base_gb * 10**9))
            _sh.disk_usage = _du

            def _stat(p, *a, **k):
                r = _st_prev(p, *a, **k)
                if tmp_gb is None:
                    return r
                _es_tmp = (_os.path.abspath(p) == _os.path.abspath(_tmp))
                return _StDev(r, 2 if _es_tmp else 1)
            _os.stat = _stat

        _libres(500)
        r = sd._c_base_datos(conn)
        comprobar("una base de 400 MB con disco de sobra es VERDE",
                  r["estado"] == sd.OK, str(r))
        comprobar("y NUNCA manda 'pasa a Postgres' ni tocar "
                  "MAX_TRADES_TOTAL: ninguno de los dos arreglaba nada",
                  "MAX_TRADES_TOTAL" not in str(r)
                  and "Postgres" not in str(r), str(r))
        comprobar("dice cuanto disco queda donde vive la base",
                  "GB libres donde vive la base" in r["detalle"],
                  r["detalle"])

        # El `-wal` ocupa disco igual: si no se suma, la cifra que ve el
        # dueño y las varas de disco se quedan cortas.
        _wal = _base + "-wal"
        open(_wal, "wb").close()
        _tam[_os.path.abspath(_wal)] = 50 * 10**6
        r = sd._c_base_datos(conn)
        comprobar("el tamaño incluye el -wal (400 + 50 = 450 MB)",
                  "450 MB" in r["detalle"], r["detalle"])
        comprobar("y se DICE que el WAL va dentro, para que la cifra "
                  "cuadre con lo que ve el dueño en su carpeta (y se "
                  "nombra tambien el -shm, que va en la misma suma)",
                  "de WAL/SHM" in r["detalle"], r["detalle"])
        _shm = _base + "-shm"
        open(_shm, "wb").close()
        _tam[_os.path.abspath(_shm)] = 20 * 10**6
        comprobar("y el -shm tambien (400 + 50 + 20 = 470 MB)",
                  "470 MB" in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        _os.remove(_shm)
        _tam.pop(_os.path.abspath(_shm), None)
        _os.remove(_wal)
        _tam.pop(_os.path.abspath(_wal), None)
        comprobar("sin WAL, la cifra va a secas",
                  "de WAL" not in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        # Una base recien creada son 150 KB: decir "0 MB" del archivo que
        # se esta midiendo no informa de nada.
        _MB400 = 155648
        comprobar("una base recien creada se dice en KB, no '0 MB'",
                  "156 KB" in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        _MB400 = 400 * 10**6

        # ROJO y AMARILLO salen del disco RELATIVO a la base.
        _libres(0.6)                                   # < 400MB * 2
        r = sd._c_base_datos(conn)
        comprobar("con el disco por debajo del doble de la base, ROJO",
                  r["estado"] == sd.CRIT, str(r))
        comprobar("y el consejo dice DONDE falta sitio, no que cambies "
                  "de motor",
                  "carpeta de la base" in r["accion"]
                  and "MAX_TRADES_TOTAL" not in r["accion"]
                  and "Postgres" not in r["accion"], r["accion"])
        _libres(1.5)                                   # entre 2x y 6x
        r = sd._c_base_datos(conn)
        comprobar("con el disco justo, AMARILLO",
                  r["estado"] == sd.WARN, str(r))
        comprobar("y ese amarillo tampoco manda cambiar de motor",
                  "Postgres" not in r["accion"], r["accion"])

        # EL FALLO QUE MOTIVO LA OLA, EN OTRO MONTAJE: un /tmp pequeño
        # no puede dar rojo permanente. Sin copia previa que medir, al
        # backup se le suponen 1,5x la base: 600 MB para una base de
        # 400 MB. Con 1,2 GB va sobrado; con la vara de la otra carpeta
        # (2x mas el margen del aviso, 2,4 GB) esto habria sido rojo o
        # amarillo eterno, que es justo el fallo que se esta cerrando.
        _libres(500, tmp_gb=1.2)
        r = sd._c_base_datos(conn)
        comprobar("un /tmp pequeño donde la copia CABE de sobra no es "
                  "rojo ni amarillo",
                  r["estado"] == sd.OK, str(r))
        comprobar("y el detalle distingue los dos sitios",
                  "en el temporal" in r["detalle"], r["detalle"])
        _libres(500, tmp_gb=0.7)      # cabe, pero justo: aviso previo
        r = sd._c_base_datos(conn)
        comprobar("con el temporal justo, AMARILLO (el aviso previo "
                  "tambien existe en el temporal)",
                  r["estado"] == sd.WARN and "temporal" in r["accion"],
                  str(r))
        _libres(500, tmp_gb=0.3)      # 300 MB: no cabe la copia
        r = sd._c_base_datos(conn)
        comprobar("pero si en el temporal no cabe la copia, ROJO",
                  r["estado"] == sd.CRIT, str(r))
        comprobar("y el consejo apunta al TEMPORAL, no a `backups/`",
                  "temporal" in r["accion"]
                  and "backups/" not in r["accion"], r["accion"])

        # Una base de 10 GB no puede dejar /salud en amarillo eterno,
        # pero el amarillo TIENE que seguir estando por encima del rojo:
        # con el techo suelto de antes (5 GB) el aviso previo caia POR
        # DEBAJO de la linea roja (2x = 20 GB) y se pasaba de verde a
        # rojo sin banda de aviso.
        # Con el temporal en OTRO disco y de sobra, quien decide es la
        # banda de la base y solo ella.
        _MB400 = 10 * 10**9
        _libres(20, tmp_gb=500)
        r = sd._c_base_datos(conn)
        comprobar("base de 10 GB con 20 GB libres: AMARILLO, no verde "
                  "(el aviso va SIEMPRE por encima del rojo)",
                  r["estado"] == sd.WARN
                  and "donde vive la base" in r["accion"], str(r))
        _libres(19, tmp_gb=500)                        # por debajo de 2x
        comprobar("y con 19 GB (menos del doble) ya es ROJO",
                  sd._c_base_datos(conn)["estado"] == sd.CRIT,
                  str(sd._c_base_datos(conn)))
        _libres(26, tmp_gb=500)                        # 20 GB + 5 de tope
        comprobar("con 26 GB libres vuelve a VERDE: el margen del aviso "
                  "lleva techo y no exige 60 GB",
                  sd._c_base_datos(conn)["estado"] == sd.OK,
                  str(sd._c_base_datos(conn)))
        _MB400 = 400 * 10**6

        # Cuando los DOS montajes van justos, el consejo nombra los dos:
        # en ROJO y tambien en el aviso previo.
        _libres(0.6, tmp_gb=0.3)
        r = sd._c_base_datos(conn)
        comprobar("si faltan sitio la carpeta de la base Y el temporal, "
                  "el consejo nombra los dos",
                  r["estado"] == sd.CRIT
                  and "la carpeta de la base y el temporal"
                  in r["accion"], r["accion"])
        _libres(0.9, tmp_gb=0.65)     # los dos justos, ninguno en rojo
        r = sd._c_base_datos(conn)
        comprobar("y en AMARILLO tambien los nombra los dos",
                  r["estado"] == sd.WARN
                  and "donde vive la base" in r["accion"]
                  and "temporal" in r["accion"], r["accion"])


        # Y una base diminuta tiene banda amarilla antes del rojo.
        _MB400 = 1000
        _libres(3)
        comprobar("base diminuta con 3 GB libres: VERDE",
                  sd._c_base_datos(conn)["estado"] == sd.OK)
        _libres(0.7)
        comprobar("con 700 MB libres: AMARILLO (aviso antes del rojo)",
                  sd._c_base_datos(conn)["estado"] == sd.WARN)
        _libres(0.1)
        comprobar("con 100 MB libres: ROJO",
                  sd._c_base_datos(conn)["estado"] == sd.CRIT)
        _MB400 = 400 * 10**6

        # Si no se puede medir el disco, AMARILLO: ciego no es sano.
        def _revienta(p):
            raise PermissionError("sin permiso")
        _sh.disk_usage = _revienta
        r = sd._c_base_datos(conn)
        comprobar("si no se puede medir el disco, AMARILLO y se dice",
                  r["estado"] == sd.WARN
                  and "no se pudo medir" in r["detalle"], str(r))

        # Los huecos de las purgas: informacion, no alarma.
        _libres(500)
        _real_exec = conn.execute

        class _ConnHuecos:
            """Base falsa con N páginas libres de 4 KB."""

            def __init__(self, paginas):
                self.paginas = paginas

            def execute(self, sql, params=()):
                if "freelist_count" in sql:
                    return _CursorFalso([(self.paginas,)])
                if "page_size" in sql:
                    return _CursorFalso([(4096,)])
                return _real_exec(sql, params)

            def commit(self):
                return conn.commit()

        r = sd._c_base_datos(_ConnHuecos(30000))      # ≈123 MB de 400
        comprobar("cuando hay huecos gordos lo dice, y no como alarma",
                  "huecos de purgas" in r["detalle"]
                  and r["estado"] == sd.OK, str(r))
        comprobar("y no manda compactar (no hay mando que lo haga)",
                  "compactando" not in r["detalle"], r["detalle"])
        # Los DOS lados del umbral, porque un hueco pequeño mencionado
        # asusta sin motivo: hacen falta 20 MB Y un 15% del archivo.
        r = sd._c_base_datos(_ConnHuecos(2500))       # ≈10 MB: poco bulto
        comprobar("10 MB de huecos no se mencionan (por debajo de 20 MB)",
                  "huecos de purgas" not in r["detalle"], r["detalle"])
        r = sd._c_base_datos(_ConnHuecos(6100))       # ≈25 MB = 6% de 400
        comprobar("25 MB de huecos en 400 MB tampoco (es solo el 6%)",
                  "huecos de purgas" not in r["detalle"], r["detalle"])
        # Y el suelo de 20 MB por separado: aqui el PORCENTAJE si pasa
        # (12 de 60 MB = 20%), pero 12 MB no son bulto que explicar.
        _MB400 = 60 * 10**6
        r = sd._c_base_datos(_ConnHuecos(2930))       # ≈12 MB = 20% de 60
        comprobar("12 MB de huecos no se mencionan aunque sean el 20% "
                  "del archivo: no llegan al suelo de 20 MB",
                  "huecos de purgas" not in r["detalle"], r["detalle"])
        r = sd._c_base_datos(_ConnHuecos(7300))       # ≈30 MB = 50% de 60
        comprobar("30 MB de huecos en 60 MB si se mencionan",
                  "huecos de purgas" in r["detalle"], r["detalle"])
        _MB400 = 400 * 10**6

        # La copia diaria: tamaño REAL y EDAD, y solo si CABE en Telegram.
        _os.makedirs(_os.path.join(_d, "backups"))
        _cop = _os.path.join(_d, "backups", "wallets_backup_1.db.gz")
        open(_cop, "wb").close()
        _tam[_os.path.abspath(_cop)] = 60 * 10**6

        # El job apunta la hora DESPUES de copiar; sin esa marca, el
        # archivo que hay en `backups/` no es una copia terminada.
        from db import get_setting as _gset
        _ts_prev = _gset(conn, "last_backup_ts", None)
        _tsm_prev = _gset(conn, "last_backup_manual_ts", None)

        def _marcar(ts, clave="last_backup_ts"):
            """Pone UNO de los dos relojes y borra el otro, para que la
            prueba no dependa de lo que haya dejado nadie."""
            for _k in ("last_backup_ts", "last_backup_manual_ts"):
                if _k == clave:
                    _sset(conn, _k, ts)
                else:
                    conn.execute("DELETE FROM settings WHERE key=?", (_k,))
            conn.commit()
        _marcar(_t.time())
        r = sd._c_base_datos(conn)
        comprobar("dice el tamaño real de la ultima copia y que no cabe "
                  "en Telegram",
                  "60 MB" in r["detalle"]
                  and "no cabe en Telegram" in r["detalle"], r["detalle"])
        comprobar("NO afirma que se enviara (eso no lo sabe: el POST "
                  "puede haber muerto)",
                  "enviada" not in r["detalle"], r["detalle"])
        comprobar("y dice la EDAD, para no contradecir al aviso de backup",
                  "hace 0 h" in r["detalle"], r["detalle"])
        _tam[_os.path.abspath(_cop)] = 10 * 10**6
        r = sd._c_base_datos(conn)
        comprobar("una copia pequeña se anuncia como 'cabe en Telegram', "
                  "no como 'enviada'",
                  "cabe en Telegram" in r["detalle"]
                  and "enviada" not in r["detalle"], r["detalle"])
        _os.utime(_cop, (_t.time() - 40 * 86400,) * 2)
        comprobar("una copia de hace 40 dias se anuncia como vieja",
                  "hace 960 h" in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        # Si el reloj del equipo iba adelantado cuando se hizo la copia,
        # el archivo y su marca quedan "en el futuro" y la resta salia
        # negativa: "hace -2 h".
        _os.utime(_cop, (_t.time() + 7200,) * 2)
        _marcar(_t.time() + 7200)
        comprobar("una copia con fecha futura no dice 'hace -2 h'",
                  "hace 0 h" in sd._c_base_datos(conn)["detalle"]
                  and "hace -" not in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        # Con varias copias se mira la MAS NUEVA, no la mas vieja: la
        # rotacion guarda 5 y la vieja podria ser de hace una semana.
        _vieja = _os.path.join(_d, "backups", "wallets_backup_0.db.gz")
        open(_vieja, "wb").close()
        _tam[_os.path.abspath(_vieja)] = 99 * 10**6
        _os.utime(_vieja, (_t.time() - 30 * 86400,) * 2)
        _os.utime(_cop, (_t.time() - 3600,) * 2)
        _marcar(_t.time() - 3600)
        r = sd._c_base_datos(conn)
        comprobar("con dos copias se informa de la MAS NUEVA (10 MB hace "
                  "1 h), no de la vieja de 99 MB",
                  "10 MB hace 1 h" in r["detalle"]
                  and "99 MB" not in r["detalle"], r["detalle"])
        _os.remove(_vieja)

        # EL CASO QUE MOTIVA EL CRUCE: el job copia con `copyfile` al
        # nombre definitivo y apunta la hora DESPUES. Si el disco se
        # llena a mitad queda un archivo truncado y flamante, y /salud
        # lo anunciaba como "última copia 0 MB hace 0 h" justo encima de
        # `_c_backup` gritando "SIN RESPALDO desde hace 90 h".
        # (a) El archivo es MAS NUEVO que la marca del job: no se le
        # llama copia, pero TAMPOCO se le acusa. Hay un camino honrado
        # que deja el reloj sin marcar (el /backup manual guarda bien y
        # revienta luego al enviar por Telegram).
        _os.utime(_cop, (_t.time(),) * 2)
        _marcar(_t.time() - 90 * 3600)
        _tam[_os.path.abspath(_cop)] = 10 * 10**6
        r = sd._c_base_datos(conn)
        comprobar("un archivo mas nuevo que la marca del job NO se "
                  "anuncia como copia terminada, pero SI se dice su edad",
                  "última copia" not in r["detalle"]
                  and "ningún backup lo ha registrado" in r["detalle"]
                  and "hace 0 h" in r["detalle"], r["detalle"])
        comprobar("y no se le acusa de estar a medias: puede ser una "
                  "copia buena cuyo reloj no se llego a marcar",
                  "a medias" not in r["detalle"], r["detalle"])
        comprobar("de ese archivo tampoco se afirma que quepa en "
                  "Telegram",
                  "cabe en Telegram" not in r["detalle"], r["detalle"])
        # (b) El archivo esta VACIO: eso no admite interpretacion.
        _marcar(_t.time())
        _tam[_os.path.abspath(_cop)] = 0
        comprobar("un archivo de 0 bytes se dice VACIO, no 'ultima copia'",
                  "VACÍO" in sd._c_base_datos(conn)["detalle"]
                  and "última copia"
                  not in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        _tam[_os.path.abspath(_cop)] = 10 * 10**6
        comprobar("con la marca al dia vuelve a ser una copia normal",
                  "última copia" in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        # Una copia PEQUEÑA no puede anunciarse como "0 MB": esa cifra la
        # usa la linea de al lado para decir VACIO.
        _tam[_os.path.abspath(_cop)] = 300 * 10**3
        r = sd._c_base_datos(conn)
        comprobar("una copia de 300 KB se dice en KB, no como '0 MB'",
                  "300 KB" in r["detalle"]
                  and "copia 0 MB" not in r["detalle"],
                  r["detalle"])
        comprobar("y VACIO sigue reservado para el archivo de 0 bytes",
                  "VACÍO" not in r["detalle"], r["detalle"])
        _tam[_os.path.abspath(_cop)] = 10 * 10**6
        # (c) El /backup MANUAL usa SU PROPIO reloj: con solo ese puesto,
        # la copia sigue siendo una copia.
        _marcar(_t.time(), "last_backup_manual_ts")
        comprobar("una copia registrada solo por el /backup manual "
                  "tambien cuenta como copia",
                  "última copia" in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        # (d) El margen es de UNA HORA, no de un dia: entre guardar y
        # marcar solo hay dos viajes a Telegram.
        _marcar(_t.time() - 3 * 3600)
        comprobar("con la marca de hace 3 h y el archivo de ahora, ya no "
                  "se le llama copia terminada",
                  "última copia"
                  not in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        _marcar(_t.time() - 600)
        comprobar("pero 10 minutos de diferencia entran en el margen",
                  "última copia" in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])
        _marcar(_t.time())

        # La vara del temporal se MIDE con la ultima copia, no se supone:
        # el pico ahi es la copia sin comprimir MAS su .gz.
        _tam[_os.path.abspath(_cop)] = 380 * 10**6     # casi no comprime
        _libres(500, tmp_gb=0.7)
        comprobar("si la copia casi no comprime, 700 MB de temporal no "
                  "bastan para una base de 400 MB: ROJO",
                  sd._c_base_datos(conn)["estado"] == sd.CRIT,
                  str(sd._c_base_datos(conn)))
        _tam[_os.path.abspath(_cop)] = 10 * 10**6      # comprime mucho
        _libres(500, tmp_gb=0.55)
        comprobar("y si comprime mucho, 550 MB bastan: no es ROJO",
                  sd._c_base_datos(conn)["estado"] != sd.CRIT,
                  str(sd._c_base_datos(conn)))
        # El tope de 2x: una copia MAS GRANDE que la base no puede pedir
        # 3x de temporal.
        _tam[_os.path.abspath(_cop)] = 900 * 10**6
        _libres(500, tmp_gb=0.9)
        comprobar("aunque la copia sea mayor que la base, al temporal no "
                  "se le piden mas de 2x (900 MB para 400 MB: no ROJO)",
                  sd._c_base_datos(conn)["estado"] != sd.CRIT,
                  str(sd._c_base_datos(conn)))
        # Y el suelo de 1,3x: una copia diminuta tampoco baja la vara a
        # ras de suelo, porque en el temporal esta ADEMAS el .db entero.
        _tam[_os.path.abspath(_cop)] = 1 * 10**6
        _libres(500, tmp_gb=0.45)
        comprobar("y con una copia diminuta la vara no baja de 1,3x "
                  "(450 MB para 400 MB: ROJO)",
                  sd._c_base_datos(conn)["estado"] == sd.CRIT,
                  str(sd._c_base_datos(conn)))

        # EL /backup MANUAL guarda el .db SIN comprimir en `backups/`.
        # Medir la compresion con ese archivo daria tasa ~1, la vara del
        # temporal se iria al tope y /salud daria ROJO 24 h por haber
        # pedido una copia a mano. Solo se aprende de un `.gz`.
        _db_manual = _os.path.join(_d, "backups", "backup_20260830_1200.db")
        open(_db_manual, "wb").close()
        _tam[_os.path.abspath(_db_manual)] = 400 * 10**6
        _os.utime(_db_manual, (_t.time(),) * 2)
        _marcar(_t.time(), "last_backup_manual_ts")
        _libres(500, tmp_gb=0.7)
        r = sd._c_base_datos(conn)
        comprobar("una copia manual SIN comprimir no dispara el rojo del "
                  "temporal: la tasa solo se aprende de un .gz",
                  r["estado"] != sd.CRIT, str(r))
        comprobar("(pero si se informa de ella)",
                  "400 MB hace 0 h" in r["detalle"], r["detalle"])
        _os.remove(_db_manual)
        _tam.pop(_os.path.abspath(_db_manual), None)
        _marcar(_t.time())

        # Mismo volumen = UN solo sitio. Se nota cuando la vara que
        # aprieta es la del temporal (base grande + copia que casi no
        # comprime): sin juntarlos, /salud mandaria a mirar TMPDIR de una
        # carpeta que vive en el mismo disco que acaba de nombrar.
        _MB400 = 10 * 10**9
        _tam[_os.path.abspath(_cop)] = int(9.5 * 10**9)
        _libres(27)
        r = sd._c_base_datos(conn)
        comprobar("con la base y el temporal en el mismo disco, el aviso "
                  "habla de UN solo sitio",
                  r["estado"] == sd.WARN
                  and "donde vive la base" in r["accion"]
                  and "temporal" not in r["accion"], str(r))
        _MB400 = 400 * 10**6
        _tam[_os.path.abspath(_cop)] = 10 * 10**6
        # Y en ROJO tambien: con un disco unico no puede mandar a mirar
        # TMPDIR de una carpeta que esta en el disco que acaba de nombrar
        # (y el rojo es el que sale por Telegram cada 12 h).
        _libres(0.4)
        r = sd._c_base_datos(conn)
        comprobar("con un solo disco, el ROJO tampoco nombra el temporal",
                  r["estado"] == sd.CRIT
                  and "carpeta de la base" in r["accion"]
                  and "temporal" not in r["accion"], str(r))
        _libres(500)
        # Un subdirectorio con 'backup_' en el nombre no es una copia.
        _os.remove(_cop)
        _os.makedirs(_os.path.join(_d, "backups", "backup_carpeta"))
        comprobar("un subdirectorio no se cuenta como copia",
                  "última copia" not in sd._c_base_datos(conn)["detalle"],
                  sd._c_base_datos(conn)["detalle"])

        # En Postgres no hay archivo local que medir: nada de esto aplica
        # y la comprobacion sale en verde sin tocar el disco.
        _db = __import__("db")
        _pg_prev = _db.USE_PG
        try:
            _db.USE_PG = True
            _libres(0.001)          # disco lleno: da igual, no es nuestro
            r = sd._c_base_datos(conn)
            comprobar("con la base en Postgres, VERDE y sin mirar el "
                      "disco local",
                      r["estado"] == sd.OK and "Postgres" in r["detalle"]
                      and "GB libres" not in r["detalle"], str(r))
        finally:
            _db.USE_PG = _pg_prev

        # Si el sistema no sabe decir en que volumen esta cada carpeta,
        # se cae al respaldo por ruta: con la base y el temporal en la
        # MISMA carpeta, siguen siendo un solo sitio.
        _libres(500)
        _gt_prev0 = _tf.gettempdir
        try:
            _tf.gettempdir = lambda: _os.path.dirname(_base)

            import stat as _stmod

            def _stat_revienta(p, *a, **k):
                # Solo revienta para CARPETAS: es lo unico que mira la
                # comprobacion de volumen. Si reventara para todo,
                # `getsize`/`getmtime` caerian tambien y la prueba
                # pasaria por el camino equivocado.
                r = _st_prev(p, *a, **k)
                if _stmod.S_ISDIR(r.st_mode):
                    raise OSError("st_dev no disponible")
                return r
            _os.stat = _stat_revienta
            r = sd._c_base_datos(conn)
            comprobar("si no se puede mirar el volumen, el respaldo por "
                      "ruta sigue viendo un solo sitio",
                      "en el temporal" not in r["detalle"], r["detalle"])
        finally:
            _os.stat = _st_prev
            _tf.gettempdir = _gt_prev0

        # Los consejos van en cursiva `_{accion}_`: un `_` suelto de una
        # ruta o de una excepcion rompe el Markdown del mensaje ENTERO.
        # Se juzga con el ORACULO de tdlib, no con "reglas razonables":
        # lo que importa no es que no quede ningun `_`, sino que el
        # mensaje que sale de `salud_text` lo acepte Telegram.
        _sucio = sd._chk("Base de datos", sd.CRIT, "x",
                         sd._md_plano(
                             "[Errno 13] /var/tmp_x a*b`c 'trades_store'"))
        comprobar("_md_plano deja un consejo que Telegram acepta",
                  _valido(sd.salud_text([_sucio])) is None,
                  str(_valido(sd.salud_text([_sucio]))))
        _crudo = sd._chk("Base de datos", sd.CRIT, "x",
                         "[Errno 13] /var/tmp_x a*b`c 'trades_store'")
        comprobar("(y sin el helper, ese mismo consejo lo rechazaria: la "
                  "prueba vale de algo)",
                  _valido(sd.salud_text([_crudo])) is not None)
        # Y en el DETALLE, que NO va en cursiva: ahi un `*` suelto si
        # rompe (dentro de la cursiva el analizador se salta el interior,
        # por eso el consejo solo no probaba el `*`).
        # Cada simbolo de la tabla, UNO A UNO y en el DETALLE (que NO va
        # en cursiva: dentro de la cursiva el analizador se salta el
        # interior, asi que el consejo solo no probaba ni el `*` ni el
        # backtick ni el `[`).
        for _feo in ("grpc: rechazado en *auth",
                     "grpc: rechazado en `auth",
                     "grpc: rechazado en [401",
                     "grpc: rechazado en /var/tmp_x"):
            comprobar(f"saneado en el DETALLE: {_feo[-6:]}",
                      _valido(sd.salud_text(
                          [sd._chk("X", sd.CRIT,
                                   sd._md_plano(_feo))])) is None
                      and _valido(sd.salud_text(
                          [sd._chk("X", sd.CRIT, _feo)])) is not None,
                      str(_valido(sd.salud_text(
                          [sd._chk("X", sd.CRIT, _feo)]))))
        # NINGUN chequeo del modulo puede meter el texto de una
        # excepcion crudo en el mensaje: `salud_text` envuelve el consejo
        # en cursiva y un `_` impar tumba el formato de /salud ENTERO
        # justo cuando hay un fallo que contar. Se comprueba sobre el
        # codigo fuente, no chequeo a chequeo: asi cubre tambien los que
        # no se pueden provocar desde aqui (la IA local, el arranque de
        # `diagnostico`).
        import ast as _ast
        _src = open(sd.__file__, encoding="utf-8").read()
        # Nombres cuyo contenido lo escribe ALGUIEN DE FUERA: el texto de
        # una excepcion, la url y el modelo que teclea el dueño, el
        # `error` que guarda LaserStream (que es `str(e)`), y el modulo y
        # el tipo del error mas frecuente. Si mañana entra otro dato
        # ajeno al mensaje, hay que añadirlo AQUI.
        _FUERA = {"e", "_e", "url", "modelo", "peor", "ls_err", "error",
                  "modulo", "tipo"}
        # Un numero no puede llevar formato de Markdown, y `_md_plano` es
        # justo el saneador: dentro de estas llamadas ya no se mira.
        _SEGURAS = ("_md_plano", "int", "float", "len", "round")
        _crudos = []

        def _revisar(_n, _seguro):
            if isinstance(_n, _ast.Call):
                _f = (getattr(_n.func, "id", "")
                      or getattr(_n.func, "attr", ""))
                if _f in _SEGURAS:
                    _seguro = True
            if isinstance(_n, _ast.IfExp):
                # La CONDICION no sale en el mensaje; solo las ramas.
                _revisar(_n.body, _seguro)
                _revisar(_n.orelse, _seguro)
                return
            if (isinstance(_n, _ast.Name) and _n.id in _FUERA
                    and not _seguro):
                _crudos.append(f"linea {_n.lineno}: {_n.id}")
            for _h in _ast.iter_child_nodes(_n):
                _revisar(_h, _seguro)

        _railway = []
        for _n in _ast.walk(_ast.parse(_src)):
            if not (isinstance(_n, _ast.Call)
                    and getattr(_n.func, "id", "") == "_chk"):
                continue
            for _arg in list(_n.args) + [_k.value for _k in _n.keywords]:
                _revisar(_arg, False)
                for _c in _ast.walk(_arg):
                    if (isinstance(_c, _ast.Constant)
                            and isinstance(_c.value, str)
                            and "Railway" in _c.value):
                        _railway.append(f"linea {_c.lineno}")
        comprobar("ningun chequeo mete texto de fuera en el mensaje sin "
                  "pasarlo por _md_plano",
                  not _crudos, _crudos[:4])
        comprobar("y ningun consejo manda al dueño a Railway, que dejo "
                  "de ser produccion el 26/8",
                  not _railway, _railway[:4])

        # El guardian de arriba mira el texto AJENO. Este mira el
        # NUESTRO: `salud_text` envuelve el consejo en cursiva `_{...}_`,
        # asi que un `_` impar en una frase nuestra (`PUBLIC_URL`,
        # `bot_local.env`) tumba el formato del /salud ENTERO — y en un
        # chequeo CRIT eso sale ademas por `revisar_y_avisar` cada 12 h.
        # Los backticks NO protegen dentro de la cursiva; en el detalle,
        # que no va en cursiva, si. Se juzga con el oraculo de tdlib.
        def _asignaciones(_fn):
            """name -> lista de expresiones que se le asignan."""
            _m = {}
            for _x in _ast.walk(_fn):
                if isinstance(_x, _ast.Assign):
                    for _tg in _x.targets:
                        if isinstance(_tg, _ast.Name):
                            _m.setdefault(_tg.id, []).append(_x.value)
                elif (isinstance(_x, _ast.AugAssign)
                      and isinstance(_x.target, _ast.Name)):
                    _m.setdefault(_x.target.id, []).append(_x.value)
            return _m

        def _lits(_a, _m, _p=0):
            """Variantes de texto LITERAL de una expresion. Resuelve las
            variables locales (los mensajes de `_c_base_datos` se arman
            en `det`, `_donde`, `_como`, `_aviso`, `_extra`…, y sin esto
            el guardian veia una cadena vacia) y separa las dos ramas de
            un `x if c else y`. Lo interpolado que no sea una variable
            local se ignora: eso ya lo cubre `_md_plano`."""
            if _p > 4:
                return [""]
            if isinstance(_a, _ast.Constant):
                return [_a.value] if isinstance(_a.value, str) else [""]
            if isinstance(_a, _ast.JoinedStr):
                _out = [""]
                for _v in _a.values:
                    if isinstance(_v, _ast.Constant):
                        _out = [_o + str(_v.value) for _o in _out]
                    elif (isinstance(_v, _ast.FormattedValue)
                          and isinstance(_v.value, _ast.Name)):
                        _sub = _lits(_v.value, _m, _p + 1)[:3]
                        _out = [_o + _s for _o in _out for _s in _sub]
                return _out[:6]
            if isinstance(_a, _ast.BinOp) and isinstance(_a.op, _ast.Add):
                _i = _lits(_a.left, _m, _p + 1)[:3]
                _d2 = _lits(_a.right, _m, _p + 1)[:3]
                return [_x + _y for _x in _i for _y in _d2][:6]
            if isinstance(_a, _ast.IfExp):
                return (_lits(_a.body, _m, _p + 1)
                        + _lits(_a.orelse, _m, _p + 1))[:6]
            if isinstance(_a, _ast.Name):
                _out = []
                for _v in _m.get(_a.id, [])[:4]:
                    _out += _lits(_v, _m, _p + 1)
                return _out[:6] or [""]
            return [""]

        _rotos = []
        _arbol = _ast.parse(_src)
        for _fn in _ast.walk(_arbol):
            if not isinstance(_fn, (_ast.FunctionDef,)):
                continue
            _m = _asignaciones(_fn)
            for _n in _ast.walk(_fn):
                if not (isinstance(_n, _ast.Call)
                        and getattr(_n.func, "id", "") == "_chk"):
                    continue
                _a = list(_n.args) + [_k.value for _k in _n.keywords]
                if len(_a) < 3:
                    continue
                for _det in _lits(_a[2], _m)[:4]:
                    for _acc in (_lits(_a[3], _m)[:4] if len(_a) > 3
                                 else [""]):
                        _falso = sd._chk("X", sd.CRIT, _det, _acc)
                        _mal = _valido(sd.salud_text([_falso]))
                        if _mal:
                            _rotos.append(f"linea {_n.lineno}: {_mal} | "
                                          f"{(_det + ' ↳ ' + _acc)[:70]}")
        comprobar("y ningun texto NUESTRO deja un `_` suelto que rompa "
                  "el Markdown del mensaje",
                  not _rotos, _rotos[:4])

        # Y ni siquiera un `_` PAR: dos nombres de variable de entorno en
        # la misma frase se emparejan, Telegram no protesta y el dueño ve
        # media frase en cursiva. En nuestro texto, un `_` va SIEMPRE
        # dentro de backticks.
        import re as _re
        _sueltos = []
        for _fn in _ast.walk(_arbol):
            if not isinstance(_fn, _ast.FunctionDef):
                continue
            _m = _asignaciones(_fn)
            for _n in _ast.walk(_fn):
                if not (isinstance(_n, _ast.Call)
                        and getattr(_n.func, "id", "") == "_chk"):
                    continue
                _a = list(_n.args) + [_k.value for _k in _n.keywords]
                # Igual que `_rotos`: resolviendo variables, porque casi
                # todo el texto de `_c_base_datos` se arma en `det`,
                # `_donde`, `_como`, `_aviso` y `_extra`.
                for _txt in [_x for _ar in _a[2:] for _x in _lits(_ar, _m)]:
                    if "_" in _re.sub(r"`[^`]*`", "", _txt):
                        _sueltos.append(f"linea {_n.lineno}: {_txt[:50]}")
        comprobar("y ningun `_` de nuestro texto viaja fuera de "
                  "backticks",
                  not _sueltos, _sueltos[:4])

        # El aviso automatico tiene que CABER en Telegram (4096) y, si no
        # sale, no puede quemar las 12 h de silencio: la firma se marca
        # ANTES de mandar.
        import sys as _sys
        _rt_prev = _sys.modules.get("realtime")
        _enviados = []

        class _RtFalso:
            @staticmethod
            def tg_send(txt, buttons=None):
                _enviados.append(txt)
                return len(txt.encode("utf-16-le")) // 2 <= 4096

        _muchos = [sd._chk(f"C{_i}", sd.CRIT, "d" * 300, "a" * 300)
                   for _i in range(12)]
        _diag_prev = sd.diagnostico
        _int_prev = sd.interpretar
        try:
            _sys.modules["realtime"] = _RtFalso
            sd.diagnostico = lambda: _muchos
            sd.interpretar = lambda _c: "L" * 1500
            conn.execute("DELETE FROM settings WHERE key LIKE 'salud_ultimo%'")
            conn.commit()
            sd.revisar_y_avisar()
            comprobar("el aviso automatico se recorta para caber en "
                      "Telegram (se cae la lectura de la IA, no el aviso)",
                      _enviados
                      and len(_enviados[0].encode("utf-16-le")) // 2 <= 4096
                      and "Lectura de la IA" not in _enviados[0]
                      and "recortado" in _enviados[0]
                      # y lo que queda son los chequeos, no un muñon
                      and _enviados[0].count("🔴") >= 4,
                      str(len(_enviados[0]) if _enviados else 0))
            comprobar("y lo recortado sigue siendo Markdown valido",
                      _valido(_enviados[0]) is None,
                      str(_valido(_enviados[0])))
            # Se cortan LINEAS ENTERAS: ninguna linea del mensaje puede
            # ser un trozo de otra (cortar por caracteres partiria un
            # `*Nombre*` y devolveria otro 400).
            _todas = set(sd.salud_text(_muchos, con_ia=True).split("\n"))
            comprobar("y ninguna linea sale partida por la mitad",
                      all(_l in _todas
                          for _l in _enviados[0].split("\n")[2:-1]),
                      [_l[:40] for _l in _enviados[0].split("\n")[2:-1]
                       if _l not in _todas][:2])
            # Y si aun asi Telegram lo rechaza, no se marca el silencio.
            _enviados.clear()
            conn.execute("DELETE FROM settings WHERE key LIKE 'salud_ultimo%'")
            conn.commit()
            _RtFalso.tg_send = staticmethod(
                lambda txt, buttons=None: _enviados.append(txt) or False)
            sd.revisar_y_avisar()
            comprobar("si el aviso NO sale, no se queman las 12 h de "
                      "silencio",
                      not (_gset(conn, "salud_ultimo_aviso", "") or ""),
                      repr(_gset(conn, "salud_ultimo_aviso", "")))
        finally:
            sd.diagnostico = _diag_prev
            sd.interpretar = _int_prev
            if _rt_prev is None:
                _sys.modules.pop("realtime", None)
            else:
                _sys.modules["realtime"] = _rt_prev
            conn.execute("DELETE FROM settings WHERE key LIKE 'salud_ultimo%'")
            conn.commit()

        # El guardian estatico no puede ver una lista por comprension:
        # el mensaje que dice QUE VARIABLE falta se comprueba a mano.
        _env_prev = {_k: _os.environ.pop(_k, None)
                     for _k in ("HELIUS_API_KEY", "TELEGRAM_BOT_TOKEN")}
        try:
            _r = sd._c_apis()
            comprobar("el aviso de claves enseña el nombre entero, entre "
                      "backticks, para que se pueda copiar",
                      "`HELIUS_API_KEY`" in _r["detalle"]
                      and "`TELEGRAM_BOT_TOKEN`" in _r["detalle"]
                      and _valido(sd.salud_text([_r])) is None, str(_r))
        finally:
            for _k, _v in _env_prev.items():
                if _v is not None:
                    _os.environ[_k] = _v

        # El texto de la IA es de FUERA: `revisar_y_avisar` lo pide
        # siempre, y un `_` impar del modelo tumbaba el /salud entero.
        _ia_prev = _sys.modules.get("ia_puente")
        _ab_prev = _sys.modules.get("ai_budget")
        _dia_prev = _gset(conn, "salud_ia_dia", None)

        class _IaFalsa:
            @staticmethod
            def hay_ia():
                return True

            @staticmethod
            def completar(prompt, max_tokens=0, timeout=0):
                # UN solo `_`: con dos se emparejan y Telegram lo
                # acepta, asi que la prueba no probaria nada.
                return "Falta PUBLIC_URL: sin ella el webhook no recibe."

        class _AbFalso:
            @staticmethod
            def can_call(_c):
                return True
        try:
            _sys.modules["ia_puente"] = _IaFalsa
            _sys.modules["ai_budget"] = _AbFalso
            conn.execute("DELETE FROM settings WHERE key=?",
                         ("salud_ia_dia",))
            conn.commit()
            _malo = sd._chk("Ingesta", sd.CRIT, "x", "y")
            _txt_ia = sd.interpretar([_malo])
            comprobar("la lectura de la IA se sanea antes de entrar en "
                      "el mensaje",
                      _txt_ia is not None
                      and _valido(sd.salud_text([_malo])
                                  + "\n\n" + _txt_ia) is None,
                      str(_txt_ia))
        finally:
            for _k, _v in (("ia_puente", _ia_prev), ("ai_budget", _ab_prev)):
                if _v is None:
                    _sys.modules.pop(_k, None)
                else:
                    _sys.modules[_k] = _v
            if _dia_prev is not None:
                _sset(conn, "salud_ia_dia", _dia_prev)

        # El guardian de arriba solo ve lo que entra DIRECTO en `_chk`.
        # El error de LaserStream (`str(e)` recortado, laserstream.py) se
        # arma antes en una variable, asi que ademas se comprueba el
        # comportamiento: un error con `_` no puede tumbar el /salud.
        # Un numero IMPAR de `_` es lo que de verdad tumba el mensaje;
        # con dos se emparejan y Telegram lo acepta aunque salga feo.
        _err_feo = "grpc handshake_failed [401] en *auth*"
        _ls_prev = _sys.modules.get("laserstream")

        class _LsFalso:
            @staticmethod
            def activo():
                return True

            @staticmethod
            def estado():
                return {"conectado": False, "recibidas": 0,
                        "error": _err_feo, "descartadas": 0,
                        "arranque": _t.time() - 7200}
        try:
            _sys.modules["laserstream"] = _LsFalso
            _rl = __import__("realtime")
            _ta_prev = _rl.tracked_addresses
            _pu_prev = _rl.PUBLIC_URL
            try:
                _rl.tracked_addresses = lambda: {"W1"}
                _rl.PUBLIC_URL = ""
                for _nombre, _f in (("/salud LaserStream", sd._c_laserstream),
                                    ("/salud Ingesta", sd._c_webhook)):
                    _r = _f()
                    _solo = sd._chk(_r["nombre"], _r["estado"],
                                    _r["detalle"], _r["accion"])
                    comprobar(f"{_nombre}: un error de LaserStream con "
                              f"`_` y `[` no rompe el mensaje",
                              _valido(sd.salud_text([_solo])) is None,
                              str(_valido(sd.salud_text([_solo])))
                              + " | " + str(_r))
            finally:
                _rl.tracked_addresses = _ta_prev
                _rl.PUBLIC_URL = _pu_prev
        finally:
            if _ls_prev is None:
                _sys.modules.pop("laserstream", None)
            else:
                _sys.modules["laserstream"] = _ls_prev
        _imp = ImportError("No module named 'trades_store'")
        comprobar("un ImportError de este modulo tampoco rompe el aviso "
                  "(y crudo si lo rompia)",
                  _valido(sd.salud_text([sd._chk(
                      "Errores", sd.WARN,
                      f"no se pudo comprobar "
                      f"({sd._md_plano(_imp)})")])) is None
                  and _valido(sd.salud_text([sd._chk(
                      "Errores", sd.WARN,
                      f"no se pudo comprobar ({_imp})")])) is not None)
        # "tmpraro" a proposito: el unico `_` de la ruta es el del
        # prefijo `salud18q_`, o sea IMPAR. Con dos se emparejan y
        # Telegram lo acepta, y la prueba no probaria nada.
        _tmp_raro = _os.path.join(_d, "tmpraro")
        _os.makedirs(_tmp_raro, exist_ok=True)
        _gt_prev = _tf.gettempdir
        try:
            _tf.gettempdir = lambda: _tmp_raro
            _libres(500, tmp_gb=0.3)          # solo el temporal va justo
            r = sd._c_base_datos(conn)
            comprobar("con una ruta de temporal con `_`, el consejo no "
                      "rompe el Markdown NI enseña una ruta adulterada",
                      r["estado"] == sd.CRIT
                      and "temporal" in r["accion"]
                      and "TMPDIR / %TEMP%" in r["accion"]
                      and _d not in r["accion"]
                      and _valido(sd.salud_text([r])) is None,
                      str(_valido(sd.salud_text([r]))) + " | " + r["accion"])
        finally:
            _tf.gettempdir = _gt_prev
    finally:
        _sh.disk_usage = _du_prev
        _os.path.getsize = _gs_prev
        _os.stat = _st_prev
        _cfg.DB_PATH = _ruta_prev
        # Los dos relojes se devuelven a como estaban (o se borran si no
        # existian): esta prueba escribe en la base de pruebas y el que
        # venga detras no tiene por que heredarlo.
        for _k, _v in (("last_backup_ts", _ts_prev),
                       ("last_backup_manual_ts", _tsm_prev)):
            try:
                if _v is None:
                    conn.execute("DELETE FROM settings WHERE key=?", (_k,))
                    conn.commit()
                else:
                    _sset(conn, _k, _v)
            except Exception:
                pass
        _sh.rmtree(_d, ignore_errors=True)
        conn.close()


# ---------------------------------------------------------------------
# OLA 19-A - indices que faltaban, arranque a prueba de erratas y
# secretos fuera del control de versiones.
# ---------------------------------------------------------------------
def prueba_19a():
    bloque("19-A - indices, arranque a prueba de erratas y .gitignore")
    import json as _json
    import os as _os
    import subprocess as _sp
    import time as _t
    import db as _db
    from db import get_conn

    conn = get_conn()

    # ── 1) Los indices nuevos existen de verdad tras el arranque ──────
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    for n in ("idx_signals_ts", "idx_wallets_tracked"):
        comprobar(f"el indice {n} se crea al arrancar", n in idx,
                  f"indices presentes: {sorted(idx)}")

    # ── 2) Y sirven: el plan deja de ser un escaneo completo ──────────
    # Esta es la prueba que importa. Que el indice exista no basta: hay
    # que comprobar que el planificador LO ELIGE, porque un indice cuyo
    # prefijo no coincide con el WHERE no se usa (que es justo lo que
    # pasaba con (mint, ts) y (wallet, ts) en la consulta anti-spam).
    def _plan(sql):
        return " | ".join(str(r[-1]) for r in
                          conn.execute("EXPLAIN QUERY PLAN " + sql))

    p_spam = _plan("SELECT COUNT(*) c FROM signals WHERE ts>=1 "
                   "AND alert_intento=1 AND signature<>'x'")
    comprobar("anti-spam global (solo ts) usa idx_signals_ts",
              "idx_signals_ts" in p_spam and "SCAN signals\n" not in p_spam
              and not p_spam.startswith("SCAN signals |")
              and p_spam != "SCAN signals",
              p_spam)

    p_p3 = _plan("SELECT s.wallet, s.chg_24h FROM signals s "
                 "JOIN wallets w ON w.address=s.wallet "
                 "WHERE w.is_tracked=1 AND s.side='compra' "
                 "AND s.chg_24h IS NOT NULL")
    comprobar("la puerta 3 en LOTE entra por las ⭐, no por las señales",
              "idx_wallets_tracked" in p_p3 and "SCAN s" not in p_p3, p_p3)

    p_sen = _plan("SELECT * FROM signals ORDER BY ts DESC LIMIT 10")
    comprobar("/senales ya no ordena en un arbol temporal",
              "TEMP B-TREE" not in p_sen, p_sen)

    # ── 3) corte_actividad: un valor malo CIERRA, nunca abre ──────────
    # El fallo real: `float(os.getenv(...))` a pelo lanzaba ValueError,
    # `top_addresses` lo capturaba y devolvia None = SIN FILTRO. La misma
    # errata cerraba la puerta en wallet_ident y la ABRIA aqui.
    _prev = _os.environ.get("TOP_ACTIVITY_HOURS")
    try:
        def _horas_con(valor):
            """(horas, error). Que LANCE tambien es un fallo: era justo
            lo que hacia `float()` a pelo, y esa excepcion es la que
            `top_addresses` convertia en 'sin filtro'."""
            _os.environ["TOP_ACTIVITY_HOURS"] = valor
            try:
                return ((_t.time() - _db.corte_actividad()) / 3600, None)
            except Exception as _e:
                return (None, f"{type(_e).__name__}: {_e}")

        for malo in ("48h", "4,8", "", "cero", "0", "-5"):
            _h, _err = _horas_con(malo)
            comprobar(f"TOP_ACTIVITY_HOURS={malo!r} cae al defecto de 48 h",
                      _err is None and abs(_h - 48) < 0.2,
                      _err or f"salieron {_h:.1f} h")
        _h, _err = _horas_con("72")
        comprobar("un valor valido si se respeta",
                  _err is None and abs(_h - 72) < 0.2,
                  _err or f"salieron {_h:.1f} h")

        _os.environ["TOP_ACTIVITY_HOURS"] = "48h"
        try:
            r = _db.top_addresses(conn, 50)
            _err = None
        except Exception as _e:
            r, _err = None, f"{type(_e).__name__}: {_e}"
        comprobar("top_addresses NO falla-abierto con una errata",
                  _err is None and r is not None,
                  _err or "devolvio None, que significa 'sin filtro de "
                  "actividad': alertarian y se copiarian ⭐ dormidas")
    finally:
        if _prev is None:
            _os.environ.pop("TOP_ACTIVITY_HOURS", None)
        else:
            _os.environ["TOP_ACTIVITY_HOURS"] = _prev

    # ── 4) Los tres espejos calculan el corte en UN solo sitio ────────
    import inspect as _insp
    import wallet_ident as _wi
    for _fn, _nombre in ((_db.top_wallets, "db.top_wallets"),
                         (_db._operativas, "db._operativas"),
                         (_wi.posicion, "wallet_ident.posicion")):
        _src = _insp.getsource(_fn)
        comprobar(f"{_nombre} usa corte_actividad() y no lo recalcula",
                  "corte_actividad()" in _src
                  and 'getenv("TOP_ACTIVITY_HOURS"' not in _src,
                  "sigue habiendo una copia literal del corte")

    # ── 5) Una errata en el .env ya no impide ARRANCAR ────────────────
    # Se comprueba en un proceso aparte a proposito: hay que ver el
    # IMPORT del modulo con la variable envenenada, y recargar modulos ya
    # importados dentro de la suite dejaria basura para las demas pruebas.
    _env = dict(_os.environ)
    _env.update({
        "HELIUS_DELAY": "0,1",          # coma decimal
        "PORT": "8080abc",
        "TOKEN_DEX_TTL_S": "45s",
        "TOKEN_RUG_TTL_S": "",
        "RADAR_GANADOR_X": "3,0",
        "CLUSTERS_TTL_S": "media hora",
        "CLUSTER_MAX_MIEMBROS": "cuarenta",
        "AUTO_CYCLE_HOURS": "2h",
        "TELEGRAM_ADMIN_ID": "1",
        "DATABASE_URL": "",
    })
    _env.pop("DATABASE_URL")
    _codigo = (
        "import json, config, realtime, token_check, radar, clusters, "
        "telegram_bot\n"
        "print('__VALORES__' + json.dumps({"
        "'HELIUS_DELAY': config.HELIUS_DELAY,"
        "'PORT': realtime.PORT,"
        "'DEX_TTL_S': token_check.DEX_TTL_S,"
        "'RUG_TTL_S': token_check.RUG_TTL_S,"
        "'GANADOR_X': radar.GANADOR_X,"
        "'CLUSTERS_TTL': clusters._TTL,"
        "'CLUSTER_MAX': clusters.MAX_MIEMBROS_LIDERAZGO,"
        "'AUTO_CYCLE_HOURS': telegram_bot.AUTO_CYCLE_HOURS}))"
    )
    _raiz = _os.path.dirname(_os.path.abspath(__file__))
    _p = _sp.run([sys.executable, "-c", _codigo], cwd=_raiz, env=_env,
                 capture_output=True, text=True, timeout=180)
    comprobar("con OCHO variables mal escritas, los modulos IMPORTAN igual",
              _p.returncode == 0,
              (_p.stderr or "")[-400:])
    _vals = {}
    for _l in (_p.stdout or "").splitlines():
        if _l.startswith("__VALORES__"):
            _vals = _json.loads(_l[len("__VALORES__"):])
    _esperado = {"HELIUS_DELAY": 0.1, "PORT": 8080, "DEX_TTL_S": 45,
                 "RUG_TTL_S": 1800, "GANADOR_X": 3.0, "CLUSTERS_TTL": 1800,
                 "CLUSTER_MAX": 40, "AUTO_CYCLE_HOURS": 6.0}
    for _k, _v in _esperado.items():
        comprobar(f"{_k} cae a su defecto en vez de tumbar el arranque",
                  _k in _vals and abs(float(_vals[_k]) - _v) < 1e-9,
                  f"salio {_vals.get(_k)!r}, se esperaba {_v!r}")

    # AUTO_CYCLE_HOURS=0 era el caso silencioso: no lanza excepcion, pero
    # `_con_reloj` hace `if intervalo:` y el 0 es falsy, asi que se salta
    # la guarda del reloj y el ciclo correria en CADA sondeo.
    _env0 = dict(_env)
    _env0["AUTO_CYCLE_HOURS"] = "0"
    _p0 = _sp.run([sys.executable, "-c",
                   "import telegram_bot as t;"
                   "print('__H__', t.AUTO_CYCLE_HOURS)"],
                  cwd=_raiz, env=_env0, capture_output=True, text=True,
                  timeout=180)
    comprobar("AUTO_CYCLE_HOURS=0 no deja el ciclo sin guarda de reloj",
              "__H__ 6.0" in (_p0.stdout or ""),
              (_p0.stdout or "") + (_p0.stderr or "")[-200:])

    # ── 6) Los secretos y la base quedan fuera del control de versiones
    _gi = open(_os.path.join(_raiz, ".gitignore"), encoding="utf-8").read()
    for _patron, _porque in (
            ("bot_local.env", "el .env REAL del PC, con las cuatro claves"),
            ("*.env", "cualquier otro .env"),
            ("*.db", "la base de 330 MB con el historico irreversible"),
            ("*.db-wal", "los archivos vivos de SQLite en WAL"),
            ("backups/", "los respaldos comprimidos")):
        comprobar(f".gitignore cubre {_patron} ({_porque})",
                  _patron in _gi, "no aparece en .gitignore")

    # ── 7) Ninguna dependencia puede saltar de version MAYOR sola ─────
    _req = open(_os.path.join(_raiz, "requirements.txt"),
                encoding="utf-8").read()
    _lineas = [l.strip() for l in _req.splitlines()
               if l.strip() and not l.strip().startswith("#")]
    comprobar("requirements.txt sigue declarando las 6 dependencias",
              len(_lineas) == 6, f"hay {len(_lineas)}: {_lineas}")
    for _l in _lineas:
        comprobar(f"{_l.split('>')[0]} lleva techo de version mayor",
                  "<" in _l,
                  f"'{_l}' deja que pip instale la siguiente mayor sola "
                  f"en cualquier despliegue")

    conn.close()



# ---------------------------------------------------------------------
# OLA 19-B - ningun dato incorrecto entra al historico: la comision de
# red, el detector de bots y las "ganadoras" que nunca se compraron.
# ---------------------------------------------------------------------
def prueba_19b():
    bloque("19-B - comision de red, detector de bots y ventas sin compra")
    import random as _rnd
    import time as _time
    import helius_rpc as _hr
    import wallet_metrics as _wm
    import wallet_profiler as _wp

    # ── 1) traducir() trae la comision ───────────────────────────────
    _entrada = {
        "blockTime": 1_700_000_000, "slot": 5,
        "transaction": {"signatures": ["FIRMA"],
                        "message": {"accountKeys": ["W"]}},
        "meta": {"fee": 5_000_100, "err": None,
                 "preBalances": [1_000_000_000],
                 "postBalances": [900_000_000],
                 "preTokenBalances": [], "postTokenBalances": []},
    }
    _t = _hr.traducir(_entrada)
    comprobar("traducir() incluye la clave 'fee'", "fee" in (_t or {}),
              "sin ella, wallet_profiler._sol_delta lee 0 SIEMPRE y el "
              "PnL sale sesgado a negativo en todo el sistema")
    comprobar("traducir() devuelve la comision en lamports, sin tocar",
              (_t or {}).get("fee") == 5_000_100, str((_t or {}).get("fee")))
    _sin = _hr.traducir({"transaction": {}, "meta": {}})
    comprobar("una tx sin meta.fee da 0, no None (int() no puede fallar)",
              (_sin or {}).get("fee") == 0, str((_sin or {}).get("fee")))
    comprobar("y no se ha perdido ninguna clave del contrato anterior",
              all(k in (_t or {}) for k in
                  ("signature", "timestamp", "slot", "feePayer",
                   "transactionError", "tokenTransfers", "nativeTransfers",
                   "accountData")),
              sorted((_t or {}).keys()))

    # ── 2) El sesgo desaparece de verdad en _sol_delta ────────────────
    # Compra de 1 SOL EXACTA con 0,0050001 SOL de comision, pagada por W.
    # `accountData` ya trae la comision descontada, asi que el coste del
    # TOKEN solo sale bien si se le devuelve.
    _tx = {"feePayer": "W", "fee": 5_000_100,
           "accountData": [{"account": "W",
                            "nativeBalanceChange": -(1_000_000_000
                                                     + 5_000_100)}],
           "nativeTransfers": []}
    _con = _wp._sol_delta(_tx, "W")
    comprobar("el coste del token es 1 SOL exacto, sin la comision dentro",
              abs(_con + 1.0) < 1e-9, f"salio {_con:+.7f} SOL")
    _tx_sin = {k: v for k, v in _tx.items() if k != "fee"}
    _sin_fee = _wp._sol_delta(_tx_sin, "W")
    comprobar("sin la clave 'fee' la compra sale MAS CARA (el bug viejo)",
              _sin_fee < _con - 1e-9,
              f"con fee {_con:+.7f} vs sin fee {_sin_fee:+.7f}")
    # La venta se sesga al reves: parece que entro menos SOL del que entro.
    _tx_v = {"feePayer": "W", "fee": 5_000_100,
             "accountData": [{"account": "W",
                              "nativeBalanceChange": (1_000_000_000
                                                      - 5_000_100)}],
             "nativeTransfers": []}
    comprobar("y la venta da 1 SOL exacto, no menos",
              abs(_wp._sol_delta(_tx_v, "W") - 1.0) < 1e-9,
              f"salio {_wp._sol_delta(_tx_v, 'W'):+.7f} SOL")

    # ── 3) active_hours_24 mide DENTRO de un dia ─────────────────────
    def _horas(ts):
        """Replica exacta del calculo nuevo, para poder comparar."""
        _por = {}
        for _x in ts:
            _g = _time.gmtime(_x)
            _por.setdefault((_g.tm_year, _g.tm_yday), set()).add(_g.tm_hour)
        return max((len(_h) for _h in _por.values()), default=0)

    _base = 1_700_000_000
    _rnd.seed(11)
    # Humano que duerme 8 h pero cuya franja SE DESPLAZA de dia en dia.
    # Sobre la muestra entera cubre las 24 horas; en un dia, nunca mas de
    # una franja corta. Este es el caso que marcaba 3.190 billeteras.
    _humano = []
    for _d in range(45):
        _ini = _rnd.randrange(0, 24)
        for _k in range(6):
            _humano.append(_base + _d * 86400
                           + ((_ini + _k) % 24) * 3600
                           + _rnd.randrange(3600))
    _viejo_h = len({_time.gmtime(_x).tm_hour for _x in _humano})
    comprobar("el humano de horario cambiante cubria 22+ horas con el "
              "calculo viejo (o sea: se marcaba bot)", _viejo_h >= 22,
              f"con el calculo viejo daba {_viejo_h}/24")
    comprobar("con el calculo nuevo ya NO llega al umbral de bot",
              _horas(_humano) < 22,
              f"sigue dando {_horas(_humano)}/24 en un solo dia")

    # Bot de verdad: opera las 24 h del dia, todos los dias.
    _bot = [_base + _d * 86400 + _h * 3600 + _rnd.randrange(3600)
            for _d in range(10) for _h in range(24)]
    comprobar("un bot 24/7 de verdad se sigue marcando", _horas(_bot) >= 22,
              f"solo {_horas(_bot)}/24 — el detector dejaria de servir")

    # Propiedad de seguridad del cambio: el valor nuevo NUNCA es mayor que
    # el viejo, asi que esto solo puede DESMARCAR. Ninguna billetera limpia
    # hoy puede pasar a marcarse por este cambio.
    for _n, _ts in (("humano", _humano), ("bot", _bot)):
        comprobar(f"({_n}) el valor nuevo nunca supera al viejo",
                  _horas(_ts) <= len({_time.gmtime(_x).tm_hour
                                      for _x in _ts}),
                  "el cambio podria marcar billeteras que antes no marcaba")

    # Y que el perfilador use ESE calculo, no otro.
    import inspect as _insp
    _src = _insp.getsource(_wp.profile_wallet)
    comprobar("profile_wallet agrupa por dia antes de contar horas",
              "_horas_por_dia" in _src and "tm_yday" in _src,
              "sigue contando horas sobre la muestra entera")

    # ── 4) Las ventas sin compra dejan de ser ganadoras gratis ───────
    _tokens = {
        "REAL_PERDEDORA": {"buys": 1, "sells": 1, "sol_out": 2.0,
                           "sol_in": 1.0, "pnl_sol": -1.0},
        "REAL_GANADORA": {"buys": 1, "sells": 1, "sol_out": 2.0,
                          "sol_in": 3.0, "pnl_sol": 1.0},
        "FANTASMA_1": {"buys": 0, "sells": 1, "sol_out": 0.0,
                       "sol_in": 0.5, "pnl_sol": 0.5},
        "FANTASMA_2": {"buys": 0, "sells": 1, "sol_out": 0.0,
                       "sol_in": 0.2, "pnl_sol": 0.2},
    }
    _cerr_viejo = [i for i in _tokens.values() if i["sells"] > 0]
    _cerr_nuevo = [i for i in _tokens.values()
                   if i["sells"] > 0 and i["buys"] > 0]

    def _wr(c):
        return round(100 * sum(1 for i in c if i["pnl_sol"] > 0) / len(c))

    comprobar("el criterio viejo daba 75% de acierto (pasaba el filtro)",
              _wr(_cerr_viejo) == 75, f"dio {_wr(_cerr_viejo)}%")
    comprobar("el criterio nuevo da el 50% real (no pasa, y es la verdad)",
              _wr(_cerr_nuevo) == 50, f"dio {_wr(_cerr_nuevo)}%")

    _m = _wm.trade_metrics(_tokens)
    comprobar("trade_metrics cuenta 2 cerradas, no 4",
              _m["closed"] == 2, f"conto {_m['closed']}")
    comprobar("y su profit_factor ya no lo infla el dinero regalado",
              _m["profit_factor"] == 1.0, str(_m["profit_factor"]))

    # ESPEJO: las dos listas se calculan igual en los dos modulos.
    for _fn, _nombre in ((_wp.profile_wallet, "wallet_profiler"),
                         (_wm.trade_metrics, "wallet_metrics")):
        _s = _insp.getsource(_fn)
        comprobar(f"{_nombre} exige buys > 0 para contar una cerrada",
                  'buys", 0) > 0' in _s or 'i["buys"] > 0' in _s,
                  "una venta sin compra volveria a contar como ganadora")

    # Y el airdrop vendido sigue sumando al PnL total: lo que se le quita
    # es contar como posicion ACERTADA, no el dinero.
    comprobar("el dinero del airdrop no se pierde del PnL",
              abs(sum(i["pnl_sol"] for i in _tokens.values()) - 0.7) < 1e-9,
              "el PnL total deberia seguir siendo la suma de todos")



# ---------------------------------------------------------------------
# OLA 19-C - fidelidad de la simulacion: carreras que borraban parciales,
# la IA que seguia decidiendo apagada, y el neto de los rugs.
# ---------------------------------------------------------------------
def prueba_19c():
    bloque("19-C - carreras del paper, IA apagada de verdad y neto de rugs")
    import contextlib
    import io
    import paper_trading as pt
    import ejecucion_simulada as es
    from db import get_conn, set_setting, get_setting

    conn = get_conn()
    enviados = []
    tg_real, sol_real, cot_real = pt._tg, pt._sol_a_usd, es.cotizar_venta
    _prev = {k: get_setting(conn, k, None)
             for k in ("paper_total_pct", "paper_parcial_min_pct",
                       "ia_local_activa", "paper_tp_pct", "paper_sl_pct",
                       "paper_timeout_h")}
    try:
        pt._tg = lambda t: enviados.append(t)
        pt._sol_a_usd = lambda *a, **k: 100.0
        set_setting(conn, "paper_tp_pct", "999999")
        set_setting(conn, "paper_sl_pct", "999999")
        set_setting(conn, "paper_timeout_h", "999999")
        set_setting(conn, "paper_parcial_min_pct", "0")
        set_setting(conn, "paper_total_pct", "95")

        def pos_nueva(mint, **campos):
            conn.execute("DELETE FROM paper_trades WHERE mint=?", (mint,))
            base = {"fraccion_restante": 1.0, "pnl_realizado_usd": 0.0,
                    "tokens_raw": None, "gestion": None, "status": "abierta"}
            base.update(campos)
            conn.execute(
                """INSERT INTO paper_trades
                   (mint, symbol, wallet, entry_price, entry_ts, stake_sol,
                    stake_usd, status, fraccion_restante,
                    pnl_realizado_usd, tokens_raw, gestion)
                   VALUES (?,?,'W',1.0,1,1.0,100.0,?,?,?,?,?)""",
                (mint, mint, base["status"], base["fraccion_restante"],
                 base["pnl_realizado_usd"], base["tokens_raw"],
                 base["gestion"]))
            conn.commit()
            return conn.execute("SELECT * FROM paper_trades WHERE mint=?",
                                (mint,)).fetchone()

        # ── 1) El cierre ya no pisa un parcial que llega a mitad ─────
        # Se simula la carrera REAL: `update_open_trades` lee la fila y,
        # mientras cotiza en Jupiter (hasta 12 s), un worker de
        # LaserStream copia una venta parcial. La inyeccion se hace
        # DENTRO de la cotizacion, que es exactamente donde ocurre.
        fila_vieja = pos_nueva("CARRERA", tokens_raw="1000")
        inyectadas = [0]

        def _cotiza_e_inyecta(mint, tokens, su):
            if inyectadas[0] == 0:
                inyectadas[0] += 1
                conn.execute(
                    "UPDATE paper_trades SET fraccion_restante=0.2, "
                    "pnl_realizado_usd=80.0, tokens_raw='200' "
                    "WHERE mint='CARRERA'")
                conn.commit()
            return {"usd_salida": 0.0, "precio_efectivo": 1.0,
                    "slippage_pct": 0.0}

        es.cotizar_venta = _cotiza_e_inyecta
        with contextlib.redirect_stdout(io.StringIO()):
            # `fila_vieja` es la foto ANTIGUA, como la que arrastra el job
            pt._close(conn, fila_vieja, 1.0, "tiempo", "⏱")
        f = conn.execute("SELECT * FROM paper_trades "
                         "WHERE mint='CARRERA'").fetchone()
        comprobar("la posición se cierra igual", f["status"] == "cerrada",
                  f"status = {f['status']}")
        # precio == entrada, asi que pct = 0 y pnl_usd = 0 + realizado.
        # Con el bug: 100*1.0*0/100 + 0 = 0 (los 80 $ desaparecen).
        comprobar("el parcial que llegó a mitad NO se pierde del histórico",
                  f["pnl_usd"] is not None and abs(f["pnl_usd"] - 80.0) < 1e-6,
                  f"pnl_usd = {f['pnl_usd']} (con el bug sale 0.0: se "
                  f"cerraba con la fracción y lo realizado ANTIGUOS)")
        comprobar("y se recalculó con la fila fresca, no con la foto vieja",
                  inyectadas[0] == 1, "la inyección no llegó a ocurrir")

        # ── 2) Dos parciales a la vez: ninguno pisa al otro ──────────
        fila_vieja = pos_nueva("PARCIAL2", tokens_raw="1000")
        inyectadas[0] = 0

        def _cotiza_e_inyecta2(mint, tokens, su):
            if inyectadas[0] == 0:
                inyectadas[0] += 1
                conn.execute(
                    "UPDATE paper_trades SET fraccion_restante=0.4, "
                    "pnl_realizado_usd=25.0 WHERE mint='PARCIAL2'")
                conn.commit()
            return {"usd_salida": 0.0, "precio_efectivo": 1.0,
                    "slippage_pct": 0.0}

        es.cotizar_venta = _cotiza_e_inyecta2
        with contextlib.redirect_stdout(io.StringIO()):
            pt._venta_parcial(conn, fila_vieja, 1.0, 50.0, firma="F-A")
        f = conn.execute("SELECT * FROM paper_trades "
                         "WHERE mint='PARCIAL2'").fetchone()
        # Con el arreglo: el espejo del 50% se aplica sobre 0.4 → 0.2.
        # Con el bug: sobre 1.0 → 0.5, y los 25 $ del otro parcial se
        # pisan con 0.
        comprobar("el segundo espejo se aplica sobre la fracción FRESCA",
                  abs((f["fraccion_restante"] or 0) - 0.2) < 1e-9,
                  f"fracción = {f['fraccion_restante']} (con el bug: 0.5)")
        comprobar("y no borra lo realizado por el parcial que iba delante",
                  abs((f["pnl_realizado_usd"] or 0) - 25.0) < 1e-6,
                  f"realizado = {f['pnl_realizado_usd']} (con el bug: 0.0)")
        es.cotizar_venta = cot_real

        # ── 3) El neto de un rug ya no ignora lo realizado ───────────
        fila = pos_nueva("RUG", fraccion_restante=0.2,
                         pnl_realizado_usd=80.0, tokens_raw=None)
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, fila, 0.0001, "sin liquidez", "💀")
        f = conn.execute("SELECT * FROM paper_trades "
                         "WHERE mint='RUG'").fetchone()
        # neto = realizado - stake*frac - costos = 80 - 100*0.2 - 0 = 60
        comprobar("un rug con parciales sin cotizar ya no se apunta como "
                  "pérdida total",
                  f["pnl_usd_neto"] is not None
                  and abs(f["pnl_usd_neto"] - 60.0) < 1e-6,
                  f"pnl_usd_neto = {f['pnl_usd_neto']} "
                  f"(con el bug: -100.0, la pérdida del importe entero)")
        comprobar("y sigue siendo peor que el bruto (el resto murió)",
                  f["pnl_usd_neto"] < (f["pnl_usd"] or 0) + 1e-9,
                  f"neto {f['pnl_usd_neto']} vs bruto {f['pnl_usd']}")

        # Caso comun intacto: sin parciales, el neto es el de siempre.
        fila = pos_nueva("RUG2", fraccion_restante=1.0,
                         pnl_realizado_usd=0.0, tokens_raw=None)
        with contextlib.redirect_stdout(io.StringIO()):
            pt._close(conn, fila, 0.0001, "sin liquidez", "💀")
        f = conn.execute("SELECT * FROM paper_trades "
                         "WHERE mint='RUG2'").fetchone()
        comprobar("un rug SIN parciales sigue dando la pérdida del importe",
                  abs(f["pnl_usd_neto"] + 100.0) < 1e-6,
                  f"pnl_usd_neto = {f['pnl_usd_neto']}")

        # ── 4) Con ia_local_activa=0, la IA NO decide las salidas ────
        import decision_ia as di
        dec_real = di.decidir_salida
        llamadas = [0]

        def _espia_decidir(*a, **k):
            llamadas[0] += 1
            return {"salida": "holdear", "max_min": 60,
                    "decidido_por": "ia_local", "razon": "prueba"}

        di.decidir_salida = _espia_decidir
        try:
            for activa, esperado in (("0", 0), ("1", 1)):
                set_setting(conn, "ia_local_activa", activa)
                pos_nueva("IAOFF", gestion="ia")
                llamadas[0] = 0
                fila = conn.execute("SELECT * FROM paper_trades "
                                    "WHERE mint='IAOFF'").fetchone()
                with contextlib.redirect_stdout(io.StringIO()):
                    pt.close_on_wallet_sell(
                        conn,
                        {"mint": "IAOFF", "wallet": "W", "side": "venta",
                         "ts": 1, "signature": f"sig-ia-{activa}"},
                        {"price": 1.0, "symbol": "IAOFF", "liq": 50000},
                        {"known": True, "fully_sold": True,
                         "pct_sold": 100.0})
                f = conn.execute("SELECT * FROM paper_trades "
                                 "WHERE mint='IAOFF'").fetchone()
                if activa == "0":
                    comprobar("con ia_local_activa=0 NO se llama a la IA "
                              "aunque la fila diga gestion='ia'",
                              llamadas[0] == 0,
                              f"se llamó {llamadas[0]} veces — bloquea un "
                              f"worker hasta 135 s y puede holdear en "
                              f"pleno modo copia pura")
                    comprobar("y la posición se cierra copiando la venta",
                              f["status"] == "cerrada",
                              f"status = {f['status']}, "
                              f"política = {f['politica']}")
                else:
                    comprobar("con ia_local_activa=1 el experimento A/B "
                              "sigue funcionando igual que antes",
                              llamadas[0] == 1,
                              f"se llamó {llamadas[0]} veces")
                    comprobar("y su decisión de holdear se respeta",
                              f["politica"] == "holdear",
                              f"política = {f['politica']}")
        finally:
            di.decidir_salida = dec_real
            set_setting(conn, "ia_local_activa", "0")

        # ── 5) Una venta del 0% no se copia ni consume el evento ─────
        set_setting(conn, "paper_parcial_min_pct", "0")
        pos_nueva("CERO")
        fila_id = conn.execute("SELECT id FROM paper_trades "
                               "WHERE mint='CERO'").fetchone()["id"]
        conn.execute("DELETE FROM paper_fills WHERE trade_id=?", (fila_id,))
        conn.commit()
        enviados.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            pt.close_on_wallet_sell(
                conn,
                {"mint": "CERO", "wallet": "W", "side": "venta", "ts": 1,
                 "signature": "sig-cero"},
                {"price": 1.0, "symbol": "CERO", "liq": 50000},
                {"known": True, "fully_sold": False, "pct_sold": 0.0})
        f = conn.execute("SELECT * FROM paper_trades "
                         "WHERE mint='CERO'").fetchone()
        comprobar("una venta del 0% no toca la posición",
                  f["status"] == "abierta"
                  and abs((f["fraccion_restante"] or 1) - 1.0) < 1e-9,
                  f"status={f['status']} frac={f['fraccion_restante']}")
        _fills = conn.execute(
            "SELECT COUNT(*) c FROM paper_fills WHERE trade_id=?",
            (fila_id,)).fetchone()["c"]
        comprobar("ni consume la firma: si el dato llega bien luego, se "
                  "puede reprocesar", _fills == 0,
                  f"quedaron {_fills} eventos apuntados")
        comprobar("y no manda ningún mensaje de 'vendió el 0%'",
                  not [m for m in enviados if "CERO" in m],
                  str(enviados))

        # ── 6) El preset de copia pura ya no crea una trampa ─────────
        import telegram_bot as tb
        comprobar("/copiapura ya no fuerza paper_total_pct a 100",
                  tb._COPIA_PURA.get("paper_total_pct") != "100",
                  "con 100 la rama de cierre total es inalcanzable salvo "
                  "que la ⭐ baje del 2%: el resto se queda abierto para "
                  "siempre y bloquea re-copiar ese token")
        comprobar("la clave sigue en el preset (si no, /copiapura off no "
                  "la restauraría)",
                  "paper_total_pct" in tb._COPIA_PURA)
        comprobar("y las demás claves del modo copia pura no se han tocado",
                  tb._COPIA_PURA.get("paper_tp_pct") == "999999"
                  and tb._COPIA_PURA.get("paper_sl_pct") == "999999"
                  and tb._COPIA_PURA.get("paper_timeout_h") == "999999"
                  and tb._COPIA_PURA.get("paper_hold_extra") == "0"
                  and tb._COPIA_PURA.get("ia_local_activa") == "0",
                  str(tb._COPIA_PURA))
    finally:
        pt._tg, pt._sol_a_usd = tg_real, sol_real
        es.cotizar_venta = cot_real
        for k, v in _prev.items():
            if v is not None:
                set_setting(conn, k, v)
        for m in ("CARRERA", "PARCIAL2", "RUG", "RUG2", "IAOFF", "CERO"):
            conn.execute("DELETE FROM paper_trades WHERE mint=?", (m,))
        conn.commit()



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
    prueba_filtro()
    prueba_18m()
    prueba_reembudo()
    prueba_18o_rastrear()
    prueba_18o_promocion()
    prueba_18o_carrera()
    prueba_18o_puertas12()
    prueba_18o_turno()
    prueba_18o_quorum()
    prueba_18o_medidas()
    prueba_18p_radar()
    prueba_18q_salud_base()
    prueba_19a()
    prueba_19b()
    prueba_19c()

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


