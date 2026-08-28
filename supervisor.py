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
    pip_pendiente = not _pip()
    if pip_pendiente:
        print("⚠️  pip fallo (¿sin red?); se reintenta antes de arrancar")
    # Salio bien: se puede volver a avisar, y se levanta el enfriamiento
    # por si venia puesto desde la rama del crash (si no, un commit nuevo
    # en la siguiente media hora se quedaria esperando sin motivo).
    aviso_fallo_commit = None
    proximo_intento = 0.0
    print(f"✅ Codigo en {_git('rev-parse', '--short', 'HEAD')}")
    return True


def lanzar() -> subprocess.Popen:
    global pip_pendiente
    if pip_pendiente:
        print("🔁 Reintentando pip install pendiente...")
        pip_pendiente = not _pip()
    subprocess.run([sys.executable, "migrate_to_pg.py"], cwd=DESTINO)
    print(f"🚀 Bot arrancando (commit {_git('rev-parse', '--short', 'HEAD')})")
    return subprocess.Popen([sys.executable, "telegram_bot.py"], cwd=DESTINO)


def parar(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    global proximo_intento
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
        while True:
            time.sleep(10)
            if proc.poll() is not None:          # el bot murio solo
                vida = time.monotonic() - nacio
                # Backoff exponencial (auditoria 19/8): un commit que
                # rompe el arranque producia un bucle infinito de ~25 s
                # sin aviso — cada vuelta re-corria migraciones y
                # re-golpeaba Telegram/Helius. Vivir >2 min resetea la
                # cuenta (fue una muerte normal, no un bucle).
                muertes_seguidas = (muertes_seguidas + 1
                                    if vida < VIDA_CORTA_S else 0)
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
