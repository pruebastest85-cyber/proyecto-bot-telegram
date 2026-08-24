"""
Vigilancia del desarrollador (Ola 12, 21/8/2026).

La señal de rug más fiable que existe es que el CREADOR del token venda.
Los bots de referencia (Trojan, Cielo) la llaman "Dev Sell". Aquí:

  1. Al abrir una posición de paper, se resuelve quién creó el token
     (el pagador de la primera transacción del mint — 1 llamada RPC) y
     se guarda en la fila (`dev_wallet`).
  2. En cada pasada del job de paper (15 min), para cada posición
     abierta se miran las últimas transacciones del dev (1 llamada RPC
     por posición). Si el dev movió/vendió ese token, se alerta UNA vez
     ("🚨 el dev vendió") y se marca `dev_alerted` para no repetir.

Es observación pura: no cierra nada solo — avisa para que decidas tú (y
el dato queda en la fila para que la IA de salidas lo vea en contexto).
Costo Helius: ~1 llamada por posición abierta cada 15 min + 1 al abrir.
"""

import threading
import time

from db import get_conn

# Candado de vuelo unico para la pasada de respaldo (Ola 15): el job del
# paper la lanza en fondo y no debe haber dos a la vez.
_REV_LOCK = threading.Lock()


def resolver_dev(mint: str) -> str | None:
    """El pagador de la PRIMERA transacción del token ≈ su creador.
    None si no se pudo (sin red, historial raro): mejor sin dato."""
    try:
        from helius_rpc import primeras_txs
        txs, _completo = primeras_txs(mint, max_txs=1)
        if txs and txs[0].get("feePayer"):
            return txs[0]["feePayer"]
        # (Ola 17-I) Lista vacia = Helius no contesto (su error ya se
        # capturo una capa mas abajo) o el historial es raro. En ambos
        # casos NO hay dev, y quien llama debe enterarse.
    except Exception as e:
        print(f"· dev_watch: no pude resolver el dev de {mint[:8]}: {e}")
    return None


def guardar_dev(trade_id: int, mint: str) -> None:
    """Resuelve y guarda el dev de una posición recién abierta.
    Pensado para correr en hilo de fondo: nunca bloquea el camino
    caliente y abre su propia conexión.

    (Ola 17-I, auditoria 6) Antes, si no se resolvia el dev, esto hacia
    `return` MUDO — y el `print` de `resolver_dev` tampoco salia, porque
    `helius_rpc._rpc` ya captura los errores de red por dentro y devuelve
    lista vacia. Resultado: esa posicion se quedaba SIN vigilancia
    dev-sell (`revisar_devs` filtra `dev_wallet IS NOT NULL`) y en
    /paper se veia igual que las demas. El propio modulo llama a esto
    "la señal de rug mas fiable que existe".
    """
    dev = resolver_dev(mint)
    if not dev:
        print(f"· dev_watch: NO pude identificar al dev de {mint[:8]}… — "
              f"esta posición queda SIN vigilancia de venta del dev")
        try:
            from errores import record as _rec
            _rec("dev_watch.sin_dev",
                 RuntimeError(f"posición {trade_id}, mint {mint[:12]}…"))
        except Exception:
            pass
        return
    try:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE paper_trades SET dev_wallet=? WHERE id=?",
                (dev, trade_id))
            conn.commit()
        finally:
            conn.close()
        print(f"· dev_watch: dev de {mint[:8]}… = {dev[:8]}…")
        # (Ola 12b) Refrescar la lista vigilada: el dev entra a
        # LaserStream en su proximo chequeo (<=60 s).
        try:
            from realtime import invalidar_vigiladas
            invalidar_vigiladas()
        except Exception:
            pass
    except Exception as e:
        print(f"· dev_watch: no pude guardar el dev: {e}")


