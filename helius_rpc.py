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

import threading
import time

import requests

import config
from avisos import aviso as _avisar_ex   # (19-AE)

try:
    from api_usage import record as _api_rec
except Exception as _ex:
    _avisar_ex("helius_rpc:modulo:34", _ex)
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
    # (Ola 18-F) `sorted`, no `set`. Iterar un conjunto de cadenas da un
    # orden que cambia ENTRE PROCESOS (Python aleatoriza el hash de las
    # cadenas en cada arranque). Ese orden llegaba tal cual a
    # `tokenTransfers`, y `wallet_profiler` cogia el PRIMER mint de la
    # lista para atribuirle el SOL de la transaccion: la misma billetera,
    # con las mismas transacciones, daba un PnL por token distinto en cada
    # ejecucion. De ahi que una ⭐ apareciera y desapareciera sola.
    for clave in sorted(set(pre) | set(post)):
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
    """Convierte una transacción del RPC al formato que usa el resto del bot.

    (19-B) FALTABA `fee`, y eso sesgaba el PnL de TODO el sistema.

    `wallet_profiler._sol_delta` parte del cambio de SOL nativo de la
    cuenta (`accountData`), que INCLUYE la comisión de red, y luego se la
    devuelve para quedarse solo con el precio del token:
    `return raw + fee + tip`. Lee la comisión de `tx.get("fee", 0)`.

    Este diccionario no traia esa clave, asi que desde que el RPC es la
    ruta preferente (`USE_RPC_HISTORY=1`, el defecto) `fee` valia SIEMPRE
    0: cada compra se contabilizaba mas cara de lo que fue y cada venta
    mas barata. Es exactamente el sesgo sistematico a negativo que el
    docstring de `_sol_delta` dice haber corregido — y que seguia vivo
    porque la correccion se escribio para el camino antiguo (la Enhanced
    API si devuelve `fee`) y nadie la trajo a este.

    Aguas abajo envenenaba `pnl_total`, `net_pnl_sol`, `pnl_30d`, el
    winrate y el neto de la puerta 1 (que exige > 0 ESTRICTAMENTE) y el
    veredicto de `grading`. Es aditivo: nadie mas lee esta clave, asi que
    añadirla no cambia ningun otro consumidor.

    `meta.fee` viene en lamports, que es la unidad que `_sol_delta`
    espera (divide entre LAMPORTS).
    """
    try:
        tx = entrada.get("transaction") or {}
        meta = entrada.get("meta") or {}
        msg = tx.get("message") or {}
        firmas = tx.get("signatures") or []
        return {
            "signature": firmas[0] if firmas else "",
            "timestamp": entrada.get("blockTime"),
            "slot": entrada.get("slot"),
            "fee": meta.get("fee") or 0,
            "feePayer": _fee_payer(msg),
            "transactionError": meta.get("err"),
            "tokenTransfers": _token_transfers(meta),
            "nativeTransfers": _native_transfers(msg, meta),
            "accountData": _account_data(msg, meta),
        }
    except Exception as _ex:
        _avisar_ex("helius_rpc:traducir:186", _ex)
        return None


# ─────────────────────────── llamada al RPC ──────────────────────────────

# ── (Ola 18-D) ¿La paginación se acabó, o se ROMPIÓ? ──────────────────
# `_rpc` devuelve ([], None) tanto cuando de verdad no hay mas paginas
# como cuando la red falla o Helius rechaza la peticion. Quien pagina no
# podia distinguirlo, y `primeras_txs` leia ese None como "historial
# completo": con la primera pagina buena y la segunda caida se registraban
# puestos de compra calculados sobre una ventana truncada, y el token se
# marcaba analizado para siempre. Eso ensucia `appearances`, que segun
# CLAUDE.md es lo unico irrecuperable del sistema.
#
# POR HILO (`threading.local`), por el mismo motivo que en
# wallet_analyzer (Ola 17-K): el ciclo, la extraccion manual y dev_check
# corren a la vez y una bandera global la limpiaria el hilo equivocado.
_local = threading.local()


def _set_fallo(motivo):
    _local.fallo = motivo


def reset_fallo():
    """Empieza a contar de cero. La llaman las funciones que paginan
    (`primeras_txs`, `historial_wallet`). Una llamada buena a `_rpc`
    tambien la limpia, asi que quien use `_rpc` suelto no ensucia a
    nadie."""
    _local.fallo = None


def ultimo_fallo() -> str | None:
    """Motivo del ultimo fallo de `_rpc` EN ESTE HILO, o None."""
    return getattr(_local, "fallo", None)


