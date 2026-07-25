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
_ESTADO = {"conectado": False, "desde": None, "recibidas": 0, "error": None}


def activo() -> bool:
    return os.getenv("USE_LASERSTREAM", "1") == "1"


def estado() -> dict:
    return dict(_ESTADO)


def _guardar_slot(slot: int) -> None:
    """Recuerda hasta dónde procesamos, para reanudar tras un corte."""
    if not slot or slot <= _ULTIMO_SLOT[0]:
        return
    _ULTIMO_SLOT[0] = slot
    try:
        from db import get_conn, set_setting
        conn = get_conn()
        try:
            set_setting(conn, "laserstream_slot", slot)
        finally:
            conn.close()
    except Exception:
        pass


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
        return                      # confirmación de suscripción u otro aviso

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
    if slot:
        _guardar_slot(int(slot))
    try:
        from realtime import process_transactions
        # En su propio hilo, igual que hace el webhook: no bloquear el socket
        threading.Thread(target=process_transactions, args=([t],),
                         daemon=True).start()
    except Exception as e:
        print(f"· LaserStream: no se pudo procesar ({e})")


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
            _ESTADO.update({"conectado": True, "desde": time.time(),
                            "error": None})
            print(f"📡 LaserStream conectado · {len(direcciones)} billeteras")
            espera = 5
            ultimo_ping = time.time()
            while True:
                try:
                    ws.settimeout(60)
                    mensaje = ws.recv()
                except Exception:
                    mensaje = None
                if mensaje:
                    _procesar(mensaje)
                if time.time() - ultimo_ping > 30:
                    try:
                        ws.ping()          # el socket cierra a los 10 min sin uso
                        ultimo_ping = time.time()
                    except Exception:
                        break
                if mensaje is None and time.time() - ultimo_ping > 120:
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
    if not config.HELIUS_API_KEY:
        return False
    if _HILO and _HILO.is_alive():
        return True
    _ULTIMO_SLOT[0] = _cargar_slot()
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
