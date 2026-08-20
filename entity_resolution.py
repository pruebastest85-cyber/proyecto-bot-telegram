"""
Entity Resolution: ¿qué wallets pertenecen al MISMO operador o grupo?

No afirma identidades: genera HIPÓTESIS con evidencia y nivel de
confianza. Dos wallets probablemente son el mismo operador si, una y
otra vez, compran los MISMOS tokens, casi al MISMO tiempo y con montos
MUY parecidos. Eso es coordinación, no coincidencia.

Evidencia (de la tabla `appearances`, sin APIs nuevas):
  • nº de tokens ganadores en común
  • sincronía temporal (segundos de diferencia entre sus compras)
  • similitud de montos (buy_sol)

Combina las tres en una confianza 0-100 y une por transitividad en
"entidades". Cacheado en memoria.
"""

import calendar
import threading
import time
from collections import defaultdict
from statistics import median

from db import get_conn

_CACHE = {"e": None, "ts": 0.0}
_BUILD_LOCK = threading.Lock()
_TTL = 1800        # 30 min. Antes 300 s, pero predictions_job corre
                   # cada 10 min y forzaba una reconstruccion en CADA
                   # pasada. Son datos historicos: media hora de
                   # retraso no cambia ninguna cifra.
MIN_SHARED = 3        # tokens en común mínimos para considerar un vínculo
MIN_CONF = 70         # confianza mínima para unir dos wallets


def _ts(s):
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _pair_confidence(shared, gaps, size_ratios):
    f_shared = min(1.0, shared / 5.0)
    med_gap = median(gaps) if gaps else 999
    f_time = max(0.0, 1.0 - med_gap / 300.0)      # 0 a los 5 min
    f_size = median(size_ratios) if size_ratios else 0.0
    conf = round(100 * (0.40 * f_shared + 0.35 * f_time + 0.25 * f_size))
    return conf, round(med_gap), round(100 * f_size)


def _build():
    # PARES EN SQL (Ola 5, auditoria 19/8 - C15): esto era el mismo
    # O(n²) en memoria que ya mato el servicio en influence.py (un token
    # con 500 compradores = 124.750 entradas de dict con listas, cacheadas
    # 30 min). Ahora la base agrega y devuelve SOLO las parejas con
    # shared >= MIN_SHARED (las de menos las descartaba _pair_confidence
    # igualmente); el detalle fino (gaps/montos) se lee solo para esas
    # pocas parejas calificadas.
    conn = get_conn()
    pair = {}
    alias = {}
    try:
        pares = conn.execute(
            """SELECT a1.wallet wa, a2.wallet wb, COUNT(*) shared
               FROM appearances a1
               JOIN appearances a2 ON a2.mint = a1.mint
                    AND a2.wallet > a1.wallet
               JOIN wallets w1 ON w1.address = a1.wallet
                    AND COALESCE(w1.is_bot, 0) = 0
               JOIN wallets w2 ON w2.address = a2.wallet
                    AND COALESCE(w2.is_bot, 0) = 0
               GROUP BY a1.wallet, a2.wallet
               HAVING COUNT(*) >= ?""", (MIN_SHARED,)).fetchall()
        for pr in pares:
            wa, wb = pr["wa"], pr["wb"]
            det = conn.execute(
                """SELECT a1.buy_time t1, a2.buy_time t2,
                          a1.buy_sol s1, a2.buy_sol s2
                   FROM appearances a1
                   JOIN appearances a2 ON a2.mint = a1.mint
                        AND a2.wallet = ?
                   WHERE a1.wallet = ?""", (wb, wa)).fetchall()
            gaps, sizes = [], []
            for d in det:
                ta, tb = _ts(d["t1"]), _ts(d["t2"])
                if ta is not None and tb is not None:
                    gaps.append(abs(ta - tb))
                if d["s1"] and d["s2"]:
                    sizes.append(min(d["s1"], d["s2"])
                                 / max(d["s1"], d["s2"]))
            pair[(wa, wb)] = {"shared": pr["shared"], "gaps": gaps,
                              "sizes": sizes}
        # Aliases solo de las billeteras que aparecen en parejas
        # calificadas (en trozos: SQLite acepta ~999 parametros).
        miembros = sorted({w for k in pair for w in k})
        for i in range(0, len(miembros), 500):
            trozo = miembros[i:i + 500]
            for r in conn.execute(
                    "SELECT address, alias FROM wallets WHERE address IN "
                    f"({','.join('?' * len(trozo))})", tuple(trozo)):
                alias[r["address"]] = r["alias"] or r["address"][:6]
    finally:
        conn.close()

    # aristas fuertes → union-find
    edges = {}
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

    for (a, b), p in pair.items():
        if p["shared"] < MIN_SHARED:
            continue
        conf, med_gap, size_pct = _pair_confidence(
            p["shared"], p["gaps"], p["sizes"])
        if conf >= MIN_CONF:
            edges[(a, b)] = {"conf": conf, "shared": p["shared"],
                             "gap_s": med_gap, "size_pct": size_pct}
            union(a, b)

    groups = defaultdict(set)
    for (a, b) in edges:
        r = find(a)
        groups[r].update((a, b))

    entities = []
    for root, members in groups.items():
        # confianza de la entidad = media de las aristas internas
        confs = [e["conf"] for (a, b), e in edges.items()
                 if a in members and b in members]
        internal = [(a, b, e) for (a, b), e in edges.items()
                    if a in members and b in members]
        avg_conf = round(sum(confs) / len(confs)) if confs else 0
        # evidencia representativa (arista más fuerte)
        best = max(internal, key=lambda x: x[2]["conf"]) if internal else None
        ev = []
        if best:
            e = best[2]
            ev = [f"{e['shared']} tokens en común",
                  f"compran con ~{e['gap_s']}s de diferencia",
                  f"montos {e['size_pct']}% parecidos"]
        entities.append({
            "members": sorted(members),
            "aliases": [alias.get(m, m[:6]) for m in sorted(members)],
            "size": len(members),
            "confidence": avg_conf,
            "evidence": ev,
        })
    entities.sort(key=lambda x: (x["size"], x["confidence"]), reverse=True)
    return {"entities": entities, "alias": alias}


