"""
Track record de señales: mide qué pasó con el precio de cada token
1h y 24h después de cada señal, y arma la estadística por billetera.

Esto le da a la IA el dato definitivo para decidir si una billetera
vale la pena: no cómo se ve su historial, sino cuántas de sus señales
REALMENTE ganaron dentro de nuestro sistema.

Alertas de subida (v6): saltan por MÚLTIPLOS del precio de la señal
(x2, x3, x4, x5…) y solo UNA vez por token, aunque varias billeteras ⭐
lo hayan comprado. El mayor múltiplo ya avisado por token se guarda en
la tabla settings (clave "mult_alert:<mint>").
"""

import threading
import time

import requests

import config
from db import get_conn, get_setting, set_setting

try:
    from api_usage import record as _api_rec
except Exception:          # nunca romper el flujo por el contador
    def _api_rec(*a, **k):
        pass


HOUR = 3600
DAY = 86400

WATCH_HOURS = 48   # cuánto tiempo vigilamos el precio tras la señal
MIN_MULTIPLE = 2   # empezar a avisar desde el doble (x2) en adelante


# Suelos de liquidez (22/8): un par con liquidez ~0 imprime CUALQUIER
# precio con una operacion de polvo, y DexScreener extrapola MCs absurdos
# (caso real: tarjeta "x15699, MC $10.2M" de un token con MC real de $2 y
# liquidez $0). Bajo LIQ_MUERTO el token se da por muerto (perdida total);
# entre medias hay precio pero NO es fiable para tarjetas.
import os as _os_liq


def _f_env(n, d):
    try:
        return float(_os_liq.getenv(n, d))
    except (TypeError, ValueError):
        return d


LIQ_MUERTO_USD = _f_env("LIQ_MUERTO_USD", 100.0)
LIQ_FIABLE_USD = _f_env("LIQ_FIABLE_USD", 1000.0)
# Tarjetas de hito: base con MC de polvo o multiplo imposible = no fiable
MC_BASE_MIN_USD = _f_env("MC_BASE_MIN_USD", 1000.0)
MULT_TARJETA_MAX = _f_env("MULT_TARJETA_MAX", 1000.0)


# ── (Ola 18-E) ¿DexScreener dijo "no hay par", o no dijo nada? ────────
# `_price_mc_ex` se traga los fallos de red y devuelve
# (None, None, False, 0.0) — lo MISMO que cuando responde bien pero sin
# ningun par con precio usable. Quien llama no podia distinguirlos, y el
# paper cerraba posiciones a -99% ("sin liquidez") por un timeout de red,
# grabando una perdida total que nunca comprobo. El historico medido es
# lo unico irreversible del sistema (CLAUDE.md), asi que ese dato falso
# no se puede deshacer.
#
# POR HILO, como en helius_rpc (Ola 18-D) y wallet_analyzer (Ola 17-K):
# el job del paper, la medicion y los comandos corren a la vez.
_local_px = threading.local()


def _set_fallo_precio(motivo):
    _local_px.fallo = motivo


def ultimo_fallo_precio() -> str | None:
    """Motivo por el que el ULTIMO sondeo de precio de ESTE hilo no pudo
    hacerse, o None si DexScreener contesto (aunque fuera para decir que
    el token ya no cotiza)."""
    return getattr(_local_px, "fallo", None)


