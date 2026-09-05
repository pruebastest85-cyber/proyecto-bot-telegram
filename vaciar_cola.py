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
from avisos import aviso as _avisar_ex   # (19-AE)

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
    from datetime import datetime, timedelta, timezone
    try:
        import config as _c
        _min = int(getattr(_c, "MIN_WINNING_TOKENS", 1))
        _s0 = float(getattr(_c, "MIN_BUY_SOL", 1.0))
        _m0 = float(getattr(_c, "MIN_ENTRY_MULTIPLE", 3.0))
    except Exception as _ex:
        _avisar_ex("vaciar_cola:en_cola:96", _ex)
        _min, _s0, _m0 = 1, 1.0, 3.0
    try:
        from ai_analyst import REEVAL_DAYS as _rd, RECHAZO_DIAS as _rechazo
    except Exception as e:
        print(f"· vaciar_cola: no pude leer los plazos de ai_analyst ({e})")
        _rd, _rechazo = 3, 14
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
    vacios = 0          # (19-X) trozos seguidos que no perfilaron a nadie
    try:
        while True:
            with _lock:
                if _estado["parar"]:
                    motivo = "parado a mano"
                    break
            # Freno de presupuesto: el mismo que respeta el resto del bot.
            # (19-X) Las dos guardas de presupuesto FALLAN CERRANDO. Antes
            # eran `except Exception: pass`: si no se podia leer el
            # presupuesto, el vaciado seguia gastando y el techo que puso
            # el dueño dejaba de aplicarse sin ninguna linea en el log —
            # lo contrario de la politica del puente ("si no puedo leer
            # el presupuesto, no gasto"). Parar un vaciado es inofensivo:
            # se relanza (o se reanuda solo tras un reinicio, 19-R).
            try:
                from helius_budget import puede_llamar
                if not puede_llamar():
                    motivo = "freno de presupuesto de Helius (85%)"
                    break
            except Exception as e:
                motivo = f"no pude comprobar el freno de presupuesto ({e})"
                print(f"· vaciarcola: {motivo}; paro por prudencia")
                break
            if techo_creditos > 0:
                try:
                    import helius_budget as hb
                    conn = get_conn()
                    try:
                        _usados_ahora = hb.creditos_usados(conn)
                        with _lock:
                            if _estado["creditos_inicio"] is None:
                                # (19-AQ) partida no leida al arrancar:
                                # se fija ahora, antes de gastar nada.
                                _estado["creditos_inicio"] = _usados_ahora
                            _cred_ini = _estado["creditos_inicio"]
                        gastado = _usados_ahora - _cred_ini
                    finally:
                        conn.close()
                    if gastado >= techo_creditos:
                        motivo = (f"alcanzado el techo que pusiste "
                                  f"({techo_creditos:,} créditos)")
                        break
                except Exception as e:
                    motivo = f"no pude medir el gasto para el techo ({e})"
                    print(f"· vaciarcola: {motivo}; paro por prudencia")
                    break
            n = _un_trozo(TROZO)
            if n < 0:
                # El ciclo automático tiene el candado: no es un error,
                # está haciendo el MISMO trabajo. Se espera y se vuelve.
                time.sleep(ESPERA_CANDADO)
                continue
            if n == 0:
                # (19-X) `evaluate_tracked` devuelve 0 tanto si no habia
                # nadie como si habia 25 y ninguna dio datos (o el filtro
                # de identidad las vacio). Antes cualquier 0 era "cola
                # vacia" y el vaciado se daba por terminado con miles
                # pendientes. Se mira la cola de verdad; si no esta vacia
                # se insiste, y tras tres trozos seguidos en blanco se
                # para diciendo por que.
                pendientes = 0
                try:
                    from db import get_conn as _gcq
                    _cq = _gcq()
                    try:
                        pendientes = en_cola(_cq)
                    finally:
                        _cq.close()
                except Exception as e:
                    print(f"· vaciarcola: no pude contar la cola ({e})")
                if pendientes <= 0:
                    motivo = "cola vacía"
                    break
                vacios += 1
                if vacios >= 3:
                    motivo = (f"tres trozos seguidos sin poder perfilar a "
                              f"nadie ({pendientes:,} siguen en cola: sin "
                              f"datos de Helius o filtradas por identidad)")
                    break
                continue
            vacios = 0
            with _lock:
                _estado["hechas"] += n
                _estado["trozos"] += 1
                hechas, trozos = _estado["hechas"], _estado["trozos"]
                _cred0 = _estado["creditos_inicio"] or 0
            # (19-R) El avance se guarda en CADA trozo. Si el bot se
            # reinicia a mitad, la reanudacion sabe cuantas llevaba y
            # -sobre todo- desde que credito se mide el techo.
            try:
                from db import get_conn as _gc
                _c = _gc()
                try:
                    _guardar(_c, True, techo_creditos, _cred0, hechas)
                finally:
                    _c.close()
            except Exception as _e:
                print(f"· vaciarcola: no pude guardar el avance ({_e})")
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
        # (19-R) Termino DE VERDAD (cola vacia, techo, freno o el dueño
        # lo paro): se apaga la marca para que el proximo arranque NO lo
        # reanude. Si el bot muere por un despliegue no se pasa por aqui,
        # la marca sigue encendida y `reanudar_si_procede` lo continua —
        # que es justo la diferencia que hay que distinguir.
        #
        # EL ORDEN IMPORTA: primero se apaga la marca EN LA BASE y solo
        # despues se suelta el estado en memoria. Al reves habria una
        # rendija (milisegundos, pero real) en la que el vaciado ya
        # figura como terminado mientras la marca sigue encendida: un
        # despliegue justo ahi resucitaria un vaciado ya acabado. Todo
        # el bloque va en try/except, asi que pase lo que pase se llega
        # a soltar "corriendo" y el mando no se queda trabado.
        try:
            from db import get_conn as _gc2
            _c2 = _gc2()
            try:
                _guardar(_c2, False, 0, 0, 0)
            finally:
                _c2.close()
        except Exception as _e:
            print(f"· vaciarcola: no pude apagar la marca ({_e})")
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


