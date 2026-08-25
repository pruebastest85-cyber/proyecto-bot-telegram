"""
Grafo de influencia dirigido y ponderado entre billeteras.

Deja de ser "A, B, C compran juntas" para responder:
  - ¿Quién inicia el movimiento? (Leader Score)
  - ¿Quién copia a quién y cuánto tarda? (Follower Score, roles)
  - Peso dirigido A→B (probabilidad de que B compre tras A y en qué tiempo)
  - Líderes ocultos: ROI normal, pero las TOP compran después de ella
  - Predicción: "A compró; B,C,F suelen entrar en 15–40 s (prob 87%)"

Fuente: tabla `appearances` (wallet, mint, buy_time al segundo, buy_rank).
Para cada token ganador ordena a las billeteras por hora de compra y
acumula, sobre TODOS los tokens, quién precede a quién y con qué demora.

DÓNDE SE HACE LA CUENTA — y por qué importa
-------------------------------------------
El conteo de parejas lo hace la BASE DE DATOS, no Python.

Antes se traían todas las apariciones y se recorrían en memoria todas las
parejas de compradores de cada token. Eso crece al CUADRADO: 100
compradores en un token son 4.950 parejas; 500 son 124.750. Se guardaba un
diccionario con una lista por pareja, se cacheaba y no se soltaba nunca.
El servicio moría por «Out of memory» en Railway (agosto 2026).

Ahora la agregación va en SQL con `HAVING shared >= MIN_SHARED`. No se
pierde ni un dato: las parejas con menos coincidencias ya las descartaba
`_weight()`, así que nunca se usaban para nada. Simplemente dejan de
fabricarse y de ocupar memoria.

Empate de tiempos: si dos billeteras compran el token en el mismo segundo,
antes el orden lo decidía el orden en que la base devolvía las filas — es
decir, era arbitrario y podía cambiar entre ejecuciones. Ahora desempata la
dirección de la billetera, que es estable. Es MÁS determinista que antes,
no menos.
"""

import calendar
import threading
import time

import db as _db
from db import get_conn

_CACHE = {"g": None, "ts": 0.0, "fallo": 0.0}
_TTL = 1800        # 30 min. Antes 300 s, pero predictions_job corre
                   # cada 10 min y forzaba una reconstruccion en CADA
                   # pasada. Son datos historicos: media hora de
                   # retraso no cambia ninguna cifra.
MIN_SHARED = 3          # nº mínimo de tokens compartidos para confiar en una arista
STRONG_EDGE = 0.60      # peso mínimo para considerar A→B una relación fuerte


def _ts(s):
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _pg() -> bool:
    return bool(getattr(_db, "USE_PG", False))


def _ts_sql(col: str) -> str:
    """Segundos desde época a partir del texto ISO, o NULL si no tiene ese
    formato exacto. Replica lo que hace `_ts()` en Python: si la cadena no
    encaja con "%Y-%m-%dT%H:%M:%SZ", el tiempo se considera desconocido."""
    if _pg():
        return (f"CASE WHEN {col} ~ "
                f"'^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}:[0-9]{{2}}:"
                f"[0-9]{{2}}Z$' "
                f"THEN CAST(EXTRACT(EPOCH FROM CAST({col} AS timestamp)) "
                f"AS BIGINT) END")
    return (f"CASE WHEN {col} GLOB "
            f"'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
            f"[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z' "
            f"THEN CAST(strftime('%s', {col}) AS INTEGER) END")


def _cte_apariciones() -> str:
    """Apariciones utilizables, con su clave de orden dentro del token.

    `ord` reproduce el criterio de Python: la hora de compra si se conoce
    y, si no, 10^12 + buy_rank para que las desconocidas queden al final.
    """
    ts = _ts_sql("a.buy_time")
    return f"""
    ap AS (
        SELECT a.mint AS mint, a.wallet AS wallet,
               {ts} AS ts,
               COALESCE({ts}, 1000000000000 + COALESCE(a.buy_rank, 0)) AS ord
        FROM appearances a
        JOIN wallets w ON w.address = a.wallet
        WHERE COALESCE(w.is_bot, 0) = 0
    )"""


