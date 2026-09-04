"""
Perfilador de billeteras: investiga a fondo una billetera candidata.

Responde las preguntas clave:
  - ¿Sigue activa? (última transacción, ritmo reciente)
  - ¿En qué otros tokens operó y cuánto ganó/perdió en cada uno?
  - ¿Cuál es su PnL realizado aproximado en SOL? (total y últimos 30 días)
  - ¿Parece bot? (frecuencia de transacciones)

Método: descarga sus últimas ~2000 transacciones parseadas (Helius),
detecta compras (SOL sale + token entra) y ventas (token sale + SOL
entra) y agrega por token. El SOL de cada swap se mide SIN comisiones
de red ni propinas de Jito, para no sesgar el costo. El PnL realizado
es SOL que entró por ventas menos SOL que salió por compras; el PnL
neto le suma el valor de los tokens que aún mantiene (ver unrealized_pnl).
El PnL 30d se calcula sobre las txs de la muestra de los últimos 30 días.
"""

import time
from collections import defaultdict

import requests

import config

try:
    from api_usage import record as _api_rec
except Exception:          # nunca romper el flujo por el contador
    def _api_rec(*a, **k):
        pass

LAMPORTS = 1_000_000_000
WSOL = "So11111111111111111111111111111111111111112"
# Mints que NO son "apuestas" de memecoin: se saltan como posición. WSOL
# se trata aparte como efectivo (1 WSOL = 1 SOL).
STABLE_MINTS = {
    WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# Cuentas de propina de Jito (MEV bundles). La propina NO es precio del
# token: se resta para no inflar el costo de las operaciones.
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}


def _fetch_txs(address: str, pages: int | None = None) -> tuple[list, bool]:
    """(Ola 18-D) Devuelve `(txs, entero)`. `entero` es False cuando la
    descarga se corto por un fallo, para que el perfil no se calcule —ni
    se guarde— sobre media historia."""
    if pages is None:
        pages = getattr(config, "PROFILE_MAX_PAGES", 10)
    # Ruta preferente: RPC (10x más barato y hasta 1.000 txs por llamada),
    # así el perfil ve MUCHO más historial por el mismo presupuesto.
    if getattr(config, "USE_RPC_HISTORY", True):
        try:
            from helius_rpc import historial_wallet
            txs, entero = historial_wallet(address, max_txs=pages * 100,
                                           con_estado=True)
            # (Ola 18-D) Media historia da un PnL, un win rate y un
            # "posible bot" equivocados, y el perfil se guarda en la base
            # como si estuviera medido — `ai_analyst` puede marcar
            # `is_bot=1` y sacar la billetera del embudo con ese dato.
            # Asi que si la paginacion se corto por un fallo se devuelve
            # NADA, no medio historial: `profile_wallet` da
            # `tx_sampled = 0` y `ai_analyst` la deja pendiente para el
            # proximo ciclo. Tampoco se cae al camino antiguo, que cuesta
            # PROFILE_MAX_PAGES (50) x 100 = 5.000 creditos por billetera
            # y ademas trunca en silencio exactamente igual (su `except
            # RequestException: break` devuelve lo que llevara sin decir
            # nada). Ojo: esto vale TAMBIEN cuando no llego ni una
            # transaccion — si la pagina 1 se cayo, caer al camino antiguo
            # seria pagar 5.000 creditos justo cuando Helius no responde.
            if txs and entero:
                return (txs, True)
            if not entero:
                print("  · El historial se cortó a mitad; no perfilo con "
                      "datos incompletos, se reintenta en el próximo ciclo")
                return ([], False)
            # Sin fallo y sin transacciones: puede ser una billetera nueva
            # o que el RPC no cubra este caso. Se prueba el camino antiguo,
            # como se hacia siempre.
        except Exception as e:
            print(f"  · RPC no disponible en perfil ({e}); método antiguo")
    url = config.HELIUS_PARSED_TX.format(address=address)
    all_txs, before = [], None
    for _ in range(pages):
        # (Ola 18-D) Freno de presupuesto, que aqui no estaba: este bucle
        # gasta HELIUS_CREDITS_PER_CALL (100) por vuelta y hasta
        # PROFILE_MAX_PAGES (50) vueltas por billetera. Desde que un corte
        # a mitad ya no da el perfil por bueno, una billetera que falle
        # siempre en la misma pagina se reintenta cada ciclo — sin este
        # freno podria comerse la cuota del mes ella sola.
        try:
            from helius_budget import puede_llamar
            if not puede_llamar():
                print("  ⛔ Presupuesto de Helius casi agotado: no perfilo "
                      "por el método antiguo")
                return ([], False)
        except Exception:
            pass
        params = {"api-key": config.HELIUS_API_KEY, "limit": 100}
        if before:
            params["before"] = before
        try:
            r = requests.get(url, params=params, timeout=30)
            time.sleep(config.HELIUS_DELAY)
            if r.status_code == 429:
                time.sleep(15)
                r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            # (Ola 17-B) 100 creditos por llamada, igual que en
            # wallet_analyzer: el perfilado es el mayor consumidor y era
            # invisible para el freno de presupuesto.
            _api_rec("helius")
            _api_rec("helius_credits", config.HELIUS_CREDITS_PER_CALL)
            batch = r.json()
        except requests.RequestException as e:
            # (Ola 18-D) Antes se devolvia lo que llevara descargado sin
            # decir nada, y el perfil salia calculado sobre media
            # historia. Ahora se avisa igual que en la ruta RPC.
            print(f"  · Error Helius en perfil: {e}")
            return ([], False)
        if not isinstance(batch, list):
            # (Ola 18-D) Helius puede devolver 200 con un cuerpo que no es
            # una lista (p. ej. {"error": ...}), que `raise_for_status` no
            # coge. Antes se cortaba aqui y el historial salia truncado
            # dandose por entero: el mismo fallo M23 que la Ola 6 cerro en
            # `primeras_txs`. Una lista VACIA si es final legitimo.
            print("  · Helius devolvió algo que no es una lista de "
                  "transacciones; no perfilo con datos a medias")
            return ([], False)
        if not batch:
            break
        all_txs.extend(batch)
        if len(batch) < 100:
            break
        before = batch[-1].get("signature")
    return (all_txs, True)


