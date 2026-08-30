"""
(19-K) Vaciado de la cola de perfilado de una sentada.

EL PROBLEMA
-----------
`evaluate_tracked` perfila un puñado de billeteras por ciclo. Medido en
la base del dueño el 30/8: **8.647** billeteras en cola, 8.617 de ellas
sin perfilar NUNCA. A 50 por ciclo y un ciclo cada 2 h son 600 al día:
catorce días para vaciarla, y entran nuevas cada día. La cola no baja.

Mientras tanto sobraba presupuesto: 1,5 M de créditos de Helius gastados
de una cuota de 10 M, con el ritmo proyectando 3,4 M para todo el ciclo.
Es decir, la cola no bajaba por un número fijo en la configuración, no
porque faltara dinero.

QUÉ HACE
--------
Trocea la cola en pasadas cortas de `TROZO` billeteras y las va
perfilando hasta que la cola se vacía, salta el freno de presupuesto, se
alcanza el techo de créditos que puso el dueño, o el dueño lo para.

POR QUÉ EN TROZOS Y NO DE UNA
-----------------------------
`cycle_lock` es lo que impide que el ciclo automático y un comando
manual perfilen A LA VEZ (gastando la cuota dos veces y peleando por el
candado de escritura de SQLite). Un vaciado de 8.647 billeteras tarda
horas: quedarse con ese candado tomado todo el rato dejaría al bot sin
ciclo automático, sin /descubrir y sin /analizar durante toda la noche.

Así que el candado se toma y se suelta EN CADA TROZO. Entre trozo y
trozo el ciclo automático puede colarse — y no pasa nada, porque hace
exactamente el mismo trabajo sobre la misma cola.

LO QUE ESTE MÓDULO NO HACE
--------------------------
No decide estrellas por su cuenta ni se salta ninguna puerta: llama al
mismo `evaluate_tracked` de siempre. Perfilar es lo caro y lo lento; la
decisión con el historial ya guardado es gratis y la hace `/promover`.
"""

import threading
import time

TROZO = 25          # billeteras por pasada; el candado se suelta al acabar
ESPERA_CANDADO = 45  # s de espera si el ciclo automático tiene el candado

_lock = threading.Lock()
_estado: dict = {
    "corriendo": False,
    "parar": False,
    "hechas": 0,
    "trozos": 0,
    "inicio": 0.0,
    "creditos_inicio": 0,
    "motivo_fin": "",
    "techo": 0,
}


def estado() -> dict:
    """Copia del estado, para que quien lo lea no pueda tocarlo."""
    with _lock:
        return dict(_estado)


def corriendo() -> bool:
    with _lock:
        return bool(_estado["corriendo"])


def parar() -> bool:
    """Pide el alto. Devuelve False si no había nada corriendo."""
    with _lock:
        if not _estado["corriendo"]:
            return False
        _estado["parar"] = True
        return True


def en_cola(conn) -> int:
    """Cuántas billeteras hay esperando perfilado ahora mismo.

    Es la MISMA condición que usa `evaluate_tracked` para elegir a quién
    perfilar. Está copiada a mano y eso es una deuda conocida: si allí
    cambia la consulta, aquí hay que cambiarla. Se acepta porque la
    alternativa —sacar la consulta a una función compartida— toca el
    camino caliente del ciclo, y hay una prueba que compara los dos
    números para que la copia no se separe en silencio.
    """
    import os
    from datetime import datetime, timedelta, timezone
    try:
        import config as _c
        _min = int(getattr(_c, "MIN_WINNING_TOKENS", 1))
        _s0 = float(getattr(_c, "MIN_BUY_SOL", 1.0))
        _m0 = float(getattr(_c, "MIN_ENTRY_MULTIPLE", 3.0))
    except Exception:
        _min, _s0, _m0 = 1, 1.0, 3.0
    try:
        from ai_analyst import REEVAL_DAYS as _rd
    except Exception:
        _rd = 3
    try:
        _rechazo = int(float(os.getenv("REEVAL_RECHAZADAS_DIAS", "14")))
    except (TypeError, ValueError):
        _rechazo = 14
    ahora = datetime.now(timezone.utc)
    cutoff = (ahora - timedelta(days=_rd)).isoformat(timespec="seconds")
    cutoff_r = (ahora - timedelta(days=_rechazo)).isoformat(timespec="seconds")
    return conn.execute(
        """SELECT COUNT(*) AS c FROM wallets w
           WHERE COALESCE(is_bot,0)=0 AND winning_tokens_count >= ?
             AND (ai_class IS NULL OR pnl_updated IS NULL
                  OR (is_tracked = 1 AND pnl_updated < ?)
                  OR (is_tracked = 0 AND pnl_updated < ?))
             AND EXISTS (SELECT 1 FROM appearances a
                         WHERE a.wallet = w.address
                           AND COALESCE(a.buy_sol, 0) >= ?
                           AND (a.entry_multiple IS NULL
                                OR a.entry_multiple >= ?))""",
        (_min, cutoff, cutoff_r, _s0, _m0)).fetchone()["c"]


