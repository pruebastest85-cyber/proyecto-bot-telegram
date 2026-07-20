"""
Wallet DNA: la ficha única y completa de una billetera.

Reúne en un solo perfil todo lo que el sistema sabe:
  - Wallet Score y nivel de riesgo (wallet_score)
  - Clasificación IA + alias (tabla wallets)
  - Métricas quant: Profit Factor, Sharpe, Expectancy, Drawdown, ROI
  - PnL realizado + en cartera + neto (wallet_profiler + unrealized_pnl)
  - Retención mediana (proxy del horizonte de la operación)
  - Track record real de sus señales (signal_tracker)
  - Cluster de co-compra al que pertenece (clusters)

Es lo que se muestra con /adn <address>.
"""

import time

from db import get_conn
from wallet_profiler import profile_wallet


def wallet_dna_text(address: str) -> str | None:
    p = profile_wallet(address)
    if not p.get("tx_sampled"):
        return None

    from wallet_score import compute_score
    conn = get_conn()
    row = conn.execute(
        "SELECT alias, ai_class, ai_reason FROM wallets WHERE address=?",
        (address,)).fetchone()
    try:
        from signal_tracker import wallet_track_record
        track = wallet_track_record(conn, address)
    except Exception:
        track = None
    conn.close()

    s = compute_score(p, track)
    m = p.get("metrics") or {}
    alias = (row["alias"] if row and row["alias"] else None)
    ai_class = (row["ai_class"] if row and row["ai_class"] else "indeterminado")

    dias = ((time.time() - p["last_tx_ts"]) / 86400) if p.get("last_tx_ts") else None
    activa = "🟢 activa" if (dias is not None and dias < 7) else \
             ("🟡 poco activa" if (dias is not None and dias < 30) else "🔴 inactiva")

    ret = p.get("hold_median_min")
    if ret is None:
        horizonte = "?"
    elif ret < 60:
        horizonte = f"{ret:.0f} min"
    elif ret < 2880:
        horizonte = f"{ret / 60:.1f} h"
    else:
        horizonte = f"{ret / 1440:.1f} días"

    head = f"🧬 *WALLET DNA — {alias}*\n" if alias else "🧬 *WALLET DNA*\n"
    lines = [head, f"`{address}`", ""]
    # Clasificación en cascada (Elite/Seguimiento/Observación/Descartada)
    try:
        from grading import grade_wallet, format_grade
        from influence import influence as _inf_fn
        g = grade_wallet(p, _inf_fn(address), ai_class)
        lines.append(format_grade(g))
    except Exception:
        pass
    lines += [f"🧮 *Score: {s['score']}/100* · riesgo {s['riesgo']} · {activa}",
              f"Tipo IA: *{ai_class.upper()}*"]

    # Rentabilidad
    if m.get("roi_avg") is not None:
        lines.append(f"ROI prom/máx: {m['roi_avg']:+d}% / {m['roi_max']:+d}%")
    if s.get("win_rate") is not None:
        lines.append(f"Win Rate: {s['win_rate']}% · "
                     f"{m.get('closed', s.get('trades', 0))} ops cerradas")
    if m.get("profit_factor") is not None:
        lines.append(f"Profit Factor: {m['profit_factor']} · "
                     f"Expectancy: {m.get('expectancy_sol', 0):+.3f} SOL/op")
    if m.get("sharpe") is not None or m.get("max_drawdown_sol") is not None:
        seg = []
        if m.get("sharpe") is not None:
            seg.append(f"Sharpe {m['sharpe']}")
        if m.get("max_drawdown_sol") is not None:
            seg.append(f"Max DD -{m['max_drawdown_sol']:.2f} SOL")
        lines.append(" · ".join(seg))

    # PnL
    lines.append(f"PnL realizado: {p['pnl_total_sol']:+.1f} SOL")
    if p.get("held_tokens"):
        lines.append(f"En cartera: {p.get('unrealized_sol', 0):+.1f} SOL · "
                     f"Neto: {p.get('net_pnl_sol', p['pnl_total_sol']):+.1f} SOL")
    lines.append(f"Horizonte (retención mediana): {horizonte}")

    # Calidad de salida (Birdeye, bajo demanda; solo si hay key)
    try:
        from birdeye import exit_quality, format_exit_quality
        eq = format_exit_quality(exit_quality(p))
        if eq:
            lines.append(eq)
    except Exception:
        pass

    # Track record
    if track and track.get("senales_medidas"):
        try:
            from signal_tracker import format_track_record
            tl = format_track_record(track)
            if tl:
                lines.append(tl)
        except Exception:
            pass

    # Cluster
    try:
        from clusters import cluster_for
        c = cluster_for(address)
        if c:
            otros = [a for a in c["aliases"]
                     if a not in (alias, address[:6])][:5]
            lines.append(f"🕸 Cluster: {c['size']} billeteras, "
                         f"{c['shared_tokens']} tokens en común"
                         + (f" · con {', '.join(otros)}" if otros else ""))
            # ¿lidera o sigue dentro del cluster?
            me = next((o for o in c.get("order", [])
                       if o["wallet"] == address), None)
            if me is not None:
                if c.get("leader_wallet") == address:
                    lines.append("   👑 *Lidera el cluster* (compra primero; "
                                 "vigila esta billetera para adelantarte)")
                elif me.get("follows_alias"):
                    lp = (f", ~{me['lead_pct']}% adelanta"
                          if me.get("lead_pct") is not None else "")
                    lines.append(f"   ↪️ Sigue a *{me['follows_alias']}* "
                                 f"(rank medio {me['avg_rank']}{lp})")
    except Exception:
        pass

    # ── Perfil social: rol en el grafo de influencia ──────────────────
    try:
        from influence import influence
        inf = influence(address)
        if inf and inf.get("leader_score") is not None:
            lines.append(
                f"🧠 Rol en la red: *{inf.get('role') or '—'}* · "
                f"Leader {inf['leader_score']} / Follower {inf['follower_score']} "
                f"· 1ª el {inf['pct_first']}% de las veces")
            try:
                from predictions import leader_health_line
                hl = leader_health_line(address)
                if hl:
                    lines.append(f"   {hl}")
            except Exception:
                pass
            if inf["followers"]:
                top = ", ".join(
                    f"{f['alias']} ({f['prob']}%)" for f in inf["followers"][:3])
                lines.append(f"   👥 {inf['followers_count']} seguidoras · "
                             f"tras ella suelen entrar: {top}")
            if inf["leaders"]:
                jefe = inf["leaders"][0]
                lines.append(f"   🎯 Ella suele ir detrás de *{jefe['alias']}* "
                             f"(~{jefe['eta_s']}s)")
    except Exception:
        pass

    # ── Alpha Discovery + Originality: ¿descubre o copia? ──
    inf_for_alpha = None
    try:
        from influence import influence as _inf2
        inf_for_alpha = _inf2(address)
    except Exception:
        pass
    try:
        from alpha import format_alpha, smart_explanation
        al = format_alpha(address, inf_for_alpha)
        if al:
            lines.append(al)
        exp = smart_explanation(address, inf_for_alpha)
        if exp:
            lines.append(f"💡 _{exp}_")
    except Exception:
        pass
    try:
        from similarity import format_similar
        sim = format_similar(address)
        if sim:
            lines.append(sim)
    except Exception:
        pass

    if row and row["ai_reason"]:
        lines.append(f"\n_IA: {row['ai_reason']}_")
    lines.append(f"🔗 gmgn.ai/sol/address/{address}")
    return "\n".join(lines)
