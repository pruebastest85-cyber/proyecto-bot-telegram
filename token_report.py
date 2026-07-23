"""
Ficha de token bajo demanda: cuando el usuario envía un contrato en el chat,
arma un resumen claro (estilo señal) con seguridad y smart-money.

Reutiliza token_check (DexScreener + RugCheck) y appearances (billeteras
propias). No modifica nada del embudo existente.

Fase 1: seguridad + link + smart-money.
Fase 2: se inserta el veredicto de la IA (bloque _ai_block).
"""

from db import get_conn
from token_check import analyze_token


def _mc(x) -> str:
    """Formato compacto: $540K · $1.1M · $2.3B."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    if x <= 0:
        return "?"
    if x >= 1e9:
        return f"${x / 1e9:.1f}B"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:.0f}"


def risk_score(t: dict) -> int:
    """Risk Score heurístico 0-100 (misma lógica que token_dna)."""
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


def smart_money(mint: str) -> list:
    """Billeteras de la propia red que compraron el token, con su grado."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT a.wallet, a.buy_rank, w.grade, w.alias
               FROM appearances a JOIN wallets w ON w.address = a.wallet
               WHERE a.mint = ? AND COALESCE(w.is_bot,0)=0
               ORDER BY a.buy_rank""", (mint,)).fetchall()
        return list(rows)
    except Exception:
        return []
    finally:
        conn.close()


def dex_url(t: dict, mint: str) -> str:
    pair = t.get("pair")
    chain = t.get("chain") or "solana"
    if pair:
        return f"https://dexscreener.com/{chain}/{pair}"
    return f"https://dexscreener.com/solana/{mint}"


def _ai_block(t: dict, smart_ctx: dict, mint: str) -> list:
    """Bloque del veredicto de la IA sobre el token. Si no hay IA o
    presupuesto, devuelve [] y queda el Risk Score heurístico como respaldo."""
    try:
        from ai_token import token_verdict
        v = token_verdict(t, smart_ctx, mint)
    except Exception:
        v = None
    if not v:
        return []
    nivel = (v.get("nivel") or "").lower()
    emoji = "🔴" if "alto" in nivel else "🟡" if "medio" in nivel else "🟢"
    cab = f"{emoji} *Veredicto IA: {(v.get('nivel') or '?').capitalize()}*"
    conf = v.get("confianza")
    if conf is not None:
        try:
            cab += f" ({float(conf):.0f}%)"
        except (TypeError, ValueError):
            pass
    out = [cab]
    if v.get("razon"):
        out.append(f"_{v['razon']}_")
    out.append("")
    return out


def token_report(mint: str) -> dict:
    """
    Devuelve {found, text, url, data}. found=False si el mint no es un token
    tradeable (p. ej. es una billetera) → el llamador puede seguir su flujo.
    """
    t = analyze_token(mint)
    found = bool(t.get("price") or t.get("liq") or t.get("mc"))
    if not found:
        return {"found": False, "text": "", "url": None, "data": t}

    rows = smart_money(mint)
    smart = [r for r in rows if r["grade"] in ("Elite", "Seguimiento")]
    elite = [r for r in rows if r["grade"] == "Elite"]

    risk = risk_score(t)
    nivel = "alto" if risk >= 60 else "medio" if risk >= 30 else "bajo"

    sym = (t.get("symbol") or "?").strip() or "?"
    if not sym.startswith("$"):
        sym = "$" + sym

    partes = [f"MC {_mc(t.get('mc'))}", f"Liq {_mc(t.get('liq'))}"]
    if t.get("age_days") is not None:
        partes.append(f"{t['age_days']:g}d")
    lines = [f"🧬 *{sym}* · " + " · ".join(partes), f"`{mint}`", ""]

    # Bloque del veredicto de la IA (Fase 2).
    ranks = [r["buy_rank"] for r in smart if r["buy_rank"]]
    smart_ctx = {
        "billeteras_calidad_que_compraron": len(smart),
        "de_ellas_elite": len(elite),
        "mejor_rank_de_compra": min(ranks) if ranks else None,
    }
    lines += _ai_block(t, smart_ctx, mint)

    # Registrar la foto del token para el aprendizaje (qué tokens valen la pena)
    try:
        from token_learning import record_submission
        record_submission(mint, t, risk, len(smart), len(elite))
    except Exception:
        pass

    seg = ["mint " + ("⚠️" if t.get("mint_auth") else "✅"),
           "freeze " + ("⚠️" if t.get("freeze_auth") else "✅")]
    if t.get("lp_locked_pct") is not None:
        ic = "🔒" if t["lp_locked_pct"] >= 80 else "⚠️"
        seg.append(f"LP {ic} {t['lp_locked_pct']:.0f}%")
    if t.get("top10_pct") is not None:
        ic = "⚠️" if t["top10_pct"] >= 40 else "✅"
        seg.append(f"top10 {ic} {t['top10_pct']:.0f}%")
    lines.append(f"⚠️ Riesgo {risk}/100 ({nivel})")
    lines.append("🔐 " + " · ".join(seg))
    if t.get("risks"):
        lines.append("🚩 " + ", ".join(t["risks"][:4]))

    if smart:
        quienes = ", ".join((r["alias"] or r["wallet"][:6]) for r in smart[:5])
        lines.append(f"🧠 Smart-money: {len(smart)} de tu red lo compró "
                     f"({len(elite)} ⭐ Elite)")
        lines.append(f"   _{quienes}_")
    else:
        lines.append("🧠 Smart-money: ninguna billetera de tu red lo compró aún")

    if t.get("vol24"):
        extra = f"📊 Vol 24h {_mc(t.get('vol24'))}"
        if t.get("buys5") is not None:
            extra += f" · 5min {t['buys5']}c/{t['sells5']}v"
        lines.append(extra)

    return {"found": True, "text": "\n".join(lines),
            "url": dex_url(t, mint), "data": t}
