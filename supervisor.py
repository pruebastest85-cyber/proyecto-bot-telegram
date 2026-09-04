"""
Supervisor del BOT LOCAL: lo que hace Railway, pero en tu PC.

- Lanza el bot (migrate_to_pg.py + telegram_bot.py) y lo vigila.
- Cada 5 minutos consulta GitHub; si hay commit nuevo: baja el codigo
  (origin/main MANDA: cualquier edicion local a archivos del repo se
  pisa sin aviso — para probar cambios, subirlos a GitHub), reinstala
  dependencias y reinicia el bot solo.
- Si pip falla a mitad (p. ej. sin red), se reintenta antes de cada
  arranque hasta que pase.
- Si el bot crashea, lo reinicia en 15 segundos.
- El apagado usa terminate(): la base local es SQLite con journal,
  disenada para sobrevivir cortes; el bot ya es resistente a eso.

Lo arranca BOT_LOCAL_ARRANCAR.bat con las variables de bot_local.env ya
cargadas. Para pararlo todo: cierra la ventana.
"""

import os
import subprocess
import sys
import time
from avisos import aviso as _avisar_ex   # (19-AE)

DESTINO = os.path.join(os.path.expanduser("~"), "wallet-edge-local")
CADA_S = 300          # revisar GitHub cada 5 min
REINICIO_S = 15       # espera BASE tras un crash (crece si se repite)
VIDA_CORTA_S = 120    # vivir menos que esto cuenta como muerte al arrancar
ESPERA_MAX_S = 900    # tope del backoff: 15 min entre reintentos
ESPERA_FALLO_S = 1800 # (Ola 18-D) si la actualizacion falla, media hora
                      # antes de VOLVER A INTENTARLA. El bot se relanza
                      # enseguida igualmente: la espera es para no matarlo
                      # y relanzarlo cada 5 min en balde, no para dejarlo
                      # caido.

pip_pendiente = False  # quedo un pip a medias por reintentar
# (Ola 18-D) Ultimo commit por el que ya se aviso de un fallo de
# actualizacion. Sin esto, un repo atascado manda el mismo mensaje de
# Telegram cada 5 minutos — 288 al dia — y deja de leerse.
aviso_fallo_commit = None
# Momento a partir del cual se puede volver a intentar una actualizacion
# que fallo. Es una MARCA DE TIEMPO, no un sleep: dormir aqui dejaria al
# bot apagado, porque para actualizar hay que pararlo primero.
# (Ola 18-M) Todos los intervalos usan el reloj MONOTONICO: la hora de
# pared (time.time) salta en Windows con la sincronizacion NTP o un
# cambio manual — un salto hacia atras convertia "vivio 3 s" en "vivio
# horas" (reseteando el contador anti-bucle) y un salto hacia delante
# aplazaba la proxima actualizacion indefinidamente.
proximo_intento = 0.0
# (19-G) Ultimo commit que arranco BIEN (vivio mas de VIDA_CORTA_S). Es
# el destino del rollback cuando uno nuevo no levanta.
commit_bueno = None
# (19-W) Commit que YA fue rechazado (no compila, o murio dos veces al
# arrancar y hubo rollback). Sin esta memoria, tras el rollback HEAD =
# commit_bueno != origin/main, y a la media hora `hay_actualizacion()`
# volvia a ser cierto: matar el bot bueno, instalar el malo, dos muertes,
# rollback, media hora, otra vez — ~45 vueltas al dia hasta que el dueño
# subiera otro commit, cortando LaserStream (y su cola en vuelo) en cada
# vuelta y mandando dos mensajes por vuelta. Un commit nuevo distinto lo
# desbloquea solo.
commit_rechazado = None
# (19-G) Fichero de instancia unica. Nada impedia arrancar DOS
# supervisores —doble clic en el .bat mas el del arranque de Windows—, y
# dos supervisores son dos bots con el mismo token de Telegram (el
# `Conflict` que ya esta en la tabla `errors`) y dos escritores sobre la
# misma SQLite.
LOCKFILE = os.path.join(DESTINO, ".supervisor.lock")
_lock_fh = None


def _avisar(texto: str) -> None:
    """Aviso best-effort a Telegram. El supervisor es quien mas lo
    necesita: cuando el bot esta en bucle de arranque, nadie mas puede
    avisar (auditoria 19/8: el bucle era silencioso e infinito)."""
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "")
    admin = os.getenv("TELEGRAM_ADMIN_ID", "")
    if not (tok and admin):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": int(admin), "text": texto}, timeout=10)
    except Exception as e:
        print(f"· No pude avisar a Telegram: {e}")


