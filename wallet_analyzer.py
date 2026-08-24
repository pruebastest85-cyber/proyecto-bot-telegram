"""
Módulo 2: Análisis de compradores tempranos.

v3 — Presupuesto de Helius:
  - Analiza máximo MAX_TOKENS_PER_CYCLE tokens por ciclo (el resto
    queda en cola para el siguiente), y HISTORY_MAX_PAGES páginas de
    historial por token. Así la cuota gratuita alcanza todo el mes.

Detección de compras robusta: leer tokenTransfers y nativeTransfers
directamente (cubre pump.fun, Raydium, Jupiter, Meteora, etc.):
  - la billetera (feePayer) RECIBIÓ el mint  → tokenTransfers
  - la billetera ENVIÓ SOL en esa misma tx   → nativeTransfers
"""

import threading
import time
import requests

import config
from wallet_profiler import JITO_TIP_ACCOUNTS, _api_rec
from db import (get_conn, pending_tokens, mark_analyzed,
                upsert_wallet_appearance, recompute_scores, top_wallets)

LAMPORTS = 1_000_000_000  # 1 SOL


_AVISO_FRENO = [0.0]


# (Ola 17-I, auditoria 6) Motivo del ultimo fallo de descarga, o None.
# Existe porque `[]` significaba TRES cosas a la vez: "el token no tiene
# transacciones", "Helius no contesto" y "el freno de presupuesto esta
# activo". Con las tres indistinguibles, `analyze_token` marcaba el token
# como ANALIZADO y no se volvia a mirar jamas — perdiendo hasta 600
# apariciones de billeteras, que segun CLAUDE.md son lo unico
# verdaderamente irrecuperable del sistema.
# (Ola 17-K) POR HILO, no global. En la 17-I la puse como una lista
# global y el bot corre con varios hilos que llaman a estas funciones:
# el ciclo (`run_analysis`), la extraccion cuando el dueño pega un mint
# (`token_extract`) y `dev_check` (que importa las dos funciones). La
# ventana entre descargar y leer la bandera dura MINUTOS —el historial
# pagina hasta 40 veces con timeout de 60 s—, asi que un segundo hilo
# podia limpiarla y hacer que un token cuya descarga FALLO se marcara
# como analizado: exactamente la perdida irreversible que la 17-I venia
# a impedir. Con `threading.local` cada hilo tiene la suya.
_local = threading.local()


def _set_fallo(motivo):
    _local.fallo = motivo


def motivo_fallo_descarga():
    """Motivo del ultimo fallo EN ESTE HILO, o None si no lo hubo."""
    return getattr(_local, "fallo", None)


def fetch_parsed_txs(address: str, before: str | None = None,
                     limit: int = 100) -> list[dict]:
    """Descarga transacciones parseadas de una dirección desde Helius.

    Antes de gastar, comprueba el presupuesto de créditos del mes: cada
    llamada cuesta 100 créditos y quedarse sin cuota a mitad de mes dejaría
    al bot ciego. El freno permite tener topes generosos sin riesgo.
    """
    try:
        from helius_budget import puede_llamar
        if not puede_llamar():
            if time.time() - _AVISO_FRENO[0] > 3600:
                _AVISO_FRENO[0] = time.time()
                print("  ⛔ Presupuesto de Helius casi agotado: se pausan "
                      "las descargas de historial hasta el próximo ciclo")
            _set_fallo("presupuesto de Helius agotado")
            return []
    except Exception:
        pass
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
        # (Ola 17-B) La Enhanced Transactions API cuesta 100 CREDITOS por
        # llamada (config.HELIUS_CREDITS_PER_CALL). Antes solo se apuntaba
        # en el contador de LLAMADAS, asi que el freno del 85% no veia el
        # gasto mas caro del sistema: la base del dueno tenia 535.930
        # creditos contados frente a ~809.100 invisibles.
        _api_rec("helius")
        _api_rec("helius_credits", config.HELIUS_CREDITS_PER_CALL)
        data = r.json()
        # (Ola 17-K) Una descarga que SI funciona limpia la bandera. Sin
        # esto, un fallo del RPC seguido de una respuesta legitima vacia
        # de la ruta antigua dejaba el token pendiente PARA SIEMPRE,
        # reintentandose en cada ciclo y quemando creditos.
        _set_fallo(None)
        return data if isinstance(data, list) else []
    except requests.RequestException as e:
        print(f"  · Error Helius: {e}")
        _set_fallo(f"Helius no respondio ({str(e)[:80]})")
        return []


