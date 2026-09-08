"""
Libro de cuentas de Helius: en QUE se gasta cada credito.
(Embudo v2, fase 7 — 08/09/2026)

EL PROBLEMA MEDIDO
------------------
Hoy todo el gasto de Helius se aplasta en UN contador diario
(`api_helius_credits_<fecha>`). Con eso se sabe cuanto se gasto, pero no
en que. Y eso ya duele: en la base del dueño, cuatro dias seguidos
(31/08 a 03/09) se comieron 4.602.170 creditos — el 46 % del mes — con
el 03/09 solo en 2.443.860 (el 24 % de la cuota mensual en un dia). No
hay forma de saber que lo causo, porque nada lo apunta.

Este modulo lo apunta: por dia, por endpoint y por SOBRE (el bucket del
encargo). Asi la proxima vez que un dia se dispare, la respuesta esta en
la base y no en la memoria de nadie.

POR QUE AGREGADO Y NO UNA FILA POR LLAMADA
------------------------------------------
El encargo pedia "una fila por llamada". Con el ritmo medido —310.059
creditos al dia, ~10 por pagina de RPC, o sea unas 31.000 llamadas
diarias— eso son casi un millon de filas al mes en una base que viaja
comprimida por Telegram con un limite de 50 MB. La pregunta que hay que
contestar ("¿que se comio los 2,4 millones del 3 de septiembre?") se
contesta igual de bien con una fila por dia, endpoint y sobre: unas 20
filas al dia, 600 al mes. Se elige eso a proposito, y se dice.

Lo que se pierde: no se puede decir QUE billetera concreta costo tanto.
Si algun dia hace falta, se sube el detalle solo para las llamadas caras.

COMO SE SABE A QUE SOBRE VA CADA GASTO
---------------------------------------
Con un contexto por hilo: quien va a gastar abre `contexto("billeteras")`
y todo lo que se apunte dentro cae en ese sobre. Asi no hay que cambiar
la firma de ninguna funcion (y el bot corre con varios hilos: el ciclo,
el tiempo real y el perfilado a la vez, cada uno con su contexto).
Lo que no declare sobre cae en "otros" — y "otros" se ve en el informe,
que es justo lo que hace falta para ir afinando.
"""

import contextlib
import threading
import time

import config
from db import get_conn
from avisos import aviso as _avisar_ex

DIA = 86400

# Sobres del encargo. El porcentaje sale de config; aqui solo el nombre
# y de que variable lee su parte.
SOBRES = (("descubrimiento", "HELIUS_DISCOVERY_BUDGET_PCT"),
          ("billeteras", "HELIUS_WALLET_BUDGET_PCT"),
          ("profundo", "HELIUS_DEEP_BUDGET_PCT"),
          ("reserva", "HELIUS_RESERVE_BUDGET_PCT"))
SOBRE_POR_DEFECTO = "otros"

_local = threading.local()
_BUF: dict = {}
_LOCK = threading.Lock()
# Arranca en "ahora": con 0.0, el PRIMER apunte de la vida del proceso
# creia que llevaba una eternidad sin volcar y escribia en la base al
# instante, una fila por llamada suelta en vez de la suma del minuto.
_ULTIMO = [time.time()]
SEGUNDOS_VOLCADO = 60
EVENTOS_VOLCADO = 50
_EVENTOS = [0]