# Una fila por cada par ordenado (x antes que y) dentro de un mismo token.
# El desempate por dirección de billetera hace el orden estable.
_CTE_PARES = """
    pares AS (
        SELECT x.wallet AS wa, y.wallet AS wb,
               CASE WHEN x.ts IS NOT NULL AND y.ts IS NOT NULL
                         AND y.ts >= x.ts
                    THEN y.ts - x.ts END AS gap
        FROM ap x
        JOIN ap y ON x.mint = y.mint
                 AND (x.ord < y.ord OR (x.ord = y.ord AND x.wallet < y.wallet))
    )"""


def _mediana(origen: str, grupo: str, valor: str) -> str:
    """SQL que devuelve (k, m) con la mediana exacta de `valor` por `grupo`.

    En Postgres lo hace PERCENTILE_CONT. SQLite no lo tiene, así que se
    toma el valor central — o la media de los dos centrales si hay un
    número par — que es exactamente lo que hace statistics.median.
    """
    if _pg():
        return (f"SELECT {grupo} AS k, "
                f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {valor}) AS m "
                f"FROM {origen} WHERE {valor} IS NOT NULL GROUP BY {grupo}")
    return (f"SELECT k, AVG(v * 1.0) AS m FROM ("
            f"  SELECT {grupo} AS k, {valor} AS v,"
            f"         ROW_NUMBER() OVER (PARTITION BY {grupo} "
            f"                            ORDER BY {valor}) AS rn,"
            f"         COUNT(*) OVER (PARTITION BY {grupo}) AS cg"
            f"  FROM {origen} WHERE {valor} IS NOT NULL"
            f") t WHERE rn IN ((cg + 1) / 2, (cg + 2) / 2) GROUP BY k")


def _redondear(x):
    """round() de Python sobre lo que devuelva la base (puede ser Decimal)."""
    return None if x is None else round(float(x))


