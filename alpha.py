"""
Alpha Discovery Score + Originality Score.

Deja de preguntar "¿esta wallet es buena?" y pregunta
"¿DESCUBRE proyectos antes que el mercado, o solo COPIA a otros?".

Para cada token ganador compara la hora de compra de la wallet contra la
COHORTE de referencia (billeteras Elite/Seguimiento/rastreadas):

  - Alpha Discovery Score: con qué frecuencia entra ANTES que la cohorte,
    cuánto se adelanta y cuántas la siguen después. Vale más que el ROI:
    mide la capacidad de encontrar gemas temprano.
  - Originality Score: % de sus compras que ocurren antes que cualquier
    otra wallet de élite. Alto = crea tendencias. Bajo = copia con retraso.

Fuente: tabla `appearances` (buy_time) + `grade` de cada wallet. Sin APIs
nuevas. Cacheado en memoria como el grafo de influencia.
"""

import calendar
import time
from collections import defaultdict

from db import get_conn

_CACHE = {"g": None, "ts": 0.0}
_TTL = 300
MIN_SHARED = 3       # tokens compartidos con la cohorte para dar un score


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
            """SELECT a.mint, a.wallet, a.buy_time,
                      w.grade, w.is_tracked, w.alias
               FROM appearances a JOIN wallets w ON w.address = a.wallet
               WHERE COALESCE(w.is_bot, 0) = 0""").fetchall()
    finally:
        conn.close()

    # Cohorte de referencia: billeteras de calidad probada
    ref = set()
    alias = {}
    by_token = defaultdict(list)
    for r in rows:
        alias[r["wallet"]] = r["alias"] or r["wallet"][:6]
        if (r["grade"] in ("Elite", "Seguimiento")) or r["is_tracked"]:
            ref.add(r["wallet"])
        by_token[r["mint"]].append((r["wallet"], _ts(r["buy_time"])))

    stats = defaultdict(lambda: {"shared": 0, "leads": 0, "lead_min": 0.0,
                                 "reach": 0})
    for entries in by_token.values():
        # tiempos de la cohorte de referencia en este token
        ref_ts = [(w, t) for w, t in entries if w in ref and t is not None]
        if len(ref_ts) < 2:
            continue
        for w, t in entries:
            if t is None:
                continue
            others = [ot for ow, ot in ref_ts if ow != w]
            if not others:
                continue
            st = stats[w]
            st["shared"] += 1
            earliest_other = min(others)
            if t <= earliest_other:
                st["leads"] += 1
                st["lead_min"] += (earliest_other - t) / 60.0
                # cuántas de la cohorte entraron DESPUÉS de w
                st["reach"] += sum(1 for ot in others if ot > t)

    out = {}
    for w, st in stats.items():
        if st["shared"] < MIN_SHARED:
            continue
        disc = st["leads"] / st["shared"]
        avg_lead = (st["lead_min"] / st["leads"]) if st["leads"] else 0.0
        avg_reach = (st["reach"] / st["leads"]) if st["leads"] else 0.0
        norm_lead = min(1.0, avg_lead / 30.0)
        norm_reach = min(1.0, avg_reach / 5.0)
        alpha = round(100 * (0.5 * disc + 0.3 * norm_lead + 0.2 * norm_reach))
        out[w] = {
            "alpha_score": alpha,
            "originality": round(100 * disc),
            "shared": st["shared"], "leads": st["leads"],
            "avg_lead_min": round(avg_lead, 1),
            "avg_followers": round(avg_reach, 1),
            "alias": alias.get(w, w[:6]),
        }
    return out


def graph():
    if _CACHE["g"] is not None and time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["g"]
    g = _build()
    _CACHE["g"] = g
    _CACHE["ts"] = time.time()
    return g


def alpha_profile(address: str) -> dict | None:
    return graph().get(address)


def smart_explanation(address: str, inf: dict | None = None) -> str | None:
    """Explicación en lenguaje natural del rasgo MÁS inusual de la wallet."""
    ap = alpha_profile(address)
    if not ap:
        return None
    parts = []
    if ap["originality"] >= 70 and ap["avg_lead_min"] > 0:
        parts.append(
            f"descubre proyectos ~{ap['avg_lead_min']:.0f} min antes que la "
            f"media de las billeteras de élite")
    elif ap["originality"] <= 30:
        parts.append("suele COPIAR: entra después de la cohorte de élite en "
                     f"el {100 - ap['originality']}% de los casos")
    foll = (inf or {}).get("followers_count", 0)
    if foll >= 2:
        parts.append(f"otras {foll} wallets tienden a seguirla")
    if not parts:
        return None
    return ("Lo más inusual de esta wallet: " + " y ".join(parts) + ".")


def format_alpha(address: str, inf: dict | None = None) -> str | None:
    ap = alpha_profile(address)
    if not ap:
        return None
    orig = ap["originality"]
    tag = ("🎯 creadora de tendencia" if orig >= 70
           else "↩️ copiadora" if orig <= 30 else "mixta")
    line = (f"🔭 *Alpha Discovery: {ap['alpha_score']}* · "
            f"Originality: {orig} ({tag})")
    det = (f"   entra 1ª antes que la élite en {ap['leads']}/{ap['shared']} "
           f"tokens · adelanto ~{ap['avg_lead_min']:.0f} min")
    return line + "\n" + det


def top_alpha(limit: int = 10) -> list[dict]:
    g = graph()
    items = sorted(g.items(), key=lambda kv: kv[1]["alpha_score"], reverse=True)
    return [dict(v, address=a) for a, v in items[:limit]]


def alpha_text(limit: int = 10) -> str:
    top = top_alpha(limit)
    if not top:
        return ("🔭 Aún no puedo medir Alpha Discovery: hacen falta más "
                "billeteras de élite coincidiendo temprano en tokens ganadores.")
    out = ["🔭 *Alpha Discovery — quién descubre antes que el mercado*\n"]
    for i, w in enumerate(top, 1):
        out.append(f"{i}. *{w['alias']}* · Alpha {w['alpha_score']} · "
                   f"Orig {w['originality']} · 1ª en {w['leads']}/{w['shared']} "
                   f"(~{w['avg_lead_min']:.0f} min antes)")
    out.append("\n_Alpha alto = encuentra gemas antes que las demás élite. "
               "Originality alto = crea tendencia; bajo = copia._")
    return "\n".join(out)
