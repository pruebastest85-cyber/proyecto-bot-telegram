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

import time

from db import get_conn


def resolver_dev(mint: str) -> str | None:
    """El pagador de la PRIMERA transacción del token ≈ su creador.
    None si no se pudo (sin red, historial raro): mejor sin dato."""
    try:
        from helius_rpc import primeras_txs
        txs, _completo = primeras_txs(mint, max_txs=1)
        if txs and txs[0].get("feePayer"):
            return txs[0]["feePayer"]
    except Exception as e:
        print(f"· dev_watch: no pude resolver el dev de {mint[:8]}: {e}")
    return None


def guardar_dev(trade_id: int, mint: str) -> None:
    """Resuelve y guarda el dev de una posición recién abierta.
    Pensado para correr en hilo de fondo: nunca bloquea el camino
    caliente y abre su propia conexión."""
    dev = resolver_dev(mint)
    if not dev:
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
    rows = conn.execute(
        """SELECT id, symbol, mint FROM paper_trades
           WHERE status='abierta' AND dev_wallet=? AND mint=?
             AND COALESCE(dev_alerted, 0) = 0""",
        (trade["wallet"], trade["mint"])).fetchall()
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


def _dev_vendio(dev: str, mint: str) -> bool:
    """¿Hay una transacción reciente del dev sacando ese token?
    Mira sus últimas ~25 txs: un envío/venta del mint DESDE el dev."""
    try:
        from helius_rpc import _rpc
        txs, _tok = _rpc(dev, orden="desc", limite=25)
        for tx in txs or []:
            for tt in tx.get("tokenTransfers") or []:
                if (tt.get("mint") == mint
                        and tt.get("fromUserAccount") == dev
                        and (tt.get("tokenAmount") or 0) > 0):
                    return True
    except Exception as e:
        print(f"· dev_watch: fallo mirando al dev {dev[:8]}: {e}")
    return False


def revisar_devs() -> int:
    """Pasada sobre las posiciones abiertas con dev conocido y sin
    alerta previa. Devuelve cuántas alertas mandó."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, symbol, mint, dev_wallet FROM paper_trades
               WHERE status='abierta' AND dev_wallet IS NOT NULL
                 AND COALESCE(dev_alerted, 0) = 0
               ORDER BY entry_ts DESC LIMIT 15""").fetchall()
    finally:
        conn.close()
    avisadas = 0
    for r in rows:
        if not _dev_vendio(r["dev_wallet"], r["mint"]):
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
