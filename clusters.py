"""
Descubrimiento de clusters de co-compra — la ventaja competitiva.

En vez de analizar billeteras sueltas, busca REDES: grupos de
billeteras que compran temprano LOS MISMOS tokens ganadores una y otra
vez. Si A, B y C aparecen juntas en varios ganadores, cuando una compra
las otras suelen ir detrás → señal colectiva de alto valor.

Fuente: la tabla `appearances` (wallet, mint, buy_rank) que ya llena el
pipeline. No requiere APIs nuevas.

Método:
  1. Por cada token ganador, toma las billeteras que lo compraron
     TEMPRANO (buy_rank <= EARLY_RANK).
  2. Cuenta co-apariciones por par de billeteras.
  3. Une los pares fuertes (>= MIN_SHARED tokens en común) en clusters
     (union-find) y puntúa cada cluster.
"""

import os
import threading
import time
from collections import defaultdict

import db as _db
from db import get_conn

EARLY_RANK = 30       # "temprano" = entre los primeros 30 compradores
MIN_SHARED = 2        # nº mínimo de tokens ganadores compartidos por par

# (Ola 17-A) Mínimo de comparaciones para poder decir quién lidera.
# Sin esto, una billetera que coincidió con otra en UN token y compró
# antes salía con "100% adelanta" y se coronaba 👑 por delante de una
# con 95% sobre 400 comparaciones — y el bot llegaba a decir "vigila
# esta billetera para adelantarte" a partir de una sola coincidencia.
MIN_COMPS_LIDER = 6

# ── (Ola 17-D, auditoria 4) Cache, tope y conteo de pares en SQL ──────
#
# find_clusters() no tenia NINGUNA cache y se llamaba desde
# predictions.on_buy, o sea desde el hilo del webhook, CADA VEZ que una
# ⭐ compraba (4.505 senales en 24 h en la base del dueno). Cada llamada:
#   1. traia appearances a RAM y montaba un diccionario con TODAS las
#      parejas de compradores por token — el mismo patron cuadratico que
#      mato el servicio en influence.py (CLAUDE.md §5), y
#   2. corria _leadership, que es CUBICO: tokens x miembros².
# Con 300 miembros y 2.000 tokens compartidos son ~180 millones de
# iteraciones por señal recibida.
#
# Tres arreglos, los mismos que ya funcionaron en influence.py:
#   a) las parejas las cuenta la BASE con un JOIN agrupado y solo
#      devuelve las que superan el minimo (las demas se descartaban
#      igual, o sea que no se pierde nada);
#   b) cache con TTL y candado de construccion, para que dos hilos no
#      construyan a la vez;
#   c) tope de miembros en el calculo de liderazgo, y se DICE cuando se
#      recorta (un tope silencioso se lee como "lo mire todo").
_CACHE = {"c": None, "ts": 0.0, "fallo": 0.0}


def _int_env(n, d):
    """(19-A) Conversion protegida: una errata en el .env mataba este
    modulo en el import, y de el dependen /clusters, el Meta Score de las
    predicciones y la ficha del ADN."""
    try:
        return int(float(os.getenv(n, d)))
    except (TypeError, ValueError):
        print(f"· {n}={os.getenv(n)!r} no es un numero; se usa {d}")
        return int(d)


_TTL = _int_env("CLUSTERS_TTL_S", 1800)                # 30 min
_BUILD_LOCK = threading.Lock()
MAX_MIEMBROS_LIDERAZGO = _int_env("CLUSTER_MAX_MIEMBROS", 40)


def _pg() -> bool:
    return bool(getattr(_db, "USE_PG", False))