def _graph():
    if _CACHE["e"] is not None and time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["e"]
    # Candado de construccion (Ola 5): sin el, predictions_job y un /adn
    # simultaneos con el cache vencido construian DOS grafos a la vez.
    with _BUILD_LOCK:
        if _CACHE["e"] is not None and time.time() - _CACHE["ts"] < _TTL:
            return _CACHE["e"]
        # Soltar el viejo ANTES de construir el nuevo (ver alpha.graph).
        _CACHE["e"] = None
        g = _build()
        _CACHE["e"] = g
        _CACHE["ts"] = time.time()
        return g


def entity_for(address: str) -> dict | None:
    for e in _graph()["entities"]:
        if address in e["members"]:
            return e
    return None


def format_entity(address: str) -> str | None:
    e = entity_for(address)
    if not e or e["size"] < 2:
        return None
    otras = [a for a in e["aliases"]][:6]
    line = (f"🕵️ *Posible mismo operador* ({e['size']} wallets · "
            f"confianza {e['confidence']}%)")
    det = "   " + ", ".join(otras)
    if e["evidence"]:
        det += "\n   evidencia: " + " · ".join(e["evidence"])
    return line + "\n" + det


def entities_text(limit: int = 8) -> str:
    ents = [e for e in _graph()["entities"] if e["size"] >= 2]
    if not ents:
        return ("🕵️ Aún no detecto entidades multi-wallet. Necesito más "
                "billeteras que coincidan en tokens, tiempos y montos.")
    out = ["🕵️ *Entidades detectadas* (wallets del mismo operador)\n",
           "_Hipótesis con evidencia, no afirmaciones._\n"]
    for i, e in enumerate(ents[:limit], 1):
        nombres = ", ".join(e["aliases"][:6]) + (
            f" +{e['size']-6}" if e["size"] > 6 else "")
        out.append(f"*{i}. {e['size']} wallets · confianza {e['confidence']}%*\n"
                   f"{nombres}\n_evidencia: {' · '.join(e['evidence'])}_")
    return "\n".join(out)