def _rpc(address: str, *, orden: str = "desc", limite: int = 1000,
         token_pag: str | None = None) -> tuple[list[dict], str | None]:
    """Una página del historial. Devuelve (transacciones_traducidas, token).

    Si falla, ademas de devolver ([], None) deja el motivo en la bandera
    del hilo: `ultimo_fallo()`."""
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
        _set_fallo(f"Helius no respondió ({str(e)[:80]})")
        return ([], None)
    if data.get("error"):
        print(f"  · RPC rechazado: {data['error']}")
        _set_fallo(f"Helius rechazó la petición ({str(data['error'])[:80]})")
        return ([], None)

    # (Ola 18-D) Llamada buena: se limpia la marca. Asi ningun lector
    # hereda el fallo de una llamada anterior de este mismo hilo — los
    # hilos se reutilizan (`asyncio.to_thread`) y `dev_watch` llama a
    # `_rpc` directamente, sin reiniciar nada.
    _set_fallo(None)
    res = data.get("result") or {}
    filas = res.get("data") or []
    # Coste real: 10 créditos por cada 100 devueltas (mínimo 10)
    creditos = max(10, 10 * ((len(filas) + 99) // 100))
    _api_rec("helius_credits", creditos)

    salida = [t for t in (traducir(x) for x in filas) if t]
    return (salida, res.get("paginationToken"))


def simbolo_token(mint: str) -> str | None:
    """(22/8) Ticker del token via Helius DAS (getAsset). Existe desde el
    SEGUNDO CERO del mint — a diferencia de DexScreener, que indexa el par
    con retraso y deja a los tokens recien nacidos sin nombre ("EjAuFt").
    Cuesta ~10 creditos y se apunta en el contador exacto."""
    try:
        r = requests.post(config.HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAsset",
            "params": {"id": mint}}, timeout=5)
        r.raise_for_status()
        _api_rec("helius_credits", 10)
        _api_rec("helius")
        contenido = ((r.json() or {}).get("result") or {}).get(
            "content") or {}
        s = ((contenido.get("metadata") or {}).get("symbol") or "").strip()
        if s and not mint.startswith(s):
            return s
    except Exception as e:
        print(f"· simbolo_token({mint[:8]}…) fallo: {e}")
    return None


def primeras_txs(mint: str, max_txs: int = 1500) -> tuple[list[dict], bool]:
    """
    Las PRIMERAS transacciones del token, en orden cronológico real.

    Devuelve (transacciones, completo). `completo` es True SOLO si se
    agotó la paginación de verdad: es False si se corta por cupo
    (`max_txs`) o —desde la Ola 18-D— si una página falla. Pedir desde el
    inicio hace posible ver el arranque del token, a diferencia de paginar
    hacia atrás, pero no lo garantiza.
    """
    # Paginacion honesta (Ola 6, auditoria 19/8 - M23): cortar por
    # "pagina vacia" mentia — una pagina cuyas filas fallaron todas en
    # traducir() daba lote vacio con token de paginacion VIVO, el bucle
    # abortaba a mitad de historia y aun asi devolvia completo=True →
    # ranks inventados desde una ventana truncada, el bug exacto que
    # este modulo existe para evitar. Se corta cuando se AGOTA la
    # paginacion; "completo" significa eso y nada mas.
    # (Ola 18-D) Y ahora tambien se corta cuando la peticion FALLA, sin
    # dar por completo lo que solo esta roto: sin esto, la primera pagina
    # buena + la segunda caida devolvia token=None y por tanto
    # completo=True, con puestos de compra calculados sobre media historia.
    reset_fallo()
    todas, token, completo = [], None, False
    for _ in range(40):                     # tope duro anti-bucle
        lote, token = _rpc(mint, orden="asc",
                           limite=min(1000, max(1, max_txs - len(todas))),
                           token_pag=token)
        todas.extend(lote or [])
        if ultimo_fallo():
            break                           # roto, NO completo
        if not token:
            completo = True                 # no hay mas paginas
            break
        if len(todas) >= max_txs:
            break                           # truncado por cupo: NO completo
    return (todas[:max_txs], completo and bool(todas))


def historial_wallet(address: str, max_txs: int = 4000,
                     con_estado: bool = False):
    """Historial reciente de una billetera (para perfilar), del más nuevo al
    más viejo — el mismo orden que esperaba el perfilador.

    (Ola 18-D) `con_estado=True` devuelve `(txs, entero)`. `entero` es
    False si la paginación se cortó por algo que NO se pidió: un fallo de
    red, un rechazo de Helius, o una página sin nada traducible con la
    paginación todavía viva (el fallo M23 que la Ola 6 cerró en
    `primeras_txs` y aquí seguía abierto). Llegar al tope de `max_txs` NO
    lo pone en False: eso es un corte pedido por el llamador.

    Importa porque el perfilador calcula PnL, win rate y "posible bot"
    sobre lo que le llegue: media historia da un perfil equivocado y lo
    guarda en la base como si estuviera medido."""
    # (Ola 18-D) Se reinicia la bandera para que el llamador pueda
    # preguntar despues si esta historia se corto por un fallo — antes
    # podia quedar la marca de una llamada anterior de este mismo hilo.
    reset_fallo()
    todas, token = [], None
    while len(todas) < max_txs:
        lote, token = _rpc(address, orden="desc",
                           limite=min(1000, max_txs - len(todas)),
                           token_pag=token)
        if not lote:
            if token:
                # Pagina sin nada traducible pero con paginacion VIVA: se
                # esta cortando a mitad de historia, no al final.
                _set_fallo("página vacía con la paginación todavía viva")
            break
        todas.extend(lote)
        if not token:
            break
    entero = not ultimo_fallo()
    txs = todas[:max_txs]
    return (txs, entero) if con_estado else txs
