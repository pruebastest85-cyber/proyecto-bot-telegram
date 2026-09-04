"""
Mantenimiento y aprendizaje del sistema:
  - send_db_backup: manda la base SQLite a tu Telegram (backup diario)
  - watchdog_check: avisa si el webhook de Helius lleva 12h mudo
  - weekly_learning: la IA compara señales ganadoras vs perdedoras,
    guarda los hallazgos (se inyectan a los veredictos futuros) y
    te manda el informe.
"""

import json
import os
import time

import requests

from config import DB_PATH
from db import get_conn, get_setting, set_setting
from avisos import aviso as _avisar_ex   # (19-AE)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def guardar_copia_en_disco(path: str, nombre: str) -> str:
    """Copia el backup junto a la base (carpeta `backups/`) y rota a 5.

    Devuelve la ruta final. (Ola 17-C) Extraida de send_db_backup para
    que el /backup MANUAL la use tambien: hasta ahora el manual solo
    intentaba enviar por Telegram y, con la base en 262 MB, moria sin
    dejar copia en ninguna parte.
    """
    import shutil
    destino_dir = os.path.join(
        os.path.dirname(os.path.abspath(DB_PATH)) or ".", "backups")
    os.makedirs(destino_dir, exist_ok=True)
    destino = os.path.join(destino_dir, nombre)
    shutil.copyfile(path, destino)
    try:                       # rotacion: conservar los 5 más nuevos
        viejos = sorted(
            (os.path.join(destino_dir, f)
             for f in os.listdir(destino_dir)
             if "backup_" in f),
            key=os.path.getmtime, reverse=True)[5:]
        for v in viejos:
            os.remove(v)
    except Exception as e:
        print(f"· Backup: rotación falló (no crítico): {e}")
    return destino