def _price_mc_ex(mint: str):
    """(precio_usd, market_cap, muerto, liq_usd) según DexScreener, del par de mayor
    liquidez. Se devuelven JUNTOS porque el MC no se puede deducir del
    precio: si el suministro cambia (típico al migrar un token de pump.fun),
    extrapolar el MC da cifras falsas.

    (Ola 8, 21/8) `muerto=True` SOLO cuando DexScreener responde 200 sin
    ningún par listado: el token dejó de cotizar (rug/par retirado). Un
    fallo de red o un 429 NO es muerte — devuelve (None, None, False) y
    se reintenta. Antes ambos casos eran indistinguibles y las señales de
    tokens muertos quedaban sin medir PARA SIEMPRE: las peores pérdidas
    desaparecían del win rate y todos los % de acierto salían inflados.

    (Ola 18-E) Y como `(None, None, False, 0.0)` sale TAMBIÉN cuando la
    petición no llega a hacerse, el motivo queda en `ultimo_fallo_precio()`
    para quien necesite distinguirlo. Ver el comentario de arriba."""
    _set_fallo_precio(None)
    try:
        r = requests.get(config.DEXSCREENER_TOKEN.format(address=mint),
                         timeout=15)
        _api_rec("dexscreener")
        r.raise_for_status()
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return (None, None, True, 0.0)
        mejor, mejor_liq, mejor_liq_sabida = None, -1.0, False
        # (Ola 18-H, 3ª vuelta) La mayor liquidez LEIDA de cualquier par,
        # solo para poder DECIRLO en el log. No se devuelve: ver abajo.
        mejor_conocida = None
        for p in pairs:
            # (Ola 18-E) La liquidez tambien va DENTRO del try. Estaba
            # fuera, y si DexScreener mandaba `liquidity` como lista en
            # UN par (o `usd` como texto raro), reventaba el sondeo
            # ENTERO del token — que ademas se leia como "fallo de red",
            # cuando en realidad es la respuesta de ese mint y se repite
            # igual en cada pasada. Un par ilegible se salta y ya.
            try:
                px = float(p.get("priceUsd") or 0)
                if px <= 0:
                    continue
                # (Ola 18-E) "no viene el dato" NO es "vale 0". Antes las
                # dos cosas daban `liq=0.0`, o sea por debajo de
                # LIQ_MUERTO_USD, o sea MUERTO — y el paper cierra eso a
                # -99% sobre el historico, que no se puede deshacer.
                _liq_bruta = (p.get("liquidity") or {}).get("usd")
                liq = None if _liq_bruta is None else float(_liq_bruta)
                if liq is not None and liq < 0:
                    liq = None      # dato absurdo: se trata como ausente
            except (TypeError, ValueError, AttributeError):
                continue
            # `mejor is None` va primero: si no, un par SIN dato de
            # liquidez nunca superaria el -1.0 inicial y el token entero
            # saldria "sin precio usable".
            #
            # (Ola 18-H) El desconocido se ordena EN el umbral de muerte,
            # no por debajo de todo. Con `-1.0`, un par sin dato perdia
            # contra CUALQUIER liquidez legible, incluido un pool muerto
            # con `liquidity.usd: 0` — que en Solana es lo normal (el pool
            # de pump.fun que queda abandonado tras migrar). Entonces
            # `mejor` era el pool muerto, `mejor_liq_sabida` pasaba a True,
            # la guarda de "no se puede declarar muerto lo que no se ha
            # leido" no llegaba a saltar, y el token se daba por MUERTO:
            # -99% en la simulacion y -100% en `signals.chg_1h/chg_24h`,
            # que es historico y no se deshace. O sea: el arreglo de la
            # Ola 18-E solo funcionaba si el token tenia UN par.
            #
            # Con `LIQ_MUERTO_USD` el orden dice lo que se quiere decir:
            # una liquidez conocida y SANA gana al desconocido (es mas
            # dato); una liquidez conocida y de POLVO pierde contra el
            # desconocido (no vale para declarar muerto todo el token).
            _cmp = float(LIQ_MUERTO_USD) if liq is None else liq
            if liq is not None and (mejor_conocida is None
                                    or liq > mejor_conocida):
                mejor_conocida = liq
            # (Ola 18-H) En el empate exacto gana el par cuya liquidez SI
            # se conoce. Sin esto, con una liquidez leida de exactamente
            # LIQ_MUERTO_USD el resultado —y hasta el precio devuelto—
            # dependia del orden en que DexScreener listara los pares, que
            # no es un criterio.
            if mejor is None or (_cmp, liq is not None) > (mejor_liq,
                                                           mejor_liq_sabida):
                mejor_liq, mejor = _cmp, p
                mejor_liq_sabida = liq is not None
        if not mejor:
            # Hay pares pero ninguno con precio usable: sin dato, no muerte.
            return (None, None, False, 0.0)
        px = float(mejor.get("priceUsd") or 0) or None
        mc = mejor.get("marketCap") or mejor.get("fdv")
        try:
            mc = float(mc) if mc else None
        except (TypeError, ValueError):
            mc = None
        if not mejor_liq_sabida:
            # (Ola 18-E) Hay precio pero DexScreener no dice la liquidez
            # del par que gana. No se puede declarar muerto por algo que
            # no se ha leido: `muerto` se queda en False.
            #
            # (Ola 18-H, 3ª vuelta) Y se devuelve None, NO la liquidez
            # de otro par. Este cuarto valor significa "la liquidez del
            # pool del que sale este precio", y de ese pool no se sabe.
            #
            # La 2ª vuelta devolvia aqui la mayor liquidez leida en
            # cualquier par —normalmente el 0 del pool de pump.fun que
            # queda abandonado tras migrar— para que el suelo de 1.000 $
            # del paper siguiera mordiendo. Salio mal por dos sitios, y la
            # auditoria lo midio: (a) `realtime` escribe este valor en
            # `signals.liq`, asi que un 0 que nadie leyo del par bueno
            # entraba al HISTORICO, y `decision_ia` se lo cuenta luego a
            # la IA como "liquidez muy baja"; y (b) el paper rechazaba sin
            # mirar `paper_liq_desconocida`, dejando inerte el interruptor
            # que la 1ª vuelta puso a proposito y sesgando la simulacion
            # justo en la medicion que existe para producir.
            #
            # Asi que se dice la verdad —no se sabe— y se DEJA CONSTANCIA
            # de lo que si se pudo leer, para que quede en el log y no en
            # la base.
            if mejor_conocida is not None:
                print(f"  · {mint[:8]}… liquidez del par con precio: "
                      f"desconocida (del otro par se leyo "
                      f"${mejor_conocida:,.0f})")
            return (px, mc, False, None)
        if mejor_liq < LIQ_MUERTO_USD:
            # Liquidez de polvo: intradeable. Para la medicion es una
            # perdida total, igual que un par retirado.
            return (None, None, True, mejor_liq)
        return (px, mc, False, mejor_liq)
    except requests.exceptions.InvalidJSONError as e:
        # (Ola 18-H, 4ª vuelta) VA PRIMERA a proposito.
        # `requests.exceptions.JSONDecodeError` hereda de
        # `RequestException` ANTES que de `ValueError`, asi que el caso
        # canonico de "DexScreener contesto pero lo que mando no se puede
        # interpretar" caia en la rama de red y se marcaba `red:` — o sea
        # que el fallo mas propio del token era el que se clasificaba como
        # pasajero.
        _set_fallo_precio(f"dato: {type(e).__name__}: {str(e)[:80]}")
        return (None, None, False, 0.0)
    except requests.RequestException as e:
        # (Ola 18-H) Fallo de TRANSPORTE: la petición no llegó o no
        # volvió (corte de salida, timeout, 429 por ritmo, 5xx). No dice
        # NADA de este token en concreto: el mismo mint puede contestar
        # perfectamente en la siguiente pasada, y de hecho depende de en
        # qué lugar de la tanda le tocó. El prefijo `red:` existe para
        # que quien decida algo a partir de esto pueda distinguirlo —
        # `paper_trading` lo usa para no castigar a una posición por un
        # 429 que era del ritmo, no suyo.
        _set_fallo_precio(f"red: {type(e).__name__}: {str(e)[:80]}")
        return (None, None, False, 0.0)
    except (ValueError, TypeError, AttributeError, KeyError) as e:
        # (Ola 18-H) Fallo al LEER la respuesta: DexScreener contestó,
        # pero lo que mandó para este mint no se puede interpretar. Eso sí
        # es propio del token y se va a repetir igual en cada pasada.
        _set_fallo_precio(f"dato: {type(e).__name__}: {str(e)[:80]}")
        return (None, None, False, 0.0)


