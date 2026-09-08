"""
Registro auditable de decisiones del embudo.
(Embudo v2, fase 5 — 08/09/2026)

POR QUE EXISTE
--------------
El encargo lo pide con estas palabras: "Todas las decisiones importantes
deben quedar registradas y ser auditables". Hoy el bot decide (este token
sirve / esta billetera no) y lo unico que queda es una linea de log que se
pierde al reiniciar. Cuando dentro de un mes el dueño pregunte "¿por que
esta billetera no entro?", la respuesta tiene que salir de la BASE, no de
la memoria de nadie.

Cada fila dice: QUE se estaba mirando (entity_type/entity_id), EN QUE PASO
del embudo (stage), QUE se decidio (decision), POR QUE (reason), con que
NOTA (score), de que DATOS (data_source) y con que VERSION de la
configuracion (config_version) — esto ultimo es lo que permite comparar
dos epocas del bot sin adivinar con que umbrales corria cada una.

BARATO A PROPOSITO
------------------
Escribe una fila y ya. No consulta APIs, no gasta creditos y nunca hace
fallar a quien lo llama: si el registro falla, el trabajo de verdad sigue
adelante (`registrar` se traga su propio error y avisa). Un log que tumba
el proceso al que vigila no sirve de nada.

TAMAÑO
------
`podar` conserva las ultimas EVENTS_MAX filas. La base viaja en el backup
de Telegram, asi que ninguna tabla puede crecer sin freno.
"""

import time

import config
from avisos import aviso as _avisar_ex

# Tope de filas conservadas. Se poda por `id` (autoincremental en SQLite,
# SERIAL en Postgres): las mas viejas son las de id mas bajo en los dos
# motores, asi que la misma consulta vale para ambos.
EVENTS_MAX = int(getattr(config, "ANALYSIS_EVENTS_MAX", 50_000))

# Cada cuantas escrituras se comprueba el tope. Contar filas en cada
# `registrar` seria un COUNT(*) por evento; asi es una vez cada 500.
_PODA_CADA = 500
_ESCRITAS = [0]


def _version() -> str:
    """Version de configuracion vigente, para poder comparar epocas."""
    try:
        return str(getattr(config, "CONFIG_VERSION", "") or "desconocida")
    except Exception as _ex:
        _avisar_ex("analysis_events:_version", _ex)
        return "desconocida"


def registrar(conn, entity_type: str, entity_id: str, stage: str,
              decision: str, reason: str = "", score=None,
              data_source: str = "", model_version: str = "",
              ts: int | None = None) -> bool:
    """Deja constancia de una decision. True si se escribio.

    NUNCA levanta: el registro es un acompañante del trabajo, no una
    condicion para hacerlo. Si la tabla no existe todavia (base vieja sin
    migrar) o el disco falla, se avisa y se sigue.
    """
    try:
        conn.execute(
            """INSERT INTO analysis_events
               (ts, entity_type, entity_id, stage, decision, reason,
                score, data_source, model_version, config_version)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (int(ts if ts is not None else time.time()),
             str(entity_type or "")[:40], str(entity_id or "")[:80],
             str(stage or "")[:40], str(decision or "")[:40],
             str(reason or "")[:400],
             (float(score) if score is not None else None),
             str(data_source or "")[:60], str(model_version or "")[:40],
             _version()))
        conn.commit()
    except Exception as _ex:
        _avisar_ex("analysis_events:registrar", _ex)
        return False
    _ESCRITAS[0] += 1
    if _ESCRITAS[0] % _PODA_CADA == 0:
        podar(conn)
    return True


def registrar_lote(conn, eventos: list[dict]) -> int:
    """Varias decisiones de una tacada (un solo commit). Devuelve cuantas
    se escribieron. Pensado para el analisis de un token, que decide sobre
    cientos de billeteras seguidas: un commit por fila multiplicaria por
    cien el trabajo de disco."""
    if not eventos:
        return 0
    n = 0
    ahora = int(time.time())
    ver = _version()
    for e in eventos:
        try:
            conn.execute(
                """INSERT INTO analysis_events
                   (ts, entity_type, entity_id, stage, decision, reason,
                    score, data_source, model_version, config_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (int(e.get("ts") or ahora),
                 str(e.get("entity_type") or "")[:40],
                 str(e.get("entity_id") or "")[:80],
                 str(e.get("stage") or "")[:40],
                 str(e.get("decision") or "")[:40],
                 str(e.get("reason") or "")[:400],
                 (float(e["score"]) if e.get("score") is not None else None),
                 str(e.get("data_source") or "")[:60],
                 str(e.get("model_version") or "")[:40],
                 ver))
            n += 1
        except Exception as _ex:
            _avisar_ex("analysis_events:registrar_lote", _ex)
    try:
        conn.commit()
    except Exception as _ex:
        _avisar_ex("analysis_events:registrar_lote:commit", _ex)
        return 0
    _ESCRITAS[0] += n
    if _ESCRITAS[0] % _PODA_CADA < n:
        podar(conn)
    return n


def podar(conn) -> int:
    """Deja como mucho EVENTS_MAX filas. Devuelve cuantas se borraron."""
    try:
        r = conn.execute("SELECT COUNT(*) c FROM analysis_events").fetchone()
        total = int(r["c"] if r else 0)
        if total <= EVENTS_MAX:
            return 0
        sobran = total - EVENTS_MAX
        # `id` crece en los dos motores, asi que "mas viejo" = id mas bajo.
        conn.execute(
            """DELETE FROM analysis_events WHERE id IN (
                 SELECT id FROM analysis_events ORDER BY id ASC LIMIT ?)""",
            (sobran,))
        conn.commit()
        return sobran
    except Exception as _ex:
        _avisar_ex("analysis_events:podar", _ex)
        return 0


def historial(conn, entity_id: str, limite: int = 20) -> list[dict]:
    """Las ultimas decisiones sobre una entidad (token o billetera), de
    mas nueva a mas vieja. Es lo que contesta "¿por que esta fuera?"."""
    try:
        filas = conn.execute(
            """SELECT ts, entity_type, entity_id, stage, decision, reason,
                      score, data_source, config_version
               FROM analysis_events WHERE entity_id = ?
               ORDER BY ts DESC, id DESC LIMIT ?""",
            (entity_id, int(limite))).fetchall()
        return [dict(f) for f in filas]
    except Exception as _ex:
        _avisar_ex("analysis_events:historial", _ex)
        return []


def resumen(conn, horas: int = 24) -> list[dict]:
    """Cuantas decisiones de cada tipo en las ultimas `horas`."""
    desde = int(time.time()) - int(horas) * 3600
    try:
        filas = conn.execute(
            """SELECT stage, decision, COUNT(*) n FROM analysis_events
               WHERE ts >= ? GROUP BY stage, decision
               ORDER BY n DESC""", (desde,)).fetchall()
        return [dict(f) for f in filas]
    except Exception as _ex:
        _avisar_ex("analysis_events:resumen", _ex)
        return []
