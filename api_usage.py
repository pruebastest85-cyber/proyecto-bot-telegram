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

from db import get_conn, get_setting
from avisos import aviso as _avisar_ex   # (19-AE)

_BUF: dict = {}
_LAST = [0.0]
_LOCK = threading.Lock()
# (19-AQ) Cuenta de EVENTOS pendientes: el "volcar cada 25" comparaba la
# suma del bufer, que mezcla llamadas (1) y creditos (10-100), asi que un
# solo record("helius_credits", 100) ya volcaba — un INSERT + commit en
# settings por casi cada llamada a Helius desde el hilo de perfilado,
# compitiendo con el tiempo real por el candado de SQLite.
_EVENTOS = [0]
EVENTOS_VOLCADO = 25

# (Ola 15 - M3) El búfer vive en memoria hasta 60 s: sin esto, cada
# reinicio (deploy o excepción) tiraba los conteos pendientes — otro
# subconteo silencioso del gasto real de Helius.
import atexit as _atexit


def _volcar_al_salir():
    # (Ola 16) Sin bloqueo indefinido: los atexit corren ANTES de matar
    # los hilos daemon, y si uno está dentro de record() con el candado
    # tomado el apagado se quedaba colgado hasta el SIGKILL.
    if not _LOCK.acquire(timeout=2):
        return
    _LOCK.release()
    try:
        flush()
    except Exception as _ex:
        _avisar_ex("api_usage:_volcar_al_salir:40", _ex)
        pass


_atexit.register(_volcar_al_salir)


def _key(api: str) -> str:
    return f"api_{api}_" + time.strftime("%Y-%m-%d")


def record(api: str, n: int = 1) -> None:
    """Cuenta n llamadas a la API. Barato: acumula en memoria."""
    try:
        with _LOCK:
            _BUF[api] = _BUF.get(api, 0) + n
            _EVENTOS[0] += 1
            flush_now = (time.time() - _LAST[0] > 60
                         or _EVENTOS[0] >= EVENTOS_VOLCADO)
        if flush_now:
            flush()
    except Exception as _ex:
        _avisar_ex("api_usage:record:60", _ex)
        pass


def _incremento_atomico(conn, k: str, n: int) -> None:
    """Suma n al contador `k` EN LA BASE, sin leer-modificar-escribir.

    (Ola 15 - M3) El patrón viejo (get_setting + set_setting fuera del
    candado) perdía conteos cuando dos hilos volcaban a la vez: siempre
    SUBCONTEO, y como de aquí sale `api_helius_credits_*` — la familia
    que alimenta el freno del 85% — el freno saltaría tarde. El UPSERT
    hace la suma dentro del motor, sin ventana de carrera. Ramas
    explícitas porque el SQL difiere (CLAUDE.md: vale en los dos)."""
    import db as _db
    if getattr(_db, "USE_PG", False):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = "
            "CAST((COALESCE(NULLIF(settings.value, '')::numeric, 0) "
            "+ %s::numeric) AS TEXT)", (k, str(n), str(n)))
    else:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = "
            "CAST(CAST(COALESCE(NULLIF(value, ''), '0') AS REAL) + ? "
            "AS TEXT)", (k, str(n), n))
    conn.commit()


def flush() -> None:
    """Vuelca los contadores acumulados a settings."""
    with _LOCK:
        items = dict(_BUF)
        _BUF.clear()
        _EVENTOS[0] = 0
        _LAST[0] = time.time()
    if not items:
        return
    conn = None
    pendientes = {}
    try:
        conn = get_conn()
        for api, n in items.items():
            try:
                _incremento_atomico(conn, _key(api), n)
            except Exception as e:
                # (Ola 16) Distinguir transitorio de PERMANENTE: si la
                # clave del día tiene basura no numérica, el UPSERT falla
                # SIEMPRE y el búfer crecía sin fin reintentando cada 60 s.
                # Ante un error de datos se sanea la clave y se sigue; solo
                # los fallos de conexión vuelven al búfer.
                _perm = e.__class__.__name__ in (
                    "DataError", "ProgrammingError", "NumericValueOutOfRange",
                    "InvalidTextRepresentation")
                if _perm or "invalid input syntax" in str(e).lower():
                    # (Ola 17-B) Antes esto hacía set_setting(clave, n), o
                    # sea REEMPLAZABA el acumulado del día por el
                    # incremento pendiente (25 o menos). Como de aquí sale
                    # `api_helius_credits_*`, que alimenta el freno del
                    # 85%, un solo ProgrammingError (que en psycopg2 cubre
                    # también "tabla inexistente" o "sin permisos") podía
                    # borrar el consumo del día y desarmar el freno.
                    # Ahora se RESCATA lo que se pueda del valor roto y se
                    # guarda copia del original antes de tocarlo.
                    try:
                        from db import get_setting, set_setting
                        _viejo = get_setting(conn, _key(api), None)
                        _num = 0.0
                        try:
                            _num = float(str(_viejo).strip())
                        except (TypeError, ValueError):
                            _num = 0.0
                        if _viejo is not None and _num == 0.0:
                            # No era un número: se conserva por si acaso.
                            set_setting(conn, _key(api) + "_roto",
                                        str(_viejo)[:200])
                        set_setting(conn, _key(api), str(int(_num) + int(n)))
                        print(f"· api_usage: clave {api} con valor inválido "
                              f"({_viejo!r}); rescatados {int(_num)} y "
                              f"sumados {int(n)} ({e})")
                    except Exception as e2:
                        print(f"· api_usage: no pude sanear {api}: {e2}")
                else:
                    pendientes[api] = n
                    print(f"· api_usage: no pude sumar {api} ({e})")
    except Exception as e:
        pendientes = items
        print(f"· api_usage: volcado fallido ({e})")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _ex:
                _avisar_ex("api_usage:flush:151", _ex)
                pass
    if pendientes:
        # (Ola 15) Lo que no se pudo escribir vuelve al búfer: antes se
        # perdía en silencio.
        with _LOCK:
            for api, n in pendientes.items():
                _BUF[api] = _BUF.get(api, 0) + n


def used_today(conn, api: str) -> int:
    try:
        return int(float(get_setting(conn, _key(api), "0") or 0))
    except Exception as _ex:
        _avisar_ex("api_usage:used_today:164", _ex)
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
        _u = _ia_used(conn)          # (19-AA) None = contador ilegible
        partes.append(f"IA {'?' if _u is None else _u}/{_cap()}")
    except Exception as e:
        print(f"· APIs hoy: no pude leer el contador de IA ({e})")
    return "🔌 APIs hoy: " + " · ".join(partes)