# (Ola 17-J, auditoria 6) Hasta cuantos segundos DESPUES de la señal se
# acepta recuperar su precio de entrada. Medido en la base del dueño: de
# 5.881 compras sin precio, 2.001 tenian precio del mismo token poco
# despues — 724 en el mismo minuto y 412 al siguiente. Recuperarlas
# devuelve un tercio de la medicion perdida.
# El tope existe porque el precio de un memecoin 10 min despues YA NO ES
# el de la compra: por encima de RESCATE_MAX_S se prefiere no tener dato
# a tener uno que parece exacto y no lo es. Y el retraso real se guarda
# en `price_lag_s`, asi que siempre se sabe cual es de que.
RESCATE_MAX_S = _f_env("RESCATE_PRECIO_MAX_S", 180.0)


def rescatar_precios(conn=None, ventana_min: int | None = None) -> int:
    """Rellena el precio de entrada de señales recientes que se quedaron
    sin el. Devuelve cuantas se recuperaron."""
    propia = conn is None
    conn = conn or get_conn()
    try:
        ahora = time.time()
        # (Ola 17-K) La ventana era de 30 min y luego se descartaba todo
        # lo que pasara de RESCATE_MAX_S (180 s): los otros 27 minutos se
        # releian y se re-imprimian en CADA pasada, 720 veces al dia,
        # siempre las mismas filas. Se pide justo lo rescatable.
        _vent = (ventana_min * 60 if ventana_min
                 else RESCATE_MAX_S)
        filas = conn.execute(
            """SELECT signature, mint, ts FROM signals
               WHERE (price_usd IS NULL OR price_usd <= 0)
                 AND ts >= ? AND ts <= ?
               ORDER BY ts DESC LIMIT 200""",
            (int(ahora - _vent), int(ahora - 5))).fetchall()
        if not filas:
            return 0
        # Solo las que siguen dentro del tope de retraso aceptable.
        vivas = [f for f in filas if (ahora - float(f["ts"])) <= RESCATE_MAX_S]
        caducadas = len(filas) - len(vivas)
        if caducadas and ventana_min:
            # Solo se dice cuando se pide una ventana ancha a proposito
            # (uso manual); en la pasada normal ya no hay caducadas.
            print(f"· Rescate de precios: {caducadas} señal(es) ya pasaron "
                  f"de {RESCATE_MAX_S:.0f}s; no se les inventa un precio")
        if not vivas:
            return 0
        precios = _prices_mc_lote(sorted({f["mint"] for f in vivas}))
        n = 0
        for f in vivas:
            v = precios.get(f["mint"])
            if not v or not v[0]:
                continue
            lag = int(ahora - float(f["ts"]))
            conn.execute(
                "UPDATE signals SET price_usd=?, mc=COALESCE(mc,?), "
                "liq=COALESCE(liq,?), price_lag_s=? WHERE signature=? "
                "AND (price_usd IS NULL OR price_usd <= 0)",
                (v[0], v[1], v[3], lag, f["signature"]))
            n += 1
        if n:
            conn.commit()
            print(f"· Rescate de precios: {n} señal(es) recuperadas "
                  f"(quedan marcadas con su retraso en price_lag_s)")
        return n
    except Exception as e:
        print(f"· Rescate de precios falló: {e}")
        return 0
    finally:
        if propia:
            conn.close()


