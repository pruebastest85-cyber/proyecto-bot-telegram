"""
LaserStream: escucha de transacciones en tiempo real por WebSocket.

QUÉ APORTA FRENTE AL WEBHOOK
----------------------------
1. Menor latencia. En copy-trading de memecoins cada segundo cuenta, y el
   webhook añade un salto extra (Helius → tu servidor HTTP).
2. `fromSlot`: al reconectar se reanuda desde el último slot procesado. Con
   el webhook, todo lo que ocurría mientras Railway reiniciaba se perdía
   PARA SIEMPRE. Esto lo recupera.

POR QUÉ NO ROMPE NADA
---------------------
No sustituye al webhook: corre EN PARALELO. Ambos desembocan en el mismo
`process_transactions`, y como `signals.signature` es clave primaria con
INSERT OR IGNORE, la segunda vía que llegue se descarta sola. Es decir:
quien llegue antes manda (normalmente LaserStream, de ahí la mejora de
latencia) y el webhook queda de red de seguridad si el socket se cae.

Se apaga con USE_LASERSTREAM=0 sin tocar código.
"""

import json
import os
import threading
import time

import config

WS_URL = "wss://mainnet.helius-rpc.com/?api-key={key}"
_HILO = None
_ULTIMO_SLOT = [0]
_SLOT_PERSISTIDO = [0.0]   # cuando se escribio a settings por ultima vez
# (Ola 17-N) `ultimo` y `arranque` existen para poder responder "¿cuánto
# lleva sin llegar NADA?", que es la única pregunta que delata una
# suscripción muerta. `recibidas` no sirve: es acumulado desde el
# arranque del proceso y nunca se reinicia, así que en cuanto llega una
# sola transacción deja de valer como señal de vida. Y `desde` tampoco:
# se reescribe en cada reconexión, y el watchdog reconecta cada 10 min,
# así que nunca crece. `ultimo` SOLO avanza con datos reales.
_ESTADO = {"conectado": False, "desde": None, "recibidas": 0,
           "error": None, "ultimo": None, "arranque": None}

# v2 (auditoria 19/8) — tres agujeros de la unica via de ingesta del bot
# local, todos silenciosos:
#   1. La lista de billeteras se leia UNA vez por conexion y el ping
#      mantenia el socket vivo dias: una ⭐ promovida no generaba señales
#      hasta que el socket casualmente se cayera. Ahora se re-verifica la
#      huella de la lista cada CHEQUEO_LISTA_S y se re-suscribe si cambio.
#   2. El "watchdog" comparaba contra el ultimo PING (que siempre es
#      reciente): codigo inalcanzable. Ahora vigila el ultimo MENSAJE
#      recibido: SILENCIO_MAX_S sin transacciones con 150+ billeteras
#      vigiladas es anomalo → re-suscribir (un WS reconecta gratis).
#   3. Las respuestas de ERROR del servidor se descartaban como "otro
#      aviso": una suscripcion rechazada dejaba el hilo "🟢 conectado"
#      recibiendo nada para siempre. Ahora se imprimen y registran.
CHEQUEO_LISTA_S = 60       # cada cuanto mirar si cambio la lista vigilada
SILENCIO_MAX_S = 600       # 10 min sin mensajes → re-suscribir
PERSISTIR_SLOT_S = 15      # el slot se escribe a settings como mucho asi
N_WORKERS = 3              # hilos consumidores (Ola 5); la cola absorbe
_COLA_MAX = 1000           # rafagas sin crear un hilo por mensaje

import queue as _queue
_COLA: "_queue.Queue" = _queue.Queue(maxsize=_COLA_MAX)
_WORKERS: list = []
# (19-D) Anti-ruido del aviso de cola llena: una tormenta descarta
# cientos seguidas y no hace falta una fila de /errores por cada una.
_ULTIMO_AVISO_COLA = [0.0]


