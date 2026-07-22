"""
Resumen unificado (dashboard en Telegram).

En vez de que el usuario tenga que recordar 15 comandos, UNA sola vista
consolida lo importante del día:

  ⭐ Nuevas Elite       · 🚀 Señales de alta confianza
  📈 Líderes emergentes · ⚠️ Líderes en declive
  🔥 Clusters activos   · 🧠 Hipótesis nuevas

Cada sección es best-effort: si no hay nada, se omite. Se muestra con
/resumen y se envía como brief diario.
"""

import time
from datetime import datetime, timedelta, timezone

from db import get_conn, get_setting


def _cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours)).isoformat(timespec="seconds")


def resumen_text() -> str:
    now = int(time.time())
    conn = get_conn()
    out = ["📋 *RESUMEN* — lo importante ahora\n"]
    algo = False

    # ⭐ Nuevas Elite (evaluadas Elite en 24h)
    try:
        elites = conn.execute(
            """SELECT alias, address, pnl_total, wallet_score FROM wallets
               WHERE grade='Elite' AND COALESCE(is_bot,0)=0
                 AND pnl_updated >= ?
               ORDER BY COALESCE(wallet_score,0) DESC LIMIT 5""",
            (_cutoff_iso(24),)).fetchall()
        if elites:
            algo = True
            out.append("⭐ *Nuevas Elite (24h):*")
            for r in elites:
                nom = (r["alias"] or r["address"][:8]).replace("*", "")
                pnl = f" · {r['pnl_total']:+.0f} SOL" if r["pnl_total"] is not None else ""
                out.append(f"  • {nom}{pnl}")
    except Exception:
        pass

    # 🚀 Señales de alta confianza (24h, alertadas, score alto)
    try:
        sigs = conn.execute(
            """SELECT symbol, signal_score, side FROM signals
               WHERE ts>=? AND alerted=1 AND signal_score>=70
               ORDER BY signal_score DESC LIMIT 5""",
            (now - 86400,)).fetchall()
        if sigs:
            algo = True
            out.append("\n🚀 *Señales de alta confianza (24h):*")
            for s in sigs:
                out.append(f"  • {s['symbol'] or '?'} "
                           f"({s['side']}, score {round(s['signal_score'])})")
    except Exception:
        pass

    conn.close()

    # 📈 Líderes emergentes (Alpha Discovery alto)
    try:
        from alpha import top_alpha
        ta = [w for w in top_alpha(6) if w["alpha_score"] >= 50][:4]
        if ta:
            algo = True
            out.append("\n📈 *Líderes emergentes (descubren antes):*")
            for w in ta:
                out.append(f"  • {w['alias']} · Alpha {w['alpha_score']} · "
                           f"~{w['avg_lead_min']:.0f} min antes")
    except Exception:
        pass

    # ⚠️ Líderes en declive (predicciones recientes fallando)
    try:
        conn = get_conn()
        try:
            since = now - 30 * 86400
            dec = conn.execute(
                """SELECT leader, AVG(outcome_pct) a, COUNT(*) n
                   FROM predictions WHERE status='evaluada' AND evaluated_ts>=?
                   GROUP BY leader HAVING n>=3 AND a<50
                   ORDER BY a ASC LIMIT 4""", (since,)).fetchall()
            gmap = {}
            if dec:
                gmap = {r["address"]: r["alias"] for r in conn.execute(
                    "SELECT address, alias FROM wallets").fetchall()}
        finally:
            conn.close()
        if dec:
            algo = True
            out.append("\n⚠️ *Líderes en declive:*")
            for d in dec:
                nom = gmap.get(d["leader"], d["leader"][:8]) or d["leader"][:8]
                out.append(f"  • {nom} · {round(d['a'])}% acierto ({d['n']} pred.)")
    except Exception:
        pass

    # 🔥 Clusters activos
    try:
        from clusters import find_clusters
        cs = find_clusters()[:3]
        if cs:
            algo = True
            out.append("\n🔥 *Clusters activos:*")
            for c in cs:
                lid = c.get("leader") or "?"
                out.append(f"  • {c['size']} billeteras · líder {lid} · "
                           f"{c['shared_tokens']} tokens en común")
    except Exception:
        pass

    # 🧠 Hipótesis nuevas
    try:
        conn = get_conn()
        try:
            hyp = get_setting(conn, "hypotheses", None)
            hts = get_setting(conn, "hypotheses_ts", "")
        finally:
            conn.close()
        if hyp:
            algo = True
            primera = hyp.split("\n")[0][:200]
            out.append(f"\n🧠 *Hipótesis ({hts}):*\n  {primera}\n  _(completo: /hipotesis)_")
    except Exception:
        pass

    if not algo:
        return ("📋 *Resumen*\n\nEl sistema aún está acumulando datos — poco "
                "que resumir todavía. Vuelve tras unos ciclos y verás Elite, "
                "señales, líderes y clusters aquí.")
    out.append("\n_Detalle: /elite · /alpha · /clusters · /predicciones · /metricas_")
    return "\n".join(out)
