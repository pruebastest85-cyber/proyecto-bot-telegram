"""
Registro de errores del sistema.

Hasta ahora los ~235 bloques `except` solo hacían print(), es decir: los
errores iban a los logs de Railway y ni el bot ni la IA podían verlos. Si
algo se rompía de madrugada, se perdía salvo que el dueño abriera los logs.

Aquí se guardan en la base para que el chequeo de salud (y la IA) puedan
leerlos, agruparlos y reportarlos.

Regla de oro: registrar un error NUNCA puede provocar otro error. Todo va
envuelto en try/except mudo; si falla el registro, el flujo sigue igual.
"""

import time

from db import get_conn

RETENCION_DIAS = 7        # los errores viejos se purgan solos


def _ensure(conn):
    # Sin AUTOINCREMENT ni SERIAL: compatible con SQLite y Postgres.
    conn.execute("""CREATE TABLE IF NOT EXISTS errors (
                      ts REAL, modulo TEXT, tipo TEXT, mensaje TEXT)""")
    conn.commit()


def record(modulo: str, exc, contexto: str = "") -> None:
    """Guarda un error. Silencioso: nunca lanza excepción."""
    try:
        tipo = type(exc).__name__ if isinstance(exc, BaseException) else "Error"
        msg = f"{contexto}: {exc}" if contexto else str(exc)
        conn = get_conn()
        try:
            _ensure(conn)
            conn.execute(
                "INSERT INTO errors (ts, modulo, tipo, mensaje) "
                "VALUES (?,?,?,?)",
                (time.time(), str(modulo)[:60], tipo[:40], msg[:400]))
            conn.commit()
            # Purga perezosa para que la tabla no crezca sin control
            conn.execute("DELETE FROM errors WHERE ts < ?",
                         (time.time() - RETENCION_DIAS * 86400,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass          # jamás romper el flujo por el registro


def resumen(horas: int = 24) -> list[dict]:
    """Errores de las últimas N horas, agrupados por módulo y tipo."""
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            # (Ola 8, 21/8) MAX(mensaje) era el maximo ALFABETICO del
            # grupo: el "ejemplo" mostrado podia no corresponder al
            # ultimo incidente aunque el "hace X h" si fuera de el.
            # Subconsulta correlacionada: el mensaje del ts mas reciente.
            # (Funciona igual en SQLite y Postgres.)
            rows = conn.execute(
                """SELECT e.modulo, e.tipo, COUNT(*) n, MAX(e.ts) ultimo,
                          (SELECT e2.mensaje FROM errors e2
                           WHERE e2.modulo = e.modulo AND e2.tipo = e.tipo
                             AND e2.ts >= ?
                           ORDER BY e2.ts DESC LIMIT 1) ejemplo
                   FROM errors e WHERE e.ts >= ?
                   GROUP BY e.modulo, e.tipo
                   ORDER BY n DESC""",
                (time.time() - horas * 3600,
                 time.time() - horas * 3600)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def total(horas: int = 24) -> int:
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            r = conn.execute("SELECT COUNT(*) c FROM errors WHERE ts >= ?",
                             (time.time() - horas * 3600,)).fetchone()
            return int(r["c"] or 0)
        finally:
            conn.close()
    except Exception:
        return 0


def errores_text(horas: int = 24) -> str:
    """Listado legible para Telegram."""
    filas = resumen(horas)
    if not filas:
        return f"✅ Sin errores registrados en las últimas {horas} h."
    out = [f"🐞 *Errores ({horas} h)* — {sum(f['n'] for f in filas)} en total\n"]
    for f in filas[:12]:
        hace = (time.time() - (f["ultimo"] or 0)) / 3600
        out.append(f"• *{f['modulo']}* · `{f['tipo']}` ×{f['n']} "
                   f"(hace {hace:.0f} h)")
        ej = (f["ejemplo"] or "")[:110].replace("`", "'")
        if ej:
            out.append(f"   _{ej}_")
    return "\n".join(out)