def alerta_dev_inmediata(conn, trade: dict) -> int:
    """(Ola 12b) El dev acaba de VENDER en tiempo real (LaserStream o
    webhook): alerta al instante para las posiciones abiertas de ese
    token que aun no avisaron. Devuelve cuantas marco."""
    # (Ola 15) Solo posiciones abiertas ANTES de esta venta: una venta
    # del dev previa a la apertura no es la alarma de ESTA posición.
    rows = conn.execute(
        """SELECT id, symbol, mint FROM paper_trades
           WHERE status='abierta' AND dev_wallet=? AND mint=?
             AND COALESCE(dev_alerted, 0) = 0
             AND entry_ts <= ?""",
        (trade["wallet"], trade["mint"],
         int(trade.get("ts") or 0) or 2**62)).fetchall()
    if not rows:
        return 0
    for r in rows:
        conn.execute("UPDATE paper_trades SET dev_alerted=1 WHERE id=?",
                     (r["id"],))
    conn.commit()
    try:
        from realtime import tg_send
        sym = rows[0]["symbol"] or trade["mint"][:8]
        tg_send(f"🚨 *DEV VENDIÓ (en vivo)* — *{sym}*\n"
                f"El creador del token (`{trade['wallet'][:8]}…`) acaba "
                f"de vender ~{trade.get('sol') or 0:.2f} SOL de su bolsa. "
                f"Suele preceder al desplome inmediato.\n"
                f"Tienes posición de paper abierta: revisa /paper.\n"
                f"`{trade['mint']}`")
    except Exception as e:
        print(f"· dev_watch: no pude avisar en vivo: {e}")
    return len(rows)


def _dev_vendio(dev: str, mint: str, desde_ts: int) -> bool:
    """¿El dev sacó ese token DESPUÉS de `desde_ts`?

    (Ola 15) El filtro temporal es lo que separa la alarma del ruido:
    sin él, el seeding del pool o una venta VIEJA del dev (anteriores a
    abrir la posición) disparaban "DEV VENDIÓ" — 18 falsos positivos
    medidos en la base el 24/8 — y, como la alerta se marca una sola
    vez, la venta REAL posterior ya nunca avisaba."""
    try:
        from helius_rpc import _rpc
        txs, _tok = _rpc(dev, orden="desc", limite=25)
        for tx in txs or []:
            ts = tx.get("timestamp") or 0
            if ts and ts <= desde_ts:
                continue
            for tt in tx.get("tokenTransfers") or []:
                if (tt.get("mint") == mint
                        and tt.get("fromUserAccount") == dev
                        and (tt.get("tokenAmount") or 0) > 0):
                    return True
    except Exception as e:
        # (Ola 17-I) "No vendio" y "no pude comprobarlo" devolvian lo
        # mismo. Se deja rastro para que un fallo sostenido de Helius no
        # parezca calma.
        print(f"· dev_watch: fallo mirando al dev {dev[:8]}: {e}")
        try:
            from errores import record as _rec
            _rec("dev_watch.consulta", e)
        except Exception:
            pass
    return False


def revisar_devs() -> int:
    """Pasada sobre las posiciones abiertas con dev conocido y sin
    alerta previa. Devuelve cuántas alertas mandó."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, symbol, mint, dev_wallet, entry_ts
               FROM paper_trades
               WHERE status='abierta' AND dev_wallet IS NOT NULL
                 AND COALESCE(dev_alerted, 0) = 0
               ORDER BY entry_ts DESC LIMIT 15""").fetchall()
    finally:
        conn.close()
    avisadas = 0
    for r in rows:
        if not _dev_vendio(r["dev_wallet"], r["mint"],
                           int(r["entry_ts"] or 0)):
            time.sleep(0.3)
            continue
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE paper_trades SET dev_alerted=1 WHERE id=?",
                (r["id"],))
            conn.commit()
        finally:
            conn.close()
        try:
            from realtime import tg_send
            tg_send(f"🚨 *DEV VENDIÓ* — *{r['symbol']}*\n"
                    f"El creador del token "
                    f"(`{r['dev_wallet'][:8]}…`) movió/vendió su bolsa. "
                    f"Suele preceder al desplome.\n"
                    f"Tienes posición de paper abierta en este token: "
                    f"revisa /paper.\n`{r['mint']}`")
        except Exception as e:
            print(f"· dev_watch: no pude avisar: {e}")
        avisadas += 1
        time.sleep(0.3)
    if avisadas:
        print(f"🚨 dev_watch: {avisadas} alertas de dev vendiendo")
    return avisadas
