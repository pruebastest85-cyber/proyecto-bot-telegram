"""
Avisos de `except` que antes eran mudos (19-AE).

Regla de la casa: ningún `except Exception:` se traga el error en
silencio. Pero muchos de esos bloques están en caminos calientes (una
lectura de ajuste por señal, un precio por posición) y un `print` por
cada fallo repetido llenaría el log en segundos cuando algo se cae de
verdad. Este módulo imprime UNA vez por etiqueta cada ESPACIADO_S y
cuenta las repeticiones, que salen en el siguiente aviso.

Sin dependencias del proyecto (ni db, ni errores) para poder importarse
desde cualquier archivo sin importaciones circulares. Nunca lanza.
"""

import threading
import time

ESPACIADO_S = 600          # un aviso por etiqueta cada 10 min

_LOCK = threading.Lock()
_ULTIMO: dict = {}         # etiqueta -> (ts del último aviso, repeticiones calladas)


def aviso(etiqueta: str, exc, detalle: str = "") -> None:
    """Imprime `· etiqueta: Tipo: mensaje` con freno por etiqueta."""
    try:
        ahora = time.time()
        with _LOCK:
            ts, calladas = _ULTIMO.get(etiqueta, (0.0, 0))
            if ahora - ts < ESPACIADO_S:
                _ULTIMO[etiqueta] = (ts, calladas + 1)
                return
            _ULTIMO[etiqueta] = (ahora, 0)
        tipo = type(exc).__name__ if isinstance(exc, BaseException) else "Error"
        msg = str(exc)[:160].replace("\n", " ")
        extra = f" [{detalle}]" if detalle else ""
        rep = f" (+{calladas} repetidos callados)" if calladas else ""
        print(f"· {etiqueta}{extra}: {tipo}: {msg}{rep}")
    except Exception:
        pass       # avisar jamás puede romper el flujo


def _reiniciar() -> None:
    """Solo para las pruebas."""
    with _LOCK:
        _ULTIMO.clear()