def _build():
    ap = _cte_apariciones()
    conn = get_conn()
    try:
        # ── Dieta de temporales (13/8/2026) ───────────────────────────
        # El join de pares se recalculaba en CADA una de las ~6 consultas
        # de este build, y en Postgres cada una podia usar 3 workers en
        # paralelo, cada uno con sus propios archivos temporales: el 12/8
        # esa multiplicacion lleno el disco entero (4+ GB de pgsql_tmp) y,
        # tras ponerle tope (temp_file_limit=512MB), excedia el tope y el
        # grafo no se construia. Ahora: sin paralelismo (una consulta = un
        # proceso) y `pares` se materializa UNA vez en una tabla temporal
        # que las demas consultas leen. Mismo resultado, ~1/6 del costo.
        try:
            conn.execute("SET max_parallel_workers_per_gather = 0")
            # MEMORIA en vez de DISCO (17/8/2026): los datos crecieron y
            # los GROUP BY / medianas del build volvieron a derramar mas
            # de 512 MB en archivos temporales, chocando con
            # temp_file_limit: el grafo llevaba dias sin construirse
            # (fallo contenido por el enfriamiento, pero fallo). work_mem
            # deja que cada ordenamiento/hash use hasta 256 MB de RAM
            # antes de tocar disco: mismo resultado exacto, con derrames
            # que vuelven a caber bajo el tope. Es de la SESION (muere
            # con esta conexion) y con el candado solo hay un build a la
            # vez, asi que el pico de memoria queda acotado (~2-3 nodos
            # x 256 MB en un contenedor con varios GB).
            conn.execute("SET work_mem = '256MB'")
        except Exception:
            pass          # SQLite no lo tiene; alli tampoco hace falta
        conn.execute(f"CREATE TEMPORARY TABLE tmp_pares AS "
                     f"WITH {ap}, {_CTE_PARES} SELECT * FROM pares")
        meta = {r["address"]: {"alias": r["alias"],
                               "wallet_score": r["wallet_score"],
                               "ai_class": r["ai_class"]}
                for r in conn.execute(
                    "SELECT address, alias, wallet_score, ai_class FROM wallets"
                ).fetchall()}

        # ── Parejas: solo las que superan MIN_SHARED ──────────────────────
        # `shared` es el nº de tokens en común (las dos direcciones sumadas).
        # Las que no llegan al mínimo las descartaba igualmente _weight(),
        # así que filtrarlas aquí no pierde nada y evita materializarlas.
        #
        # En DOS pasos a proposito (13/8/2026): antes la mediana se
        # calculaba para TODOS los pares y luego el WHERE tiraba casi
        # todas. Ese PERCENTILE_CONT ordenaba 1,8M de filas con clave de
        # texto y, sumado a los joins de la misma consulta, excedia
        # temp_file_limit (512 MB) y el grafo no se construia. Ahora:
        # 1º se decide QUIENES califican (conteo barato), 2º la mediana
        # se calcula SOLO para esos. Resultado identico (lo garantiza el
        # test de equivalencia); el ordenamiento pasa de millones de
        # filas a las de los pares que de verdad forman aristas.
        conn.execute(f"""
            CREATE TEMPORARY TABLE tmp_calif AS
            WITH cnt AS (SELECT wa, wb, COUNT(*) AS n FROM tmp_pares
                         GROUP BY wa, wb)
            SELECT c.wa AS wa, c.wb AS wb, c.n AS n,
                   c.n + COALESCE(r.n, 0) AS shared
            FROM cnt c
            LEFT JOIN cnt r ON r.wa = c.wb AND r.wb = c.wa
            WHERE c.n + COALESCE(r.n, 0) >= {int(MIN_SHARED)}""")
        conn.execute(
            "CREATE TEMPORARY TABLE tmp_pares_calif AS "
            "SELECT p.wa, p.wb, p.gap FROM tmp_pares p "
            "JOIN tmp_calif q ON q.wa = p.wa AND q.wb = p.wb")
        med_par = _mediana("tmp_pares_calif", "wa || '>' || wb", "gap")
        filas = conn.execute(f"""
            WITH med AS ({med_par})
            SELECT q.wa AS wa, q.wb AS wb, q.n AS n, m.m AS med,
                   q.shared AS shared
            FROM tmp_calif q
            LEFT JOIN med m ON m.k = q.wa || '>' || q.wb
        """).fetchall()

        edges, both = {}, {}
        for r in filas:
            edges[(r["wa"], r["wb"])] = {"count": r["n"],
                                         "med_gap": _redondear(r["med"])}
            both[frozenset((r["wa"], r["wb"]))] = r["shared"]

        # ── Estadísticas por billetera ────────────────────────────────────
        def _mapa(sql):
            return {r["k"]: r["m"] for r in conn.execute(sql).fetchall()}

        appear = _mapa(f"WITH {ap} SELECT wallet AS k, COUNT(*) AS m "
                       f"FROM ap GROUP BY wallet")
        leads = _mapa("SELECT wa AS k, COUNT(*) AS m FROM tmp_pares "
                      "GROUP BY wa")
        lags = _mapa("SELECT wb AS k, COUNT(*) AS m FROM tmp_pares "
                     "GROUP BY wb")

        # Líder de cada token = la de menor `ord`, desempatando por
        # dirección. Solo cuenta en tokens con 2+ compradores, igual que
        # antes. Se usa para `first_count` y para la demora.
        cte_lider = f"""
            {ap},
            n_tok AS (SELECT mint, COUNT(*) AS c FROM ap GROUP BY mint),
            lider AS (
                SELECT p.mint AS mint, MIN(p.wallet) AS wallet
                FROM ap p
                JOIN n_tok t ON t.mint = p.mint AND t.c >= 2
                WHERE p.ord = (SELECT MIN(q.ord) FROM ap q
                               WHERE q.mint = p.mint)
                GROUP BY p.mint
            )"""
        first = _mapa(f"WITH {cte_lider} "
                      f"SELECT wallet AS k, COUNT(*) AS m "
                      f"FROM lider GROUP BY wallet")

        # (Ola 17-A) Denominador correcto de pct_first. `first` solo se
        # cuenta en tokens con 2+ compradores (el JOIN de n_tok), pero se
        # dividia entre TODAS las apariciones, incluidos los tokens que la
        # billetera compro sola: una billetera 1ª en el 100% de sus
        # tokens compartidos salia como "1ª el 50% de las veces".
        compart = _mapa(
            f"WITH {ap}, n_tok2 AS (SELECT mint, COUNT(*) AS c FROM ap "
            f"GROUP BY mint) "
            f"SELECT p.wallet AS k, COUNT(*) AS m FROM ap p "
            f"JOIN n_tok2 t ON t.mint = p.mint AND t.c >= 2 "
            f"GROUP BY p.wallet")

        lead_s = _mapa(_mediana('tmp_pares', 'wa', 'gap'))
        lag_s = _mapa(_mediana('tmp_pares', 'wb', 'gap'))

        # Demora de cada billetera respecto al PRIMER comprador del token.
        # Se excluye a la propia líder por dirección, no por posición: así
        # un empate no borra por error a la otra billetera empatada.
        cte_demora = f"""
            {cte_lider},
            prim AS (
                SELECT l.mint AS mint, l.wallet AS wallet, a3.ts AS ts
                FROM lider l
                JOIN ap a3 ON a3.mint = l.mint AND a3.wallet = l.wallet
            ),
            demoras AS (
                SELECT a2.wallet AS wallet, a2.ts - prim.ts AS d
                FROM ap a2
                JOIN prim ON prim.mint = a2.mint
                WHERE a2.wallet <> prim.wallet
                  AND a2.ts IS NOT NULL AND prim.ts IS NOT NULL
                  AND a2.ts >= prim.ts
            )"""
        delay_s = _mapa(f"WITH {cte_demora} "
                        f"{_mediana('demoras', 'wallet', 'd')}")
    finally:
        conn.close()

    wallets = {}
    for w, n_ap in appear.items():
        lo, la = leads.get(w, 0), lags.get(w, 0)
        tot = lo + la
        fc = first.get(w, 0)
        wallets[w] = {
            "alias": (meta.get(w, {}) or {}).get("alias") or w[:6],
            "wallet_score": (meta.get(w, {}) or {}).get("wallet_score"),
            "ai_class": (meta.get(w, {}) or {}).get("ai_class"),
            "appearances": n_ap,
            "first_count": fc,
            # Sobre los tokens COMPARTIDOS, que son los unicos donde
            # "ser la primera" significa algo. None = aun no hay ninguno.
            "shared_tokens": compart.get(w, 0),
            "pct_first": (round(100 * fc / compart[w])
                          if compart.get(w) else None),
            "leader_score": round(100 * lo / tot) if tot else None,
            "follower_score": round(100 * la / tot) if tot else None,
            "avg_lead_s": _redondear(lead_s.get(w)),
            "avg_lag_s": _redondear(lag_s.get(w)),
            "avg_delay_s": _redondear(delay_s.get(w)),
        }
    # (tmp_pares y el SET de paralelismo mueren solos con la conexion,
    #  que el finally de arriba ya cerro)
    return {"edges": edges, "both": both, "wallets": wallets, "meta": meta}


