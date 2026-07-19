"""
PnL no realizado: valora los tokens que la billetera AÚN mantiene.

Combina dos fuentes gratuitas:
  - Helius RPC getTokenAccountsByOwner → balances actuales por mint
  - DexScreener → precio actual de cada mint (convertido a SOL)

Devuelve el valor de mercado en SOL de la cartera abierta, para
sumarlo al PnL realizado y obtener el PnL neto (mark-to-market):

    pnl_neto = pnl_realizado + valor_en_cartera

Diseño defensivo: TODO es best-effort. Si una API falla o un token no
tiene precio, ese token aporta 0 SOL y queda reflejado en la cobertura
(priced_tokens / held_tokens). Nunca lanza excepciones hacia arriba.
"""

import time

import requests

import config

WSOL = "So11111111111111111111111111111111111111112"
# USDC/USDT: los ignoramos como "cartera de trading" (son caja, no apuesta)
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

_SOL_CACHE = {"px": None, "ts": 0.0}


def _sol_usd() -> float | None:
    """Precio SOL/USD (mediana de pares WSOL en DexScreener), cache 5 min."""
    if _SOL_CACHE["px"] and time.time() - _SOL_CACHE["ts"] < 300:
        return _SOL_CACHE["px"]
    try:
        r = requests.get(config.DEXSCREENER_TOKEN.format(address=WSOL),
                         timeout=15)
        pairs = (r.json() or {}).get("pairs") or []
        usd = sorted(float(p["priceUsd"]) for p in pairs if p.get("priceUsd"))
        if usd:
            _SOL_CACHE["px"] = usd[len(usd) // 2]   # mediana
            _SOL_CACHE["ts"] = time.time()
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    return _SOL_CACHE["px"]


def get_token_balances(address: str) -> dict[str, float]:
    """{mint: uiAmount} de los tokens con saldo > 0 (SPL + Token-2022)."""
    balances: dict[str, float] = {}
    for program in (TOKEN_PROGRAM, TOKEN_2022):
        try:
            r = requests.post(
                config.HELIUS_RPC, timeout=20,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getTokenAccountsByOwner",
                      "params": [address, {"programId": program},
                                 {"encoding": "jsonParsed"}]})
            time.sleep(config.HELIUS_DELAY)
            accts = (r.json().get("result") or {}).get("value") or []
        except (requests.RequestException, ValueError, KeyError, TypeError):
            accts = []
        for a in accts:
            try:
                info = a["account"]["data"]["parsed"]["info"]
                mint = info["mint"]
                amt = info["tokenAmount"]["uiAmount"] or 0.0
            except (KeyError, TypeError):
                continue
            if amt > 0:
                balances[mint] = balances.get(mint, 0.0) + float(amt)
    return balances


def get_prices_sol(mints: list[str]) -> dict[str, float]:
    """{mint: precio en SOL} vía DexScreener (lotes de 30 mints)."""
    prices: dict[str, float] = {}
    sol_usd = _sol_usd()
    if not sol_usd:
        return prices
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            r = requests.get(
                config.DEXSCREENER_TOKEN.format(address=",".join(chunk)),
                timeout=20)
            time.sleep(config.DEXSCREENER_DELAY)
            pairs = (r.json() or {}).get("pairs") or []
        except (requests.RequestException, ValueError, TypeError):
            pairs = []
        # Por mint, quedarse con el par de MAYOR liquidez (precio más fiable)
        best: dict[str, tuple[float, float]] = {}
        for p in pairs:
            mint = (p.get("baseToken") or {}).get("address")
            px_usd = p.get("priceUsd")
            if not mint or not px_usd:
                continue
            liq = ((p.get("liquidity") or {}).get("usd")) or 0.0
            try:
                px = float(px_usd)
            except (TypeError, ValueError):
                continue
            if mint not in best or liq > best[mint][1]:
                best[mint] = (px, liq)
        for mint, (px_usd, _liq) in best.items():
            prices[mint] = px_usd / sol_usd
    return prices


def holdings_value(address: str, skip_mints=None) -> dict:
    """
    Valor de mercado (en SOL) de los tokens que la billetera aún tiene.

    Ignora WSOL (ya se cuenta como saldo SOL) y stablecoins (caja, no
    apuesta abierta). Devuelve el total, el desglose por mint y la
    cobertura: cuántos de los tokens con saldo pudieron valorarse.
    """
    skip = set(skip_mints or ()) | {WSOL} | STABLES
    balances = {m: a for m, a in get_token_balances(address).items()
                if m not in skip}
    out = {"unrealized_sol": 0.0, "holdings": {}, "held_tokens": len(balances),
           "priced_tokens": 0, "sol_usd": _SOL_CACHE.get("px")}
    if not balances:
        return out
    prices = get_prices_sol(list(balances))
    for mint, amt in balances.items():
        px = prices.get(mint)
        if px is None:
            continue
        val = amt * px
        if val <= 0:
            continue
        out["holdings"][mint] = round(val, 3)
        out["unrealized_sol"] += val
        out["priced_tokens"] += 1
    out["unrealized_sol"] = round(out["unrealized_sol"], 3)
    out["sol_usd"] = _SOL_CACHE.get("px")
    return out