def _guardar(conn, activo: bool, techo: int, cred_inicio: int,
             hechas: int) -> None:
    """(19-R) Deja constancia EN LA BASE de que hay un vaciado en curso.

    El vaciado vive en un hilo del bot, y el supervisor reinicia el bot
    en CADA despliegue: el hilo muere en silencio, sin avisar y sin
    dejar rastro. Le paso al dueño el 2/9 — su vaciado se corto cuando
    desplegué la 19-Q y no se entero hasta mirar los numeros. Con
    diecisiete despliegues en una semana, eso es un vaciado cortado
    cada pocas horas.

    `creditos_inicio` se guarda a proposito: el techo de gasto tiene que
    medirse sobre el vaciado ENTERO, no reiniciarse en cada reanudacion.
    Si no, un bot que se reinicie cinco veces gastaria cinco veces el
    techo que puso el dueño — el fallo seria justo el que el techo
    existe para evitar.
    """
    from db import set_setting
    try:
        set_setting(conn, "vaciarcola_activo", "1" if activo else "0")
        if activo:
            set_setting(conn, "vaciarcola_techo", str(int(techo)))
            set_setting(conn, "vaciarcola_cred_inicio", str(int(cred_inicio)))
            set_setting(conn, "vaciarcola_hechas", str(int(hechas)))
        conn.commit()
    except Exception as e:
        print(f"· vaciarcola: no pude guardar el estado ({e})")


