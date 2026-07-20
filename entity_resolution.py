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
import time
from collections import defaultdict
from statistics import median

from db import get_conn

_CACHE = {"e": None, "ts": 0.0}
_TTL = 300
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
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT a.mint, a.wallet, a.buy_time, a.buy_sol, w.alias
               FROM appearances a JOIN wallets w ON w.address = a.wallet
               WHERE COALESCE(w.is_bot,0)=0""").fetchall()
    finally:
        conn.close()

    alias = {}
    by_token = defaultdict(list)
    for r in rows:
        alias[r["wallet"]] = r["alias"] or r["wallet"][:6]
        by_token[r["mint"]].append((r["wallet"], _ts(r["buy_time"]),
                                    r["buy_sol"]))

    pair = defaultdict(lambda: {"shared": 0, "gaps": [], "sizes": []})
    for entries in by_token.values():
        n = len(entries)
        for i in range(n):
            wa, ta, sa = entries[i]
            for j in range(i + 1, n):
                wb, tb, sb = entries[j]
                if wa == wb:
                    continue
                key = tuple(sorted((wa, wb)))
                p = pair[key]
                p["shared"] += 1
                if ta is not None and tb is not None:
                    p["gaps"].append(abs(ta - tb))
                if sa and sb:
                    p["sizes"].append(min(sa, sb) / max(sa, sb))

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
