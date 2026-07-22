"""
Módulo 1: Descubrimiento de tokens ganadores en Solana.

Estrategia inversa: en lugar de buscar billeteras al azar, encontramos
tokens que YA subieron fuerte y luego extraemos quién compró temprano.

v2: además de los pools en tendencia, escanea los pools NUEVOS de
GeckoTerminal para detectar ganadores más temprano.

Fuentes gratuitas:
  - GeckoTerminal trending + new pools (sin clave, 30 req/min)
  - DexScreener (sin clave) para validar métricas del par
"""

import time
import requests

import config
from db import get_conn, save_winning_token

HEADERS = {"accept": "application/json",
           "user-agent": "wallet-discovery-pipeline/0.2"}


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


def _fetch_pools(url: str, pages: int, etiqueta: str) -> list[dict]:
    pools = []
    for page in range(1, pages + 1):
        data = _get(url, params={"page": page}, delay=config.GECKO_DELAY)
        if not data or "data" not in data:
            break
        pools.extend(data["data"])
    print(f"→ {len(pools)} pools {etiqueta} descargados")
    return pools


def fetch_trending_pools(pages: int = 3) -> list[dict]:
    """Pools en tendencia de Solana (los que ya están explotando)."""
    return _fetch_pools(config.GECKO_TRENDING, pages, "en tendencia")


def fetch_new_pools(pages: int = 2) -> list[dict]:
    """Pools recién creados (para cazar ganadores más temprano)."""
    return _fetch_pools(config.GECKO_NEW_POOLS, pages, "nuevos")


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

    # Solo tokens RECIENTES (estrategia memecoin): MAX_TOKEN_AGE_DAYS
    # existia en config pero nunca se aplicaba.
    created = a.get("pool_created_at")
    if created:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - dt).days > \
                    config.MAX_TOKEN_AGE_DAYS:
                return None
        except (ValueError, TypeError):
            pass

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
    """Ciclo completo: tendencias + pools nuevos, filtra y guarda ganadores."""
    conn = get_conn()
    pools = fetch_trending_pools()
    try:
        pools += fetch_new_pools()
    except Exception as e:
        print(f"  · new_pools falló (no crítico): {e}")
    saved, vistos = 0, set()
    for pool in pools:
        cand = pool_to_candidate(pool)
        if not cand or cand["mint"] in vistos:
            continue
        vistos.add(cand["mint"])
        if not save_winning_token(conn, cand):
            continue           # ya existia: actualizado, no cuenta como nuevo
        saved += 1
        print(f"  ✓ Ganador: {cand['symbol']:<12} "
              f"+{cand['price_change_24h']:.0f}% 24h | "
              f"vol ${cand['volume_24h_usd']:,.0f} | "
              f"liq ${cand['liquidity_usd']:,.0f}")
    conn.close()
    print(f"→ {saved} tokens ganadores NUEVOS guardados")
    return saved


if __name__ == "__main__":
    run_discovery()