_BUILD_LOCK = threading.Lock()


def _vacio() -> dict:
    """Grafo vacio VALIDO: mismas cuatro claves que el de verdad, para
    que `g["wallets"]` nunca reviente. Uno nuevo en cada llamada, porque
    `_indice_aristas` cuelga su indice del propio dict y no queremos que
    ensucie una constante compartida."""
    return {"edges": {}, "both": {}, "wallets": {}, "meta": {}}


def graph(construir: bool = True):
    """Grafo de co-compra, cacheado.

    `construir=False` (Ola 18-C) es el modo del CAMINO CALIENTE: devuelve
    lo que haya en cache — aunque este pasado de TTL — y si no hay nada,
    un grafo vacio valido. NUNCA construye, NUNCA pide el candado, NUNCA
    toca la base. Es el mismo patron que ya funciona en
    `clusters.find_clusters(construir=False)`.

    Por que: `_build()` son 13 consultas pesadas —37,2 s medidos el 25/8
    sobre la base real del dueño (32.409 apariciones, 362 tokens)— y se
    disparaban desde el hilo de ingesta (webhook/LaserStream) cada vez que
    una ⭐ compraba con el cache caducado. Mientras construia, ese hilo no
    atendia nada: son los ~40 min diarios de senales perdidas que midio la
    auditoria del 25/8.

    Devolver el grafo viejo no es una perdida: las parejas de co-compra se
    mueven en semanas, no en minutos, y `precalentar()` lo refresca desde
    `predictions_job` en cuanto vence el TTL de 30 min.
    """
    if _CACHE["g"] and time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["g"]
    if not construir:
        # Cache caducado o frio: se entrega lo viejo (o el vacio), pero
        # se vuelve sin construir. Copia superficial NO: los llamadores
        # solo leen, y `_indice_aristas` cuelga su indice del propio dict
        # para reutilizarlo entre llamadas.
        return _CACHE["g"] or _vacio()
    # CANDADO: sin el, dos hilos con el cache caducado construian el grafo
    # A LA VEZ y las consultas pesadas corrian duplicadas en Postgres. El
    # 12/8/2026 varios procesos simultaneos llenaron el disco con archivos
    # temporales ("No space left on device"). El que llega segundo espera
    # y recibe el grafo que construyo el primero, sin pagar nada.
    with _BUILD_LOCK:
        # Doble comprobacion: si otro hilo lo construyo mientras
        # esperabamos el candado, ya esta fresco y se usa tal cual.
        if _CACHE["g"] is not None and time.time() - _CACHE["ts"] < _TTL:
            return _CACHE["g"]
        # Enfriamiento tras fallo: el 13/8 la construccion fallaba (temp
        # de Postgres) y cada llamada la reintentaba entera: 13 consultas
        # pesadas en 4 minutos, pura lena al fuego. Si fallo hace poco, no
        # se reintenta: se sigue con lo que haya en cache (Ola 18-C; antes
        # se devolvia el vacio y el camino caliente quedaba ciego 10 min,
        # el mismo fallo que la Ola 17-E cerro en clusters.py).
        if time.time() - _CACHE.get("fallo", 0) < 600:
            return _CACHE["g"] or _vacio()
        # (Ola 18-C) El cache NO se vacia antes de construir. Antes se
        # ponia a None para que durante la construccion no conviviesen dos
        # grafos en RAM (CLAUDE.md §5), pero eso solo tenia sentido cuando
        # el que pedia el grafo era el propio hilo de ingesta y esperaba
        # bloqueado: nadie leia el cache mientras tanto. Ahora el camino
        # caliente NO espera — lee el cache y sigue — asi que vaciarlo lo
        # dejaba ciego durante todo el build, cada 30 min. Se cambia RAM
        # por señales: el grafo viejo se mantiene hasta que el nuevo esta
        # listo, y el relevo es una sola asignacion (atomica).
        try:
            g = _build()
        except Exception as e:
            _CACHE["fallo"] = time.time()
            # No se toca `_CACHE["g"]`: se sigue con el grafo anterior en
            # vez de quedarse ciego (clusters.py hace lo mismo).
            if _CACHE["g"] is None:
                print(f"· Grafo de influencia falló ({e}) y NO hay copia "
                      f"anterior: me quedo sin grafo; reintento en 10 min")
            else:
                print(f"· Grafo de influencia falló ({e}); se sigue con la "
                      f"copia anterior y se reintenta en 10 min")
            return _CACHE["g"] or _vacio()
        _CACHE["g"] = g
        _CACHE["ts"] = time.time()
        _CACHE["fallo"] = 0.0
        return g