def _prices_mc_lote(mints: list) -> dict:
    """{mint: (px, mc, muerto, liq)} para VARIOS mints en una sola
    peticion (DexScreener acepta hasta 30 direcciones separadas por coma).

    (Ola 17-G) Antes esto era un bucle de una peticion por token con
    pausa entre cada una. Solo devuelve los mints que aparecieron en la
    respuesta: un mint AUSENTE **no** se marca como muerto aqui — podria
    faltar por un recorte de la respuesta, y marcar muerto por error
    anota un -99% en el historico medido, que es lo unico irreversible
    del sistema (CLAUDE.md). El llamador consulta esos uno a uno.
    Si la peticion falla entera, devuelve {} y no se concluye nada.
    """
    out = {}
    if not mints:
        return out
    for i in range(0, len(mints), 30):
        trozo = list(mints)[i:i + 30]
        try:
            r = requests.get(
                config.DEXSCREENER_TOKEN.format(address=",".join(trozo)),
                timeout=20)
            _api_rec("dexscreener")
            r.raise_for_status()
            pairs = (r.json() or {}).get("pairs") or []
        except (requests.RequestException, ValueError, TypeError):
            continue                  # ni un dato: el llamador reintenta
        mejor = {}
        for p in pairs:
            m = (p.get("baseToken") or {}).get("address")
            if not m:
                continue
            try:
                px = float(p.get("priceUsd") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            # (Ola 17-H) Antes, una liquidez malformada caia a `liq=0.0`
            # y mas abajo `0 < LIQ_MUERTO_USD` marcaba el token MUERTO:
            # se escribia -100% en el historico medido por un problema de
            # FORMATO. La ruta individual no hace eso (su except devuelve
            # muerto=False y reintenta). Ahora se distingue: `liq=None`
            # significa "no se pudo leer", y sin ese dato NO se declara
            # ninguna muerte.
            try:
                _lq = ((p.get("liquidity") or {}).get("usd"))
                # (Ola 18-E) El campo AUSENTE tambien es "no se pudo
                # leer", no "vale 0". Antes daba 0.0 y unas lineas mas
                # abajo eso era muerte: -100% en el historico medido por
                # un campo que DexScreener no mando. La ruta individual ya
                # lo distingue desde esta misma ola; aqui decia lo
                # contrario para el MISMO token, y este lote es el que
                # manda en `track_outcomes`.
                liq = float(_lq) if _lq is not None else None
            except (TypeError, ValueError, AttributeError):
                liq = None
            if liq is not None and liq < 0:
                liq = None          # dato absurdo: se trata como ausente
            # (Ola 18-H) Mismo cambio que en `_price_mc_ex`: el par sin
            # dato de liquidez se ordena EN el umbral de muerte, no por
            # debajo de todo. Si no, un pool residual con `usd: 0` ganaba
            # al par bueno sin dato y el mint salia con `liq = 0` → muerte
            # → -100% en el historico. Ahora ese mint se queda sin
            # conclusion (`continue` de abajo) y `track_outcomes` lo
            # confirma uno a uno, que es justo para lo que existe ese
            # repesque.
            _orden = float(LIQ_MUERTO_USD) if liq is None else liq
            # (Ola 18-E) `mejor[m][2]`, NO `[1]`. La posicion 1 es `liq`,
            # que puede ser None; la 2 es `_orden`, que existe justo para
            # poder comparar siempre. Comparar contra `[1]` reventaba con
            # `float > None` en cuanto el PRIMER par guardado de un mint
            # no traia liquidez — y eso, desde que el campo ausente ya no
            # se falsea como 0.0, es un caso corriente. La excepcion salia
            # de esta funcion (el `try` de arriba solo cubre la peticion),
            # mataba la pasada entera de `track_outcomes` antes de escribir
            # ninguna medicion y dejaba la conexion sin cerrar.
            # (Ola 18-H, 3ª vuelta) Mismo desempate que en `_price_mc_ex`:
            # en el empate exacto gana el par cuya liquidez SI se conoce,
            # para que el resultado no dependa del orden en que
            # DexScreener liste los pares.
            if m not in mejor or ((_orden, liq is not None)
                                  > (mejor[m][2], mejor[m][1] is not None)):
                mejor[m] = (p, liq, _orden)
        for m, (p, liq, _o) in mejor.items():
            px = float(p.get("priceUsd") or 0) or None
            mc = p.get("marketCap") or p.get("fdv")
            try:
                mc = float(mc) if mc else None
            except (TypeError, ValueError):
                mc = None
            if liq is None:
                # Liquidez ilegible: sin dato, NO se concluye muerte.
                # El llamador lo confirmara uno a uno.
                continue
            # Misma regla que `_price_mc_ex`: liquidez de polvo cuenta
            # como muerte para la medicion.
            if liq < LIQ_MUERTO_USD:
                out[m] = (None, None, True, liq)
            else:
                out[m] = (px, mc, False, liq)
        if i + 30 < len(mints):
            time.sleep(config.DEXSCREENER_DELAY)
    return out


def _price_mc(mint: str):
    """(precio_usd, market_cap) — compatibilidad con los llamadores que no
    necesitan saber si el token murió."""
    px, mc, _m, _l = _price_mc_ex(mint)
    return (px, mc)


def _price(mint: str) -> float | None:
    """Precio actual: el mismo dato de _price_mc, tirando el MC. Un solo
    sitio decide el criterio del par (mejor liquidez)."""
    return _price_mc(mint)[0]


def _alert_milestone(conn, s, pct: float, price: float,
                     mc_actual: float | None = None):
    """Avisa cuando el token señalado alcanza un NUEVO múltiplo (x2, x3, x4…).

    Se deduplica POR TOKEN: se guarda en settings el mayor múltiplo ya
    avisado para ese mint, así que aunque el token lo hayan comprado
    varias billeteras ⭐, solo se manda una alerta por cada múltiplo.
    """
    base = s["price_usd"]
    if not base or base <= 0:
        return
    # ── Base ANCLADA por token (18/8/2026) ────────────────────────────
    # "Quien fue la primera ⭐" se recalcula en cada pasada, y el ranking
    # rota a diario con las re-evaluaciones: si la primera cambia, la
    # base cambia y la escalera salta (BOLLOCKS: x42 y luego "x16"). El
    # candado monotonico evita repetir tarjetas, pero con base nueva mas
    # cara el token quedaria MUDO para siempre (x43 desde una base 2,6
    # veces mas cara ≈ nunca). Solucion: la PRIMERA vez que un token
    # alcanza un multiplo, su base (precio, billetera, ts, mc) queda
    # ANCLADA en settings y toda la escalera futura se mide contra ella,
    # cambie quien cambie en el ranking. Un narrador, una escalera.
    w_base, ts_base, mc_base = s["wallet"], s["ts"], None
    try:
        mc_base = s["mc"]
    except Exception:
        pass
    key_base = f"mult_base:{s['mint']}"
    try:
        import json as _json
        _raw = get_setting(conn, key_base, None)
        if _raw:
            _pin = _json.loads(_raw)
            if _pin.get("price"):
                base = float(_pin["price"])
                w_base = _pin.get("wallet") or w_base
                ts_base = _pin.get("ts") or ts_base
                mc_base = _pin.get("mc", mc_base)
    except Exception:
        pass
    mult = int(price / base)          # 2 = x2 (doble), 3 = x3, …
    if mult < MIN_MULTIPLE:
        return
    # (22/8) Cordura de la base y del multiplo: una base anclada con MC
    # de $652 no es un estado real de mercado (par recien creado, sin
    # liquidez) y de ahi salen "x15699" — tecnicamente consistentes,
    # practicamente falsos (caso real UNKNOWN, MC real $2). Mejor ninguna
    # tarjeta que una absurda.
    if mc_base is not None and mc_base < MC_BASE_MIN_USD:
        print(f"  · {s['mint'][:8]}… sin tarjeta: base con MC "
              f"${mc_base:,.0f} (< ${MC_BASE_MIN_USD:,.0f}, no fiable)")
        return
    if mult > MULT_TARJETA_MAX:
        print(f"  · {s['mint'][:8]}… sin tarjeta: x{mult} supera el tope "
              f"de cordura (x{MULT_TARJETA_MAX:,.0f})")
        return

    # El candado va PRIMERO y es MONOTONICO: el marcador del token solo
    # puede subir. Antes el filtro de top (mas abajo) sobreescribia el
    # marcador con CUALQUIER multiplo, incluso menor: si la "primera ⭐"
    # del token cambiaba (una promocion o degradacion reordena quien
    # califica como primera), la base cambiaba, un multiplo chico de una
    # billetera fuera del top PISABA el marcador hacia abajo... y el
    # siguiente escalon intermedio re-alertaba como nuevo. Caso real:
    # BOLLOCKS aviso x42 y horas despues "x16" (18/8/2026).
    key = f"mult_alert:{s['mint']}"
    last = 0
    try:
        last = int(float(get_setting(conn, key, "0") or 0))
    except (TypeError, ValueError):
        last = 0
    if mult <= last:
        return                        # ese escalón ya sonó (o la escalera va más arriba)

    if last == 0:
        # primer escalón del token: anclar la base para siempre
        try:
            import json as _json
            set_setting(conn, key_base, _json.dumps(
                {"price": base, "wallet": w_base, "ts": ts_base,
                 "mc": mc_base}))
        except Exception:
            pass

    # Solo el top del ranking manda tarjeta. Si la señal la dio una ⭐
    # fuera de las mejores, se marca el escalón (solo hacia ARRIBA:
    # mult > last garantizado por el candado de arriba) y se silencia.
    try:
        from db import top_addresses, en_top
        _top = top_addresses(conn)
        # `en_top` guarda el contrato de tres estados: None = sin filtro
        # (top_alertas = 0 o consulta fallida) y pasa todo; un conjunto
        # vacío SÍ filtra y no lo pasa nadie.
        if w_base and not en_top(_top, w_base):
            print(f"  🔇 x{mult} de {s['mint'][:8]}… sin tarjeta: "
                  f"la billetera está fuera del top")
            set_setting(conn, key, mult)
            return
    except Exception as e:
        print(f"· Filtro de top falló ({e}); mando la tarjeta igual")

    try:
        from realtime import tg_send
    except Exception:
        return

    # Nombre SIEMPRE legible (los alias son deterministas) + posición en /top
    try:
        from wallet_ident import identidad
        _ident = identidad(conn, w_base)
    except Exception:
        _ident = {"nombre": f"{w_base[:8]}…", "pos": None}
    alias = _ident["nombre"]
    _pos = _ident.get("pos")
    hace = (time.time() - ts_base) / 3600
    from card_image import _fmt_price, _ago, _fmt_mc
    simbolo = s["symbol"] or s["mint"][:8]
    # (Ola 8, 21/8) El % SIEMPRE contra la MISMA base anclada que produce
    # el xN. Antes se usaba el pct del llamador (calculado contra la señal
    # vigente de la primera ⭐, que puede diferir del ancla) y, si pct<=0,
    # un fallback con el multiplo TRUNCADO ((mult-1)*100: ratio real 2,9
    # → "+100%" en vez de +190%). Resultado: tarjetas tipo "x5 (+90%)"
    # con dos números incompatibles en la misma línea.
    subida = (price / base - 1) * 100 if base else (mult - 1) * 100
    # Market Cap: el de la llamada (guardado) y el REAL de ahora.
    # ANTES se extrapolaba (mc0 × subida del precio) y eso daba cifras
    # falsas: si el suministro cambia —lo normal al migrar un token de
    # pump.fun— el MC NO escala con el precio. Se llegó a anunciar
    # "MC $51,5M" en un token que nunca estuvo cerca de esa cifra.
    mc0 = mc_base
    ratio = (price / base) if base else mult
    mc1 = mc_actual                      # dato real, no extrapolado

    # Verificación cruzada: si el múltiplo por precio y el del MC no se
    # parecen, uno de los dos datos no es fiable → no se alerta. Mejor
    # perder un aviso que mandar un número inventado.
    if mc0 and mc1 and mc0 > 0:
        mult_mc = mc1 / mc0
        if ratio > 0 and (mult_mc / ratio > 3 or ratio / mult_mc > 3):
            print(f"  ⚠️ {s['mint'][:8]}… descartada: precio dice x{ratio:.0f} "
                  f"pero el MC dice x{mult_mc:.1f} (dato poco fiable)")
            return
    linea_precio = (f"💰 MC {_fmt_mc(mc0)}  →  *{_fmt_mc(mc1)}*"
                    if mc0 else
                    f"💵 ${_fmt_price(base)}  →  *${_fmt_price(price)}*")
    caption = (
        f"🚀 *{simbolo}* hizo *x{mult}*  (+{subida:.0f}%)\n"
        f"{linea_precio}\n"
        f"👤 Primer llamado: *{alias}*"
        + (f"  ·  🏆 #{_pos} del top" if _pos else "")
        + f"  ·  {_ago(hace)}\n"
        f"`{s['mint']}`\n"
        f"📊 [DexScreener](https://dexscreener.com/solana/{s['mint']})")

    # Tarjeta con imagen (estilo Trojan); si falla, cae al texto normal
    enviado = False
    try:
        from card_image import make_multiple_card
        from realtime import tg_send_photo
        img = make_multiple_card(mult, simbolo, subida, base, price,
                                 alias, hace, mc_base=mc0, mc_now=mc1,
                                 pos_top=_pos)
        tg_send_photo(img, caption)
        enviado = True
    except Exception as e:
        print(f"· Tarjeta de imagen falló, uso texto: {e}")
    if not enviado:
        tg_send(caption)

    set_setting(conn, key, mult)      # marca el múltiplo avisado para el token
    print(f"🚀 Alerta de subida: {simbolo} x{mult}")


STREAK_N = 4  # señales perdedoras seguidas para perder la ⭐


def _check_streaks(conn):
    """Degrada ⭐ con racha perdedora: si sus últimas STREAK_N señales
    medidas a 24h cerraron todas en negativo, pierde la estrella.
    Se puede restaurar a mano con /rastrear <address>."""
    ws = conn.execute(
        "SELECT DISTINCT wallet FROM signals "
        "WHERE side='compra' AND chg_24h IS NOT NULL").fetchall()
    for row in ws:
        w = row["wallet"]
        info = conn.execute(
            "SELECT is_tracked, alias FROM wallets WHERE address=?",
            (w,)).fetchone()
        if not info or not info["is_tracked"]:
            continue
        ult = conn.execute(
            "SELECT chg_24h FROM signals WHERE wallet=? AND side='compra' "
            "AND chg_24h IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (w, STREAK_N)).fetchall()
        if len(ult) < STREAK_N or any(r["chg_24h"] > 0 for r in ult):
            continue
        # RACHA ≠ RUINA (19/8): con el win rate tipico de memecoins
        # (~25%), CUALQUIER billetera rentable encadena 4 rojas un
        # tercio del tiempo — una ganadora grande paga muchas perdidas
        # chicas. Si su mes va en POSITIVO, la racha no la degrada.
        pnl30 = conn.execute(
            "SELECT pnl_30d FROM wallets WHERE address=?",
            (w,)).fetchone()
        if pnl30 and (pnl30["pnl_30d"] or 0) > 0:
            continue
        conn.execute(
            "UPDATE wallets SET is_tracked=0, ai_follow=0, confirmada=0, "
            "prueba_desde=NULL, ai_reason=? WHERE address=?",
            (f"Racha perdedora: últimas {STREAK_N} señales en negativo", w))
        conn.commit()
        alias = (info["alias"] or w[:8]).replace("*", "").replace("_", " ")
        print(f"📉 {alias} pierde la estrella por racha perdedora")
        try:
            from realtime import tg_send, sync_helius_webhook
            tg_send(f"📉 *{alias}* pierde la ⭐: sus últimas {STREAK_N} "
                    "señales cerraron en negativo.\n"
                    f"Restaurar: /rastrear {w}")
            sync_helius_webhook()
        except Exception as e:
            print(f"· Aviso de racha falló: {e}")


def _auto_threshold(conn):
    """Umbral que aprende: elige el min_signal_score que maximiza el
    win rate histórico a 24h (manteniendo un mínimo de señales)."""
    # EL AJUSTE MANUAL MANDA (19/8): si el dueño fijo el umbral por el
    # agente, este auto-ajuste queda apagado — antes le pisaba el valor
    # a los 15 minutos y parecia que "no se guardaba".
    from db import get_setting as _gs
    if str(_gs(conn, "umbral_manual", "0") or "0") == "1":
        return
    rows = conn.execute(
        "SELECT signal_score, chg_24h FROM signals "
        "WHERE side='compra' AND chg_24h IS NOT NULL "
        "AND signal_score IS NOT NULL").fetchall()
    if len(rows) < 10:
        return  # aún pocos datos para decidir
    mejor, mejor_wr = 0, -1.0
    for umbral in (0, 40, 50, 60, 70):
        sel = [r for r in rows if r["signal_score"] >= umbral]
        if len(sel) < 5:
            continue
        wr = sum(1 for r in sel if r["chg_24h"] > 0) / len(sel)
        if wr > mejor_wr:
            mejor, mejor_wr = umbral, wr
    from db import get_setting, set_setting
    actual = float(get_setting(conn, "min_signal_score", "0") or 0)
    if mejor != actual:
        set_setting(conn, "min_signal_score", str(mejor))
        print(f"🎚️ Umbral auto-ajustado: {actual:.0f} → {mejor} "
              f"(win rate {mejor_wr*100:.0f}%)")
        try:
            from realtime import tg_send
            tg_send(f"🎚️ Umbral de alerta auto-ajustado a *{mejor}* "
                    f"(win rate histórico {mejor_wr*100:.0f}% con ese corte). "
                    "Las señales por debajo se miden pero no alertan.")
        except Exception:
            pass


def track_outcomes() -> int:
    """
    1) Rellena price_1h/price_24h (y % de cambio) de las señales de
       compra que ya cumplieron la edad necesaria.
    2) Vigila el precio de las señales recientes (<48h) y avisa cuando
       el token alcanza un nuevo múltiplo (x2, x3, x4…).
    Pensado para correr como job periódico (cada ~15 min).
    """
    now = time.time()
    conn = get_conn()
    # 1) Señales PENDIENTES de medicion, solo dentro de su ventana valida:
    #    la "1h" se mide entre 1h y 3h; la "24h" entre 24h y 30h. Antes una
    #    medicion atrasada (backlog, caida) se guardaba con la etiqueta
    #    equivocada y contaminaba track record, umbral automatico y rachas.
    #    CAPACIDAD DIMENSIONADA (Ola 6, auditoria 19/8 - C12): con
    #    LIMIT 30 cada 15 min salian ~2.880 mediciones/dia contra ~5.500
    #    de demanda — el backlog envejecia, caia fuera de la ventana y la
    #    mayoria de señales quedaba sin chg_1h/chg_24h PARA SIEMPRE,
    #    matando de hambre a track record, umbral automatico y rachas.
    #    Dos arreglos: medir solo lo que alimenta decisiones (⭐ y liga
    #    de ascenso; el track record de una billetera aleatoria no decide
    #    nada) y subir el cupo (los precios van cacheados por mint, el
    #    costo real es por token, no por señal).
    pend = conn.execute(
        """SELECT s.signature, s.wallet, s.mint, s.ts, s.price_usd,
                  s.price_1h, s.price_24h, s.alerted_pct, s.symbol
           FROM signals s
           JOIN wallets w ON w.address = s.wallet
                AND COALESCE(w.is_bot, 0) = 0
                AND (w.is_tracked = 1 OR w.winning_tokens_count >= 2)
           WHERE s.side='compra' AND s.price_usd IS NOT NULL
             AND s.price_usd > 0
             AND ((s.price_1h IS NULL AND s.ts <= ? AND s.ts >= ?)
               OR (s.price_24h IS NULL AND s.ts <= ? AND s.ts >= ?))
           ORDER BY s.ts ASC LIMIT 100""",
        (int(now - HOUR), int(now - 3 * HOUR),
         int(now - DAY), int(now - 30 * HOUR))).fetchall()
    # (Ola 17-I, auditoria 6) La ventana se CIERRA: la medicion de "1h"
    # solo se acepta entre 1h y 3h. Si DexScreener esta caido mas de dos
    # horas seguidas, esas señales salen de la ventana y NO VUELVEN A
    # ENTRAR JAMAS. No se puede arreglar midiendo tarde (el precio de
    # hace 5 h no es el de 1 h, y etiquetarlo como "1h" contaminaria el
    # track record), pero SI se puede dejar de perderlo en silencio.
    _perdidas = conn.execute(
        """SELECT COUNT(*) c FROM signals s
           JOIN wallets w ON w.address = s.wallet
                AND COALESCE(w.is_bot, 0) = 0
                AND (w.is_tracked = 1 OR w.winning_tokens_count >= 2)
           WHERE s.side='compra' AND s.price_usd IS NOT NULL
             AND s.price_usd > 0 AND s.price_1h IS NULL
             AND s.ts < ? AND s.ts >= ?""",
        (int(now - 3 * HOUR), int(now - 9 * HOUR))).fetchone()["c"]
    if _perdidas:
        print(f"· Medición: {_perdidas} señal(es) de las últimas 9 h se "
              f"quedaron sin medir a 1 h y ya están fuera de ventana")
        try:
            from errores import record as _rec
            _rec("medicion.fuera_de_ventana",
                 RuntimeError(f"{_perdidas} señales sin chg_1h, "
                              f"ventana cerrada"))
        except Exception:
            pass
    if len(pend) >= 100:
        # Tope alcanzado: el excedente envejece y puede caer fuera de la
        # ventana. Ya paso una vez con LIMIT 30 (ver comentario arriba) y
        # nada avisaba; ahora si.
        print("· Medición: cupo de 100 señales por pasada AGOTADO; hay "
              "más esperando y podrían caducar. Si se repite, sube el "
              "cupo o baja el intervalo del job.")
    # 1b) VENTAS de billeteras ⭐, mismas ventanas de medicion. Se miden
    #     para conocer la "deriva post-venta" de cada billetera: si el token
    #     sigue subiendo despues de que ella vende (vende demasiado pronto)
    #     o se desploma (sale en la cima). Ese perfil decide, en el copy
    #     trading, si conviene holdear mas que la billetera copiada o salir
    #     antes. Solo ⭐: las salidas de las candidatas no se copian, y
    #     medirlas gastaria cupo de DexScreener sin uso. Query y cupo
    #     APARTE para que las ventas no compitan con las compras.
    pend_ventas = conn.execute(
        """SELECT s.signature, s.wallet, s.mint, s.ts, s.price_usd,
                  s.price_1h, s.price_24h, s.alerted_pct, s.symbol
           FROM signals s
           JOIN wallets w ON w.address = s.wallet
           WHERE s.side='venta' AND s.price_usd IS NOT NULL
             AND s.price_usd > 0 AND w.is_tracked = 1
             AND ((s.price_1h IS NULL AND s.ts <= ? AND s.ts >= ?)
               OR (s.price_24h IS NULL AND s.ts <= ? AND s.ts >= ?))
           ORDER BY s.ts ASC LIMIT 15""",
        (int(now - HOUR), int(now - 3 * HOUR),
         int(now - DAY), int(now - 30 * HOUR))).fetchall()
    # 2) Señales recientes (<48h) para las alertas de multiplos (x2, x3…);
    #    query aparte para que no compitan por el cupo con las mediciones.
    #
    #    SOLO BILLETERAS ⭐ (is_tracked=1). El webhook vigila ⭐ + candidatas
    #    prometedoras; las compras de las candidatas se registran pero NO
    #    alertan (liga de ascenso, se les construye historial en silencio).
    #    Sin este filtro las candidatas no mandaban alerta de compra pero SI
    #    mandaban las cartas de x2/x3, que era justo la basura que llenaba
    #    el chat. Las mediciones (query `pend` de arriba) siguen sin filtrar:
    #    queremos seguir midiendo a las candidatas para poder promocionarlas.
    #    LA PRIMERA ⭐ MANDA (13/8/2026): los multiplos se miden SOLO desde
    #    la señal de la primera ⭐ que compro el token, igual que el paper
    #    trading. Antes cada señal usaba SU precio de compra como base: si
    #    A compro barato y B caro, el mismo pump daba "x5" con la base de A
    #    y "x2" con la de B, numeros distintos que se colaban uno tras otro
    #    por el deduplicador (que compara el VALOR del multiplo). Con 5 ⭐
    #    comprando lo mismo, era una metralleta de tarjetas repetidas.
    recent = conn.execute(
        """SELECT s.signature, s.wallet, s.mint, s.ts, s.price_usd,
                  s.price_1h, s.price_24h, s.alerted_pct, s.symbol, s.mc
           FROM signals s
           JOIN wallets w ON w.address = s.wallet
           JOIN (SELECT s2.mint AS mint, MIN(s2.ts) AS t0
                 FROM signals s2
                 JOIN wallets w2 ON w2.address = s2.wallet
                      AND w2.is_tracked = 1
                 WHERE s2.side='compra' AND s2.price_usd IS NOT NULL
                   AND s2.price_usd > 0 AND s2.ts >= ?
                 GROUP BY s2.mint) primera
             ON primera.mint = s.mint AND primera.t0 = s.ts
           WHERE s.side='compra' AND s.price_usd IS NOT NULL
             AND s.price_usd > 0 AND s.ts >= ?
             AND w.is_tracked = 1
           ORDER BY s.ts DESC LIMIT 100""",
        (int(now - WATCH_HOURS * HOUR),
         int(now - WATCH_HOURS * HOUR))).fetchall()

    prices: dict = {}          # cache: 1 consulta por mint → (precio, mc)

    def _px_mc(mint):
        if mint not in prices:
            prices[mint] = _price_mc_ex(mint)
            time.sleep(config.DEXSCREENER_DELAY)
        return prices[mint]

    def _px(mint):
        return _px_mc(mint)[0]

    # ── Fase A: TODA la red primero (18/8/2026) ─────────────────────────
    # Antes el primer UPDATE abria la transaccion de escritura y luego el
    # bucle seguia descargando precios (con pausa entre cada uno): el
    # candado de escritura quedaba retenido MINUTOS y el webhook chocaba
    # con "database is locked" en el SQLite local. Ahora se llena la cache
    # de precios sin ninguna transaccion abierta, y la fase de escritura
    # dura milisegundos. En Postgres no cambia nada.
    # (Ola 17-G) Primero de golpe, en lotes de 30, y solo despues uno a
    # uno los que NO aparecieron: un mint ausente puede estar muerto o
    # puede faltar por un recorte de la respuesta, y esa diferencia vale
    # un -99% falso en el historico. El lote ahorra las llamadas de los
    # que sí estan, que son la inmensa mayoria.
    _todos = {s["mint"] for s in
              list(pend) + list(pend_ventas) + list(recent)}
    if _todos:
        _lote = _prices_mc_lote(sorted(_todos))
        prices.update(_lote)
        _faltan = [m for m in _todos if m not in _lote]
        if _faltan:
            print(f"· Medición: {len(_lote)} precios en lote, "
                  f"{len(_faltan)} a confirmar uno a uno")
        for _m in _faltan:
            _px_mc(_m)

    # ── Fase B: escritura rapida desde la cache ─────────────────────────
    updated = 0
    for s in list(pend) + list(pend_ventas):
        base = s["price_usd"]
        p, _mc, muerto, _liq = _px_mc(s["mint"])
        if not p:
            if not muerto:
                continue
            # (Ola 8) Token sin ningún par en DexScreener: dejó de cotizar.
            # Se registra como pérdida total (-100%) en vez de dejar la
            # señal sin medir para siempre e inflar el win rate.
            p, pct = 0.0, -100.0
        else:
            pct = (p / base - 1) * 100
        age = now - s["ts"]
        if s["price_1h"] is None and HOUR <= age <= 3 * HOUR:
            conn.execute(
                "UPDATE signals SET price_1h=?, chg_1h=? WHERE signature=?",
                (p, pct, s["signature"]))
            updated += 1
        if s["price_24h"] is None and DAY <= age <= 30 * HOUR:
            conn.execute(
                "UPDATE signals SET price_24h=?, chg_24h=? WHERE signature=?",
                (p, pct, s["signature"]))
            updated += 1
    conn.commit()   # mediciones guardadas ya: candado liberado al instante
    _ya_visto = set()
    for s in recent:
        # empate de ts en el mismo mint: solo una base por token
        if s["mint"] in _ya_visto:
            continue
        _ya_visto.add(s["mint"])
        base = s["price_usd"]
        p, mc_now, _muerto, _liq = _px_mc(s["mint"])
        if not p:
            continue
        # (Ola 18-E) `_liq` puede ser None: desde esta ola, "DexScreener
        # no dice la liquidez" ya no se falsea como 0. Sin ese dato NO se
        # saca tarjeta —el criterio es el mismo: sin liquidez comprobada
        # no hay precio fiable— pero se dice distinto. Antes esta linea
        # habria hecho `None < 1000.0` y reventado la pasada ENTERA de
        # `track_outcomes`, dejando ademas la conexion sin cerrar (el
        # `conn.close()` de esta funcion no esta en un `finally`).
        if _liq is None:
            print(f"  · {s['mint'][:8]}… sin tarjeta: DexScreener no dio "
                  f"la liquidez")
            continue
        if _liq < LIQ_FIABLE_USD:
            # (22/8) Sin liquidez real no hay precio fiable: ninguna
            # tarjeta de hito puede salir de un par en el que $500 de
            # volumen mueven el precio x1000.
            print(f"  · {s['mint'][:8]}… sin tarjeta: liquidez "
                  f"${_liq:,.0f} < ${LIQ_FIABLE_USD:,.0f}")
            continue
        pct = (p / base - 1) * 100
        _alert_milestone(conn, s, pct, p, mc_actual=mc_now)
        # La tarjeta manda un mensaje a Telegram (red): commit por hito
        # para no retener el candado de escritura durante el envio.
        conn.commit()
    _check_streaks(conn)
    _auto_threshold(conn)
    conn.close()
    if updated:
        print(f"📈 Track record: {updated} mediciones de señales actualizadas")
    return updated


def wallet_track_record(conn, wallet: str) -> dict | None:
    """
    Estadística de las últimas señales de compra medidas de una billetera:
    tasa de acierto y cambio promedio a 1h y 24h. None si aún no hay datos.
    """
    rows = conn.execute(
        """SELECT chg_1h, chg_24h FROM signals
           WHERE wallet=? AND side='compra'
             AND (chg_1h IS NOT NULL OR chg_24h IS NOT NULL)
           ORDER BY ts DESC LIMIT 20""", (wallet,)).fetchall()
    if not rows:
        return None
    c1 = [r["chg_1h"] for r in rows if r["chg_1h"] is not None]
    c24 = [r["chg_24h"] for r in rows if r["chg_24h"] is not None]
    out = {"senales_medidas": len(rows)}
    if c1:
        out["tasa_acierto_1h_pct"] = round(
            100 * sum(1 for x in c1 if x > 0) / len(c1))
        out["cambio_promedio_1h_pct"] = round(sum(c1) / len(c1), 1)
        out["n_1h"] = len(c1)
    if c24:
        out["tasa_acierto_24h_pct"] = round(
            100 * sum(1 for x in c24 if x > 0) / len(c24))
        out["cambio_promedio_24h_pct"] = round(sum(c24) / len(c24), 1)
        out["n_24h"] = len(c24)
    return out


def format_track_record(tr: dict | None) -> str:
    """Línea corta para mensajes de Telegram."""
    if not tr:
        return ""
    # (Ola 8, 21/8) El n mostrado es el del HORIZONTE usado, no el total
    # de filas con cualquier medición: antes se leía "20 señales: acierto
    # 24h 40%" cuando el 40% salía de solo 5 medidas a 24 h.
    partes = []
    n = tr["senales_medidas"]
    if "tasa_acierto_24h_pct" in tr:
        n = tr.get("n_24h", n)
        partes.append(f"acierto 24h: {tr['tasa_acierto_24h_pct']}% "
                      f"({tr['cambio_promedio_24h_pct']:+.0f}% prom)")
    elif "tasa_acierto_1h_pct" in tr:
        n = tr.get("n_1h", n)
        partes.append(f"acierto 1h: {tr['tasa_acierto_1h_pct']}% "
                      f"({tr['cambio_promedio_1h_pct']:+.0f}% prom)")
    if not partes:
        return ""
    return (f"🎯 Track record ({n} señales medidas): "
            + " · ".join(partes))
