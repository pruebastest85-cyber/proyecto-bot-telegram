"""
Análisis completo de un token (para señales y veredictos IA):
DexScreener (precio, liquidez, MC, edad, volumen, flujo 5min, redes)
+ RugCheck (riesgos, mint/freeze authority, LP bloqueado, top holders).
Todo con APIs gratuitas.
"""

import os
import threading
import time

import requests

import config

# ── (Ola 17-G) Cache partida por VOLATILIDAD ──────────────────────────
# Medido en la base del dueño: de 4.135 señales en 24 h, el 47% llega a
# menos de 45 s de otra señal DEL MISMO TOKEN, y el 66% a menos de 30
# min. Cada una relanzaba `analyze_token` entera: 1 petición a
# DexScreener + 2 a RugCheck, desde cero.
#
# No se cachea todo con el mismo reloj, porque las dos mitades no
# envejecen igual:
#   · PRECIO (DexScreener): en un memecoin cambia en segundos. Cache
#     corta — si no, la tarjeta daría un MC que ya no existe.
#   · SEGURIDAD (RugCheck): mint/freeze/LP/holders son casi estáticos;
#     revocar una autoridad es un evento raro. Cache larga.
# Además la parte de seguridad es la CARA (2 llamadas) y la que más
# falla: cada acierto de cache es un `sin_seguridad` menos.
DEX_TTL_S = int(os.getenv("TOKEN_DEX_TTL_S", "45"))
RUG_TTL_S = int(os.getenv("TOKEN_RUG_TTL_S", "1800"))
_CACHE_MAX = 500

_dex_cache: dict = {}
_rug_cache: dict = {}
_cache_lock = threading.Lock()


def _cache_get(cache, mint, ttl):
    with _cache_lock:
        v = cache.get(mint)
        if v and time.time() - v[0] < ttl:
            return v[1]
    return None


