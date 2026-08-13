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

_CACHE = {"g": None, "ts": 0.0}
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
            "pct_first": round(100 * fc / n_ap) if n_ap else 0,
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


def graph():
    if _CACHE["g"] and time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["g"]
    # Soltar el viejo ANTES de construir el nuevo: si no, durante la
    # construccion conviven dos grafos enteros y el pico es el doble.
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
        # de Postgres) y cada llamada la reintentaba entera: 9 consultas
        # de ~30 s en 4 minutos, pura lena al fuego. Si fallo hace poco,
        # se devuelve un grafo vacio valido y se reintenta en 10 min.
        if time.time() - _CACHE.get("fallo", 0) < 600:
            return {"edges": {}, "both": {}, "wallets": {}, "meta": {}}
        _CACHE["g"] = None
        try:
            g = _build()
        except Exception as e:
            _CACHE["fallo"] = time.time()
            print(f"· Grafo de influencia falló ({e}); reintento en 10 min")
            return {"edges": {}, "both": {}, "wallets": {}, "meta": {}}
        _CACHE["g"] = g
        _CACHE["ts"] = time.time()
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


def role(address: str) -> str | None:
    g = graph()
    w = g["wallets"].get(address)
    if not w or w["leader_score"] is None:
        return None
    if w["pct_first"] >= 50 or w["leader_score"] >= 70:
        return "Líder"
    d = w.get("avg_delay_s")
    if d is None:
        return "Seguidor"
    if d <= 30:
        return "Seguidor temprano"
    if d <= 90:
        return "Confirmador"
    return "Seguidor tardío"


def influence(address: str) -> dict | None:
    g = graph()
    if address not in g["wallets"]:
        return None
    followers, leaders = [], []
    for (a, b), e in g["edges"].items():
        if a == address:
            wgt, sh, gap = _weight(g, a, b)
            if wgt is not None and wgt >= STRONG_EDGE:
                followers.append({"wallet": b, "alias": g["wallets"].get(b, {}).get("alias", b[:6]),
                                  "prob": round(100 * wgt), "eta_s": gap, "shared": sh})
        if b == address:
            wgt, sh, gap = _weight(g, a, b)
            if wgt is not None and wgt >= STRONG_EDGE:
                leaders.append({"wallet": a, "alias": g["wallets"].get(a, {}).get("alias", a[:6]),
                                "prob": round(100 * wgt), "eta_s": gap, "shared": sh})
    followers.sort(key=lambda x: x["prob"], reverse=True)
    leaders.sort(key=lambda x: x["prob"], reverse=True)
    w = g["wallets"][address]
    return {"role": role(address), "leader_score": w["leader_score"],
            "follower_score": w["follower_score"], "pct_first": w["pct_first"],
            "avg_lead_s": w["avg_lead_s"], "avg_lag_s": w["avg_lag_s"],
            "followers": followers, "leaders": leaders,
            "followers_count": len(followers)}


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
                   f"1ª el {h['pct_first']}% de las veces")
    out.append("\n_Vigílalas: entran temprano y las buenas van detrás._")
    return "\n".join(out)