def _weight(g, a, b):
    """Peso dirigido A→B: % de tokens compartidos en que A precede a B.

    La mediana de la demora ya viene calculada por la base (`med_gap`);
    antes se guardaba la lista entera de demoras solo para sacarla aquí.
    """
    sh = g["both"].get(frozenset((a, b)), 0)
    if sh < MIN_SHARED:
        return None, sh, None
    e = g["edges"].get((a, b), {})
    return e.get("count", 0) / sh, sh, e.get("med_gap")


def _rol_de(w) -> str | None:
    """El rol a partir de la ficha de la billetera, sin volver a pedir el
    grafo. (Ola 18-C) Antes `influence()` llamaba a `role()`, que pedia
    `graph()` por segunda vez: si el cache cambiaba entre las dos
    lecturas, salia un resultado mezclado de dos grafos distintos."""
    if not w or w["leader_score"] is None:
        return None
    if (w["pct_first"] or 0) >= 50 or w["leader_score"] >= 70:
        return "Líder"
    d = w.get("avg_delay_s")
    if d is None:
        return "Seguidor"
    if d <= 30:
        return "Seguidor temprano"
    if d <= 90:
        return "Confirmador"
    return "Seguidor tardío"


def role(address: str, construir: bool = True) -> str | None:
    return _rol_de(graph(construir=construir)["wallets"].get(address))


