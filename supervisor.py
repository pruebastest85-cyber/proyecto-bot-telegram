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
REINICIO_S = 15       # espera tras un crash

pip_pendiente = False  # quedo un pip a medias por reintentar


def _git(*args, timeout=90) -> str:
    r = subprocess.run(["git", *args], cwd=DESTINO, capture_output=True,
                       text=True, timeout=timeout)
    return (r.stdout or "").strip()


def hay_actualizacion() -> bool:
    try:
        _git("fetch", "--quiet")
        return _git("rev-parse", "HEAD") != _git("rev-parse", "origin/main")
    except Exception as e:
        print(f"· No pude consultar GitHub: {e}")
        return False


def _pip() -> bool:
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        "requirements.txt", "--quiet"], cwd=DESTINO)
    return r.returncode == 0


def actualizar():
    global pip_pendiente
    print("⬇️  Commit nuevo en GitHub: actualizando...")
    _git("reset", "--hard", "origin/main")
    pip_pendiente = not _pip()
    if pip_pendiente:
        print("⚠️  pip fallo (¿sin red?); se reintenta antes de arrancar")
    print(f"✅ Codigo en {_git('rev-parse', '--short', 'HEAD')}")


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
    print("=" * 60)
    print(" SUPERVISOR BOT LOCAL - auto-actualiza desde GitHub cada 5 min")
    print(" GitHub manda: ediciones locales al codigo se pisan solas.")
    print(" Para parar todo: cierra esta ventana.")
    print("=" * 60)
    while True:
        proc = lanzar()
        ultimo_chequeo = time.time()
        while True:
            time.sleep(10)
            if proc.poll() is not None:          # el bot murio solo
                print(f"⚠️  El bot termino (codigo {proc.returncode}); "
                      f"reinicio en {REINICIO_S} s")
                time.sleep(REINICIO_S)
                if hay_actualizacion():
                    actualizar()
                break
            if time.time() - ultimo_chequeo >= CADA_S:
                ultimo_chequeo = time.time()
                if hay_actualizacion():
                    actualizar()
                    print("🔄 Reiniciando el bot con el codigo nuevo...")
                    parar(proc)
                    break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Supervisor detenido.")
