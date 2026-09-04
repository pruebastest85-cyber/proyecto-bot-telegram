"""
Ejecucion simulada con precios EJECUTABLES (fase 2b del copy trading).

El paper trading clasico es optimista: "compra" al precio que muestra
DexScreener en el momento de la señal, sin slippage, sin fees del pool y
sin demora. Este modulo pregunta a Jupiter (el agregador que usaria el
copy trading real) cuanto darian DE VERDAD por la orden, ahora mismo,
con la ruta real y su impacto de precio — pero sin ejecutar nada.

Que capta:  slippage / impacto de precio, fees de los pools (van dentro
            de la cotizacion), demora señal→orden (la cotizacion se pide
            cuando el bot procesa, no cuando la ⭐ compro).
Que NO capta: la fee de prioridad (se suma como costo fijo configurable)
            y el movimiento del precio en el ~1 s entre cotizar y aterrizar.

Todo se guarda en cantidades CRUDAS de token (tokens_raw), sin decimales:
al vender se cotiza esa misma cantidad cruda y los decimales se cancelan.
"""

import requests

QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000

# Fee de prioridad estimada por transaccion, en SOL. Configurable via
# settings ("paper_fee_sol"). 0.0005 SOL es un valor tipico con congestion
# moderada; el modo real la medira de verdad.
FEE_SOL_DEFECTO = 0.0005

TIMEOUT = 12


def _quote(input_mint: str, output_mint: str, amount_raw: int) -> dict | None:
    """Una cotizacion de Jupiter. None si no hay ruta o falla la red."""
    try:
        r = requests.get(QUOTE_URL, params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": str(int(amount_raw)), "slippageBps": 100,
        }, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        d = r.json()
        # (19-AC, auditoria BAJO) `outAmount` es TEXTO: "0" es verdadero
        # y pasaba como cotizacion valida → 0 tokens al comprar
        # (slippage "100 %") y 0 $ al vender (parecia un rug). Sin
        # salida no hay cotizacion.
        try:
            if int(d.get("outAmount") or 0) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        return d
    except Exception as e:
        print(f"· Jupiter: cotización falló ({e})")
        return None


def cotizar_compra(mint: str, usd: float, sol_usd: float) -> dict | None:
    """¿Cuantos tokens (crudos) darian AHORA por `usd` dolares?

    Devuelve tokens_raw, el impacto de la ruta y el slippage medido
    contra una orden chica (1/50 del monto): la diferencia entre lo que
    paga una orden grande y una diminuta es el costo real de ENTRAR con
    ese tamaño. Los decimales del token se cancelan en el cociente.
    """
    if not sol_usd or sol_usd <= 0 or not usd or usd <= 0:
        return None
    lamports = int(usd / sol_usd * LAMPORTS)
    if lamports < 10000:
        return None
    grande = _quote(SOL_MINT, mint, lamports)
    if not grande:
        return None
    tokens_raw = int(grande["outAmount"])
    slippage = None
    chica = _quote(SOL_MINT, mint, max(lamports // 50, 10000))
    if chica and int(chica["outAmount"]) > 0 and int(chica["inAmount"]) > 0:
        tasa_grande = tokens_raw / int(grande["inAmount"])
        tasa_chica = int(chica["outAmount"]) / int(chica["inAmount"])
        if tasa_chica > 0:
            slippage = max(0.0, (1 - tasa_grande / tasa_chica) * 100)
    try:
        # Jupiter lo entrega ya en porcentaje ("0.12" = 0.12%).
        impacto = round(abs(float(grande.get("priceImpactPct") or 0)), 3)
    except (TypeError, ValueError):
        impacto = None
    return {"tokens_raw": tokens_raw, "usd_entrada": usd,
            "slippage_pct": round(slippage, 2) if slippage is not None else None,
            "impacto_pct": impacto}


def cotizar_venta(mint: str, tokens_raw: int, sol_usd: float) -> dict | None:
    """¿Cuantos dolares darian AHORA por vender `tokens_raw` crudos?"""
    if not tokens_raw or tokens_raw <= 0 or not sol_usd or sol_usd <= 0:
        return None
    q = _quote(mint, SOL_MINT, int(tokens_raw))
    if not q:
        return None
    sol_out = int(q["outAmount"]) / LAMPORTS
    return {"usd_salida": sol_out * sol_usd, "sol_salida": sol_out}