def _indice_aristas(g) -> tuple[dict, dict]:
    """Adyacencia por billetera, construida UNA vez por grafo (Ola 5,
    auditoria 19/8 - M16): influence() recorria TODAS las aristas en
    cada llamada, y hidden_leaders() la llama por cada billetera
    candidata — O(billeteras × aristas), minutos de CPU con la escala
    real (~1,8 M de parejas). Con el indice, cada consulta es O(grado).
    Se cuelga del propio dict del grafo: muere con el."""
    idx = g.get("_idx")
    if idx is None:
        salientes, entrantes = {}, {}
        for (a, b) in g["edges"]:
            salientes.setdefault(a, []).append(b)
            entrantes.setdefault(b, []).append(a)
        idx = (salientes, entrantes)
        g["_idx"] = idx
    return idx


def influencia_ligera(address: str) -> dict | None:
    """Rol de liderazgo SOLO desde el cache (Ola 10, 21/8): NUNCA
    construye el grafo. Apta para el hilo del webhook, donde disparar la
    construccion significaria candado + pico de RAM en el camino
    caliente. None si el cache esta frio o la billetera no figura — el
    llamador trata None como 'sin dato', no como 'neutral confirmado'."""
    g = _CACHE.get("g")
    if not g:
        return None
    w = g.get("wallets", {}).get(address)
    if not w:
        return None
    return {"leader_score": w.get("leader_score"),
            "follower_score": w.get("follower_score"),
            "pct_first": w.get("pct_first"),
            "avg_lag_s": w.get("avg_lag_s")}


def influence(address: str, construir: bool = True) -> dict | None:
    """`construir=False`: solo cache, nunca construye (ver `graph`). Con el
    cache frio devuelve None, que los llamadores del camino caliente ya
    tratan como 'sin dato' (no como 'sin seguidores confirmado')."""
    g = graph(construir=construir)
    if address not in g["wallets"]:
        return None
    salientes, entrantes = _indice_aristas(g)
    followers, leaders = [], []
    for b in salientes.get(address, ()):
        wgt, sh, gap = _weight(g, address, b)
        if wgt is not None and wgt >= STRONG_EDGE:
            followers.append({"wallet": b, "alias": g["wallets"].get(b, {}).get("alias", b[:6]),
                              "prob": round(100 * wgt), "eta_s": gap, "shared": sh})
    for a in entrantes.get(address, ()):
        wgt, sh, gap = _weight(g, a, address)
        if wgt is not None and wgt >= STRONG_EDGE:
            leaders.append({"wallet": a, "alias": g["wallets"].get(a, {}).get("alias", a[:6]),
                            "prob": round(100 * wgt), "eta_s": gap, "shared": sh})
    followers.sort(key=lambda x: x["prob"], reverse=True)
    leaders.sort(key=lambda x: x["prob"], reverse=True)
    w = g["wallets"][address]
    return {"role": _rol_de(w),
            "leader_score": w["leader_score"],
            "follower_score": w["follower_score"], "pct_first": w["pct_first"],
            "avg_lead_s": w["avg_lead_s"], "avg_lag_s": w["avg_lag_s"],
            "followers": followers, "leaders": leaders,
            "followers_count": len(followers)}


def cache_lista() -> bool:
    """¿Hay grafo ya construido en cache?

    (Ola 18-C) El camino caliente pide `construir=False` y recibe None
    tanto cuando la billetera NO figura en el grafo como cuando el cache
    aun no se ha construido. Sin distinguirlo, "no tiene seguidores" y
    "todavia no lo se" se confunden — el mismo fallo que la Ola 17-E
    cerro en clusters con `clusters.cache_lista()`."""
    return _CACHE["g"] is not None