def _cache_put(cache, mint, valor):
    with _cache_lock:
        if len(cache) >= _CACHE_MAX:
            # Purga simple: fuera la mitad más vieja. Sin esto, un bot
            # que ve miles de mints al día se come la memoria.
            for k in sorted(cache, key=lambda k: cache[k][0])[:_CACHE_MAX // 2]:
                cache.pop(k, None)
        cache[mint] = (time.time(), valor)


def limpiar_cache():
    """Para pruebas y para forzar datos frescos."""
    with _cache_lock:
        _dex_cache.clear()
        _rug_cache.clear()

try:
    from api_usage import record as _api_rec
except Exception:          # nunca romper el flujo por el contador
    def _api_rec(*a, **k):
        pass

RUG_SUMMARY = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
RUG_FULL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


_ULTIMO_AVISO = [0.0]


def _get(url: str, timeout: int = 15):
    """GET que devuelve el JSON o None.

    (Ola 17-I, auditoria 6) Antes esto era un `except: pass` sin una sola
    linea de rastro, y es el envoltorio del que dependen las señales, el
    radar, las predicciones y el paper. Un 429 sostenido de DexScreener o
    de RugCheck era indistinguible de "ese token no existe" y no dejaba
    huella en ningun sitio. Ahora se avisa (como mucho uno cada 5 min,
    para no inundar el log) y se registra.
    """
    _fuente = ("DexScreener" if "dexscreener" in url
               else "RugCheck" if "rugcheck" in url else "API")
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        _motivo = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        _motivo = f"{type(e).__name__}: {str(e)[:70]}"
    if time.time() - _ULTIMO_AVISO[0] > 300:
        _ULTIMO_AVISO[0] = time.time()
        print(f"· {_fuente} no responde ({_motivo}); los datos que "
              f"dependan de ella se quedan sin rellenar")
        try:
            from errores import record as _rec
            _rec(f"api.{_fuente.lower()}", RuntimeError(_motivo))
        except Exception:
            pass
    return None


def analyze_token(mint: str) -> dict:
    t = {"symbol": "?", "pair": None, "chain": "solana",
         "price": None, "liq": None, "mc": None,
         "age_days": None, "vol24": None, "buys5": None, "sells5": None,
         "price_change_h1": None, "price_change_h24": None,
         "websites": [], "socials": [],
         "rug_score": None, "risks": [], "rug_ok": False, "mint_auth": None,
         "freeze_auth": None, "top10_pct": None, "lp_locked_pct": None}

    _dex = _cache_get(_dex_cache, mint, DEX_TTL_S)
    if _dex is None:
        _dex = _get(config.DEXSCREENER_TOKEN.format(address=mint))
        _api_rec("dexscreener")
        # (Ola 17-H) La guarda era `_dex is not None`, y eso CACHEABA las
        # respuestas vacias: un token de pump.fun con minutos de vida
        # devuelve HTTP 200 con {"pairs": null} — es un dict, no es None,
        # y se quedaba 45 s en cache. Durante esos 45 s TODAS las señales
        # de ese mint se guardaban sin precio, y una señal sin precio de
        # entrada queda fuera de la medicion PARA SIEMPRE. Solo se cachea
        # una respuesta con pares de verdad; el resto se vuelve a pedir.
        if _dex is not None and (_dex.get("pairs") or []):
            _cache_put(_dex_cache, mint, _dex)
    pairs = (_dex or {}).get("pairs") or []
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
        t["pair"] = p.get("pairAddress")
        t["chain"] = p.get("chainId") or "solana"
        t["liq"] = (p.get("liquidity") or {}).get("usd")
        # marketCap primero, fdv de respaldo (Ola 6, auditoria 19/8 -
        # M3): igual que signal_tracker._price_mc. Antes aqui era FDV a
        # secas y alla marketCap: en tokens con supply parcial (FDV≫MC)
        # el chequeo de cordura de los hitos descartaba tarjetas
        # legitimas como "dato poco fiable" y la linea "MC $a → $b"
        # mezclaba unidades.
        t["mc"] = p.get("marketCap") or p.get("fdv")
        t["price_change_h1"] = (p.get("priceChange") or {}).get("h1")
        t["price_change_h24"] = (p.get("priceChange") or {}).get("h24")
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

    # (Ola 17-G) Las dos llamadas de RugCheck viajan juntas en la cache:
    # o se guardan las dos o ninguna, para que `rug_ok` nunca quede
    # descolgado de los datos que lo justifican.
    _rug = _cache_get(_rug_cache, mint, RUG_TTL_S)
    if _rug is None:
        s = _get(RUG_SUMMARY.format(mint=mint))
        _api_rec("rugcheck")
        f = _get(RUG_FULL.format(mint=mint), timeout=25)
        _api_rec("rugcheck")
        if f:                         # solo se cachea un chequeo COMPLETO
            _cache_put(_rug_cache, mint, {"s": s, "f": f})
    else:
        s, f = _rug.get("s"), _rug.get("f")

    if s:
        t["rug_score"] = s.get("score")
        t["risks"] = [x.get("name", "") for x in (s.get("risks") or [])
                      if x.get("level") in ("danger", "warn")][:5]

    if f:
        # (Ola 16) La fuente RESPONDIO. Es el unico dato fiable para saber
        # si el chequeo se hizo: mintAuthority/freezeAuthority en null
        # significan REVOCADA (el mejor estado posible), y topHolders o
        # markets vacios son normales en un token de minutos. Deducir
        # "chequeo fallido" de esos nulos rechazaba justo los tokens
        # limpios (regresion de la Ola 15 en el radar).
        t["rug_ok"] = True
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
    # (Ola 17-A) Sin respuesta de RugCheck no se puede decir "revocada":
    # mint_auth/freeze_auth valen None tanto si la autoridad esta
    # REVOCADA como si el chequeo no llego a hacerse. Decir "✅ revocada"
    # sobre lo segundo es inventarse un veredicto de seguridad.
    if not t.get("rug_ok"):
        lines.append("🔐 ⚪ Seguridad SIN COMPROBAR (RugCheck no respondió) "
                     "— no es lo mismo que estar limpio")
    else:
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
    # (Ola 17-A) `rug_ok` va SIEMPRE: sin el, la IA ve mint_auth=null y
    # concluye "autoridad revocada, token limpio" cuando lo cierto es
    # que el chequeo no se pudo hacer.
    return {k: t.get(k) for k in
            ("symbol", "liq", "mc", "price_change_h1", "age_days", "vol24",
             "buys5", "sells5", "rug_score", "risks", "rug_ok", "mint_auth",
             "freeze_auth", "top10_pct", "lp_locked_pct")}