def _sol_delta(tx: dict, wallet: str) -> float:
    """
    SOL del SWAP para la billetera (negativo = compró, positivo = vendió).

    Parte del cambio de SOL nativo y RESTA lo que no es precio del token:
    la comisión de red (base + prioridad, campo `fee`) y las propinas de
    Jito. Antes se usaba el cambio bruto, lo que inflaba el costo de las
    compras y encogía las ventas → PnL sesgado sistemáticamente a negativo.

    Nota: no descuenta el rent de las cuentas de token (~0.002 SOL, casi
    siempre recuperable al cerrar la posición); su efecto es marginal.
    """
    # 1) Cambio de SOL nativo bruto. accountData ya incluye la comisión;
    #    la suma de nativeTransfers NO la incluye (se marca con from_balance).
    raw = None
    for acc in (tx.get("accountData") or []):
        if acc.get("account") == wallet:
            raw = int(acc.get("nativeBalanceChange", 0)) / LAMPORTS
            break
    from_balance = raw is not None
    if raw is None:
        raw = 0.0
        for n in (tx.get("nativeTransfers") or []):
            if n.get("fromUserAccount") == wallet:
                raw -= int(n.get("amount", 0)) / LAMPORTS
            if n.get("toUserAccount") == wallet:
                raw += int(n.get("amount", 0)) / LAMPORTS

    # 2) Comisión de red: solo si la paga esta billetera y solo si está
    #    reflejada en raw (rama accountData).
    fee = 0.0
    if from_balance and tx.get("feePayer") == wallet:
        try:
            fee = int(tx.get("fee", 0)) / LAMPORTS
        except (TypeError, ValueError):
            fee = 0.0

    # 3) Propinas de Jito enviadas por esta billetera (siempre en raw).
    tip = 0.0
    for n in (tx.get("nativeTransfers") or []):
        if (n.get("fromUserAccount") == wallet
                and n.get("toUserAccount") in JITO_TIP_ACCOUNTS):
            tip += int(n.get("amount", 0)) / LAMPORTS

    # fee y tip siempre restan SOL a la billetera; los devolvemos para
    # quedarnos solo con el SOL que realmente pagó/recibió por el token.
    return raw + fee + tip


