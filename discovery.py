"""
Módulo 1: Descubrimiento de tokens ganadores en Solana.

Estrategia inversa: en lugar de buscar billeteras al azar, encontramos
tokens que YA subieron fuerte y luego extraemos quién compró temprano.

Fuentes gratuitas:
  - GeckoTerminal trending pools (sin clave, 30 req/min)
  - DexScreener (sin clave) para validar métricas del par
"""

import time
import requests

import config
from db import get_conn, save_winning_token

HEADERS = {"accept": "application/json",
           "user-agent": "wallet-discovery-pipeline/0.1"}


def _get(url: str, params: dict | None = None, delay: float = 1.0) -> dict | None:
    """GET con manejo básico de errores y rate limiting."""
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        time.sleep(delay)
        if r.status_code == 429:
            print("  · Rate limit alcanzado, esperando 30s…")
            time.sleep(30)
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"  · Error de red en {url}: {e}")
        return None


def fetch_trending_pools(pages: int = 3) -> list[dict]:
    """Descarga pools en tendencia de Solana desde GeckoTerminal."""
    pools = []
    for page in range(1, pages + 1):
        data = _get(config.GECKO_TRENDING, params={"page": page},
                    delay=config.GECKO_DELAY)
        if not data or "data" not in data:
            break
        pools.extend(data["data"])
    print(f"→ {len(pools)} pools en tendencia descargados")
    return pools


def pool_to_candidate(pool: dict) -> dict | None:
    """Convierte un pool de GeckoTerminal en candidato si cumple criterios."""
    a = pool.get("attributes", {})
    try:
        change_24h = float(a.get("price_change_percentage", {}).get("h24") or 0)
        volume_24h = float(a.get("volume_usd", {}).get("h24") or 0)
        liquidity = float(a.get("reserve_in_usd") or 0)
    except (TypeError, ValueError):
        return None

    if change_24h < config.MIN_PRICE_CHANGE_24H:
        return None
    if volume_24h < config.MIN_VOLUME_24H_USD:
        return None
    if liquidity < config.MIN_LIQUIDITY_USD:
        return None

    # El mint del token base viene en relationships.base_token.data.id
    # con formato "solana_<mint>"
    rel = pool.get("relationships", {}).get("base_token", {}).get("data", {})
    raw_id = rel.get("id", "")
    mint = raw_id.split("_", 1)[1] if "_" in raw_id else None
    if not mint:
        return None

    name = a.get("name", "")          # ej. "BONK / SOL"
    symbol = name.split("/")[0].strip() if "/" in name else name

    return {
        "mint": mint,
        "symbol": symbol,
        "name": name,
        "price_change_24h": change_24h,
        "volume_24h_usd": volume_24h,
        "liquidity_usd": liquidity,
        "pair_address": a.get("address"),
    }


def run_discovery() -> int:
    """Ciclo completo: descarga tendencias, filtra y guarda ganadores nuevos."""
    conn = get_conn()
    pools = fetch_trending_pools()
    saved = 0
    for pool in pools:
        cand = pool_to_candidate(pool)
        if not cand:
            continue
        save_winning_token(conn, cand)
        saved += 1
        print(f"  ✓ Ganador: {cand['symbol']:<12} "
              f"+{cand['price_change_24h']:.0f}% 24h | "
              f"vol ${cand['volume_24h_usd']:,.0f} | "
              f"liq ${cand['liquidity_usd']:,.0f}")
    conn.close()
    print(f"→ {saved} tokens ganadores guardados/actualizados")
    return saved


if __name__ == "__main__":
    run_discovery()