def fetch_earliest_txs(mint: str, max_pages: int | None = None,
                       con_estado: bool = False):
    """Ruta preferente: getTransactionsForAddress con sortOrder=asc, que
    devuelve las PRIMERAS transacciones del token de verdad (y cuesta 10x
    menos). Si falla, cae al método antiguo de paginar hacia atrás."""
    _set_fallo(None)                   # se reinicia en cada intento
    if getattr(config, "USE_RPC_HISTORY", True):
        try:
            from helius_rpc import primeras_txs
            tope = getattr(config, "EARLY_BUYER_WINDOW", 1500)
            txs, ok = primeras_txs(mint, max_txs=tope)
            if txs:
                return (txs, ok) if con_estado else txs
        except Exception as e:
            print(f"  · RPC no disponible ({e}); uso el método antiguo")
            _set_fallo(f"RPC no disponible ({str(e)[:80]})")
    return _fetch_earliest_txs_legacy(mint, max_pages, con_estado)


def _fetch_earliest_txs_legacy(mint: str, max_pages: int | None = None,
                               con_estado: bool = False):
    """
    Pagina hacia atrás hasta las transacciones más antiguas del mint y
    devuelve las primeras EARLY_BUYER_WINDOW en orden cronológico.

    IMPORTANTE: si el token tiene más transacciones de las que caben en
    max_pages, NUNCA se llega a su inicio real. Antes eso pasaba en
    silencio y las txs más antiguas del lote se trataban como si fueran
    las primeras del token (puestos de compra inventados). Ahora se
    devuelve también si el historial está COMPLETO.

    con_estado=True → (txs, completo). False → solo txs (compatibilidad).
    """
    if max_pages is None:
        max_pages = getattr(config, "HISTORY_MAX_PAGES", 5)
    pages, before = [], None
    completo = False
    for _ in range(max_pages):
        batch = fetch_parsed_txs(mint, before=before)
        if not batch:
            completo = True        # no hay más: se agotó la historia
            break
        pages.append(batch)
        if len(batch) < 100:       # página incompleta = inicio del token
            completo = True
            break
        before = batch[-1].get("signature")
    if not pages:
        return ([], False) if con_estado else []
    all_txs = [tx for page in pages for tx in page]
    all_txs.reverse()  # más antigua primero
    txs = all_txs[: config.EARLY_BUYER_WINDOW]
    return (txs, completo) if con_estado else txs


def extract_buys(txs: list[dict], mint: str) -> list[dict]:
    """
    Detecta compras leyendo transferencias directamente:
      compra = feePayer recibió el mint Y envió SOL en la misma tx.
    """
    buys = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        buyer = tx.get("feePayer")
        if not buyer:
            continue

        got_token = False
        tokens_in = 0.0
        for t in (tx.get("tokenTransfers") or []):
            if t.get("mint") == mint and t.get("toUserAccount") == buyer:
                got_token = True
                try:
                    tokens_in += float(t.get("tokenAmount") or 0)
                except (TypeError, ValueError):
                    pass
        if not got_token:
            continue

        sol_out = 0.0
        for n in (tx.get("nativeTransfers") or []):
            if n.get("fromUserAccount") == buyer:
                if n.get("toUserAccount") in JITO_TIP_ACCOUNTS:
                    continue   # propina MEV: no es precio del token
                sol_out += int(n.get("amount", 0)) / LAMPORTS

        if sol_out <= 0:
            for acc in (tx.get("accountData") or []):
                if acc.get("account") == buyer:
                    delta = int(acc.get("nativeBalanceChange", 0))
                    if delta < 0:
                        sol_out = abs(delta) / LAMPORTS
                    break

        if sol_out > 0:
            # Precio de ENTRADA en SOL por token. Con el precio actual da el
            # múltiplo desde su entrada: mide "compró antes de que explotara"
            # sin depender del puesto de compra.
            precio_entrada = (sol_out / tokens_in) if tokens_in > 0 else None
            buys.append({
                "wallet": buyer,
                "sol": sol_out,
                "tokens": tokens_in,
                "precio_entrada": precio_entrada,
                "time": tx.get("timestamp"),
                "signature": tx.get("signature", ""),
            })
    return buys