def _git(*args, timeout=90) -> str:
    """Salida de un comando git, o "" si fallo.

    (Ola 18-D) Antes se devolvia `stdout` SIN mirar el codigo de salida ni
    `stderr`: si `git reset --hard` fallaba (un .lock olvidado, permisos,
    el disco lleno), `actualizar()` decia "✅ Codigo en <hash viejo>" y el
    supervisor reiniciaba el bot con el codigo de siempre, cada 5 minutos,
    sin una sola linea que lo delatara. Ahora el fallo se ve y se puede
    comprobar con `_git_ok`.
    """
    try:
        r = subprocess.run(["git", *args], cwd=DESTINO, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"· git {' '.join(args)}: se agoto el tiempo ({timeout}s)")
        return ""
    if r.returncode != 0:
        print(f"· git {' '.join(args)} fallo (codigo {r.returncode}): "
              f"{(r.stderr or '').strip()[:200]}")
        return ""
    return (r.stdout or "").strip()


def _git_ok(*args, timeout=90) -> bool:
    """Como `_git`, pero dice si el comando salio bien (sin salida util)."""
    try:
        r = subprocess.run(["git", *args], cwd=DESTINO, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"· git {' '.join(args)}: se agoto el tiempo ({timeout}s)")
        return False
    if r.returncode != 0:
        print(f"· git {' '.join(args)} fallo (codigo {r.returncode}): "
              f"{(r.stderr or '').strip()[:200]}")
        return False
    return True


def hay_actualizacion() -> bool:
    try:
        if not _git_ok("fetch", "--quiet"):
            # (Ola 18-D) Sin red no se puede traer nada NUEVO, pero puede
            # haber un commit ya descargado en una vuelta anterior que
            # todavia no se ha aplicado. Se sigue comparando: `rev-parse`
            # lee refs locales y no necesita red.
            print("· No pude hablar con GitHub; comparo con lo ya "
                  "descargado")
        aqui = _git("rev-parse", "HEAD")
        alla = _git("rev-parse", "origin/main")
        # (Ola 18-D) Con `_git` devolviendo "" en los fallos, comparar a
        # ciegas daba dos falsos: "" != "abc123" (bucle de actualizacion
        # infinito) y "" == "" (nunca se actualiza, en silencio).
        if not aqui or not alla:
            print("· No pude leer los commits; no toco nada esta vuelta")
            return False
        if alla == commit_rechazado:
            # (19-W) Ese ya se probo y no sirve: no se vuelve a instalar.
            return False
        return aqui != alla
    except Exception as e:
        print(f"· No pude consultar GitHub: {e}")
        return False


def _pip() -> bool:
    # (Ola 18-D) `timeout`: sin el, un pip colgado (red a medias, un
    # indice que no responde) dejaba al supervisor congelado para siempre
    # — ni actualiza ni vigila al bot, y no hay quien lo note.
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                            "requirements.txt", "--quiet"], cwd=DESTINO,
                           timeout=600)
    except subprocess.TimeoutExpired:
        print("· pip install: se agoto el tiempo (10 min)")
        return False
    return r.returncode == 0


def _avisar_una_vez(objetivo, texto):
    """Avisa por Telegram solo la primera vez que falla ESTE commit."""
    global aviso_fallo_commit
    if aviso_fallo_commit == objetivo:
        return
    aviso_fallo_commit = objetivo
    _avisar(texto)