def huella_mm(tokens: dict) -> tuple:
    """(19-X) (cuantos tokens "estilo market maker", % sobre los que opera
    ida y vuelta).

    Estilo MM = >= 3 compras, >= 3 ventas y posicion neta ~0 (|pnl| <= 5 %
    del volumen). El NUMERO absoluto no distingue a un MM de un humano que
    entra y sale tres veces en tres tokens y empata en ellos: el primero
    tiene casi todos sus tokens asi; el segundo, tres de treinta. La
    PROPORCION si los distingue, y es lo que usa `_hard_bot_reason`.
    """
    mm = sum(1 for i in tokens.values()
             if i["buys"] >= 3 and i["sells"] >= 3
             and abs(i["pnl_sol"]) <= 0.05 * (i["sol_in"] + i["sol_out"]))
    ida_vuelta = sum(1 for i in tokens.values()
                     if i["buys"] >= 1 and i["sells"] >= 1)
    return mm, (round(100 * mm / ida_vuelta) if ida_vuelta else 0)


def profile_wallet(address: str, with_holdings: bool = True) -> dict:
    txs, historial_entero = _fetch_txs(address)
    now = time.time()
    result = {
        "address": address,
        "tx_sampled": len(txs),
        "last_tx_ts": None,
        "tx_7d": 0,
        "tokens": {},        # mint -> métricas
        "pnl_total_sol": 0.0,
        "pnl_30d_sol": 0.0,
        "unrealized_sol": 0.0,     # valor en SOL de tokens aún en cartera
        "net_pnl_sol": 0.0,        # realizado + no realizado (mark-to-market)
        "held_tokens": 0,
        "priced_tokens": 0,
        "metrics": {},        # métricas quant (ver wallet_metrics)
        "possible_bot": False,
        # (Ola 18-F) Cuantas transacciones movieron mas de un token no
        # estable. En esas, el SOL se le apunta a UNO solo: es una
        # aproximacion, y conviene saber cuanto pesa en este perfil.
        "tx_multi_token": 0,
        # (Ola 18-D) False = la descarga se corto por un fallo, asi que
        # "sin transacciones" NO significa "billetera sin actividad".
        "historial_entero": historial_entero,
    }
    if not txs:
        return result

    result["last_tx_ts"] = txs[0].get("timestamp")
    tokens = defaultdict(lambda: {"sol_out": 0.0, "sol_in": 0.0,
                                  "buys": 0, "sells": 0, "symbol": "",
                                  "first_buy_ts": None, "first_sell_ts": None,
                                  "holding_sol": 0.0,
                                  "tok_in": 0.0, "tok_out": 0.0})
    timestamps = []
    buy_sizes = []
    # Operaciones extraidas: se GUARDAN al final para tener
    # historial propio y no depender siempre de Helius.
    _ops: list[dict] = []

    for tx in txs:
        ts = tx.get("timestamp") or 0
        timestamps.append(ts)   # incluye fallidas: cuenta para frecuencia/bot
        if tx.get("transactionError"):
            continue
        if now - ts <= 7 * 86400:
            result["tx_7d"] += 1

        # Lado "efectivo" del swap en SOL (nativo, ya sin fee/propina).
        delta = _sol_delta(tx, address)
        # C: si el SOL nativo casi no se movió pero la billetera movió WSOL
        # (SOL pre-envuelto), usar ese WSOL como efectivo. Solo cuando el
        # nativo es ~0, para NO duplicar el caso normal financiado con SOL.
        if abs(delta) <= 0.001:
            wsol = 0.0
            for t in (tx.get("tokenTransfers") or []):
                if t.get("mint") != WSOL:
                    continue
                try:
                    amt = float(t.get("tokenAmount") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                if t.get("toUserAccount") == address:
                    wsol += amt
                elif t.get("fromUserAccount") == address:
                    wsol -= amt
            if abs(wsol) > 0.001:
                delta = wsol
        reciente = (now - ts) <= 30 * 86400
        # Agregar transfers por mint ANTES de clasificar: las rutas partidas
        # (Jupiter) generan varios transfers del mismo mint y antes solo se
        # contaba el primero (tokens subcontados → % vendido y calidad de
        # salida distorsionados).
        mint_in: dict[str, float] = {}
        mint_out: dict[str, float] = {}
        for t in (tx.get("tokenTransfers") or []):
            mint = t.get("mint")
            if not mint or mint in STABLE_MINTS:
                continue
            try:
                amt = float(t.get("tokenAmount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if t.get("toUserAccount") == address:
                mint_in[mint] = mint_in.get(mint, 0.0) + amt
            elif t.get("fromUserAccount") == address:
                mint_out[mint] = mint_out.get(mint, 0.0) + amt
        # (Ola 18-F) Si la transaccion mueve VARIOS tokens no estables, a
        # cual se le apunta el SOL es una aproximacion — pero tiene que ser
        # SIEMPRE LA MISMA. `next(iter(...))` cogia el primero por orden de
        # insercion, que venia del orden de `tokenTransfers`, que a su vez
        # salia de iterar un conjunto: distinto en cada arranque del
        # proceso. Resultado: el PnL por token de la misma billetera
        # cambiaba entre ejecuciones y su nota con el, asi que la ⭐
        # aparecia y desaparecia sola. Ordenar por direccion del mint no es
        # mas "correcto", pero es ESTABLE, que es lo que aqui hace falta.
        # Compra: recibio token y su SOL bajo (una tx cuenta una vez)
        if delta < -0.001 and mint_in:
            if len(mint_in) > 1:
                result["tx_multi_token"] += 1
            mint = min(mint_in)
            _ops.append({"signature": tx.get("signature"), "mint": mint,
                         "side": "compra", "sol": abs(delta),
                         "tokens": mint_in[mint], "ts": ts})
            info = tokens[mint]
            info["buys"] += 1
            info["sol_out"] += abs(delta)
            info["tok_in"] += mint_in[mint]
            buy_sizes.append(round(abs(delta), 2))
            if ts and (info["first_buy_ts"] is None
                       or ts < info["first_buy_ts"]):
                info["first_buy_ts"] = ts
            if reciente:
                result["pnl_30d_sol"] += delta
        # Venta: envio token y su SOL subio
        elif delta > 0.001 and mint_out:
            if len(mint_out) > 1:
                result["tx_multi_token"] += 1
            mint = min(mint_out)          # (Ola 18-F) estable, ver arriba
            _ops.append({"signature": tx.get("signature"), "mint": mint,
                         "side": "venta", "sol": delta,
                         "tokens": mint_out[mint], "ts": ts})
            info = tokens[mint]
            info["sells"] += 1
            info["sol_in"] += delta
            info["tok_out"] += mint_out[mint]
            if ts and (info["first_sell_ts"] is None
                       or ts < info["first_sell_ts"]):
                info["first_sell_ts"] = ts
            if reciente:
                result["pnl_30d_sol"] += delta

    # ¿Bot? Más de BOT_TX_PER_HOUR_LIMIT txs/hora sostenidas en la muestra
    if len(timestamps) >= 20:
        span_h = max((max(timestamps) - min(timestamps)) / 3600, 0.01)
        if len(timestamps) / span_h > config.BOT_TX_PER_HOUR_LIMIT:
            result["possible_bot"] = True

    holds = []
    for mint, info in tokens.items():
        info["pnl_sol"] = info["sol_in"] - info["sol_out"]
        result["pnl_total_sol"] += info["pnl_sol"]
        if info["first_buy_ts"] and info["first_sell_ts"] \
                and info["first_sell_ts"] > info["first_buy_ts"]:
            holds.append((info["first_sell_ts"] - info["first_buy_ts"]) / 60)
    # Retención mediana: ¿vende en minutos o aguanta horas?
    if holds:
        holds.sort()
        result["hold_median_min"] = round(holds[len(holds) // 2], 1)
    else:
        result["hold_median_min"] = None
    # Win rate sobre posiciones cerradas
    #
    # (19-B) Exige TAMBIEN haber visto la compra. Con solo `sells > 0`, un
    # mint cuya compra quedo FUERA de la muestra (o cuyo SOL se atribuyo a
    # otro mint en una tx multi-token) entraba como cerrada con
    # `sol_out = 0`, o sea `pnl_sol = sol_in - 0 > 0`: **ganadora
    # garantizada**. Eso inflaba `win_rate_pct` y, por la misma lista,
    # `profit_factor`, `expectancy` y `max_drawdown` de `wallet_metrics`
    # — las cifras con las que `grading` y la IA deciden promover.
    #
    # El resultado era un desacople medible: la IA promovia con numeros
    # optimistas y la puerta 1 del filtro, que SI exige compra
    # (`filtro_calidad`: `sol_in > 0 and sol_out > 0 and tok_in > 0`),
    # las tumbaba despues. Medido en la base del dueño el 30/8: el 1,7%
    # de las posiciones de 90 dias son ventas sin compra en la muestra.
    #
    # Lo que NO se toca: el airdrop vendido sigue sumando al
    # `pnl_total_sol` (es dinero que entro de verdad); lo que deja de
    # hacer es contar como una posicion ACERTADA.
    closed = [i for i in tokens.values()
              if i["sells"] > 0 and i["buys"] > 0]
    if closed:
        result["win_rate_pct"] = round(
            100 * sum(1 for i in closed if i["pnl_sol"] > 0) / len(closed))
    else:
        result["win_rate_pct"] = None
    result["closed_positions"] = len(closed)

    # ── Huellas de bot / MEV / market maker ─────────────────────────
    # Flips: % de posiciones cerradas en menos de 1 minuto (MEV/sniper bot)
    result["flips_1min_pct"] = (
        round(100 * sum(1 for h in holds if h <= 1) / len(holds))
        if holds else None)
    # Actividad 24/7: horas del día (0-23) con transacciones; un humano duerme
    #
    # (19-B) Se mide DENTRO DE UN DIA, no sobre la muestra entera.
    #
    # Antes era `len({gmtime(t).tm_hour for t in timestamps})` sobre TODA
    # la muestra — hasta 5.000 transacciones, que pueden abarcar semanas o
    # meses. Con eso, cubrir 22 de las 24 horas no prueba que la billetera
    # no duerma: prueba que ha operado muchos dias distintos. La
    # esperanza de huecos con 200 transacciones repartidas es
    # 24·(23/24)^200 ≈ 0,006, o sea que CUALQUIER humano activo cruzaba
    # el umbral de `ai_analyst._hard_bot_reason` (>= 22 horas y >= 200
    # txs) y se marcaba `is_bot=1`, que es una condena permanente: sale
    # de la cola de evaluacion, del /top, de la vigilancia en tiempo real
    # y se le borra el historial.
    #
    # MEDIDO en la base del dueño el 30/8/2026: 3.190 de las 6.827
    # billeteras marcadas como bot lo estaban por este motivo — casi la
    # mitad, y el motivo mas frecuente con diferencia. Era el tapon de
    # ENTRADA del embudo.
    #
    # Ahora: el maximo de horas distintas cubiertas EN UN MISMO DIA (UTC),
    # que es lo que el nombre y el comentario prometian. El valor nuevo es
    # siempre <= al viejo, asi que este cambio solo puede DESMARCAR: una
    # billetera que hoy no esta señalada no puede pasar a estarlo.
    #
    # OJO: esto arregla los perfilados NUEVOS. Las 3.190 ya marcadas
    # siguen con `is_bot=1` porque nadie las vuelve a mirar; deshacerlo es
    # una decision del dueño (ver la nota de entrega de la ola).
    _horas_por_dia: dict = {}
    for _t in timestamps:
        if not _t:
            continue
        _g = time.gmtime(_t)
        _horas_por_dia.setdefault((_g.tm_year, _g.tm_yday), set()).add(
            _g.tm_hour)
    result["active_hours_24"] = max(
        (len(_hs) for _hs in _horas_por_dia.values()), default=0)
    # Compras de tamaño idéntico: % de la compra más repetida
    if len(buy_sizes) >= 5:
        mas_comun = max(buy_sizes.count(s) for s in set(buy_sizes))
        result["uniform_buys_pct"] = round(100 * mas_comun / len(buy_sizes))
    else:
        result["uniform_buys_pct"] = None
    # Market maker: tokens operados en ambas direcciones con posición neta ~0
    result["mm_tokens"], result["mm_pct"] = huella_mm(tokens)

    result["net_pnl_sol"] = round(result["pnl_total_sol"], 2)
    result["tokens"] = dict(tokens)

    # ── Guardar el historial en NUESTRA base ──
    # Antes estas operaciones se calculaban y se tiraban: cada re-evaluación
    # volvía a descargar lo mismo de Helius. Guardarlas permite recalcular
    # sin llamar a nadie, alimentar análisis propios (incluida una IA local)
    # y dejar de depender de una API externa para el histórico.
    try:
        from trades_store import guardar
        _nuevas = guardar(address, _ops)
        if _nuevas:
            print(f"  💾 {_nuevas} operaciones nuevas guardadas "
                  f"(historial propio)")
    except Exception as e:
        print(f"  · No se guardó el historial: {e}")

    # Métricas quant (Profit Factor, Sharpe, Expectancy, Drawdown, ROI…)
    try:
        from wallet_metrics import trade_metrics
        result["metrics"] = trade_metrics(result["tokens"])
    except Exception as e:
        print(f"  · Métricas quant no disponibles: {e}")

    # ── PnL no realizado: valora los tokens que la billetera aún tiene ──
    # El realizado castiga a los que acumulan (compra contabilizada como
    # gasto, venta aún no ocurrida). Sumar el valor de mercado de la bolsa
    # abierta da el PnL NETO real (mark-to-market).
    if with_holdings:
        try:
            from unrealized_pnl import holdings_value
            hv = holdings_value(address)
            result["unrealized_sol"] = hv["unrealized_sol"]
            result["net_pnl_sol"] = round(
                result["pnl_total_sol"] + hv["unrealized_sol"], 2)
            result["held_tokens"] = hv["held_tokens"]
            result["priced_tokens"] = hv["priced_tokens"]
            for mint, val in hv["holdings"].items():
                if mint in result["tokens"]:
                    result["tokens"][mint]["holding_sol"] = val
        except Exception as e:
            print(f"  · PnL no realizado no disponible: {e}")

    return result


def format_profile(p: dict) -> str:
    """Formatea el perfil para Telegram."""
    addr = p["address"]
    lines = [f"🔬 *Perfil de* `{addr[:16]}…`\n"]

    if not p["tx_sampled"]:
        if p.get("historial_entero") is False:
            return (f"⚠️ No pude descargar el historial de `{addr[:16]}…` "
                    "(Helius se cortó a mitad). NO es que la billetera esté "
                    "vacía: no tengo el dato. Vuelve a intentarlo en un rato.")
        return (f"🔬 Sin transacciones recuperadas para `{addr[:16]}…`. "
                "Puede ser una billetera nueva o hubo un error de API.")

    # Actividad
    if p["last_tx_ts"]:
        days = (time.time() - p["last_tx_ts"]) / 86400
        if days < 1:
            act = "🟢 ACTIVA (operó hoy)"
        elif days < 7:
            act = f"🟢 ACTIVA (última tx hace {days:.0f} días)"
        elif days < 30:
            act = f"🟡 Poco activa (última tx hace {days:.0f} días)"
        else:
            act = f"🔴 INACTIVA (última tx hace {days:.0f} días)"
        lines.append(f"{act} · {p['tx_7d']} txs en 7 días\n")

    if p["possible_bot"]:
        lines.append("⚠️ *Posible bot*: frecuencia de txs muy alta\n")
    huellas = []
    if p.get("flips_1min_pct"):
        huellas.append(f"flips <1min: {p['flips_1min_pct']}%")
    if p.get("active_hours_24"):
        huellas.append(f"horas activas: {p['active_hours_24']}/24")
    if p.get("uniform_buys_pct"):
        huellas.append(f"compras idénticas: {p['uniform_buys_pct']}%")
    if p.get("mm_tokens"):
        huellas.append(f"tokens estilo MM: {p['mm_tokens']}")
    if huellas:
        lines.append("🤖 Huellas bot/MM: " + " · ".join(huellas) + "\n")

    # Tokens por PnL
    traded = [(m, i) for m, i in p["tokens"].items()
              if i["buys"] + i["sells"] > 0]
    traded.sort(key=lambda x: x[1]["pnl_sol"], reverse=True)

    wins = sum(1 for _, i in traded if i["pnl_sol"] > 0)
    closed = sum(1 for _, i in traded if i["sells"] > 0)
    lines.append(f"📈 *Tokens operados (muestra {p['tx_sampled']} txs):* "
                 f"{len(traded)}")
    if closed:
        lines.append(f"✅ Con ventas: {closed} · ganadores: {wins}")
    # (Ola 18-F) Honestidad sobre la aproximacion: en las txs que mueven
    # varios tokens a la vez, el SOL se le apunta a uno solo.
    _multi = p.get("tx_multi_token") or 0
    if _multi:
        lines.append(f"_⚪ {_multi} txs movieron varios tokens a la vez; "
                     f"el SOL de esas se le apunta a uno solo._")
    # (Ola 8, 21/8) Ambas cifras son FLUJO de caja de la muestra/ventana
    # (una venta de algo comprado antes cuenta entera; una compra sin
    # venta resta entera): etiquetarlas "PnL" a secas sobreclamaba.
    lines.append(f"💰 *Flujo neto (muestra):* {p['pnl_total_sol']:+.2f} SOL")
    lines.append(f"📅 *Flujo neto últimos 30 días:* "
                 f"{p['pnl_30d_sol']:+.2f} SOL")
    if p.get("held_tokens"):
        cob = f"{p.get('priced_tokens', 0)}/{p['held_tokens']} con precio"
        lines.append(f"🎒 *En cartera (sin vender):* "
                     f"{p.get('unrealized_sol', 0.0):+.2f} SOL ({cob})")
        lines.append(f"🧾 *PnL neto (realizado + cartera):* "
                     f"{p.get('net_pnl_sol', p['pnl_total_sol']):+.2f} SOL")
    if p.get("win_rate_pct") is not None:
        lines.append(f"🎯 *Win rate (cerradas):* {p['win_rate_pct']}%")
    try:
        from wallet_metrics import format_metrics
        ml = format_metrics(p.get("metrics") or {})
        if ml:
            lines.append("")
            lines.extend(ml)
    except Exception:
        pass
    if p.get("hold_median_min") is not None:
        h = p["hold_median_min"]
        ret = f"{h:.0f} min" if h < 120 else f"{h / 60:.1f} h"
        lines.append(f"⏱ *Retención mediana:* {ret}")
    lines.append("")

    lines.append("*Top operaciones:*")
    for mint, i in traded[:6]:
        lines.append(
            f"• `{mint[:10]}…` {i['pnl_sol']:+.2f} SOL "
            f"({i['buys']}c/{i['sells']}v)")
    if len(traded) > 6:
        lines.append(f"…y {len(traded) - 6} más")

    lines.append("\n_PnL realizado sobre las últimas ~2000 txs. "
                 "«En cartera» valora las posiciones abiertas a precio actual "
                 "(DexScreener); «neto» = realizado + cartera._")
    lines.append(f"🔗 gmgn.ai/sol/address/{addr}")
    return "\n".join(lines)