def send_db_backup():
    """Envía un backup VERIFICADO de la base por Telegram.

    v2 (auditoria 19/8): la version anterior copiaba wallets.db byte a
    byte MIENTRAS los hilos escribian — con WAL, la copia salia sin lo
    pendiente del .wal y podia quedar corrupta por un checkpoint a mitad:
    el backup automatico que protege el historico podia ser irrestaurable
    justo cuando hiciera falta. Ahora delega en backup.make_backup() (la
    API de backup de sqlite3, consistente en caliente; en Postgres, el
    volcado JSON por lotes) y ademas VERIFICA la copia con
    PRAGMA integrity_check antes de enviarla: un backup no comprobado no
    cuenta como backup.

    (Ola 17-C, auditoria 4) La guardia de Telegram estaba AQUI, delante de
    todo: sin TELEGRAM_ADMIN_ID no habia copia en disco, ni marca de
    reloj, ni linea en el log, ni error registrado — el respaldo del
    historico dependia de que estuviera bien puesta una variable de
    mensajeria. El respaldo se hace SIEMPRE; lo unico opcional es el
    envio."""
    _puede_telegram = bool(BOT_TOKEN and ADMIN_ID)
    if not _puede_telegram:
        print("· Backup: sin TELEGRAM_BOT_TOKEN/ADMIN_ID; se guarda en "
              "disco igualmente y no se envia por Telegram")

    def _tg_texto(msg: str):
        if not _puede_telegram:
            return
        try:
            from realtime import tg_send
            tg_send(msg)
        except Exception as e2:
            print(f"· Aviso de backup no enviado: {e2}")

    path = None
    try:
        import gzip
        import shutil
        import sqlite3 as _sq

        from backup import make_backup
        path, nombre, cap = make_backup()

        if path.endswith(".db"):
            # Verificacion sobre LA COPIA (no toca la base viva).
            chk = _sq.connect(path)
            try:
                veredicto = chk.execute(
                    "PRAGMA integrity_check").fetchone()[0]
            finally:
                chk.close()
            if veredicto != "ok":
                raise RuntimeError(f"integrity_check: {veredicto}")
            cap += "\n✅ Verificado (integrity_check ok)"
            # Comprimir para Telegram (50 MB por archivo).
            gz = path + ".gz"
            try:
                with open(path, "rb") as fin, \
                        gzip.open(gz, "wb", compresslevel=6) as fout:
                    shutil.copyfileobj(fin, fout)
            except Exception:
                # (19-AC) Un .gz a medias no vale y no debe quedar.
                try:
                    os.remove(gz)
                except OSError:
                    pass
                raise
            os.remove(path)
            path, nombre = gz, nombre + ".gz"

        # ── (24/8) LA COPIA LOCAL ES EL BACKUP; Telegram es un extra. ──
        # Con la base en 262 MB el .gz supero los 50 MB de Telegram y el
        # envio moria con SSLError... y como enviar era el unico destino,
        # el bot se quedo 3 DIAS sin ningun respaldo. Ahora: la copia
        # verificada se guarda SIEMPRE junto a la base (carpeta backups/,
        # rotacion de 5) y el reloj de /salud se marca ahi. El envio a
        # Telegram solo se intenta si cabe, y su fallo ya no borra nada.
        destino = guardar_copia_en_disco(path, nombre)
        conn = get_conn()
        try:
            set_setting(conn, "last_backup_ts", time.time())
        finally:
            conn.close()
        mb = os.path.getsize(path) / 1e6
        print(f"📦 Backup verificado guardado en {destino} ({mb:.1f} MB)")

        if not _puede_telegram:
            pass                       # ya esta a salvo en disco
        elif mb > 49:
            _tg_texto(f"📦 Backup diario ✅ Verificado y guardado en el "
                      f"equipo:\n`{destino}`\n({mb:.1f} MB — demasiado "
                      f"grande para enviarlo por Telegram, que admite "
                      f"50 MB)")
        else:
            try:
                with open(path, "rb") as f:
                    _r = requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}"
                        f"/sendDocument",
                        data={"chat_id": int(ADMIN_ID),
                              "caption": cap[:1000]},
                        files={"document": (nombre, f)},
                        timeout=180)
                # (19-AD) Antes se decia "enviado" sin mirar la respuesta:
                # un 400/413 de Telegram pasaba por exito.
                _r.raise_for_status()
                print("📦 Backup también enviado por Telegram")
            except Exception as e:
                # El backup YA está a salvo en disco: avisar, no fallar.
                print(f"· Envío del backup a Telegram falló: {e}")
                _tg_texto(f"📦 Backup diario ✅ guardado en el equipo:\n"
                          f"`{destino}`\n(el envío por Telegram falló: "
                          f"{str(e)[:120]})")
    except Exception as e:
        print(f"· Backup falló: {e}")
        try:
            from errores import record
            record("backup", e)
        except Exception as _ex:
            _avisar_ex("maintenance:send_db_backup:168", _ex)
            pass
        # (Ola 17-E) Se PROPAGA. Antes se tragaba aqui, asi que el `raise`
        # que la Ola 17-B puso en `backup_job` era letra muerta y
        # `_con_reloj` marcaba `job_ts:db_backup` como EXITO aunque no
        # existiera ninguna copia. De los 7 jobs con reloj, este es el
        # que protege lo unico irrecuperable.
        _fallo_backup = e
    else:
        _fallo_backup = None
    finally:
        # (Ola 17-C) El borrado del temporal estaba DENTRO del try, al
        # final: cualquier fallo intermedio dejaba un .db/.gz de decenas
        # o cientos de MB huerfano en el temporal. Con un reintento cada
        # media hora eso llena el disco, y entonces falla hasta el
        # shutil.copyfile — el fallo se vuelve permanente y se
        # autoalimenta. Ahora se borra pase lo que pase.
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e2:
            print(f"· Backup: no pude borrar el temporal {path}: {e2}")
    if _fallo_backup is not None:
        raise _fallo_backup


def watchdog_check():
    """Alerta si no llegan transacciones de Helius hace >12h (máx 1/día)."""
    try:
        from realtime import LAST_HOOK_TS, tracked_addresses, tg_send
        if not tracked_addresses():
            return
        # (19-AC, auditoria BAJO) En local no hay webhook: LAST_HOOK_TS
        # era None PARA SIEMPRE y el watchdog nunca miraba nada. La
        # señal de vida real es la de LaserStream (`estado()["ultimo"]`);
        # se toma la mas reciente de las dos.
        _ultimo = float(LAST_HOOK_TS or 0)
        try:
            from laserstream import estado as _ls_estado
            _ultimo = max(_ultimo, float(_ls_estado().get("ultimo") or 0))
        except Exception as e:
            print(f"· Watchdog: no pude leer LaserStream ({e})")
        if not _ultimo:
            return
        horas = (time.time() - _ultimo) / 3600
        if horas < 12:
            return
        conn = get_conn()
        ultima = float(get_setting(conn, "last_watchdog_alert", "0") or 0)
        if time.time() - ultima < 86400:
            conn.close()
            return
        set_setting(conn, "last_watchdog_alert", time.time())
        conn.close()
        tg_send(f"⚠️ *Watchdog*: llevo {horas:.0f}h sin recibir "
                "transacciones de Helius. Puede ser normal (billeteras "
                "inactivas) o la conexión en tiempo real está caída — "
                "mira /salud.")
    except Exception as e:
        print(f"· Watchdog falló: {e}")