def actualizar() -> bool:
    """Trae el codigo nuevo. Devuelve si de verdad quedo en origin/main."""
    global pip_pendiente, aviso_fallo_commit, proximo_intento
    global commit_rechazado
    print("⬇️  Commit nuevo en GitHub: actualizando...")
    objetivo = _git("rev-parse", "origin/main")
    if not objetivo:
        # Sin saber a donde hay que llegar no se puede comprobar si se
        # llego: mejor no tocar el arbol de trabajo.
        print("⚠️  No pude leer origin/main; no actualizo esta vuelta")
        return False
    if not _git_ok("reset", "--hard", "origin/main"):
        _avisar_una_vez(objetivo,
                        "⚠️ El bot local no pudo actualizarse: `git reset "
                        "--hard` fallo. Sigue corriendo el codigo anterior. "
                        "Mira la ventana del supervisor para ver el motivo.")
        return False
    ahora = _git("rev-parse", "HEAD")
    if ahora != objetivo:
        # El reset dijo que si pero HEAD no se movio: no dar por hecho
        # que el codigo nuevo esta en disco.
        print(f"⚠️  Tras el reset, HEAD sigue en {ahora[:7] or '?'} y "
              f"esperaba {objetivo[:7]}")
        _avisar_una_vez(objetivo,
                        "⚠️ El bot local dice haberse actualizado pero el "
                        "codigo no cambio. Sigue con el commit anterior.")
        return False
    # (19-G) Verificacion ANTES de dar el despliegue por bueno. Si el
    # codigo nuevo no compila, se vuelve al ultimo commit que si
    # arrancaba en vez de entrar en un bucle de reinicios de 15 min sin
    # salida.
    if not compila():
        _avisar(f"⛔ El commit `{objetivo[:7]}` NO COMPILA; no lo "
                f"despliego. " +
                (f"Vuelvo a `{commit_bueno[:7]}`." if commit_bueno
                 else "No tengo un commit bueno conocido al que volver, "
                      "asi que dejo el arbol como esta."))
        if commit_bueno:
            volver_atras(commit_bueno)
        commit_rechazado = objetivo          # (19-W) no reintentarlo
        proximo_intento = time.monotonic() + ESPERA_FALLO_S
        return False
    pip_pendiente = not _pip()
    if pip_pendiente:
        print("⚠️  pip fallo (¿sin red?); se reintenta antes de arrancar")
    # Salio bien: se puede volver a avisar, y se levanta el enfriamiento
    # por si venia puesto desde la rama del crash (si no, un commit nuevo
    # en la siguiente media hora se quedaria esperando sin motivo).
    aviso_fallo_commit = None
    proximo_intento = 0.0
    commit_rechazado = None              # (19-W) este si sirvio
    print(f"✅ Codigo en {_git('rev-parse', '--short', 'HEAD')}")
    return True


