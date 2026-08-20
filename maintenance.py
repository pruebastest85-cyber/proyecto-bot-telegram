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

from db import get_conn, get_setting, set_setting

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


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
    cuenta como backup."""
    if not (BOT_TOKEN and ADMIN_ID):
        return
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
            with open(path, "rb") as fin, \
                    gzip.open(gz, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
            os.remove(path)
            path, nombre = gz, nombre + ".gz"

        mb = os.path.getsize(path) / 1e6
        if mb > 49:
            cap += (f"\n⚠️ {mb:.1f} MB: cerca del límite de 50 MB de "
                    "Telegram")
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": int(ADMIN_ID), "caption": cap[:1000]},
                files={"document": (nombre, f)},
                timeout=180)
        try:
            os.remove(path)
        except Exception:
            pass
        print(f"📦 Backup verificado enviado por Telegram ({mb:.1f} MB)")
        try:
            conn = get_conn()
            try:
                set_setting(conn, "last_backup_ts", time.time())
            finally:
                conn.close()
        except Exception:
            pass
    except Exception as e:
        print(f"· Backup falló: {e}")
        try:
            from errores import record
            record("backup", e)
        except Exception:
            pass


def watchdog_check():
    """Alerta si no llegan transacciones de Helius hace >12h (máx 1/día)."""
    try:
        from realtime import LAST_HOOK_TS, tracked_addresses, tg_send
        if not tracked_addresses() or LAST_HOOK_TS is None:
            return
        horas = (time.time() - LAST_HOOK_TS) / 3600
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
                "inactivas) o el webhook está caído — corre /ciclo para "
                "resincronizarlo.")
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
        return purgar_bots()
    except Exception as e:
        print(f"· purgar_historial_bots falló: {e}")
        return 0



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
        except Exception:
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
        except Exception:
            pass
        print("🧠 Aprendizaje semanal guardado")
