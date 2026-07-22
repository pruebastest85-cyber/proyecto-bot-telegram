"""
Presupuesto diario de IA: un tope de llamadas a Claude por día.

Regla de oro: antes de llamar a la IA, ¿puede un algoritmo determinista
responder igual de bien? Si sí, no se llama. La IA solo se usa para
CLASIFICAR billeteras, resumir y generar hipótesis — y aun así, con un
presupuesto diario. Cuando se agota:

  · las métricas, scores, DNA, similitud, clusters y rankings siguen
    calculándose normalmente (son código, $0);
  · la clasificación cae al RESPALDO por grading (rentabilidad pura);
  · las tareas menos prioritarias (hipótesis) quedan pendientes;
  · todo se reanuda al día siguiente.

Así el gasto de IA nunca se dispara aunque haya millones de eventos.
El contador se guarda por día en `settings`.
"""

import threading
import time

from db import get_setting, set_setting

_LOCK = threading.Lock()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _cap() -> int:
    try:
        import config
        return int(getattr(config, "AI_DAILY_BUDGET", 300))
    except Exception:
        return 300


def used_today(conn) -> int:
    try:
        return int(get_setting(conn, "ai_calls_" + _today(), "0") or 0)
    except Exception:
        return 0


def budget_left(conn) -> int:
    return max(0, _cap() - used_today(conn))


def can_call(conn) -> bool:
    """¿Queda presupuesto de IA hoy?"""
    return budget_left(conn) > 0


def record_call(conn, n: int = 1) -> None:
    try:
        with _LOCK:      # sin lock, dos hilos podian perder conteos
            key = "ai_calls_" + _today()
            set_setting(conn, key, used_today(conn) + n)
    except Exception:
        pass


def status_line(conn) -> str:
    cap = _cap()
    return f"🤖 IA hoy: {used_today(conn)}/{cap} llamadas · quedan {budget_left(conn)}"
