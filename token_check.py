"""
Análisis completo de un token (para señales y veredictos IA):
DexScreener (precio, liquidez, MC, edad, volumen, flujo 5min, redes)
+ RugCheck (riesgos, mint/freeze authority, LP bloqueado, top holders).
Todo con APIs gratuitas.
"""

import time

import requests

import config

try:
    from api_usage import record as _api_rec
except Exception:          # nunca romper el flujo por el contador
    def _api_rec(*a, **k):
        pass

RUG_SUMMARY = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
RUG_FULL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


def _get(url: str, timeout: int = 15):
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return None


def analyze_token(mint: str) -> dict:
    t = {"symbol": "?", "price": None, "liq": None, "mc": None,
         "age_days": None, "vol24": None, "buys5": None, "sells5": None,
         "price_change_h1": None, "websites": [], "socials": [],
         "rug_score": None, "risks": [], "mint_auth": None,
         "freeze_auth": None, "top10_pct": None, "lp_locked_pct": None}

    d = _get(config.DEXSCREENER_TOKEN.format(address=mint))
    _api_rec("dexscreener")
    pairs = (d or {}).get("pairs") or []
    if pairs:
        # Par de MAYOR liquidez: precio mas fiable que pairs[0]
        def _liq(x):
            try:
                return float(((x.get("liquidity") or {}).get("usd")) or 0)
            except (TypeError, ValueError):
                return 0.0
        p = max(pairs, key=_liq)
        try:
            t["price"] = float(p.get("priceUsd") or 0) or None
        except (TypeError, ValueError):
            pass
        t["symbol"] = (p.get("baseToken") or {}).get("symbol", "?")
        t["liq"] = (p.get("liquidity") or {}).get("usd")
        t["mc"] = p.get("fdv")
        t["price_change_h1"] = (p.get("priceChange") or {}).get("h1")
        t["vol24"] = (p.get("volume") or {}).get("h24")
        m5 = (p.get("txns") or {}).get("m5") or {}
        t["buys5"], t["sells5"] = m5.get("buys"), m5.get("sells")
        created = p.get("pairCreatedAt")
        if created:
            t["age_days"] = round((time.time() - created / 1000) / 86400, 1)
        info = p.get("info") or {}
        t["websites"] = [w.get("url") for w in (info.get("websites") or [])
                         if w.get("url")][:2]
        t["socials"] = [
            f"{(s.get('type') or 'link').capitalize()}: {s.get('url')}"
            for s in (info.get("socials") or []) if s.get("url")][:4]

    s = _get(RUG_SUMMARY.format(mint=mint))
    if s:
        t["rug_score"] = s.get("score")
        t["risks"] = [x.get("name", "") for x in (s.get("risks") or [])
                      if x.get("level") in ("danger", "warn")][:5]

    f = _get(RUG_FULL.format(mint=mint), timeout=25)
    if f:
        tok = f.get("token") or {}
        t["mint_auth"] = tok.get("mintAuthority")
        t["freeze_auth"] = tok.get("freezeAuthority")
        th = f.get("topHolders") or []
        if th:
            try:
                t["top10_pct"] = round(
                    sum(float(h.get("pct") or 0) for h in th[:10]), 1)
            except (TypeError, ValueError):
                pass
        locked = []
        for m in (f.get("markets") or []):
            lp = (m.get("lp") or {}).get("lpLockedPct")
            if lp is not None:
                locked.append(lp)
        if locked:
            t["lp_locked_pct"] = round(max(locked), 1)
    return t


def _usd(v) -> str:
    return f"${v:,.0f}" if v else "?"


def format_token_block(t: dict) -> str:
    """Bloque de texto para el mensaje de Telegram."""
    lines = [f"Liquidez: {_usd(t['liq'])} · MC: {_usd(t['mc'])}"]
    extra = []
    if t["age_days"] is not None:
        extra.append(f"edad: {t['age_days']:g}d")
    if t["vol24"]:
        extra.append(f"vol24h: {_usd(t['vol24'])}")
    if t["buys5"] is not None:
        extra.append(f"últimos 5min: {t['buys5']}c/{t['sells5']}v")
    if extra:
        lines.append(" · ".join(extra))
    seg = ["mint: " + ("⚠️ ACTIVA" if t["mint_auth"] else "✅ revocada"),
           "freeze: " + ("⚠️ ACTIVA" if t["freeze_auth"] else "✅ no")]
    if t["lp_locked_pct"] is not None:
        icono = "✅" if t["lp_locked_pct"] >= 80 else "⚠️"
        seg.append(f"LP lock: {icono} {t['lp_locked_pct']:.0f}%")
    if t["top10_pct"] is not None:
        icono = "⚠️" if t["top10_pct"] >= 40 else "✅"
        seg.append(f"top10 holders: {icono} {t['top10_pct']:.0f}%")
    lines.append("🔐 " + " · ".join(seg))
    if t["risks"]:
        lines.append("⚠️ Riesgos: " + ", ".join(t["risks"]))
    return "\n".join(lines)


def ai_payload(t: dict) -> dict:
    """Versión compacta para pasarle a la IA."""
    return {k: t[k] for k in
            ("symbol", "liq", "mc", "price_change_h1", "age_days", "vol24",
             "buys5", "sells5", "rug_score", "risks", "mint_auth",
             "freeze_auth", "top10_pct", "lp_locked_pct")}
