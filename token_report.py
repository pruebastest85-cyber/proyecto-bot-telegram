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


def _esc(x) -> str:
    """Escapa los caracteres que rompen el Markdown de Telegram.
    Los símbolos de memecoin suelen traer _ * ` [ y sin escapar el mensaje
    entero es rechazado con error 400 (y el usuario no recibe nada)."""
    t = "" if x is None else str(x)
    for ch in ("\\", "_", "*", "`", "["):
        t = t.replace(ch, "\\" + ch)
    return t


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


def risk_score(t: dict):
    """Risk Score heurístico 0-100, o None si RugCheck no respondió.

    (Ola 17-A) mint_auth/freeze_auth/top10/lp valen None tanto cuando la
    autoridad está REVOCADA (lo mejor posible) como cuando no se pudo
    comprobar nada. Sin distinguirlo, un token sin verificar salía como
    "Riesgo 0/100 (bajo)" y además se guardaba ese 0 en token_learning
    como si fuera una medición. `rug_ok` lo declara la propia fuente.
    """
    if not t.get("rug_ok"):
        return None
    r = 0
    if t.get("mint_auth"):
        r += 35
    if t.get("freeze_auth"):
        r += 25
    # (Ola 17-E) Los valores que llegan de una API externa pueden ser
    # basura (texto, negativos). Antes un top10_pct de "-50" daba un
    # "Riesgo -25/100", imposible de interpretar, y un texto reventaba.
    try:
        top10 = float(t["top10_pct"]) if t.get("top10_pct") is not None else None
    except (TypeError, ValueError):
        top10 = None
    if top10 is not None:
        r += min(40, max(0.0, top10) * 0.5)
    try:
        lp = float(t["lp_locked_pct"]) if t.get("lp_locked_pct") is not None else None
    except (TypeError, ValueError):
        lp = None
    if lp is not None and lp < 50:
        r += 20
    if t.get("risks"):
        r += 10 * len(t["risks"])
    return max(0, min(100, round(r)))


def smart_money(mint: str) -> list | None:
    """Billeteras de la propia red que compraron el token, con su grado.
    None si la consulta fallo (distinto de "ninguna")."""
    conn = None
    try:
        conn = get_conn()
        rows = conn.execute(
            """SELECT a.wallet, a.buy_rank, w.grade, w.alias
               FROM appearances a JOIN wallets w ON w.address = a.wallet
               WHERE a.mint = ? AND COALESCE(w.is_bot,0)=0
               ORDER BY a.buy_rank""", (mint,)).fetchall()
        return list(rows)
    except Exception as e:
        # (19-AD) None = "no pude consultar"; [] era "ninguna consta" y
        # el reporte lo afirmaba con la base bloqueada.
        print(f"· smart_money: no pude consultar la base ({e})")
        return None
    finally:
        if conn is not None:
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
        out.append(f"_{_esc(v['razon'])}_")
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
    _sin_consulta = rows is None                   # (19-AD)
    rows = rows or []
    smart = [r for r in rows if r["grade"] in ("Elite", "Seguimiento")]
    elite = [r for r in rows if r["grade"] == "Elite"]

    risk = risk_score(t)
    nivel = (None if risk is None else
             "alto" if risk >= 60 else "medio" if risk >= 30 else "bajo")

    sym = (t.get("symbol") or "?").strip() or "?"
    if not sym.startswith("$"):
        sym = "$" + sym
    sym = _esc(sym)

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

    if risk is None:
        lines.append("⚠️ Riesgo: sin datos (RugCheck no respondió)")
        lines.append("🔐 ⚪ *Seguridad sin comprobar* — mint/freeze/LP/holders "
                     "no se pudieron leer. No es lo mismo que estar limpio.")
    else:
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
        lines.append("🚩 " + ", ".join(_esc(x) for x in t["risks"][:4]))

    if smart:
        quienes = ", ".join(_esc(r["alias"] or r["wallet"][:6])
                            for r in smart[:5])
        lines.append(f"🧠 Smart-money: {len(smart)} de tu red lo compró "
                     f"({len(elite)} ⭐ Elite)")
        lines.append(f"   _{quienes}_")
    elif _sin_consulta:
        lines.append("🧠 Smart-money: no pude consultar la base (reintenta)")
    else:
        # (Ola 8) Solo consulta compradores TEMPRANOS registrados
        # (appearances): una compra reciente de una ⭐ puede no constar.
        lines.append("🧠 Smart-money: ninguna consta como compradora "
                     "temprana de este token")

    if t.get("vol24"):
        extra = f"📊 Vol 24h {_mc(t.get('vol24'))}"
        if t.get("buys5") is not None:
            extra += f" · 5min {t['buys5']}c/{t['sells5']}v"
        lines.append(extra)

    return {"found": True, "text": "\n".join(lines),
            "url": dex_url(t, mint), "data": t}