def compila() -> bool:
    """(19-G) ¿El codigo que acabamos de bajar al menos COMPILA?

    `actualizar()` hacia `git reset --hard` + pip y devolvia True sin
    comprobar nada. Un `SyntaxError`, un `ImportError` —o un `float()`
    de config que revienta con una errata en el .env— mataba el bot al
    arrancar, y el supervisor entraba en backoff hasta 15 minutos PARA
    SIEMPRE, sin rollback: el unico desbloqueo era subir otro commit. Un
    commit malo un viernes por la noche = produccion caida toda la
    noche, con UN solo aviso (en la muerte numero 3 exacta).

    Esto no garantiza que el bot arranque —para eso esta el rollback por
    muerte temprana—, pero caza gratis el error mas comun. Es lo mismo
    que el CLAUDE.md exige antes de SUBIR y que nadie comprobaba al
    RECIBIR.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "."],
            cwd=DESTINO, capture_output=True, text=True, timeout=300)
    except Exception as e:
        print(f"⚠️  No pude comprobar si compila ({e}); sigo adelante")
        return True          # ante la duda, no bloquear el despliegue
    if r.returncode != 0:
        print("⛔ El codigo nuevo NO compila:")
        print((r.stdout or "")[-600:])
        print((r.stderr or "")[-600:])
        return False
    return True


def volver_atras(destino: str) -> bool:
    """(19-G) Rollback al ultimo commit que si arrancaba."""
    if not destino:
        return False
    print(f"↩️  Volviendo al ultimo commit bueno ({destino[:7]})...")
    if not _git_ok("reset", "--hard", destino):
        print("⚠️  El rollback fallo; sigo con lo que haya")
        return False
    _pip()
    _avisar(f"↩️ El commit nuevo no arrancaba: he vuelto a "
            f"`{destino[:7]}`, que es el ultimo que si funcionaba. El bot "
            f"sigue vivo con ese codigo. Revisa el commit que rompio y "
            f"vuelve a subirlo arreglado.")
    return True


def _leer_env(ruta: str) -> dict:
    """(19-W) Lee `K=V` de un .env sin imprimir ni guardar nada mas.

    El .bat carga bot_local.env UNA vez al arrancar, y `Popen` sin `env=`
    hereda ese entorno congelado: ningun reinicio —ni por commit ni por
    crash— recogia un cambio en el archivo hasta cerrar la ventana y
    volver a lanzar el .bat. Vivido el 30/8 con HELIUS_CYCLE_START_DAY:
    escrito a las 23:40, el bot siguio contando con el valor viejo, y
    ningun mensaje lo decia. Es el mismo sintoma que 19-L/19-O ("el
    mando no hace nada").

    Los VALORES son secretos (tokens, claves): esta funcion no los
    imprime, no los registra y no los manda a ningun sitio; solo los
    devuelve para `Popen(env=...)`.
    """
    salida = {}
    try:
        with open(ruta, encoding="utf-8", errors="replace") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                if linea.startswith("export "):
                    linea = linea[7:].lstrip()
                k, v = linea.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k:
                    salida[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️  No pude leer {os.path.basename(ruta)} ({e}); el bot "
              f"hereda el entorno del supervisor")
    return salida


def _entorno_hijo() -> dict:
    """(19-W) Entorno con el que arranca el bot: el del supervisor mas lo
    que diga bot_local.env AHORA (el archivo manda)."""
    env = dict(os.environ)
    env.update(_leer_env(os.path.join(DESTINO, "bot_local.env")))
    return env


def lanzar() -> subprocess.Popen:
    global pip_pendiente
    if pip_pendiente:
        print("🔁 Reintentando pip install pendiente...")
        pip_pendiente = not _pip()
    # (19-G) `timeout`: git y pip ya lo tenian desde la 18-D, pero la
    # migracion se quedo fuera. Si `psycopg2.connect` se colgara
    # esperando un lock, el supervisor se congelaria aqui — sin bot, sin
    # vigilancia y sin aviso, para siempre.
    try:
        subprocess.run([sys.executable, "migrate_to_pg.py"], cwd=DESTINO,
                       timeout=300)
    except subprocess.TimeoutExpired:
        print("⚠️  migrate_to_pg.py tardo mas de 5 min; sigo sin el "
              "(sin DATABASE_URL no hace nada)")
    except Exception as e:
        print(f"⚠️  migrate_to_pg.py fallo ({e}); sigo")
    print(f"🚀 Bot arrancando (commit {_git('rev-parse', '--short', 'HEAD')})")
    # (19-G) En Windows, GRUPO DE PROCESOS PROPIO. Hace falta para poder
    # mandarle luego un CTRL_BREAK y que muera LIMPIO — ver `parar()`.
    _flags = {}
    if os.name == "nt":
        _flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    # (19-W) `env=`: el bot recoge los cambios del .env en cada arranque.
    return subprocess.Popen([sys.executable, "telegram_bot.py"],
                            cwd=DESTINO, env=_entorno_hijo(), **_flags)


def parar(proc: subprocess.Popen):
    """Para el bot lo mas limpiamente posible.

    (19-G) PRIMERO SE PIDE POR LAS BUENAS. En Windows,
    `Popen.terminate()` es `TerminateProcess`: muerte dura, sin señal,
    sin excepcion y SIN que corra ni un `atexit`. Eso se llevaba por
    delante el bufer de creditos de `api_usage` —hasta 60 s o 25
    eventos— EN CADA DESPLIEGUE, y ese contador es justo el que alimenta
    el freno del 85% de Helius: un subconteo sistematico.
    `_volcar_al_salir` esta registrado con `atexit` y nunca llegaba a
    ejecutarse.

    Con CTRL_BREAK el hijo recibe un KeyboardInterrupt, sale por el
    camino normal del interprete y los `atexit` SI corren. Si en 10 s no
    ha muerto, se sigue con la escalera de siempre.
    """
    _pidio = False
    if os.name == "nt":
        try:
            import signal as _sig
            proc.send_signal(_sig.CTRL_BREAK_EVENT)
            _pidio = True
        except Exception as e:
            print(f"⚠️  No pude mandar CTRL_BREAK ({e}); voy a terminate")
    else:
        try:
            proc.terminate()          # SIGTERM: en POSIX ya es "por las
            _pidio = True             # buenas" y corre atexit
        except Exception as e:
            print(f"⚠️  No pude mandar SIGTERM ({e})")
    if _pidio:
        try:
            proc.wait(timeout=10)
            return                     # murio limpio: bufer volcado
        except subprocess.TimeoutExpired:
            print("⚠️  El bot no murio por las buenas en 10 s; termino")
        except Exception as _ex:
            _avisar_ex("supervisor:parar:386", _ex)
            pass
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        # (19-G) `wait()` DESPUES del kill, sin timeout.
        #
        # Antes se volvia sin esperar al hijo y `lanzar()` arrancaba el
        # nuevo acto seguido: durante esa ventana habia DOS procesos con
        # el mismo token de Telegram —el `Conflict` que ya esta en la
        # tabla `errors`— y DOS escritores sobre la misma SQLite. Pasa
        # cuando el bot tarda mas de 10 s en morir, que es lo normal si
        # esta dentro de una llamada a la IA local (hasta 135 s) o de un
        # `to_thread` bloqueado.
        try:
            proc.wait()
        except Exception as e:
            print(f"⚠️  No pude esperar a que el bot muriera: {e}")


def instancia_unica() -> bool:
    """(19-G) ¿Soy el unico supervisor? True si me quedo con el candado.

    Nada impedia arrancar DOS: el doble clic en `BOT_LOCAL_ARRANCAR.bat`
    mas el del arranque de Windows, o simplemente abrirlo dos veces. Dos
    supervisores son dos `telegram_bot.py` con el mismo token —el
    `Conflict` de Telegram que ya esta en la tabla `errors`— y dos
    escritores sobre la misma SQLite.

    En Windows se usa `msvcrt.locking`; en el resto, `fcntl.flock`. El
    candado lo suelta el sistema operativo al morir el proceso, asi que
    un corte de luz no deja el fichero atascado.
    """
    global _lock_fh
    try:
        _lock_fh = open(LOCKFILE, "a+")
    except Exception as e:
        print(f"⚠️  No pude abrir {LOCKFILE} ({e}); sigo sin candado")
        return True          # ante la duda, no impedir el arranque
    try:
        if os.name == "nt":
            import msvcrt
            _lock_fh.seek(0)
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("=" * 60)
        print(" YA HAY OTRO SUPERVISOR CORRIENDO.")
        print(" Dos supervisores = dos bots con el mismo token de")
        print(" Telegram (Conflict) y dos escritores sobre la misma")
        print(" base. Cierro esta ventana; usa la que ya estaba abierta.")
        print("=" * 60)
        return False
    except Exception as e:
        print(f"⚠️  No pude tomar el candado ({e}); sigo sin el")
        return True
    try:
        _lock_fh.seek(0)
        _lock_fh.truncate()
        _lock_fh.write(f"{os.getpid()}\n")
        _lock_fh.flush()
    except Exception as _ex:
        _avisar_ex("supervisor:instancia_unica:451", _ex)
        pass
    return True


def main():
    global proximo_intento, commit_bueno, commit_rechazado
    if not instancia_unica():
        return
    print("=" * 60)
    print(" SUPERVISOR BOT LOCAL - auto-actualiza desde GitHub cada 5 min")
    print(" GitHub manda: ediciones locales al codigo se pisan solas.")
    print(" Para parar todo: cierra esta ventana.")
    print("=" * 60)
    muertes_seguidas = 0
    while True:
        proc = lanzar()
        nacio = time.monotonic()
        ultimo_chequeo = time.monotonic()
        _marcado_bueno = False
        while True:
            time.sleep(10)
            # (19-G) El commit se considera BUENO cuando el bot lleva
            # vivo mas de VIDA_CORTA_S. Ese es el destino del rollback.
            if (not _marcado_bueno
                    and time.monotonic() - nacio >= VIDA_CORTA_S
                    and proc.poll() is None):
                _h = _git("rev-parse", "HEAD")
                if _h:
                    commit_bueno = _h
                    _marcado_bueno = True
                    print(f"✅ Commit {_h[:7]} marcado como bueno "
                          f"(el bot lleva {VIDA_CORTA_S}s vivo)")
            if proc.poll() is not None:          # el bot murio solo
                vida = time.monotonic() - nacio
                # Backoff exponencial (auditoria 19/8): un commit que
                # rompe el arranque producia un bucle infinito de ~25 s
                # sin aviso — cada vuelta re-corria migraciones y
                # re-golpeaba Telegram/Helius. Vivir >2 min resetea la
                # cuenta (fue una muerte normal, no un bucle).
                muertes_seguidas = (muertes_seguidas + 1
                                    if vida < VIDA_CORTA_S else 0)
                # (19-G) ROLLBACK. Dos muertes tempranas seguidas con el
                # mismo commit significan que ese codigo NO arranca. Sin
                # esto, el supervisor reintentaba con espera creciente
                # hasta 15 min PARA SIEMPRE y el unico desbloqueo era que
                # el dueño subiera otro commit — o sea, produccion caida
                # hasta que alguien lo notara. Se vuelve al ultimo que si
                # vivio, y ahi se queda hasta que llegue un commit nuevo.
                _hoy = _git("rev-parse", "HEAD")
                if (muertes_seguidas >= 2 and commit_bueno
                        and _hoy and _hoy != commit_bueno):
                    if volver_atras(commit_bueno):
                        commit_rechazado = _hoy   # (19-W) no reinstalarlo
                        muertes_seguidas = 0
                        # No se vuelve a intentar la actualizacion
                        # enseguida: si no, dentro de 5 min se detectaria
                        # "commit nuevo" (el malo) y volveria el bucle.
                        proximo_intento = (time.monotonic()
                                           + ESPERA_FALLO_S)
                        time.sleep(REINICIO_S)
                        break
                espera = min(ESPERA_MAX_S,
                             REINICIO_S * (2 ** min(muertes_seguidas, 6)))
                print(f"⚠️  El bot termino (codigo {proc.returncode}, "
                      f"vivio {vida:.0f}s); reinicio en {espera:.0f} s")
                if muertes_seguidas == 3:
                    _avisar("🆘 Bot local en bucle de arranque: 3 muertes "
                            "seguidas con el commit "
                            f"{_git('rev-parse', '--short', 'HEAD')} "
                            f"(codigo {proc.returncode}). Reintento con "
                            "espera creciente hasta 15 min; un commit "
                            "nuevo tambien lo destraba, salvo si acabo de "
                            "fallar una actualizacion (entonces espero "
                            f"{ESPERA_FALLO_S // 60} min antes de mirar "
                            "GitHub otra vez).")
                time.sleep(espera)
                # (Ola 18-D) Tambien aqui se respeta el enfriamiento: si
                # la actualizacion acaba de fallar, cada muerte del bot
                # volvia a intentarla —fetch + reset + rev-parse, hasta
                # 90 s de espera cada uno— retrasando el relanzamiento sin
                # ninguna posibilidad de exito.
                if time.monotonic() >= proximo_intento and hay_actualizacion():
                    if not actualizar():
                        proximo_intento = time.monotonic() + ESPERA_FALLO_S
                break
            if time.monotonic() - ultimo_chequeo >= CADA_S:
                ultimo_chequeo = time.monotonic()
                if time.monotonic() < proximo_intento:
                    continue          # la ultima actualizacion fallo hace poco
                if hay_actualizacion():
                    # PRIMERO parar, DESPUES tocar archivos (19/8): antes
                    # el git reset + pip corrian con el bot viejo VIVO,
                    # que importa perezosamente — podia cargar modulos
                    # del commit nuevo a mitad de vuelo (version mixta,
                    # errores raros que solo pasaban durante un deploy).
                    print("⬇️  Commit nuevo: parando el bot antes de "
                          "actualizar...")
                    parar(proc)
                    # (Ola 18-D) Si la actualizacion NO se aplico, el bot
                    # se relanza con el codigo de siempre — y como HEAD no
                    # se movio, dentro de 5 min se detectaria "commit
                    # nuevo" otra vez: matar y relanzar el bot en bucle,
                    # cada 5 minutos, para siempre. Se espera un rato
                    # largo antes de volver a intentarlo y NO se dice que
                    # arranca con codigo nuevo, porque no es verdad.
                    if actualizar():
                        print("🔄 Arrancando el bot con el codigo nuevo...")
                        muertes_seguidas = 0
                    else:
                        # El bot ya esta parado: se relanza AHORA con el
                        # codigo de siempre. Lo que se aplaza es el
                        # proximo intento de actualizar, no el arranque:
                        # dormir aqui lo dejaria muerto media hora, sin
                        # Telegram, sin webhook y sin trabajos.
                        proximo_intento = time.monotonic() + ESPERA_FALLO_S
                        print(f"🔄 Relanzo el bot con el codigo anterior; "
                              f"no vuelvo a intentar la actualizacion "
                              f"hasta dentro de {ESPERA_FALLO_S // 60} min")
                    break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Supervisor detenido.")