def ensayo(conn) -> dict:
    """Números para que el dueño decida ANTES de gastar nada."""
    n = en_cola(conn)
    coste = 300.0
    origen = "estimado"
    usados = restantes = 0
    try:
        import helius_budget as hb
        from ai_analyst import _inicio_ciclo_iso
        usados = hb.creditos_usados(conn)
        restantes = int(hb.CUOTA_MENSUAL * hb.FRENO_PCT / 100.0) - usados
        perf = conn.execute(
            """SELECT COUNT(*) AS c FROM wallets
               WHERE pnl_updated IS NOT NULL AND pnl_updated >= ?""",
            (_inicio_ciclo_iso(conn),)).fetchone()["c"]
        if perf >= 50 and usados > 0:
            coste = max(1.0, usados / float(perf))
            origen = f"medido sobre {perf:,} perfilados de este ciclo"
    except Exception as e:
        print(f"· vaciarcola: no pude medir el presupuesto ({e})")
    return {"en_cola": n, "coste": coste, "coste_origen": origen,
            "total": int(n * coste), "usados": usados,
            "hasta_freno": max(0, restantes),
            "caben": int(max(0, restantes) / coste) if coste else 0}


def _un_trozo(limite: int) -> int:
    """Una pasada con el `cycle_lock` tomado. Devuelve cuántas perfiló,
    o -1 si el candado estaba ocupado (no es un fallo: se reintenta)."""
    import telegram_bot as tb
    from ai_analyst import evaluate_tracked
    from db import get_conn
    if not tb.cycle_lock.acquire(blocking=False):
        return -1
    try:
        conn = get_conn()
        try:
            return evaluate_tracked(conn, limite=limite)
        finally:
            conn.close()
    finally:
        tb.cycle_lock.release()


def _bucle(techo_creditos: int, avisar) -> None:
    from db import get_conn
    motivo = "cola vacía"
    try:
        while True:
            with _lock:
                if _estado["parar"]:
                    motivo = "parado a mano"
                    break
            # Freno de presupuesto: el mismo que respeta el resto del bot.
            try:
                from helius_budget import puede_llamar
                if not puede_llamar():
                    motivo = "freno de presupuesto de Helius (85%)"
                    break
            except Exception:
                pass
            if techo_creditos > 0:
                try:
                    import helius_budget as hb
                    conn = get_conn()
                    try:
                        gastado = (hb.creditos_usados(conn)
                                   - _estado["creditos_inicio"])
                    finally:
                        conn.close()
                    if gastado >= techo_creditos:
                        motivo = (f"alcanzado el techo que pusiste "
                                  f"({techo_creditos:,} créditos)")
                        break
                except Exception:
                    pass
            n = _un_trozo(TROZO)
            if n < 0:
                # El ciclo automático tiene el candado: no es un error,
                # está haciendo el MISMO trabajo. Se espera y se vuelve.
                time.sleep(ESPERA_CANDADO)
                continue
            if n == 0:
                motivo = "cola vacía"
                break
            with _lock:
                _estado["hechas"] += n
                _estado["trozos"] += 1
                hechas, trozos = _estado["hechas"], _estado["trozos"]
            # Un aviso cada 20 trozos (500 billeteras): suficiente para
            # saber que sigue vivo sin inundar el chat en 8 horas.
            if avisar and trozos % 20 == 0:
                try:
                    avisar(f"⏳ Vaciando la cola: {hechas:,} perfiladas…")
                except Exception as e:
                    print(f"· vaciarcola: no pude avisar ({e})")
    except Exception as e:
        motivo = f"cortado por un error: {e}"
        print(f"· vaciarcola: {e}")
    finally:
        with _lock:
            _estado["corriendo"] = False
            _estado["parar"] = False
            _estado["motivo_fin"] = motivo
            hechas = _estado["hechas"]
            tardo = time.time() - _estado["inicio"]
        if avisar:
            try:
                avisar(f"✅ *Vaciado terminado:* {motivo}.\n"
                       f"Perfiladas: *{hechas:,}* en "
                       f"{tardo / 3600:.1f} h.\n"
                       f"Ahora `/promover` decide, gratis, quién se lleva "
                       f"la ⭐ con lo que acabamos de aprender.")
            except Exception as e:
                print(f"· vaciarcola: no pude avisar del final ({e})")


def arrancar(techo_creditos: int = 0, avisar=None) -> bool:
    """Lanza el vaciado en segundo plano. False si ya había uno."""
    from db import get_conn
    with _lock:
        if _estado["corriendo"]:
            return False
        _estado.update({"corriendo": True, "parar": False, "hechas": 0,
                        "trozos": 0, "inicio": time.time(),
                        "motivo_fin": "", "techo": techo_creditos})
    # El contador de partida se lee DESPUÉS de marcar "corriendo": si
    # fallara, el hilo no arrancaría y el estado se quedaría en
    # "corriendo" para siempre, bloqueando cualquier intento futuro.
    try:
        import helius_budget as hb
        conn = get_conn()
        try:
            with _lock:
                _estado["creditos_inicio"] = hb.creditos_usados(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"· vaciarcola: sin contador de partida ({e}); "
              f"el techo de créditos no se podrá aplicar")
    threading.Thread(target=_bucle, args=(techo_creditos, avisar),
                     daemon=True, name="vaciar_cola").start()
    return True