def _worker() -> None:
    from realtime import process_transactions
    while True:
        t = _COLA.get()
        try:
            process_transactions([t])
        except Exception as e:
            print(f"· LaserStream worker: {e}")
        finally:
            _COLA.task_done()


def _arrancar_workers() -> None:
    vivos = [w for w in _WORKERS if w.is_alive()]
    for i in range(N_WORKERS - len(vivos)):
        w = threading.Thread(target=_worker, daemon=True,
                             name=f"ls-worker-{i}")
        w.start()
        _WORKERS.append(w)


def activo() -> bool:
    return os.getenv("USE_LASERSTREAM", "1") == "1"


def estado() -> dict:
    return dict(_ESTADO)


def _guardar_slot(slot: int) -> None:
    """Recuerda hasta dónde procesamos, para reanudar tras un corte.

    Con debounce (v2): antes se abria una conexion y se COMMITEABA por
    CADA mensaje de 150+ billeteras — presion de escritura constante
    sobre SQLite, alimentando los "database is locked". En memoria se
    actualiza siempre; a settings va como mucho cada PERSISTIR_SLOT_S.
    Si el proceso muere se pierden ≤15 s de avance: el fromSlot re-pide
    ese trozo y el INSERT OR IGNORE por firma deduplica solo."""
    if not slot or slot <= _ULTIMO_SLOT[0]:
        return
    _ULTIMO_SLOT[0] = slot
    ahora = time.time()
    if ahora - _SLOT_PERSISTIDO[0] < PERSISTIR_SLOT_S:
        return
    _SLOT_PERSISTIDO[0] = ahora
    try:
        from db import get_conn, set_setting
        conn = get_conn()
        try:
            set_setting(conn, "laserstream_slot", slot)
        finally:
            conn.close()
    except Exception:
        pass


def _huella(direcciones: list[str]) -> str:
    import hashlib
    return hashlib.sha1(",".join(sorted(direcciones)).encode()).hexdigest()


def _cargar_slot() -> int:
    try:
        from db import get_conn, get_setting
        conn = get_conn()
        try:
            return int(float(get_setting(conn, "laserstream_slot", 0) or 0))
        finally:
            conn.close()
    except Exception:
        return 0


def _suscripcion(direcciones: list[str], desde_slot: int | None) -> str:
    opciones = {
        "commitment": "confirmed",
        "encoding": "jsonParsed",
        "transactionDetails": "full",
        "showRewards": False,
        "maxSupportedTransactionVersion": 0,
    }
    if desde_slot:
        opciones["fromSlot"] = desde_slot      # reanudar sin perder nada
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
        "params": [
            {"vote": False, "failed": False,
             "accountInclude": direcciones,
             "accountExclude": [], "accountRequired": []},
            opciones,
        ],
    })


