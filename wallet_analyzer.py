"""
Módulo 2: Análisis de compradores tempranos.

Para cada token ganador pendiente:
  1. Descarga las transacciones más antiguas del mint vía Helius
     (Enhanced Transactions API, parseadas — mucho más fácil que RPC crudo).
  2. Identifica eventos de SWAP donde una billetera COMPRÓ el token con SOL.
  3. Filtra montos de prueba y billeteras enormes.
  4. Registra cada billetera con su evidencia ("el porqué").

Nota sobre el plan gratuito de Helius: paginar hasta las primeras
transacciones de un token muy activo puede consumir muchos créditos.
Por eso limitamos con EARLY_BUYER_WINDOW y HELIUS_DELAY.
"""

import time
import requests

import config
from db import (get_conn, pending_tokens, mark_analyzed,
                upsert_wallet_appearance, recompute_scores, top_wallets,
                wallet_evidence)

LAMPORTS = 1_000_000_000  # 1 SOL


def fetch_parsed_txs(address: str, before: str | None = None,
                     limit: int = 100) -> list[dict]:
    """Descarga transacciones parseadas de una dirección desde Helius."""
    url = config.HELIUS_PARSED_TX.format(address=address)
    params = {"api-key": config.HELIUS_API_KEY, "limit": limit}
    if before:
        params["before"] = before
    try:
        r = requests.get(url, params=params, timeout=30)
        time.sleep(config.HELIUS_DELAY)
        if r.status_code == 429:
            print("  · Rate limit Helius, esperando 15s…")
            time.sleep(15)
            r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"  · Error Helius: {e}")
        return []


def fetch_earliest_txs(mint: str, max_pages: int = 8) -> list[dict]:
    """
    Pagina hacia atrás hasta llegar a las transacciones más antiguas del mint.
    Devuelve la última página (las más viejas) más las inmediatamente
    posteriores, ordenadas de más antigua a más reciente.
    """
    pages, before = [], None
    for _ in range(max_pages):
        batch = fetch_parsed_txs(mint, before=before)
        if not batch:
            break
        pages.append(batch)
        if len(batch) < 100:   # llegamos al inicio de la historia del token
            break
        before = batch[-1].get("signature")
    if not pages:
        return []
    # Aplanar y quedarnos con las N más antiguas
    all_txs = [tx for page in pages for tx in page]
    all_txs.reverse()  # ahora: más antigua primero
    return all_txs[: config.EARLY_BUYER_WINDOW]


def extract_buys(txs: list[dict], mint: str) -> list[dict]:
    """
    De una lista de transacciones parseadas, extrae compras del token:
    la billetera envió SOL y recibió el mint (evento SWAP de Helius).
    """
    buys = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        events = tx.get("events", {}) or {}
        swap = events.get("swap")
        if not swap:
            continue

        buyer = tx.get("feePayer")
        sol_in = sum(int(t.get("amount", 0))
                     for t in (swap.get("nativeInput") or [])
                     if isinstance(t, dict)) / LAMPORTS
        # Compatibilidad: nativeInput a veces es dict, no lista
        ni = swap.get("nativeInput")
        if isinstance(ni, dict):
            sol_in = int(ni.get("amount", 0)) / LAMPORTS

        got_token = any(
            (out.get("mint") == mint)
            for out in (swap.get("tokenOutputs") or [])
        )
        if buyer and got_token and sol_in > 0:
            buys.append({
                "wallet": buyer,
                "sol": sol_in,
                "time": tx.get("timestamp"),
                "signature": tx.get("signature"),
            })
    return buys


def analyze_token(conn, token) -> int:
    """Analiza un token ganador y registra sus compradores tempranos."""
    mint, symbol = token["mint"], token["symbol"] or token["mint"][:8]
    print(f"\n▸ Analizando {symbol} ({mint[:12]}…)")

    txs = fetch_earliest_txs(mint)
    if not txs:
        print("  · Sin transacciones recuperadas (¿clave API válida?)")
        return 0

    buys = extract_buys(txs, mint)
    print(f"  · {len(txs)} txs tempranas → {len(buys)} compras detectadas")

    registered = 0
    for rank, buy in enumerate(buys):
        if not (config.MIN_BUY_SOL <= buy["sol"] <= config.MAX_BUY_SOL):
            continue
        ts = buy["time"]
        buy_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""
        reason = (f"Compró {buy['sol']:.2f} SOL de {symbol} como comprador "
                  f"#{rank + 1} antes de subida de "
                  f"+{token['price_change_24h']:.0f}% en 24h "
                  f"(tx {buy['signature'][:16]}…)")
        upsert_wallet_appearance(conn, buy["wallet"], mint, buy["sol"],
                                 buy_time, rank + 1, reason)
        registered += 1

    mark_analyzed(conn, mint)
    print(f"  ✓ {registered} billeteras registradas con evidencia")
    return registered


def run_analysis():
    conn = get_conn()
    tokens = pending_tokens(conn)
    if not tokens:
        print("No hay tokens pendientes. Corre primero discovery.py")
        return
    print(f"→ {len(tokens)} tokens ganadores pendientes de análisis")

    for token in tokens:
        analyze_token(conn, token)

    recompute_scores(conn, config.MIN_WINNING_TOKENS)

    print("\n══ TOP BILLETERAS CANDIDATAS ══")
    for w in top_wallets(conn, 15):
        flag = "★ RASTREAR" if w["is_tracked"] else ""
        print(f"  {w['address'][:20]}…  ganadores={w['winning_tokens_count']}  "
              f"score={w['score']:.1f}  {flag}")

    # Mostrar la evidencia de la mejor billetera como ejemplo del "porqué"
    best = top_wallets(conn, 1)
    if best:
        print(f"\n══ EVIDENCIA DE {best[0]['address'][:20]}… ══")
        for ev in wallet_evidence(conn, best[0]["address"]):
            print(f"  · {ev['reason']}")
    conn.close()


if __name__ == "__main__":
    run_analysis()
