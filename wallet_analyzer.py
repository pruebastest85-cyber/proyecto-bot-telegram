"""
Módulo 2: Análisis de compradores tempranos.

v2 — Detección de compras corregida:
Helius no siempre llena events.swap (pump.fun, Raydium y otros DEX
aparecen con formatos distintos). Método robusto: leer tokenTransfers
y nativeTransfers directamente:
  - la billetera (feePayer) RECIBIÓ el mint  → tokenTransfers
  - la billetera ENVIÓ SOL en esa misma tx   → nativeTransfers
Eso es una compra, sin importar en qué DEX ocurrió.
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
        data = r.json()
        return data if isinstance(data, list) else []
    except requests.RequestException as e:
        print(f"  · Error Helius: {e}")
        return []


def fetch_earliest_txs(mint: str, max_pages: int = 8) -> list[dict]:
    """
    Pagina hacia atrás hasta las transacciones más antiguas del mint
    y devuelve las primeras EARLY_BUYER_WINDOW en orden cronológico.
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
    all_txs = [tx for page in pages for tx in page]
    all_txs.reverse()  # más antigua primero
    return all_txs[: config.EARLY_BUYER_WINDOW]


def extract_buys(txs: list[dict], mint: str) -> list[dict]:
    """
    Detecta compras leyendo transferencias directamente:
      compra = feePayer recibió el mint Y envió SOL en la misma tx.
    Cubre pump.fun, Raydium, Jupiter, Meteora, etc.
    """
    buys = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        buyer = tx.get("feePayer")
        if not buyer:
            continue

        # ¿Recibió el token?
        got_token = False
        for t in (tx.get("tokenTransfers") or []):
            if t.get("mint") == mint and t.get("toUserAccount") == buyer:
                got_token = True
                break
        if not got_token:
            continue

        # ¿Cuánto SOL salió de su billetera en esta tx?
        sol_out = 0.0
        for n in (tx.get("nativeTransfers") or []):
            if n.get("fromUserAccount") == buyer:
                sol_out += int(n.get("amount", 0)) / LAMPORTS

        # Respaldo: usar accountData/nativeBalanceChange si no hubo
        # nativeTransfers claros (algunos DEX mueven SOL vía cuentas WSOL)
        if sol_out <= 0:
            for acc in (tx.get("accountData") or []):
                if acc.get("account") == buyer:
                    delta = int(acc.get("nativeBalanceChange", 0))
                    if delta < 0:
                        sol_out = abs(delta) / LAMPORTS
                    break

        if sol_out > 0:
            buys.append({
                "wallet": buyer,
                "sol": sol_out,
                "time": tx.get("timestamp"),
                "signature": tx.get("signature", ""),
            })
    return buys


def analyze_token(conn, token) -> int:
    """Analiza un token ganador y registra sus compradores tempranos."""
    mint, symbol = token["mint"], token["symbol"] or token["mint"][:8]
    print(f"\n▸ Analizando {symbol} ({mint[:12]}…)")

    txs = fetch_earliest_txs(mint)
    if not txs:
        print("  · Sin transacciones recuperadas")
        mark_analyzed(conn, mint)
        return 0

    buys = extract_buys(txs, mint)
    print(f"  · {len(txs)} txs tempranas → {len(buys)} compras detectadas")

    registered = 0
    for rank, buy in enumerate(buys):
        if not (config.MIN_BUY_SOL <= buy["sol"] <= config.MAX_BUY_SOL):
            continue
        ts = buy["time"]
        buy_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""
        sig = (buy["signature"] or "")[:16]
        reason = (f"Compró {buy['sol']:.2f} SOL de {symbol} como comprador "
                  f"#{rank + 1} antes de subida de "
                  f"+{token['price_change_24h']:.0f}% en 24h (tx {sig}…)")
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

    # Filtro IA: clasifica candidatas nuevas y descarta bots/insiders
    try:
        from ai_analyst import evaluate_tracked
        n = evaluate_tracked(conn)
        if n:
            print(f"\n🧠 IA evaluó {n} billeteras candidatas")
    except Exception as e:
        print(f"· Filtro IA omitido: {e}")

    print("\n══ TOP BILLETERAS CANDIDATAS ══")
    for w in top_wallets(conn, 15):
        flag = "★ RASTREAR" if w["is_tracked"] else ""
        print(f"  {w['address'][:20]}…  ganadores={w['winning_tokens_count']}  "
              f"score={w['score']:.1f}  {flag}")
    conn.close()


if __name__ == "__main__":
    run_analysis()