def _precio_actual(mint):
    """Precio actual en SOL por token (para medir el crecimiento desde la
    entrada). Devuelve (precio_sol, mc_usd) o (None, None)."""
    try:
        from token_check import analyze_token as _tc
        t = _tc(mint)
        precio_usd, mc = t.get("price"), t.get("mc")
        if not precio_usd:
            return (None, None)
        from realtime import _sol_price
        sol_usd = _sol_price() or 0
        if not sol_usd:
            return (None, mc)
        return (float(precio_usd) / float(sol_usd), mc)
    except Exception as e:
        print(f"  · No se pudo obtener el precio actual: {e}")
        return (None, None)


def analyze_token(conn, token) -> int:
    """Analiza un token ganador y registra a los compradores que entraron
    ANTES de que explotara (medido por el crecimiento desde su entrada)."""
    mint, symbol = token["mint"], token["symbol"] or token["mint"][:8]
    print(f"\n▸ Analizando {symbol} ({mint[:12]}…)")

    precio_ahora, mc_ahora = _precio_actual(mint)
    min_mult = float(getattr(config, "MIN_ENTRY_MULTIPLE", 3.0))

    txs, historial_completo = fetch_earliest_txs(mint, con_estado=True)
    if not historial_completo:
        print("  · Historial incompleto: no se alcanzó el inicio del token; "
              "los puestos de compra NO son fiables y no se registrarán")
    if not txs:
        # (Ola 17-I) Solo se da por analizado si de verdad NO HAY nada que
        # analizar. Si la descarga fallo, el token se deja PENDIENTE para
        # el proximo ciclo: marcarlo lo sacaba de `pending_tokens` para
        # siempre (db.pending_tokens filtra `analyzed = 0`) y con el se
        # perdian sus compradores tempranos.
        _motivo = motivo_fallo_descarga()
        if _motivo:
            print(f"  ⚠️ NO se marca como analizado: {_motivo}. "
                  f"Se reintentará en el próximo ciclo.")
            try:
                from errores import record as _rec
                _rec("wallet_analyzer.descarga",
                     RuntimeError(f"{mint[:12]}…: {_motivo}"))
            except Exception:
                pass
            return 0
        print("  · Sin transacciones recuperadas (el token no tiene "
              "historial): se marca como analizado")
        mark_analyzed(conn, mint)
        return 0

    # Solo es la 1ª tx REAL del token si se alcanzó su inicio; si no, el
    # delay no significa nada y se deja vacío.
    t0 = (txs[0].get("timestamp") or 0) if historial_completo else 0
    buys = extract_buys(txs, mint)
    print(f"  · {len(txs)} txs tempranas → {len(buys)} compras detectadas")

    end_rank = int(getattr(config, "BUYER_END_RANK", 600))
    min_obs = float(getattr(config, "MIN_OBS_BUY_SOL", 0.3))
    registered = 0
    descartados_tarde = 0
    for rank, buy in enumerate(buys):
        if rank + 1 > end_rank:
            break              # fuera de la ventana de observación
        # OBSERVAR es barato: alimenta clusters/grafo/afinidad con devs.
        # El corte fino (candidata a perfil) se aplica al elegir a quién
        # perfilar (BUYER_START_RANK, MIN_BUY_DELAY_SEC, MIN_BUY_SOL).
        if not (min_obs <= buy["sol"] <= config.MAX_BUY_SOL):
            continue

        # ── Crecimiento desde su entrada ──
        # Es el filtro anti-basura: si el token apenas subió desde que esta
        # billetera compró, NO anticipó nada y no interesa registrarla.
        pe = buy.get("precio_entrada")
        mult_entrada = mc_entrada = None
        if pe and pe > 0 and precio_ahora:
            mult_entrada = precio_ahora / pe
            if mc_ahora and mult_entrada > 0:
                mc_entrada = float(mc_ahora) / mult_entrada
            if mult_entrada < min_mult:
                descartados_tarde += 1
                continue          # llegó tarde: no aporta señal
        ts = buy["time"]
        delay = int(ts - t0) if (ts and t0 and ts >= t0) else None
        # Sin historial completo el puesto sería inventado: mejor sin dato
        # que con un dato falso.
        rank_real = (rank + 1) if historial_completo else None
        buy_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""
        sig = (buy["signature"] or "")[:16]
        if mult_entrada:
            _mc = (f" (MC ~${mc_entrada:,.0f})" if mc_entrada else "")
            reason = (f"Compró {buy['sol']:.2f} SOL de {symbol}{_mc} y el "
                      f"token subió x{mult_entrada:.1f} desde su entrada "
                      f"(tx {sig}…)")
        else:
            # (Ola 8, 21/8) Sin historial completo el "#puesto" seria el
            # puesto dentro del lote incompleto — el dato inventado que
            # rank_real ya evita. El texto de /evidencia tampoco lo dice.
            _puesto = (f"como comprador #{rank + 1} "
                       if historial_completo else
                       "entre los primeros compradores observados ")
            reason = (f"Compró {buy['sol']:.2f} SOL de {symbol} {_puesto}"
                      f"antes de subida de "
                      f"+{token['price_change_24h']:.0f}% en 24h (tx {sig}…)")
        upsert_wallet_appearance(conn, buy["wallet"], mint, buy["sol"],
                                 buy_time, rank_real, reason, delay,
                                 price_at_buy=pe, mc_at_buy=mc_entrada,
                                 entry_multiple=mult_entrada)
        registered += 1

    mark_analyzed(conn, mint)
    print(f"  ✓ {registered} billeteras registradas"
          + (f" · {descartados_tarde} descartadas por entrar tarde "
             f"(<x{min_mult:g} desde su compra)" if descartados_tarde else ""))
    return registered