def _leadership(members, shared_tokens, ranks):
    """Ordena a los miembros por quién compra ANTES (líder → seguidores).

    Para cada token compartido compara los buy_rank de los miembros
    presentes: quien tiene menor rank compró primero. Acumula:
      - lead_pct: % de comparaciones en que el miembro fue el más temprano,
      - avg_rank: puesto medio de compra,
      - follows: a quién sigue más a menudo (quién le precede).
    """
    members = set(members)
    leads = {m: 0 for m in members}
    comps = {m: 0 for m in members}
    rank_sum = {m: 0.0 for m in members}
    appears = {m: 0 for m in members}
    precede = {m: defaultdict(int) for m in members}
    for t in shared_tokens:
        present = [(m, ranks[(t, m)]) for m in members
                   if (t, m) in ranks]
        n = len(present)
        if n < 2:
            for m, r in present:
                rank_sum[m] += r
                appears[m] += 1
            continue
        # (Ola 17-D) Antes esto eran DOS bucles anidados sobre `present`
        # por cada token. Ordenando una vez, "cuantos compraron despues
        # que yo" sale de la posicion, sin comparar pareja por pareja.
        present.sort(key=lambda x: x[1])
        for m, r in present:
            rank_sum[m] += r
            appears[m] += 1
        i = 0
        while i < n:
            j = i
            while j + 1 < n and present[j + 1][1] == present[i][1]:
                j += 1                      # bloque de empates
            antes = i                       # con rank ESTRICTAMENTE menor
            despues = n - 1 - j             # con rank ESTRICTAMENTE mayor
            for k in range(i, j + 1):
                m = present[k][0]
                comps[m] += n - 1
                leads[m] += despues
            if antes:
                # `follows`: quien le precede. Se cuentan todos los
                # anteriores, igual que antes.
                for k in range(i, j + 1):
                    pm = precede[present[k][0]]
                    for q in range(0, i):
                        pm[present[q][0]] += 1
            i = j + 1
    order = []
    for m in members:
        # Muestra insuficiente = NO se sabe, no "100%".
        lead_pct = (round(100 * leads[m] / comps[m])
                    if comps[m] >= MIN_COMPS_LIDER else None)
        avg_rank = round(rank_sum[m] / appears[m], 1) if appears[m] else None
        # (Ola 17-E) Desempate por direccion, no por orden de insercion
        # del dict: ese orden depende de como se recorrio un `set`, o sea
        # del PYTHONHASHSEED, y un reinicio podia cambiar "sigue a X" por
        # "sigue a Y" sin que hubiera cambiado ningun dato.
        follows_w = (max(sorted(precede[m].items()), key=lambda kv: kv[1])[0]
                     if precede[m] else None)
        order.append({"wallet": m, "lead_pct": lead_pct, "comps": comps[m],
                      "avg_rank": avg_rank, "appears": appears[m],
                      "follows": follows_w})
    # líder primero: mayor lead_pct, luego menor rank medio
    order.sort(key=lambda o: (-(o["lead_pct"] or -1),
                              o["avg_rank"] if o["avg_rank"] is not None else 1e9))
    return order


def _pares_fuertes(conn, min_shared: int):
    """Parejas con `min_shared`+ tokens tempranos en comun, contadas por la
    BASE. Devuelve [(wa, wb), ...].

    (Ola 17-D) Esto es lo que antes se hacia en RAM recorriendo TODAS las
    parejas de compradores de cada token. Las que no llegan al minimo se
    descartaban igual mas abajo, asi que no se pierde nada: solo deja de
    construirse en memoria un diccionario que crecia al cuadrado.
    """
    marca = "%s" if _pg() else "?"
    sql = f"""
        WITH ap AS (
            SELECT a.mint AS mint, a.wallet AS wallet
            FROM appearances a
            JOIN wallets w ON w.address = a.wallet
            WHERE a.buy_rank IS NOT NULL AND a.buy_rank <= {marca}
              AND COALESCE(w.is_bot, 0) = 0
        )
        SELECT p1.wallet AS wa, p2.wallet AS wb, COUNT(*) AS shared
        FROM ap p1
        JOIN ap p2 ON p2.mint = p1.mint AND p2.wallet > p1.wallet
        GROUP BY p1.wallet, p2.wallet
        HAVING COUNT(*) >= {marca}"""
    filas = conn.execute(sql, (EARLY_RANK, min_shared)).fetchall()
    return [(r["wa"], r["wb"]) for r in filas]


def _datos_de(conn, wallets: set):
    """(tokens_por_billetera, ranks) SOLO de las billeteras implicadas."""
    if not wallets:
        return {}, {}
    tokens = defaultdict(set)
    ranks = {}
    lista = sorted(wallets)
    PASO = 400                      # trocear el IN: SQLite topa en 999
    for i in range(0, len(lista), PASO):
        trozo = lista[i:i + PASO]
        marcas = ",".join("?" * len(trozo))
        filas = conn.execute(
            f"""SELECT mint, wallet, buy_rank FROM appearances
                WHERE buy_rank IS NOT NULL AND buy_rank <= ?
                  AND wallet IN ({marcas})""",
            [EARLY_RANK] + trozo).fetchall()
        for r in filas:
            tokens[r["wallet"]].add(r["mint"])
            ranks[(r["mint"], r["wallet"])] = r["buy_rank"]
    return tokens, ranks


