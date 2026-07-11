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

import config
from db import get_conn, get_setting, set_setting

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def send_db_backup():
    """Envía el archivo de la base de datos por Telegram."""
    if not (BOT_TOKEN and ADMIN_ID):
        return
    try:
        with open(config.DB_PATH, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": int(ADMIN_ID),
                      "caption": "📦 Backup diario de la base de datos. "
                                 "Guárdalo por si acaso."},
                files={"document": ("wallets_backup.db", f)},
                timeout=120)
        print("📦 Backup de la base enviado por Telegram")
    except Exception as e:
        print(f"· Backup falló: {e}")


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


def weekly_learning():
    """Analiza con IA las señales medidas y guarda hallazgos accionables."""
    if not ANTHROPIC_API_KEY:
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
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90)
        r.raise_for_status()
        hallazgos = "".join(b.get("text", "") for b in
                            r.json().get("content", [])).strip()
    except Exception as e:
        conn.close()
        print(f"· Aprendizaje IA falló: {e}")
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