def precalentar():
    """Construye la cache del grafo FUERA del camino caliente.

    La llama `predictions_job` cada 10 min; como `graph()` vuelve al
    instante mientras el cache esta fresco, la construccion de verdad solo
    ocurre en la primera pasada tras vencer el TTL de 30 min. Asi los
    llamadores con `construir=False` (ingesta) encuentran siempre un grafo
    listo sin pagar los ~37 s de construccion medidos el 25/8 sobre la
    base real. Mismo arreglo que ya se hizo con `clusters.precalentar`.

    OJO: `graph()` se traga sus propias excepciones y devuelve el grafo
    anterior, asi que el `except` de aqui casi nunca salta. Para no
    cantar "al día" cuando en realidad fallo, se mira `_CACHE["fallo"]`.
    """
    try:
        g = graph()
        n = len(g.get("wallets") or {})
        if not cache_lista():
            # Sin grafo: o es el primer arranque, o fallo y no habia copia.
            # Este es el caso grave — el camino caliente no evalua nada.
            if time.time() - _CACHE["fallo"] < 600:
                print("⚠ Influencia: la construcción falló y no hay copia "
                      "anterior; SIN grafo, reintento en 10 min")
            else:
                print("⚠ Influencia: sin grafo en caché todavía")
        elif time.time() - _CACHE["fallo"] < 600:
            print(f"⚠ Influencia: la construcción falló; se sigue con la "
                  f"copia anterior ({n} billeteras)")
        else:
            edad = int(time.time() - _CACHE["ts"])
            print(f"🕸 Influencia: caché al día ({n} billeteras, "
                  f"{edad//60} min)")
    except Exception as e:
        print(f"· Influencia: no pude precalentar la caché: {e}")


def predict_followers(address: str, min_prob: int = 60) -> dict | None:
    """Si `address` compra, ¿quién suele entrar detrás y en cuánto tiempo?"""
    inf = influence(address)
    if not inf:
        return None
    fol = [f for f in inf["followers"] if f["prob"] >= min_prob]
    return {"leader": address, "followers": fol} if fol else None


def hidden_leaders(limit: int = 10) -> list[dict]:
    """
    Líderes ocultos: Leader Score alto y sus seguidores incluyen billeteras
    TOP, pero su propio wallet_score es sólo normal. Valen más de lo que
    su ROI sugiere.
    """
    g = graph()
    out = []
    for addr, w in g["wallets"].items():
        if (w["leader_score"] or 0) < 65 or w["appearances"] < MIN_SHARED:
            continue
        own = w.get("wallet_score") or 0
        if own >= 65:
            continue    # ya luce fuerte por sí sola; no es "oculta"
        inf = influence(addr)
        top_followers = [f for f in inf["followers"]
                         if (g["wallets"].get(f["wallet"], {}).get("wallet_score") or 0) >= 65]
        if len(top_followers) >= 2:
            out.append({"wallet": addr, "alias": w["alias"],
                        "leader_score": w["leader_score"],
                        "wallet_score": round(own),
                        "top_followers": len(top_followers),
                        "pct_first": w["pct_first"]})
    out.sort(key=lambda x: (x["top_followers"], x["leader_score"]), reverse=True)
    return out[:limit]


def _fmt_eta(s):
    if s is None:
        return "?"
    return f"{s}s" if s < 90 else f"{round(s/60)}min"


def predict_text(address: str) -> str:
    p = predict_followers(address)
    g = graph()
    alias = g["wallets"].get(address, {}).get("alias", address[:6])
    if not p:
        return (f"🔮 No tengo suficientes coincidencias para predecir a los "
                f"seguidores de {alias}. Hace falta que aparezca junto a las "
                f"mismas billeteras en más tokens.")
    out = [f"🔮 *Predicción — si compra {alias}:*\n",
           "Históricamente, tras su compra suelen entrar:"]
    for f in p["followers"][:8]:
        out.append(f"• *{f['alias']}* — {f['prob']}% · ~{_fmt_eta(f['eta_s'])} "
                   f"después ({f['shared']} coincidencias)")
    out.append("\n_Ventana estimada a partir del historial. Úsala para "
               "adelantarte a la propagación del cluster._")
    return "\n".join(out)


def hidden_leaders_text(limit: int = 8) -> str:
    hl = hidden_leaders(limit)
    if not hl:
        return ("🕵️ Aún no detecto líderes ocultos. Necesito más historial de "
                "co-compras entre billeteras rastreadas.")
    out = ["🕵️ *Líderes ocultos* (compran antes que las TOP, pero su ROI no "
           "lo delata):\n"]
    for i, h in enumerate(hl, 1):
        out.append(f"{i}. *{h['alias']}* · Leader {h['leader_score']} · "
                   f"score propio {h['wallet_score']} · "
                   f"{h['top_followers']} seguidoras TOP · "
                   + (f"1ª el {h['pct_first']}% de sus tokens compartidos"
                      if h.get("pct_first") is not None
                      else "sin tokens compartidos aún"))
    out.append("\n_Vigílalas: entran temprano y las buenas van detrás._")
    return "\n".join(out)
