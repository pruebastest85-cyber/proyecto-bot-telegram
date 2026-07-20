"""
Similitud entre wallets: descubre ESTRELLAS NUEVAS antes de tiempo.

No espera a que una wallet tenga cientos de operaciones. Construye un
"vector de comportamiento" (un embedding sencillo) por billetera y mide
la similitud coseno con las billeteras de élite. Si una wallet nueva con
pocas operaciones se parece 97% a una Elite, es candidata a estrella
mucho antes de que su historial lo demuestre.

El vector combina rasgos ya disponibles (sin APIs nuevas):
  temprana (buy_rank), experiencia, liderazgo, originalidad, alpha,
  wallet_score y consistencia. Cacheado en memoria.
"""

import math
import time

from db import get_conn
import influence as _inf
import alpha as _alpha

_CACHE = {"v": None, "ts": 0.0}
_TTL = 300

FEATURES = ["temprana", "experiencia", "leader", "pct_first",
            "originality", "alpha", "wallet_score", "consistency"]


def _vectors():
    conn = get_conn()
    try:
        wl = {r["address"]: r for r in conn.execute(
            "SELECT address, alias, grade, wallet_score, consistency "
            "FROM wallets WHERE COALESCE(is_bot,0)=0").fetchall()}
        ranks = {r["wallet"]: (r["r"], r["n"]) for r in conn.execute(
            "SELECT wallet, AVG(buy_rank) r, COUNT(*) n "
            "FROM appearances GROUP BY wallet").fetchall()}
    finally:
        conn.close()

    infg = _inf.graph()
    alpg = _alpha.graph()
    vectors, meta = {}, {}
    for w, (avg_rank, n) in ranks.items():
        if w not in wl:
            continue
        iw = infg.get(w, {})
        aw = alpg.get(w, {})
        row = wl[w]
        vec = [
            1.0 / (1.0 + (avg_rank or 50)),               # entra temprano
            min(1.0, (n or 0) / 10.0),                    # experiencia
            (iw.get("leader_score") or 0) / 100.0,        # liderazgo
            (iw.get("pct_first") or 0) / 100.0,           # % veces 1ª
            (aw.get("originality") or 0) / 100.0,         # originalidad
            (aw.get("alpha_score") or 0) / 100.0,         # alpha discovery
            min(1.0, (row["wallet_score"] or 0) / 100.0),
            min(1.0, (row["consistency"] or 0) / 100.0),
        ]
        vectors[w] = vec
        meta[w] = {"alias": row["alias"] or w[:6], "grade": row["grade"],
                   "n": n}
    return vectors, meta


def _graph():
    if _CACHE["v"] is not None and time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["v"]
    g = _vectors()
    _CACHE["v"] = g
    _CACHE["ts"] = time.time()
    return g


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na and nb else 0.0


def similar_to(address: str, top: int = 5, min_sim: float = 0.0) -> list[dict]:
    vectors, meta = _graph()
    if address not in vectors:
        return []
    base = vectors[address]
    sims = []
    for w, v in vectors.items():
        if w == address:
            continue
        s = _cos(base, v)
        if s >= min_sim:
            sims.append({"address": w, "alias": meta[w]["alias"],
                         "grade": meta[w]["grade"], "n": meta[w]["n"],
                         "sim": round(100 * s)})
    sims.sort(key=lambda x: x["sim"], reverse=True)
    return sims[:top]


def rising_stars(min_sim: int = 88, limit: int = 10) -> list[dict]:
    """Wallets AÚN no-Elite muy parecidas a una Elite: posibles estrellas."""
    vectors, meta = _graph()
    elite = {w: v for w, v in vectors.items()
             if meta[w]["grade"] == "Elite"}
    if not elite:
        return []
    out = []
    for w, v in vectors.items():
        if meta[w]["grade"] in ("Elite",):
            continue
        best_w, best_s = None, 0.0
        for ew, ev in elite.items():
            s = _cos(v, ev)
            if s > best_s:
                best_s, best_w = s, ew
        if best_w and 100 * best_s >= min_sim:
            out.append({"address": w, "alias": meta[w]["alias"],
                        "n": meta[w]["n"], "sim": round(100 * best_s),
                        "like": meta[best_w]["alias"],
                        "grade": meta[w]["grade"]})
    out.sort(key=lambda x: x["sim"], reverse=True)
    return out[:limit]


def format_similar(address: str) -> str | None:
    sims = similar_to(address, top=4)
    if not sims:
        return None
    top = sims[0]
    line = (f"🧬 Parecido de comportamiento: *{top['sim']}%* con "
            f"*{top['alias']}*")
    if len(sims) > 1:
        otras = ", ".join(f"{s['alias']} {s['sim']}%" for s in sims[1:4])
        line += f" · también: {otras}"
    return line


def rising_stars_text(limit: int = 10) -> str:
    rs = rising_stars(limit=limit)
    if not rs:
        return ("🌱 Aún no detecto estrellas emergentes. Hacen falta más "
                "billeteras Elite de referencia y candidatas con historial.")
    out = ["🌱 *Estrellas emergentes* (aún no Elite, pero se comportan como una)\n"]
    for i, w in enumerate(rs, 1):
        out.append(f"{i}. *{w['alias']}* — {w['sim']}% parecida a "
                   f"*{w['like']}* (solo {w['n']} apariciones)")
    out.append("\n_Se parecen a tus mejores wallets con poco historial. "
               "Vigílalas antes de que lo demuestren._")
    return "\n".join(out)


def similar_text(address: str) -> str:
    sims = similar_to(address, top=8)
    if not sims:
        return ("No tengo suficientes rasgos de esa billetera para medir "
                "similitud (¿aparece en tokens ganadores de tu base?).")
    out = [f"🧬 *Billeteras más parecidas a* `{address[:10]}…`\n"]
    for s in sims:
        g = f" [{s['grade']}]" if s["grade"] else ""
        out.append(f"• *{s['alias']}*{g} — {s['sim']}% parecida "
                   f"({s['n']} apariciones)")
    out.append("\n_Similitud de comportamiento (embedding). Útil para "
               "encontrar clones de tus mejores wallets._")
    return "\n".join(out)
