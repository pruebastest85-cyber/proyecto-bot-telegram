"""
Cliente del método `getTransactionsForAddress` (RPC exclusivo de Helius).

POR QUÉ EXISTE
--------------
La Enhanced Transactions API cuesta 100 créditos por cada 100 transacciones.
Este método cuesta 10 créditos por cada 100 y devuelve hasta 1.000 por
llamada: es 10 veces más barato y 10 veces menos viajes.

Pero lo decisivo no es el precio, es `sortOrder="asc"`: permite pedir las
transacciones MÁS ANTIGUAS de un token directamente. Antes había que paginar
hacia atrás desde la más reciente con la esperanza de llegar al principio, y
en tokens concurridos nunca se llegaba — así que los "compradores tempranos"
que encontrábamos no eran los primeros. Ahora se pide el inicio real.

CÓMO ENCAJA
-----------
La respuesta trae la estructura CRUDA de Solana, distinta del formato ya
masticado de Helius. Para no reescribir medio sistema, aquí se traduce al
mismo formato que el resto del código ya entiende (feePayer, tokenTransfers,
nativeTransfers, accountData, timestamp, signature). Así `extract_buys` y
`wallet_profiler` siguen funcionando sin tocarlos.
"""

import time

import requests

import config

try:
    from api_usage import record as _api_rec
except Exception:
    def _api_rec(*a, **k):
        pass

LAMPORTS = 1_000_000_000


# ───────────────────────── traducción de formato ─────────────────────────

def _pubkey(k):
    """accountKeys puede venir como string o como {pubkey, signer, ...}."""
    if isinstance(k, dict):
        return k.get("pubkey")
    return k


def _fee_payer(tx_msg) -> str | None:
    keys = (tx_msg or {}).get("accountKeys") or []
    for k in keys:
        if isinstance(k, dict) and k.get("signer"):
            return k.get("pubkey")
    return _pubkey(keys[0]) if keys else None


def _token_transfers(meta) -> list[dict]:
    """Deltas de token por (dueño, mint) a partir de los saldos pre/post.

    Se usan los saldos y no las instrucciones porque es robusto ante
    cualquier DEX o ruta (pump.fun, Raydium, Jupiter, Meteora…).
    """
    pre, post = {}, {}
    for lado, dest in ((meta.get("preTokenBalances") or [], pre),
                       (meta.get("postTokenBalances") or [], post)):
        for b in lado:
            dueno, mint = b.get("owner"), b.get("mint")
            if not dueno or not mint:
                continue
            try:
                cant = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            except (TypeError, ValueError):
                cant = 0.0
            dest[(dueno, mint)] = dest.get((dueno, mint), 0.0) + cant

    out = []
    for clave in set(pre) | set(post):
        dueno, mint = clave
        delta = post.get(clave, 0.0) - pre.get(clave, 0.0)
        if abs(delta) < 1e-12:
            continue
        if delta > 0:
            out.append({"mint": mint, "toUserAccount": dueno,
                        "fromUserAccount": None, "tokenAmount": delta})
        else:
            out.append({"mint": mint, "fromUserAccount": dueno,
                        "toUserAccount": None, "tokenAmount": -delta})
    return out


def _native_transfers(tx_msg, meta) -> list[dict]:
    """Transferencias de SOL leídas de las instrucciones del System Program.
    Se mantienen aparte de las comisiones para poder excluir propinas Jito."""
    out = []
    instrucciones = list((tx_msg or {}).get("instructions") or [])
    for inner in (meta.get("innerInstructions") or []):
        instrucciones.extend(inner.get("instructions") or [])
    for ix in instrucciones:
        if not isinstance(ix, dict):
            continue
        if ix.get("program") != "system":
            continue
        p = ix.get("parsed") or {}
        if p.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = p.get("info") or {}
        try:
            lamports = int(info.get("lamports") or 0)
        except (TypeError, ValueError):
            continue
        if lamports <= 0:
            continue
        out.append({"fromUserAccount": info.get("source"),
                    "toUserAccount": info.get("destination"),
                    "amount": lamports})
    return out


