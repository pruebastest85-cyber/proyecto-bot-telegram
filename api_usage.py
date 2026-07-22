"""
Contadores DIARIOS de consumo de APIs externas (Helius, DexScreener,
Birdeye). Junto con ai_budget (llamadas de IA), permiten vigilar desde
/status si el bot se acerca a los límites de cada plan:

  · Helius: plan de 10M créditos/mes (cada request de txs parseadas
    consume créditos; aquí contamos REQUESTS como aproximación).
  · DexScreener: gratuito, ~300 req/min de rate limit.
  · Birdeye: plan gratuito de 30.000 CU/mes.

Diseño: los registros se acumulan en memoria y se vuelcan a la tabla
settings (clave "api_<nombre>_<fecha>") cada ~60 s o cada 25 eventos,
para no abrir una conexión de base por cada llamada de red.
"""

import threading
import time

from db import get_conn, get_setting, set_setting

_BUF: dict = {}
_LAST = [0.0]
_LOCK = threading.Lock()


def _key(api: str) -> str:
    return f"api_{api}_" + time.strftime("%Y-%m-%d")


def record(api: str, n: int = 1) -> None:
    """Cuenta n llamadas a la API. Barato: acumula en memoria."""
    try:
        with _LOCK:
            _BUF[api] = _BUF.get(api, 0) + n
            flush_now = (time.time() - _LAST[0] > 60
                         or sum(_BUF.values()) >= 25)
        if flush_now:
            flush()
    except Exception:
        pass


def flush() -> None:
    """Vuelca los contadores acumulados a settings."""
    with _LOCK:
        items = dict(_BUF)
        _BUF.clear()
        _LAST[0] = time.time()
    if not items:
        return
    try:
        conn = get_conn()
        for api, n in items.items():
            k = _key(api)
            set_setting(conn, k,
                        int(float(get_setting(conn, k, "0") or 0)) + n)
        conn.close()
    except Exception:
        pass


def used_today(conn, api: str) -> int:
    try:
        return int(float(get_setting(conn, _key(api), "0") or 0))
    except Exception:
        return 0


def usage_line(conn) -> str:
    """Línea para /status con el consumo del día."""
    hel = used_today(conn, "helius")
    dex = used_today(conn, "dexscreener")
    bir = used_today(conn, "birdeye")
    partes = [f"Helius {hel} req", f"DexScreener {dex}"]
    if bir:
        partes.append(f"Birdeye {bir}")
    try:
        from ai_budget import used_today as _ia_used, _cap
        partes.append(f"IA {_ia_used(conn)}/{_cap()}")
    except Exception:
        pass
    return "🔌 APIs hoy: " + " · ".join(partes)
