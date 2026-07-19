"""
Cliente de Birdeye para la CALIDAD DE SALIDA (% del ATH al vender).

Birdeye da el precio histórico de cada token en SOL (OHLCV native), así
que podemos comparar el precio medio al que la billetera VENDIÓ contra
el máximo histórico del token: ¿salió cerca del pico o demasiado pronto?

Diseño ahorrador de créditos (plan gratis = 30.000 CU/mes, 1 req/s):
  - Solo se llama BAJO DEMANDA (en /adn), nunca en el escaneo masivo.
  - El ATH de cada token se cachea en memoria (TTL 1 día): un token se
    consulta una sola vez aunque aparezca en varias billeteras.
  - Si no hay BIRDEYE_API_KEY, TODO devuelve None y el bot sigue igual.

Requiere la variable de entorno BIRDEYE_API_KEY.
"""

import os
import time
from statistics import median

import requests

API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()
BASE = "https://public-api.birdeye.so"
WSOL = "So11111111111111111111111111111111111111112"

_ATH_CACHE: dict[str, tuple[float, float]] = {}   # mint -> (ath_sol, ts)
_CACHE_TTL = 86400
_RATE_GAP = 1.1        # ≥1s entre llamadas (límite del plan gratis)
_last_call = [0.0]


def enabled() -> bool:
    return bool(API_KEY)


def _get(path: str, params: dict):
    # Respeta el límite de 1 req/s del plan gratuito
    wait = _RATE_GAP - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(
            BASE + path, params=params, timeout=20,
            headers={"X-API-KEY": API_KEY, "x-chain": "solana",
                     "accept": "application/json"})
        _last_call[0] = time.time()
        if r.status_code == 200:
            return r.json()
    except requests.RequestException as e:
        print(f"  · Birdeye error {path}: {e}")
    return None


def token_ath_sol(mint: str, lookback_days: int = 45) -> float | None:
    """Máximo histórico del token en SOL (cacheado). None si no se pudo."""
    if not API_KEY or not mint:
        return None
    hit = _ATH_CACHE.get(mint)
    if hit and time.time() - hit[1] < _CACHE_TTL:
        return hit[0]
    now = int(time.time())
    data = _get("/defi/ohlcv", {
        "address": mint, "type": "1H", "currency": "native",
        "time_from": now - lookback_days * 86400, "time_to": now})
    items = (((data or {}).get("data") or {}).get("items")) or []
    highs = []
    for it in items:
        h = it.get("h", it.get("high"))
        try:
            if h is not None:
                highs.append(float(h))
        except (TypeError, ValueError):
            continue
    if not highs:
        return None
    ath = max(highs)
    _ATH_CACHE[mint] = (ath, time.time())
    return ath


def exit_quality(profile: dict, limit: int = 8) -> dict | None:
    """
    ¿A qué % del ATH suele vender? Mediana sobre las posiciones cerradas
    más grandes (hasta `limit`). Devuelve None si no hay key o datos.

    precio_medio_venta_sol = sol_in / tokens_vendidos
    pct = precio_medio_venta / ATH_sol · 100   (100% ≈ vendió en el pico)
    """
    if not API_KEY:
        return None
    tokens = (profile or {}).get("tokens") or {}
    closed = [(m, i) for m, i in tokens.items()
              if i.get("sells", 0) > 0 and i.get("tok_out", 0) > 0
              and i.get("sol_in", 0) > 0]
    if not closed:
        return None
    closed.sort(key=lambda x: x[1].get("sol_out", 0), reverse=True)

    pcts, detail = [], []
    for mint, i in closed[:limit]:
        ath = token_ath_sol(mint)
        if not ath or ath <= 0:
            continue
        avg_sell = i["sol_in"] / i["tok_out"]
        pct = round(min(100.0, 100.0 * avg_sell / ath))
        pcts.append(pct)
        detail.append((mint, pct))
    if not pcts:
        return None
    return {"exit_ath_median_pct": round(median(pcts)),
            "sampled": len(pcts), "detail": detail}


def format_exit_quality(eq: dict | None) -> str | None:
    if not eq or eq.get("exit_ath_median_pct") is None:
        return None
    p = eq["exit_ath_median_pct"]
    if p >= 80:
        cal = "excelente (vende cerca del pico)"
    elif p >= 55:
        cal = "buena"
    elif p >= 30:
        cal = "regular (sale a media subida)"
    else:
        cal = "vende demasiado pronto"
    return (f"🎯 Calidad de salida: vende al ~{p}% del ATH "
            f"· {cal} ({eq['sampled']} tokens)")
