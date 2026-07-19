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

Cacheado en memoria (TTL corto) porque recorrer el grafo es O(pares).
"""

import calendar
import time
from collections import defaultdict
from statistics import median

from db import get_conn

_CACHE = {"g": None, "ts": 0.0}
_TTL = 300
MIN_SHARED = 3          # nº mínimo de tokens compartidos para confiar en una arista
STRONG_EDGE = 0.60      # peso mínimo para considerar A→B una relación fuerte


def _ts(s):
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def _build():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT a.mint, a.wallet, a.buy_time, a.buy_rank
               FROM appearances a JOIN wallets w ON w.address = a.wallet
               WHERE COALESCE(w.is_bot, 0) = 0""").fetchall()
        meta = {r["address"]: {"alias": r["alias"],
                               "wallet_score": r["wallet_score"],
                               "ai_class": r["ai_class"]}
                for r in conn.execute(
                    "SELECT address, alias, wallet_score, ai_class FROM wallets"
                ).fetchall()}
    finally:
        conn.close()

    by_token = defaultdict(list)
    for r in rows:
        by_token[r["mint"]].append((r["wallet"], _ts(r["buy_time"]), r["buy_rank"]))

    edges = defaultdict(lambda: {"count": 0, "gaps": []})   # (a,b): a antes de b
    both = defaultdict(int)                                  # frozenset{a,b}
    appear = defaultdict(int)
    first = defaultdict(int)
    leads = defaultdict(int)
    lags = defaultdict(int)
    lead_gaps = defaultdict(list)     # segundos que la wallet va por delante
    lag_gaps = defaultdict(list)      # segundos que la wallet va por detrás
    delay_leader = defaultdict(list)  # segundos tras el PRIMER comprador del token

    for entries in by_token.values():
        for w, ts, rk in entries:
            appear[w] += 1
        if len(entries) < 2:
            continue
        ordered = sorted(entries,
                         key=lambda e: (e[1] if e[1] is not None
                                        else 10**12 + (e[2] or 0)))
        first[ordered[0][0]] += 1
        lead_w, lead_ts, _ = ordered[0]
        if lead_ts is not None:
            for w, tw, _ in ordered[1:]:
                if tw is not None and tw >= lead_ts:
                    delay_leader[w].append(tw - lead_ts)
        n = len(ordered)
        for i in range(n):
            a, ta, _ = ordered[i]
            for j in range(i + 1, n):
                b, tb, _ = ordered[j]
                if a == b:
                    continue
                e = edges[(a, b)]
                e["count"] += 1
                both[frozenset((a, b))] += 1
                leads[a] += 1
                lags[b] += 1
                if ta is not None and tb is not None and tb >= ta:
                    gap = tb - ta
                    e["gaps"].append(gap)
                    lead_gaps[a].append(gap)
                    lag_gaps[b].append(gap)

    wallets = {}
    for w in appear:
        lo, la = leads[w], lags[w]
        tot = lo + la
        wallets[w] = {
            "alias": (meta.get(w, {}) or {}).get("alias") or w[:6],
            "wallet_score": (meta.get(w, {}) or {}).get("wallet_score"),
            "ai_class": (meta.get(w, {}) or {}).get("ai_class"),
            "appearances": appear[w],
            "first_count": first[w],
            "pct_first": round(100 * first[w] / appear[w]) if appear[w] else 0,
            "leader_score": round(100 * lo / tot) if tot else None,
            "follower_score": round(100 * la / tot) if tot else None,
            "avg_lead_s": round(median(lead_gaps[w])) if lead_gaps[w] else None,
            "avg_lag_s": round(median(lag_gaps[w])) if lag_gaps[w] else None,
            "avg_delay_s": round(median(delay_leader[w])) if delay_leader[w] else None,
        }
    return {"edges": edges, "both": both, "wallets": wallets, "meta": meta}


def graph():
    if _CACHE["g"] and time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["g"]
    g = _build()
    _CACHE["g"] = g
    _CACHE["ts"] = time.time()
    return g


def _weight(g, a, b):
    """Peso dirigido A→B: % de tokens compartidos en que A precede a B."""
    sh = g["both"].get(frozenset((a, b)), 0)
    if sh < MIN_SHARED:
        return None, sh, None
    c = g["edges"].get((a, b), {}).get("count", 0)
    gaps = g["edges"].get((a, b), {}).get("gaps", [])
    return c / sh, sh, (round(median(gaps)) if gaps else None)


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