def _build_clusters(min_shared: int) -> list[dict]:
    conn = get_conn()
    try:
        pares = _pares_fuertes(conn, min_shared)
        implicadas = {w for p in pares for w in p}
        tokens_de, ranks = _datos_de(conn, implicadas)
        pair_tokens = {(a, b): tokens_de[a] & tokens_de[b] for a, b in pares}

        # 2) union-find sobre pares fuertes
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        strong = pair_tokens          # la base ya aplico el minimo
        for (a, b) in strong:
            union(a, b)

        # 3) agrupar y describir
        groups = defaultdict(set)
        group_tokens = defaultdict(set)
        for (a, b), toks in strong.items():
            root = find(a)
            groups[root].update((a, b))
            group_tokens[root].update(toks)

        # alias por billetera para mostrar
        alias = {r["address"]: r["alias"] for r in conn.execute(
            "SELECT address, alias FROM wallets WHERE alias IS NOT NULL"
        ).fetchall()}

        def nm(w):
            return alias.get(w) or w[:6]

        clusters = []
        recortados = 0
        for root, members in groups.items():
            toks = group_tokens[root]
            # (Ola 17-D) Tope de miembros en el calculo de liderazgo: un
            # componente conexo puede absorber cientos de billeteras y el
            # coste crece con el cuadrado. Se quedan las que MAS aparecen
            # (las que mas evidencia tienen), y se dice cuando se recorta:
            # un tope callado se lee como "lo mire todo".
            miembros_calc = members
            recorte = 0
            if len(members) > MAX_MIEMBROS_LIDERAZGO:
                recorte = len(members) - MAX_MIEMBROS_LIDERAZGO
                miembros_calc = set(sorted(
                    members,
                    key=lambda m: len(tokens_de.get(m, ())),
                    reverse=True)[:MAX_MIEMBROS_LIDERAZGO])
                recortados += 1
            order = _leadership(miembros_calc, toks, ranks)
            for o in order:
                o["alias"] = nm(o["wallet"])
                o["follows_alias"] = nm(o["follows"]) if o["follows"] else None
            clusters.append({
                "members": sorted(members),
                "aliases": [nm(m) for m in sorted(members)],
                "size": len(members),
                "shared_tokens": len(toks),
                "strength": len(members) * len(toks),
                "order": order,
                # (Ola 17-E) Cuantas quedaron FUERA del calculo de
                # liderazgo. Antes esto solo salia por el log del
                # servidor: el mensaje de Telegram decia "Cluster de 400
                # billeteras · 👑 Líder: X" sin poder mencionar que el
                # orden se calculo sobre 40 de esas 400.
                "orden_recortado": recorte,
                # (Ola 17-A) Solo hay 👑 si el primero tiene muestra
                # suficiente; si no, el cluster existe pero SIN líder
                # declarado, que es la verdad.
                "leader": (order[0]["alias"] if order
                           and order[0]["lead_pct"] is not None else None),
                "leader_wallet": (order[0]["wallet"] if order
                                  and order[0]["lead_pct"] is not None
                                  else None),
            })
        # Desempate estable por el primer miembro (ver nota de `follows`).
        clusters.sort(key=lambda c: (-c["strength"],
                                     c["members"][0] if c["members"] else ""))
        if recortados:
            print(f"· Clusters: en {recortados} cluster(s) el orden de "
                  f"liderazgo se calculo sobre las "
                  f"{MAX_MIEMBROS_LIDERAZGO} billeteras con mas "
                  f"apariciones (el resto sigue contando como miembro)")
        return clusters
    finally:
        conn.close()


def _copia(cs):
    """(Ola 17-E) La cache entregaba el objeto VIVO: cualquier `.sort()`,
    `.pop()` o asignacion de un llamador corrompia la cache global
    durante los 30 min del TTL, en silencio. Hoy ningun consumidor la
    muta, pero es una mina esperando a que alguien lo haga."""
    if not cs:
        return [] if cs is not None else None
    return [dict(c, order=[dict(o) for o in c.get("order", ())],
                 members=list(c.get("members", ())),
                 aliases=list(c.get("aliases", ()))) for c in cs]