def _procesar(mensaje: str) -> None:
    """Traduce la notificación y la manda al MISMO camino que el webhook."""
    try:
        d = json.loads(mensaje)
    except Exception:
        return
    params = d.get("params") or {}
    resultado = params.get("result") or {}
    if not resultado:
        # NO es una transaccion. Antes se descartaba TODO en silencio,
        # incluidas las respuestas de error: una suscripcion rechazada
        # dejaba el hilo "conectado" sin recibir nada para siempre.
        if "error" in d:
            err = str(d.get("error") or "")[:200]
            print(f"· LaserStream: ERROR del servidor: {err}")
            _ESTADO["error"] = err[:120]
            try:
                from errores import record
                record("laserstream", RuntimeError(err))
            except Exception:
                pass
        elif "result" in d:
            print(f"· LaserStream: suscripción confirmada "
                  f"(id {d.get('result')})")
        return

    slot = resultado.get("slot")
    tx = resultado.get("transaction") or resultado
    entrada = {"slot": slot,
               "blockTime": resultado.get("blockTime") or int(time.time()),
               "transaction": (tx or {}).get("transaction") or tx,
               "meta": (tx or {}).get("meta") or resultado.get("meta") or {}}
    try:
        from helius_rpc import traducir
        t = traducir(entrada)
    except Exception:
        t = None
    if not t or not t.get("signature"):
        return

    _ESTADO["recibidas"] += 1
    _ESTADO["ultimo"] = time.time()      # (17-N) señal de vida REAL
    # Pool FIJO de workers (Ola 5, auditoria 19/8 - M4): antes se lanzaba
    # UN HILO POR MENSAJE — con rafagas de 150+ billeteras, decenas de
    # hilos concurrentes peleando por la escritura de SQLite ("database
    # is locked") y carreras en positions/paper. Ahora el socket solo
    # encola y N workers consumen: la concurrencia queda acotada y las
    # rafagas se absorben en la cola en vez de en hilos.
    try:
        _COLA.put_nowait(t)
    except Exception:
        # ── (19-D) EL DESCARTE NO AVANZA EL SLOT, Y NO ES MUDO ────────
        #
        # `_guardar_slot` se llamaba ARRIBA, ANTES de encolar. Si la cola
        # de 1.000 estaba llena, la transaccion se tiraba con un `print`
        # —sin `errores.record`— pero el slot YA habia avanzado y se
        # persistia en `settings.laserstream_slot`. Al reconectar,
        # `fromSlot` reanudaba DESPUES de esas transacciones: la señal no
        # existe, `positions` queda descuadrada, y no hay ni una linea en
        # /errores que lo delate. LaserStream es la UNICA via de ingesta
        # del bot (sin PUBLIC_URL el webhook no recibe nada), asi que lo
        # que se pierde ahi no lo recupera nadie.
        #
        # Ahora el slot solo avanza si la transaccion quedo ENCOLADA. Al
        # reconectar, `fromSlot` vuelve a pedir desde la ultima que si se
        # acepto; los repes mueren en el INSERT OR IGNORE por firma, que
        # es exactamente para lo que esta.
        _ESTADO["descartadas"] = _ESTADO.get("descartadas", 0) + 1
        print("· LaserStream: cola llena; transaccion descartada "
              f"({_ESTADO['descartadas']} en total); el slot NO avanza")
        # Anti-ruido: en una tormenta se descartan cientos seguidas y no
        # hace falta una fila por cada una para enterarse.
        _ahora = time.time()
        if _ahora - _ULTIMO_AVISO_COLA[0] > 300:
            _ULTIMO_AVISO_COLA[0] = _ahora
            try:
                from errores import record
                record("laserstream.cola_llena", RuntimeError(
                    f"cola llena ({_COLA_MAX}); {_ESTADO['descartadas']} "
                    f"transacciones descartadas desde el arranque. Los "
                    f"{N_WORKERS} workers no dan abasto."))
            except Exception as e:
                print(f"  · (y no pude registrarlo en /errores: {e})")
        return
    if slot:
        _guardar_slot(int(slot))


