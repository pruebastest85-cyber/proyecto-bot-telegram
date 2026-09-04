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

try:
    from api_usage import record as _api_rec
except Exception:          # nunca romper el flujo por el contador
    def _api_rec(*a, **k):
        pass

WSOL = "So11111111111111111111111111111111111111112"
# USDC/USDT: los ignoramos como "cartera de trading" (son caja, no apuesta)
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

_SOL_CACHE = {"px": None, "ts": 0.0, "aviso": 0.0}

# (Ola 17-F) Cuanto se acepta un precio de SOL guardado cuando la fuente
# no responde. SOL no se mueve tanto en unas horas como para que un
# importe en dolares deje de ser util, y la alternativa era NO DAR LA
# CIFRA: sin precio de SOL, `stake_usd` quedaba NULL al abrir la
# posicion y la alerta de cierre caia al texto en SOL — el dueño dejaba
# de ver "cuanto habria ganado o perdido de verdad".
SOL_PX_MAX_H = 12


def _sol_px_guardado():
    """(precio, horas de antiguedad) del ultimo SOL/USD que sí se leyó."""
    try:
        from db import get_conn, get_setting
        conn = get_conn()
        try:
            px = get_setting(conn, "sol_usd_ultimo", None)
            ts = get_setting(conn, "sol_usd_ultimo_ts", None)
        finally:
            conn.close()
        if px is None or ts is None:
            return (None, None)
        return (float(px), (time.time() - float(ts)) / 3600.0)
    except Exception:
        return (None, None)


def _guardar_sol_px(px: float):
    try:
        from db import get_conn, set_setting
        conn = get_conn()
        try:
            set_setting(conn, "sol_usd_ultimo", float(px))
            set_setting(conn, "sol_usd_ultimo_ts", time.time())
        finally:
            conn.close()
    except Exception:
        pass


def _sol_usd() -> float | None:
    """Precio SOL/USD (mediana de pares WSOL en DexScreener), cache 5 min.

    (Ola 17-F) Antes, si DexScreener fallaba UNA vez pasados los 5 min de
    cache, esto devolvia None y toda la cadena en dolares se venia abajo
    en silencio: `stake_usd` NULL al abrir, y al cerrar la alerta decia
    "PnL: -0.031 SOL sobre 1.00 SOL" en vez de los dolares. Medido en la
    base del dueño: 67 de 238 operaciones cerradas sin `stake_usd`.
    Ahora el ultimo precio bueno se guarda en la base (sobrevive a los
    reinicios) y se usa como respaldo hasta SOL_PX_MAX_H horas.
    """
    if _SOL_CACHE["px"] and time.time() - _SOL_CACHE["ts"] < 300:
        return _SOL_CACHE["px"]
    # (19-AC, auditoria BAJO) Con DexScreener caida, CADA llamada volvia
    # a pedir (15 s de timeout) porque el respaldo no refrescaba nada:
    # un cierre con tres consultas de precio tardaba 45 s. Tras un fallo
    # se usa el respaldo directamente durante 60 s.
    if time.time() - _SOL_CACHE.get("reintento", 0) < 60:
        px_r = _SOL_CACHE["px"]
        if px_r and (time.time() - _SOL_CACHE["ts"]) / 3600.0 <= SOL_PX_MAX_H:
            return px_r
    fallo = None
    try:
        r = requests.get(config.DEXSCREENER_TOKEN.format(address=WSOL),
                         timeout=15)
        _api_rec("dexscreener")
        pairs = (r.json() or {}).get("pairs") or []
        usd = sorted(float(p["priceUsd"]) for p in pairs if p.get("priceUsd"))
        if usd:
            _SOL_CACHE["px"] = usd[len(usd) // 2]   # mediana
            _SOL_CACHE["ts"] = time.time()
            _guardar_sol_px(_SOL_CACHE["px"])
            return _SOL_CACHE["px"]
        fallo = f"DexScreener no devolvio ningun par con precio (HTTP {r.status_code})"
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        fallo = f"{type(e).__name__}: {str(e)[:120]}"

    # No se pudo leer: respaldo con el ultimo precio conocido.
    _SOL_CACHE["reintento"] = time.time()
    if _SOL_CACHE["px"]:
        edad_h = (time.time() - _SOL_CACHE["ts"]) / 3600.0
        if edad_h <= SOL_PX_MAX_H:
            _avisar_px(fallo, edad_h)
            return _SOL_CACHE["px"]
    px, edad_h = _sol_px_guardado()
    if px and edad_h is not None and edad_h <= SOL_PX_MAX_H:
        _SOL_CACHE["px"], _SOL_CACHE["ts"] = px, time.time() - edad_h * 3600
        _avisar_px(fallo, edad_h)
        return px
    # Sin respaldo utilizable: se dice, no se calla.
    _avisar_px(fallo, edad_h, sin_respaldo=True)
    return None


def _avisar_px(fallo, edad_h, sin_respaldo: bool = False):
    """Un aviso cada 30 min como mucho: antes esto fallaba en absoluto
    silencio (`except: pass`) y el dueño veia el sintoma —importes en SOL
    en vez de en dolares— sin ninguna pista de la causa."""
    if time.time() - _SOL_CACHE.get("aviso", 0) < 1800:
        return
    _SOL_CACHE["aviso"] = time.time()
    if sin_respaldo:
        print(f"· Precio de SOL NO disponible ({fallo}) y sin respaldo "
              f"reciente: los importes se daran en SOL, no en dolares")
    else:
        print(f"· Precio de SOL: la fuente no responde ({fallo}); se usa "
              f"el ultimo conocido de hace {edad_h:.1f} h")


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
            # (Ola 17-B) Llamada RPC estandar: ~10 creditos. Tampoco se
            # contaba, y son 2 por billetera (SPL + Token-2022).
            _api_rec("helius")
            _api_rec("helius_credits", config.HELIUS_CREDITS_RPC)
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


def get_prices_sol(mints: list[str]) -> dict[str, tuple]:
    """{mint: (precio en SOL, liquidez USD)} vía DexScreener (lotes de 30)."""
    prices: dict[str, tuple] = {}
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
            _api_rec("dexscreener")
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
        for mint, (px_usd, liq) in best.items():
            prices[mint] = (px_usd / sol_usd, liq)
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
    cap_frac = float(getattr(config, "LIQ_CAP_FRACTION", 0.10))
    sol_px = _SOL_CACHE.get("px")
    for mint, amt in balances.items():
        info = prices.get(mint)
        if info is None:
            continue
        px, liq_usd = info
        val = amt * px
        # Ganancia REALIZABLE: la posición no puede valer más que una
        # fracción de la liquidez del pool — evita "wallets milagrosas"
        # con PnL teórico enorme que jamás podría venderse.
        if liq_usd and sol_px and cap_frac > 0:
            try:
                val = min(val, cap_frac * float(liq_usd) / float(sol_px))
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if val <= 0:
            continue
        out["holdings"][mint] = round(val, 3)
        out["unrealized_sol"] += val
        out["priced_tokens"] += 1
    out["unrealized_sol"] = round(out["unrealized_sol"], 3)
    out["sol_usd"] = _SOL_CACHE.get("px")
    return out
