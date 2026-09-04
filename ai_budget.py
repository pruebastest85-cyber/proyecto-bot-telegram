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
from avisos import aviso as _avisar_ex   # (19-AE)

_LOCK = threading.Lock()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _cap() -> int:
    try:
        import config
        return int(getattr(config, "AI_DAILY_BUDGET", 300))
    except Exception as _ex:
        _avisar_ex("ai_budget:_cap:35", _ex)
        return 300


def used_today(conn) -> int | None:
    """Llamadas a la nube hoy. None si el contador NO se puede leer.

    (19-AA, auditoria M6) Antes devolvia 0 ante cualquier fallo: con la
    base bloqueada `budget_left` daba 300 (abierto), se llamaba a la
    nube de pago y `record_call` calculaba 0+1 y RESETEABA el contador.
    Justo cuando hay carga —que es cuando SQLite se bloquea— el tope no
    protegia. Ahora "no se" es None, y los de abajo lo tratan como
    cerrado."""
    try:
        # (Ola 16) int(float(...)): si algún día este contador pasa al
        # UPSERT atómico de api_usage, el valor queda como "124.0" e
        # int("124.0") lanza ValueError. Barato blindarlo ahora.
        return int(float(get_setting(conn, "ai_calls_" + _today(), "0")
                         or 0))
    except Exception as e:
        print(f"· Presupuesto IA nube: no pude leer el contador ({e}); "
              f"se trata como agotado")
        return None


def budget_left(conn) -> int:
    """Cero si el contador no se puede leer: no gastar sin contar."""
    usadas = used_today(conn)
    if usadas is None:
        return 0
    return max(0, _cap() - usadas)


def can_call(conn) -> bool:
    """¿Queda presupuesto de IA hoy?

    Consciente del proveedor (18/8/2026): el presupuesto existe para no
    quemar dolares de la NUBE. Con la IA local de titular (ia_proveedor
    distinto de "nube"), este portero nunca frena — la local es gratis y
    el freno de la nube lo aplica el puente solo al intento de nube."""
    try:
        from db import get_setting
        if str(get_setting(conn, "ia_proveedor", "local_primero")
               or "local_primero") != "nube":
            return True
    except Exception as _ex:
        _avisar_ex("ai_budget:can_call:80", _ex)
        pass
    return budget_left(conn) > 0


def record_call(conn, n: int = 1) -> None:
    # (v3) Solo la registra quien de verdad gasto: ia_puente._nube al
    # terminar bien. Los llamadores ya no la invocan.
    try:
        with _LOCK:      # sin lock, dos hilos podian perder conteos
            key = "ai_calls_" + _today()
            usadas = used_today(conn)
            if usadas is None:
                # (19-AA) Sin lectura no hay suma: escribir 0+1 pisaba
                # el contador real del dia.
                print("· Presupuesto IA nube: no pude apuntar la llamada "
                      "(contador ilegible)")
                return
            set_setting(conn, key, usadas + n)
    except Exception as e:
        print(f"· Presupuesto IA nube: no pude apuntar la llamada ({e})")


def status_line(conn) -> str:
    cap = _cap()
    usadas = used_today(conn)
    if usadas is None:
        return f"🤖 IA hoy: ?/{cap} llamadas (contador ilegible: la nube no se usa)"
    return f"🤖 IA hoy: {usadas}/{cap} llamadas · quedan {budget_left(conn)}"