def reanudar_si_procede(avisar=None) -> bool:
    """(19-R) Al arrancar el bot: si habia un vaciado en curso, seguir.

    Se llama desde `_post_init`. Solo reanuda si el estado guardado dice
    que estaba activo — o sea, si murio por un reinicio y no porque
    terminara o el dueño lo parara (esos casos apagan la marca).
    """
    from db import get_conn, get_setting
    conn = get_conn()
    try:
        if (get_setting(conn, "vaciarcola_activo", "0") or "0").strip() != "1":
            return False
        try:
            techo = int(float(get_setting(conn, "vaciarcola_techo", "0") or 0))
            cred0 = int(float(get_setting(
                conn, "vaciarcola_cred_inicio", "0") or 0))
            hechas = int(float(get_setting(
                conn, "vaciarcola_hechas", "0") or 0))
        except (TypeError, ValueError):
            techo, cred0, hechas = 0, 0, 0
    finally:
        conn.close()
    if not arrancar(techo_creditos=techo, avisar=avisar,
                    _cred_inicio=cred0, _hechas=hechas):
        return False
    print(f"· vaciarcola: reanudado tras el reinicio "
          f"({hechas:,} ya perfiladas, techo {techo:,})")
    if avisar:
        try:
            avisar(f"🔄 *Vaciado reanudado* tras el reinicio del bot.\n"
                   f"Llevaba *{hechas:,}* perfiladas"
                   + (f" · techo {techo:,} créditos" if techo else "")
                   + ".\n_El gasto se sigue contando desde el principio, "
                     "no desde ahora._")
        except Exception as e:
            print(f"· vaciarcola: aviso de reanudacion perdido ({e})")
    return True


def arrancar(techo_creditos: int = 0, avisar=None,
             _cred_inicio: int | None = None, _hechas: int = 0) -> bool:
    """Lanza el vaciado en segundo plano. False si ya había uno.

    `_cred_inicio` y `_hechas` los usa `reanudar_si_procede`: al
    reanudar hay que CONSERVAR el contador de gasto y el total, no
    empezar de cero.
    """
    from db import get_conn
    with _lock:
        if _estado["corriendo"]:
            return False
        # `creditos_inicio` se pone AQUI a proposito, aunque unas lineas
        # mas abajo se vuelva a calcular: si no se tocara, un vaciado
        # nuevo heredaria en silencio el contador de partida del vaciado
        # ANTERIOR cuando la lectura del presupuesto falle. El techo se
        # mediria entonces desde un numero de hace horas y saltaria (o
        # no saltaria) por motivos invisibles.
        _estado.update({"corriendo": True, "parar": False, "hechas": _hechas,
                        "trozos": 0, "inicio": time.time(),
                        "motivo_fin": "", "techo": techo_creditos,
                        "creditos_inicio": int(_cred_inicio or 0)})
    # El contador de partida se lee DESPUÉS de marcar "corriendo": si
    # fallara, el hilo no arrancaría y el estado se quedaría en
    # "corriendo" para siempre, bloqueando cualquier intento futuro.
    try:
        import helius_budget as hb
        conn = get_conn()
        try:
            with _lock:
                _estado["creditos_inicio"] = (
                    _cred_inicio if _cred_inicio is not None
                    else hb.creditos_usados(conn))
            _guardar(conn, True, techo_creditos,
                     _estado["creditos_inicio"], _hechas)
        finally:
            conn.close()
    except Exception as e:
        # (19-AQ) Antes: "no se podra aplicar" y creditos_inicio en 0, con
        # lo que el bucle medía el gasto del CICLO ENTERO contra el techo y
        # paraba en el primer trozo con "alcanzado el techo que pusiste".
        # Ahora queda en None y el bucle lo mide al arrancar; si no puede,
        # para diciendo por que.
        with _lock:
            _estado["creditos_inicio"] = None
        print(f"· vaciarcola: sin contador de partida ({e}); "
              f"se intentará medir al arrancar el bucle")
    threading.Thread(target=_bucle, args=(techo_creditos, avisar),
                     daemon=True, name="vaciar_cola").start()
    return True