def run_analysis():
    """
    Analiza tokens ganadores pendientes y DESPUÉS evalúa candidatas.

    OJO: antes, si no había tokens nuevos, esta función hacía `return` y se
    saltaba TODO lo demás — recálculo de scores, evaluación por IA y
    perfilado. Resultado: en cuanto discovery dejaba de encontrar tokens
    nuevos (lo normal al cabo de unos días), el embudo se paralizaba por
    completo y no volvía a promocionar ni una billetera.
    Ahora la evaluación de candidatas corre SIEMPRE, haya tokens nuevos o no.
    """
    conn = get_conn()
    tokens = pending_tokens(conn)
    if not tokens:
        print("Sin tokens ganadores pendientes; se evalúan las candidatas "
              "que ya están en la base.")
    else:
        limite = getattr(config, "MAX_TOKENS_PER_CYCLE", 6)
        en_cola = max(0, len(tokens) - limite)
        tokens = tokens[:limite]
        print(f"→ Analizando {len(tokens)} tokens ganadores"
              + (f" ({en_cola} en cola para el próximo ciclo)"
                 if en_cola else ""))
        for token in tokens:
            analyze_token(conn, token)

    recompute_scores(conn, config.MIN_WINNING_TOKENS,
                     getattr(config, "MAX_TRACKED_CANDIDATES", 60))

    # Filtro IA: clasifica candidatas nuevas y descarta bots/insiders
    try:
        from ai_analyst import evaluate_tracked
        n = evaluate_tracked(conn)
        if n:
            print(f"\n🧠 IA evaluó {n} billeteras candidatas")
    except Exception as e:
        print(f"· Filtro IA omitido: {e}")

    # Depuración de las ⭐ que YA existen. `evaluate_tracked` sólo mira
    # candidatas sin evaluar o caducadas, así que una ⭐ evaluada hace poco
    # nunca volvía a pasar por los filtros nuevos. Esto los aplica sobre lo
    # ya guardado en la base, sin gastar créditos.
    try:
        from ai_analyst import depurar_estrellas
        depurar_estrellas(conn)
    except Exception as e:
        print(f"· Depuración de ⭐ omitida: {e}")

    print("\n══ TOP BILLETERAS CANDIDATAS ══")
    for w in top_wallets(conn, 15):
        flag = "★ RASTREAR" if w["is_tracked"] else ""
        print(f"  {w['address'][:20]}…  ganadores={w['winning_tokens_count']}  "
              f"score={w['score']:.1f}  {flag}")
    conn.close()


if __name__ == "__main__":
    run_analysis()