def find_clusters(min_shared: int = MIN_SHARED, construir: bool = True):
    """Clusters, con cache de 30 min.

    (Ola 17-D) `construir=False` devuelve lo que haya en cache (aunque
    este caducado) y NO reconstruye: es lo que usa el camino caliente del
    webhook, donde bloquear el hilo de ingesta cuesta señales.
    """
    fresco = (_CACHE["c"] is not None
              and time.time() - _CACHE["ts"] < _TTL
              and _CACHE.get("min_shared") == min_shared)
    if fresco:
        return _copia(_CACHE["c"])
    if not construir:
        return (_copia(_CACHE["c"])
                if _CACHE.get("min_shared") == min_shared else None)
    # Candado: sin el, dos hilos con la cache caducada construyen a la vez
    # y las consultas pesadas corren duplicadas (le paso a influence.py).
    with _BUILD_LOCK:
        if (_CACHE["c"] is not None
                and time.time() - _CACHE["ts"] < _TTL
                and _CACHE.get("min_shared") == min_shared):
            return _copia(_CACHE["c"])
        # Enfriamiento tras fallo: no reintentar una construccion cara
        # cada vez que llega una señal.
        if time.time() - _CACHE.get("fallo", 0) < 600:
            # (Ola 17-E) Devolver la cache VIEJA si existe y es del mismo
            # umbral. Antes se devolvia [] y, como `_CACHE["c"]` ya se
            # habia puesto a None, durante 10 min el camino caliente veia
            # "sin cluster" en cada señal y meta_score caia al neutro.
            if (_CACHE["c"] is not None
                    and _CACHE.get("min_shared") == min_shared):
                return _copia(_CACHE["c"])
            return []
        # (19-AA, auditoria M19) La cache vieja se QUEDA puesta mientras
        # se construye la nueva. Antes se ponia a None "para ahorrar
        # memoria", pero `_previo` conservaba la referencia igual (no se
        # ahorraba nada) y, durante la construccion, `predictions.on_buy`
        # veia `cache_lista()=False` → meta_score al neutro (hasta 14
        # puntos menos) → alertas ALPHA perdidas en esa ventana.
        # `influence.py` (18-C) ya lo hacia bien; esto lo iguala.
        _previo = _CACHE["c"] if _CACHE.get("min_shared") == min_shared else None
        try:
            c = _build_clusters(min_shared)
        except Exception as e:
            _CACHE["fallo"] = time.time()
            _CACHE["c"] = _previo          # no quedarse ciego por un fallo
            print(f"· Clusters: construcción falló ({e}); se sigue con la "
                  f"copia anterior y se reintenta en 10 min")
            return _copia(_previo) or []
        _CACHE["c"] = c
        _CACHE["ts"] = time.time()
        _CACHE["min_shared"] = min_shared
        _CACHE["fallo"] = 0.0
        return _copia(c)


def clusters_text(limit: int = 6) -> str:
    clusters = find_clusters()
    if not clusters:
        return ("🕸 Aún no hay clusters: hacen falta más billeteras ⭐ que "
                "coincidan temprano en varios tokens ganadores. Corre más "
                "ciclos de descubrimiento y vuelve a intentar.")
    out = [f"🕸 *Clusters de co-compra* ({len(clusters)} detectados)\n"]
    for i, c in enumerate(clusters[:limit], 1):
        out.append(f"*{i}. Cluster de {c['size']} billeteras* · "
                   f"{c['shared_tokens']} tokens en común")
        if c.get("orden_recortado"):
            out.append(f"   _(el orden se calculó sobre las "
                       f"{MAX_MIEMBROS_LIDERAZGO} con más apariciones; "
                       f"{c['orden_recortado']} quedaron fuera del cálculo)_")
        if c.get("leader"):
            _o0 = c["order"][0]
            out.append(f"   👑 Líder: *{c['leader']}* (compra primero — "
                       f"{_o0['lead_pct']}% sobre {_o0['comps']} comparaciones)")
        else:
            out.append(f"   ⚪ Sin líder claro todavía (hacen falta "
                       f"{MIN_COMPS_LIDER}+ comparaciones por billetera)")
        seguidores = [o for o in c.get("order", [])
                      if o["wallet"] != c.get("leader_wallet")][:5]
        for o in seguidores:
            lp = (f"{o['lead_pct']}% adelanta ({o['comps']} comp.)"
                  if o["lead_pct"] is not None else "adelanto s/d")
            sig = f" · sigue a {o['follows_alias']}" if o.get("follows_alias") else ""
            out.append(f"   • {o['alias']} (rank medio {o['avg_rank']}{('; ' + lp) if lp else ''}{sig})")
        out.append("")
    out.append("_Cuando el 👑 líder compra, los seguidores suelen entrar "
               "detrás. Ésa es tu ventana._")
    return "\n".join(out)


def cluster_for(address: str, construir: bool = True) -> dict | None:
    """El cluster (si existe) al que pertenece una billetera.

    (Ola 17-D) `construir=False` para el camino caliente: si la cache
    esta fria devuelve None en vez de bloquear el hilo del webhook
    construyendo el grafo entero. La proxima señal ya lo tendra.
    """
    for c in (find_clusters(construir=construir) or ()):
        if address in c["members"]:
            return c
    return None


def cache_lista(min_shared: int = MIN_SHARED) -> bool:
    """¿Hay clusters ya calculados en cache para este umbral?

    (Ola 17-E) El camino caliente pide `construir=False` y recibe None
    tanto cuando la billetera NO tiene cluster como cuando la cache aun
    no se ha construido. Sin distinguirlo, `meta_score` aplicaba el
    neutro 0.3 (6 de 20 puntos) y lo presentaba como medido — justo el
    fallo que la Ola 17-A cerro, entrando por otra puerta.
    """
    return (_CACHE["c"] is not None
            and _CACHE.get("min_shared") == min_shared)


def precalentar():
    """Construye la cache fuera del camino caliente (jobs periódicos)."""
    try:
        n = len(find_clusters())
        print(f"🕸 Clusters: cache al día ({n} clusters)")
    except Exception as e:
        print(f"· Clusters: no pude precalentar la cache: {e}")