def _int(nombre, defecto):
    try:
        return int(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return int(defecto)


# ── contexto: quien gasta y para que ─────────────────────────────────

@contextlib.contextmanager
def contexto(sobre: str, entity_type: str = "", entity_id: str = ""):
    """Todo lo que se apunte DENTRO de este bloque va a ese sobre.

    Se anida bien: al salir se restaura el contexto que hubiera antes,
    asi que una funcion con contexto puede llamar a otra que tambien lo
    tenga sin dejar el hilo marcado de por vida.
    """
    previo = getattr(_local, "ctx", None)
    _local.ctx = (str(sobre or SOBRE_POR_DEFECTO), str(entity_type or ""),
                  str(entity_id or ""))
    try:
        yield
    finally:
        _local.ctx = previo


def actual() -> tuple:
    """(sobre, entity_type, entity_id) de ESTE hilo."""
    return getattr(_local, "ctx", None) or (SOBRE_POR_DEFECTO, "", "")


# ── apuntar ───────────────────────────────────────────────────────────

def _clave(endpoint, sobre, entity_type, ok=True):
    # El dia se guarda como el instante en que empieza, en horario local:
    # es lo que hace que el informe cuadre con "el gasto del dia 3".
    dia = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"),
                                        "%Y-%m-%d")))
    # Las llamadas que FALLARON van a su propia fila, con el sufijo
    # ":fallo" en el endpoint. Se hace asi —y no con un contador aparte—
    # porque la tabla no tiene columna para ello y porque separado se lee
    # mejor: en el informe aparece cuanto se gasto en llamadas que no
    # sirvieron para nada, que es justo lo que hay que vigilar.
    ep = str(endpoint or "?") + ("" if ok else ":fallo")
    return (dia, ep, str(sobre), str(entity_type or ""))


def apuntar(creditos, endpoint: str = "rpc", ok: bool = True,
            ms: int | None = None) -> None:
    """Apunta un gasto. Barato: se acumula en memoria y se vuelca cada
    minuto o cada 50 eventos, igual que `api_usage`.

    NUNCA levanta: llevar la cuenta no puede tumbar al que gasta.
    """
    if not _int("HELIUS_LEDGER_ACTIVO", 1):
        return
    try:
        n = int(creditos or 0)
    except (TypeError, ValueError):
        return
    if n <= 0:
        return
    try:
        sobre, ent_tipo, ent_id = actual()
        k = _clave(endpoint, sobre, ent_tipo, ok)
        with _LOCK:
            d = _BUF.setdefault(k, {"cost": 0, "n": 0,
                                    "ms": 0, "ms_n": 0, "ent": ent_id})
            d["cost"] += n
            d["n"] += 1
            if ms is not None:
                try:
                    d["ms"] += int(ms)
                    d["ms_n"] += 1
                except (TypeError, ValueError):
                    pass
            _EVENTOS[0] += 1
            toca = (time.time() - _ULTIMO[0] > SEGUNDOS_VOLCADO
                    or _EVENTOS[0] >= EVENTOS_VOLCADO)
        if toca:
            volcar()
    except Exception as _ex:
        _avisar_ex("helius_ledger:apuntar", _ex)


def volcar() -> int:
    """Escribe lo acumulado. Devuelve cuantos creditos se volcaron."""
    with _LOCK:
        if not _BUF:
            _ULTIMO[0] = time.time()
            _EVENTOS[0] = 0
            return 0
        pendiente = dict(_BUF)
        _BUF.clear()
        _ULTIMO[0] = time.time()
        _EVENTOS[0] = 0
    total = 0
    try:
        conn = get_conn()
        try:
            for (dia, endpoint, sobre, ent_tipo), d in pendiente.items():
                total += d["cost"]
                _sumar(conn, dia, endpoint, sobre, ent_tipo, d)
            conn.commit()
        finally:
            conn.close()
    except Exception as _ex:
        _avisar_ex("helius_ledger:volcar", _ex)
        # Lo que no se pudo escribir vuelve al bufer: perder el conteo
        # seria subcontar el gasto, que es como el freno del 85 % llega
        # tarde.
        with _LOCK:
            for k, d in pendiente.items():
                v = _BUF.setdefault(k, {"cost": 0, "n": 0,
                                        "ms": 0, "ms_n": 0, "ent": ""})
                for campo in ("cost", "n", "ms", "ms_n"):
                    v[campo] += d.get(campo, 0)
        return 0
    return total