def _bucle() -> None:
    """Conexión persistente con reintentos y espera creciente."""
    try:
        import websocket        # websocket-client
    except ImportError:
        _ESTADO["error"] = "falta la librería websocket-client"
        print("· LaserStream desactivado: falta websocket-client")
        return

    espera = 5
    while True:
        if not activo():
            time.sleep(60)
            continue
        try:
            from realtime import watch_addresses
            direcciones = watch_addresses() or []
        except Exception:
            direcciones = []
        if not direcciones:
            time.sleep(120)        # aún no hay ⭐ que vigilar
            continue

        url = WS_URL.format(key=config.HELIUS_API_KEY)
        try:
            ws = websocket.create_connection(url, timeout=30)
            ws.send(_suscripcion(direcciones, _cargar_slot() or None))
            huella = _huella(direcciones)
            _ESTADO.update({"conectado": True, "desde": time.time(),
                            "error": None})
            print(f"📡 LaserStream conectado · {len(direcciones)} billeteras")
            espera = 5
            ultimo_ping = time.time()
            ultimo_mensaje = time.time()      # ultimo MENSAJE, no ping
            ultimo_chequeo_lista = time.time()
            while True:
                try:
                    ws.settimeout(60)
                    mensaje = ws.recv()
                except Exception:
                    mensaje = None
                if mensaje:
                    ultimo_mensaje = time.time()
                    _procesar(mensaje)
                ahora = time.time()
                if ahora - ultimo_ping > 30:
                    try:
                        ws.ping()          # el socket cierra a los 10 min sin uso
                        ultimo_ping = ahora
                    except Exception:
                        print("· LaserStream: ping falló; reconectando")
                        break
                # Watchdog REAL (v2): silencio de datos, no de pings. El
                # ping siempre esta fresco (lo mandamos nosotros); lo que
                # delata una suscripcion muerta es no recibir NADA.
                if ahora - ultimo_mensaje > SILENCIO_MAX_S:
                    print(f"· LaserStream: {SILENCIO_MAX_S // 60} min sin "
                          f"mensajes con {len(direcciones)} billeteras "
                          "vigiladas; re-suscribo por si la suscripción "
                          "murió en silencio")
                    espera = 1
                    break
                # Gestor de suscripcion (v2): si la lista vigilada cambio
                # (ciclo que promueve/degrada), re-suscribir YA — antes la
                # ⭐ nueva no generaba señales hasta la proxima caida
                # casual del socket.
                if ahora - ultimo_chequeo_lista >= CHEQUEO_LISTA_S:
                    ultimo_chequeo_lista = ahora
                    try:
                        nuevas = watch_addresses() or []
                    except Exception:
                        nuevas = []
                    if nuevas and _huella(nuevas) != huella:
                        print("· LaserStream: la lista vigilada cambió "
                              f"({len(direcciones)} → {len(nuevas)}); "
                              "re-suscribo")
                        espera = 1
                        break
        except Exception as e:
            _ESTADO.update({"conectado": False, "error": str(e)[:120]})
            print(f"· LaserStream desconectado: {e}; reintento en {espera}s")
            try:
                from errores import record
                record("laserstream", e)
            except Exception:
                pass
        finally:
            _ESTADO["conectado"] = False
            try:
                ws.close()
            except Exception:
                pass
        time.sleep(espera)
        espera = min(espera * 2, 300)      # hasta 5 min entre reintentos


def start() -> bool:
    """Arranca la escucha en segundo plano. Devuelve si quedó activa."""
    global _HILO
    if not activo():
        print("· LaserStream desactivado (USE_LASERSTREAM=0)")
        return False
    # (17-O) `arranque` se fijaba DESPUES del corte por falta de clave,
    # que es justo el unico caso en que hacia falta: sin clave el estado
    # quedaba entero a None y /salud no sabia ni cuanto llevaba asi.
    # Tambien se deja dicho el motivo, que antes iba vacio.
    if _ESTADO.get("arranque") is None:
        _ESTADO["arranque"] = time.time()
    if not config.HELIUS_API_KEY:
        _ESTADO["error"] = "falta HELIUS_API_KEY"
        print("· LaserStream no arranca: falta HELIUS_API_KEY")
        return False
    if _HILO and _HILO.is_alive():
        return True
    _ULTIMO_SLOT[0] = _cargar_slot()
    _arrancar_workers()
    _HILO = threading.Thread(target=_bucle, daemon=True, name="laserstream")
    _HILO.start()
    return True


def estado_line() -> str:
    if not activo():
        return "⚪ LaserStream: desactivado"
    e = estado()
    if e["conectado"]:
        mins = (time.time() - (e["desde"] or time.time())) / 60
        return (f"🟢 LaserStream: conectado hace {mins:.0f} min · "
                f"{e['recibidas']} transacciones recibidas")
    return f"🔴 LaserStream: desconectado{' — ' + e['error'] if e['error'] else ''}"