def _account_data(tx_msg, meta) -> list[dict]:
    """Cambio neto de SOL por cuenta (incluye comisión), como respaldo."""
    keys = (tx_msg or {}).get("accountKeys") or []
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    out = []
    for i, k in enumerate(keys):
        if i >= len(pre) or i >= len(post):
            break
        try:
            delta = int(post[i]) - int(pre[i])
        except (TypeError, ValueError):
            continue
        out.append({"account": _pubkey(k), "nativeBalanceChange": delta})
    return out


def traducir(entrada: dict) -> dict | None:
    """Convierte una transacción del RPC al formato que usa el resto del bot."""
    try:
        tx = entrada.get("transaction") or {}
        meta = entrada.get("meta") or {}
        msg = tx.get("message") or {}
        firmas = tx.get("signatures") or []
        return {
            "signature": firmas[0] if firmas else "",
            "timestamp": entrada.get("blockTime"),
            "slot": entrada.get("slot"),
            "feePayer": _fee_payer(msg),
            "transactionError": meta.get("err"),
            "tokenTransfers": _token_transfers(meta),
            "nativeTransfers": _native_transfers(msg, meta),
            "accountData": _account_data(msg, meta),
        }
    except Exception:
        return None


# ─────────────────────────── llamada al RPC ──────────────────────────────

def _rpc(address: str, *, orden: str = "desc", limite: int = 1000,
         token_pag: str | None = None) -> tuple[list[dict], str | None]:
    """Una página del historial. Devuelve (transacciones_traducidas, token)."""
    cfg = {
        "transactionDetails": "full",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
        "sortOrder": orden,
        "limit": max(1, min(1000, int(limite))),
        "filters": {"status": "succeeded", "tokenAccounts": "balanceChanged"},
    }
    if token_pag:
        cfg["paginationToken"] = token_pag
    cuerpo = {"jsonrpc": "2.0", "id": 1,
              "method": "getTransactionsForAddress",
              "params": [address, cfg]}
    try:
        r = requests.post(config.HELIUS_RPC, json=cuerpo, timeout=60)
        time.sleep(getattr(config, "HELIUS_DELAY", 0.1))
        if r.status_code == 429:
            time.sleep(15)
            r = requests.post(config.HELIUS_RPC, json=cuerpo, timeout=60)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"  · Error RPC Helius: {e}")
        return ([], None)
    if data.get("error"):
        print(f"  · RPC rechazado: {data['error']}")
        return ([], None)

    res = data.get("result") or {}
    filas = res.get("data") or []
    # Coste real: 10 créditos por cada 100 devueltas (mínimo 10)
    creditos = max(10, 10 * ((len(filas) + 99) // 100))
    _api_rec("helius_credits", creditos)

    salida = [t for t in (traducir(x) for x in filas) if t]
    return (salida, res.get("paginationToken"))


def primeras_txs(mint: str, max_txs: int = 1500) -> tuple[list[dict], bool]:
    """
    Las PRIMERAS transacciones del token, en orden cronológico real.

    Devuelve (transacciones, completo). `completo` es True porque al pedir
    desde el inicio sí estamos viendo el arranque del token — a diferencia
    de paginar hacia atrás, donde casi nunca se llegaba.
    """
    todas, token = [], None
    while len(todas) < max_txs:
        lote, token = _rpc(mint, orden="asc",
                           limite=min(1000, max_txs - len(todas)),
                           token_pag=token)
        if not lote:
            break
        todas.extend(lote)
        if not token:
            break
    return (todas[:max_txs], bool(todas))


def historial_wallet(address: str, max_txs: int = 4000) -> list[dict]:
    """Historial reciente de una billetera (para perfilar), del más nuevo al
    más viejo — el mismo orden que esperaba el perfilador."""
    todas, token = [], None
    while len(todas) < max_txs:
        lote, token = _rpc(address, orden="desc",
                           limite=min(1000, max_txs - len(todas)),
                           token_pag=token)
        if not lote:
            break
        todas.extend(lote)
        if not token:
            break
    return todas[:max_txs]
