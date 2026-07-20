"""
Token DNA: la ficha de un token, igual que el Wallet DNA lo es de una
billetera. Responde de un vistazo: ¿quién de calidad lo compró, qué tan
temprano, qué riesgo tiene y qué probabilidad de sobrevivir?

Combina lo que el sistema ya sabe:
  - appearances → qué billeteras (y de qué grado) lo compraron y su rank
  - token_check (DexScreener + RugCheck) → liquidez, MC, riesgo, autoridades
  - influence → si alguno de los compradores es líder de cluster

La "probabilidad de supervivencia" es una ESTIMACIÓN heurística a partir
de liquidez, LP bloqueado, autoridades y concentración de holders — no
una garantía.
"""

from db import get_conn
from token_check import analyze_token


def _score_liquidez(liq):
    if not liq:
        return 0
    return min(100, round(100 * min(liq, 200000) / 200000))


def _score_riesgo(t):
    r = 0
    if t.get("mint_auth"):
        r += 35
    if t.get("freeze_auth"):
        r += 25
    top10 = t.get("top10_pct")
    if top10 is not None:
        r += min(40, top10 * 0.5)
    lp = t.get("lp_locked_pct")
    if lp is not None and lp < 50:
        r += 20
    if t.get("risks"):
        r += 10 * len(t["risks"])
    return min(100, round(r))


def token_dna_text(mint: str) -> str:
    t = analyze_token(mint)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT a.wallet, a.buy_rank, a.buy_time,
                      w.grade, w.alias, w.wallet_score
               FROM appearances a JOIN wallets w ON w.address = a.wallet
               WHERE a.mint = ? AND COALESCE(w.is_bot,0)=0
               ORDER BY a.buy_rank""", (mint,)).fetchall()
    finally:
        conn.close()

    smart = [r for r in rows if r["grade"] in ("Elite", "Seguimiento")]
    elite = [r for r in rows if r["grade"] == "Elite"]

    # líderes de cluster entre los compradores
    leaders = []
    try:
        from influence import graph as _ig
        gw = _ig()["wallets"]
        for r in rows:
            w = gw.get(r["wallet"])
            if w and (w.get("leader_score") or 0) >= 60:
                leaders.append(r["alias"] or r["wallet"][:6])
    except Exception:
        pass

    liq_s = _score_liquidez(t.get("liq"))
    risk_s = _score_riesgo(t)
    survival = max(0, min(100, round(0.5 * liq_s + 0.5 * (100 - risk_s))))

    sym = t.get("symbol", "?")
    lines = [f"🧬 *TOKEN DNA — {sym}*", f"`{mint}`", ""]
    if t.get("mc") or t.get("liq"):
        def usd(v):
            return f"${v:,.0f}" if v else "?"
        lines.append(f"MC {usd(t.get('mc'))} · Liquidez {usd(t.get('liq'))}"
                     + (f" · edad {t['age_days']:g}d" if t.get("age_days") is not None else ""))
    lines.append(f"💧 Liquidity Score: {liq_s}/100")
    lines.append(f"🧠 Smart Money: {len(smart)} (Elite: {len(elite)})")
    if leaders:
        lines.append(f"👑 Líderes que lo compraron: {', '.join(leaders[:5])}")
    if smart:
        rangos = [r["buy_rank"] for r in smart if r["buy_rank"]]
        if rangos:
            lines.append(f"⚡ Convicción temprana: mejor comprador de calidad "
                         f"fue el #{min(rangos)}")
    lines.append(f"⚠️ Risk Score: {risk_s}/100 "
                 + ("(alto)" if risk_s >= 60 else "(medio)" if risk_s >= 30 else "(bajo)"))
    seg = ["mint: " + ("⚠️ activa" if t.get("mint_auth") else "✅ revocada"),
           "freeze: " + ("⚠️ activa" if t.get("freeze_auth") else "✅ no")]
    if t.get("lp_locked_pct") is not None:
        seg.append(f"LP lock {t['lp_locked_pct']:.0f}%")
    if t.get("top10_pct") is not None:
        seg.append(f"top10 {t['top10_pct']:.0f}%")
    lines.append("🔐 " + " · ".join(seg))
    lines.append(f"\n🛡 *Probabilidad de supervivencia (estimada): {survival}%*")
    if smart:
        quienes = ", ".join((r["alias"] or r["wallet"][:6]) for r in smart[:6])
        lines.append(f"\n_Comprado por billeteras de calidad: {quienes}._")
    else:
        lines.append("\n_Ninguna billetera de tu base de calidad lo ha "
                     "comprado (aún)._")
    lines.append("_La probabilidad de supervivencia es una estimación de "
                 "riesgo, no una garantía._")
    return "\n".join(lines)