def purgar_historial_bots() -> int:
    """
    Borra el historial de operaciones de billeteras marcadas como bot.

    Se marca a una billetera como bot DESPUES de perfilarla, asi que sus
    operaciones ya estan guardadas cuando llega el veredicto. Sin esta
    limpieza se acumulan indefinidamente: llegaron a 4,9 millones de filas
    (3,4 GB, el 98% de la tabla) y llenaron el volumen de Postgres al 100%,
    tumbando la base entera. La clasificacion se conserva en wallets.is_bot.
    """
    try:
        from trades_store import purgar_bots
        n = purgar_bots()
    except Exception as e:
        print(f"· purgar_historial_bots falló: {e}")
        n = 0
    # Misma pasada diaria: poda de posiciones cerradas y frias (v2,
    # auditoria 19/8 — positions crecia sin tope y el JOIN de actividad
    # del conjunto operativo la recorre en cada refresco).
    try:
        from db import get_conn as _gc, purgar_posiciones_muertas
        _c = _gc()
        try:
            purgar_posiciones_muertas(_c)
        finally:
            _c.close()
    except Exception as e:
        print(f"· Poda de positions omitida: {e}")
    return n



def weekly_learning():
    """Analiza con IA las señales medidas y guarda hallazgos accionables."""
    if not __import__("ia_puente").hay_ia():
        return
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        """SELECT s.symbol, s.mc, s.liq, s.sol, s.chg_1h, s.chg_24h,
                  s.verdict, s.signal_score, w.alias, w.wallet_score
           FROM signals s LEFT JOIN wallets w ON w.address = s.wallet
           WHERE s.side='compra'
             AND (s.chg_24h IS NOT NULL OR s.chg_1h IS NOT NULL)
           ORDER BY s.ts DESC LIMIT 60""").fetchall()]
    if len(rows) < 5:
        conn.close()
        print("· Aprendizaje: aún no hay suficientes señales medidas")
        return
    prompt = (
        "Eres el analista de un sistema de señales de billeteras en Solana. "
        "Estas son las señales de compra medidas (chg_1h/chg_24h = % del "
        "token tras la señal; verdict = lo que la IA recomendó; "
        "signal_score = score calculado). Analiza:\n"
        "1. ¿Qué separa a las ganadoras de las perdedoras? (MC, liquidez, "
        "monto, billetera, score)\n"
        "2. ¿Los veredictos 'entrar' acertaron más que los 'evitar'?\n"
        "3. ¿El signal_score alto predijo mejores resultados?\n"
        "Da 3-5 HALLAZGOS accionables, cortos, en español.\n\n"
        f"DATOS: {json.dumps(rows, ensure_ascii=False, default=str)}")
    try:
        # Puente de IA (18/8/2026): la LOCAL es titular; la nube, opcional.
        from ia_puente import completar
        hallazgos = (completar(prompt, max_tokens=600, timeout=120)
                     or "").strip()
        if not hallazgos:
            raise RuntimeError("IA no disponible (ni local ni nube)")
    except Exception as e:
        conn.close()
        print(f"· Aprendizaje IA falló: {e}")
        try:
            from errores import record
            record("weekly_learning", e)
        except Exception as _ex:
            _avisar_ex("maintenance:weekly_learning:301", _ex)
            pass
        return
    if not hallazgos:
        conn.close()
        return
    if hallazgos:
        set_setting(conn, "learnings", hallazgos[:2000])
        conn.close()
        try:
            from realtime import tg_send
            tg_send("🧠 *Informe semanal de aprendizaje* "
                    f"({len(rows)} señales analizadas):\n\n{hallazgos[:3000]}"
                    "\n\n_Estos hallazgos ya se inyectan a los veredictos "
                    "de las próximas señales._")
        except Exception as _ex:
            _avisar_ex("maintenance:weekly_learning:316", _ex)
            pass
        print("🧠 Aprendizaje semanal guardado")