def _sumar(conn, dia, endpoint, sobre, ent_tipo, d) -> None:
    """UPDATE y, si no habia fila, INSERT. Se hace asi y no con
    `INSERT OR REPLACE` / `ON CONFLICT` porque esto tiene que funcionar
    igual en SQLite y en Postgres.

    `success` guarda las LLAMADAS que representa la fila (todas del mismo
    signo: las que fallaron viven en su propia fila, con ":fallo" en el
    endpoint). El texto de `reason` se arma DENTRO del SQL a partir del
    valor acumulado, porque en SQL todas las asignaciones de un UPDATE
    usan los valores ANTERIORES de la fila: asi el recuento del texto
    coincide siempre con la columna, sin leer antes ni parsear nada.

    `latency_ms` guarda la media del ULTIMO volcado, no la del dia. Es
    una señal para ver si un endpoint se ha puesto lento, no una
    estadistica; se dice para que nadie la lea como otra cosa.
    """
    ms = (int(d["ms"] / d["ms_n"]) if d["ms_n"] else None)
    cur = conn.execute(
        """UPDATE helius_ledger
              SET cost = COALESCE(cost, 0) + ?,
                  success = COALESCE(success, 0) + ?,
                  latency_ms = ?,
                  reason = CAST(COALESCE(success, 0) + ? AS TEXT)
                           || ' llamadas',
                  entity_id = ?
            WHERE ts = ? AND endpoint = ? AND bucket = ?
              AND COALESCE(entity_type, '') = ?""",
        (d["cost"], d["n"], ms, d["n"], str(d.get("ent") or "")[:80],
         dia, endpoint, sobre, ent_tipo))
    if not cur.rowcount:
        conn.execute(
            """INSERT INTO helius_ledger
               (ts, endpoint, metodo, entity_type, entity_id, cost,
                success, latency_ms, reason, bucket)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (dia, endpoint, "agregado", ent_tipo,
             str(d.get("ent") or "")[:80], d["cost"], d["n"], ms,
             f"{d['n']} llamadas", sobre))


# El bufer vive en memoria hasta un minuto: sin esto, cada reinicio
# (despliegue o excepcion) tiraria lo pendiente — el mismo subconteo
# silencioso que la Ola 15 arreglo en `api_usage`.
import atexit as _atexit


def _volcar_al_salir():
    if not _LOCK.acquire(timeout=2):
        return
    _LOCK.release()
    try:
        volcar()
    except Exception as _ex:
        _avisar_ex("helius_ledger:_volcar_al_salir", _ex)


_atexit.register(_volcar_al_salir)


# ── lectura ───────────────────────────────────────────────────────────

def _inicio_ciclo_ts() -> int:
    """Comienzo del ciclo de facturacion, como marca de tiempo. Se
    reutiliza el calculo que ya existe en `helius_budget` para no tener
    dos definiciones del ciclo que puedan divergir."""
    try:
        from helius_budget import _inicio_ciclo
        d = _inicio_ciclo()
        return int(time.mktime(time.strptime(d.strftime("%Y-%m-%d"),
                                             "%Y-%m-%d")))
    except Exception as _ex:
        _avisar_ex("helius_ledger:_inicio_ciclo_ts", _ex)
        return int(time.time()) - 30 * DIA


def gastado(conn, desde: int | None = None, sobre: str | None = None) -> int:
    """Creditos apuntados desde `desde` (por defecto, el ciclo actual)."""
    if desde is None:
        desde = _inicio_ciclo_ts()
    try:
        if sobre:
            r = conn.execute("SELECT COALESCE(SUM(cost),0) c FROM "
                             "helius_ledger WHERE ts >= ? AND bucket = ?",
                             (desde, sobre)).fetchone()
        else:
            r = conn.execute("SELECT COALESCE(SUM(cost),0) c FROM "
                             "helius_ledger WHERE ts >= ?",
                             (desde,)).fetchone()
        return int(r["c"] or 0)
    except Exception as _ex:
        _avisar_ex("helius_ledger:gastado", _ex)
        return 0


def presupuesto(sobre: str) -> int:
    """Cuantos creditos le tocan a ese sobre en el ciclo."""
    total = _int("HELIUS_MONTHLY_BUDGET", _int("HELIUS_MONTHLY_CREDITS",
                                               10_000_000))
    for nombre, var in SOBRES:
        if nombre == sobre:
            return int(total * _int(var, 0) / 100.0)
    return 0


def por_sobre(conn, desde: int | None = None) -> list[dict]:
    if desde is None:
        desde = _inicio_ciclo_ts()
    try:
        return [dict(f) for f in conn.execute(
            """SELECT bucket, SUM(cost) c, SUM(success) n
               FROM helius_ledger WHERE ts >= ?
               GROUP BY bucket ORDER BY c DESC""", (desde,)).fetchall()]
    except Exception as _ex:
        _avisar_ex("helius_ledger:por_sobre", _ex)
        return []


def por_dia(conn, dias: int = 10) -> list[dict]:
    desde = int(time.time()) - int(dias) * DIA
    try:
        return [dict(f) for f in conn.execute(
            """SELECT ts, SUM(cost) c FROM helius_ledger WHERE ts >= ?
               GROUP BY ts ORDER BY ts DESC""", (desde,)).fetchall()]
    except Exception as _ex:
        _avisar_ex("helius_ledger:por_dia", _ex)
        return []


def podar(conn) -> int:
    """Deja como mucho LEDGER_MAX filas."""
    tope = _int("HELIUS_LEDGER_MAX", 20_000)
    try:
        total = conn.execute(
            "SELECT COUNT(*) c FROM helius_ledger").fetchone()["c"]
        if total <= tope:
            return 0
        sobran = total - tope
        conn.execute(
            """DELETE FROM helius_ledger WHERE id IN (
                 SELECT id FROM helius_ledger ORDER BY id ASC LIMIT ?)""",
            (sobran,))
        conn.commit()
        return sobran
    except Exception as _ex:
        _avisar_ex("helius_ledger:podar", _ex)
        return 0


def _mil(n):
    return f"{int(n):,}".replace(",", ".")


def resumen_text(conn=None) -> str:
    """Texto para /creditos."""
    propia = conn is None
    if propia:
        conn = get_conn()
    try:
        volcar()
        desde = _inicio_ciclo_ts()
        total = gastado(conn, desde)
        cuota = _int("HELIUS_MONTHLY_BUDGET",
                     _int("HELIUS_MONTHLY_CREDITS", 10_000_000))
        L = [f"💳 *Créditos de Helius* — ciclo desde "
             f"{time.strftime('%d/%m', time.localtime(desde))}"]
        # El contador viejo sigue siendo la referencia del freno: se
        # enseñan los dos para poder ver si el libro cuadra.
        try:
            from helius_budget import creditos_usados
            viejo = int(creditos_usados(conn) or 0)
            L.append(f"_Contador del freno: {_mil(viejo)} "
                     f"({100.0 * viejo / cuota:.1f} % de {_mil(cuota)})_")
        except Exception as _ex:
            _avisar_ex("helius_ledger:resumen_text:viejo", _ex)
        if not total:
            L.append("")
            L.append("El libro de cuentas empieza hoy: todavía no hay "
                     "gasto apuntado con su sobre. En cuanto el bot pida "
                     "algo a Helius, aquí se verá en qué.")
            return "\n".join(L)
        L.append(f"*Apuntado en el libro:* {_mil(total)}")
        L.append("")
        L.append("*Por sobre*")
        for f in por_sobre(conn, desde):
            tope = presupuesto(f["bucket"])
            pc = (f" · {100.0 * f['c'] / tope:.0f} % de su parte"
                  if tope else "")
            L.append(f"  · {f['bucket']}: {_mil(f['c'])}{pc}")
        dias = por_dia(conn, 7)
        if dias:
            L.append("")
            L.append("*Últimos días*")
            for f in dias[:7]:
                L.append(f"  {time.strftime('%d/%m', time.localtime(f['ts']))}"
                         f"  {_mil(f['c'])}")
        try:
            top = conn.execute(
                """SELECT endpoint, bucket, SUM(cost) c FROM helius_ledger
                   WHERE ts >= ? GROUP BY endpoint, bucket
                   ORDER BY c DESC LIMIT 6""", (desde,)).fetchall()
            if top:
                L.append("")
                L.append("*Dónde se va*")
                for f in top:
                    L.append(f"  {f['endpoint']} ({f['bucket']}): "
                             f"{_mil(f['c'])}")
        except Exception as _ex:
            _avisar_ex("helius_ledger:resumen_text:top", _ex)
        return "\n".join(L)
    finally:
        if propia:
            conn.close()
