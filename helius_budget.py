"""
Presupuesto de créditos de Helius.

v2 (Ola 7, 21/8/2026) — CONTADORES HONESTOS. La versión anterior sumaba
DOS veces el mismo gasto: los créditos exactos que apunta el cliente RPC
(familia api_helius_credits_*) MÁS todas las llamadas a 100 créditos
planos (familia api_helius_*), y encima sobre el mes CALENDARIO en vez
del ciclo de facturación. Resultado medido en vivo: /salud decía 2,2 M
cuando el panel de Helius marcaba 328 k — y el freno del 85% podía
saltar con la cuota real casi intacta. Ahora:

  · Solo cuentan los créditos EXACTOS (api_helius_credits_*), que son
    los que el cliente RPC pondera por endpoint y cuadran con el panel.
  · La ventana es el CICLO de facturación, no el mes calendario. El día
    de corte se lee del setting "helius_cycle_day" (editable sin
    reiniciar) o del env HELIUS_CYCLE_START_DAY; por defecto 1.
  · El número del panel de Helius sigue mandando: esto es telemetría
    local para el freno y para /salud, no contabilidad de facturación.
"""

import datetime as _dt
import os

from db import get_conn, get_setting


def _int_env(n, d):
    try:
        return int(os.getenv(n, d))
    except (TypeError, ValueError):
        return d


CUOTA_MENSUAL = _int_env("HELIUS_MONTHLY_CREDITS", 10_000_000)
FRENO_PCT = _int_env("HELIUS_STOP_AT_PCT", 85)   # frenar al 85% de la cuota


def _dia_corte(conn=None) -> int:
    """Día del mes en que renueva el plan de Helius (1-28)."""
    v = None
    try:
        if conn is not None:
            v = get_setting(conn, "helius_cycle_day", None)
    except Exception:
        v = None
    if v is None:
        v = os.getenv("HELIUS_CYCLE_START_DAY", "1")
    try:
        return min(28, max(1, int(float(v))))
    except (TypeError, ValueError):
        return 1


def _inicio_ciclo(conn=None) -> "_dt.date":
    hoy = _dt.date.today()
    corte = _dia_corte(conn)
    if hoy.day >= corte:
        return hoy.replace(day=corte)
    primero = hoy.replace(day=1)
    fin_mes_ant = primero - _dt.timedelta(days=1)
    return fin_mes_ant.replace(day=min(corte, fin_mes_ant.day))


def _fechas_ciclo(conn=None) -> list[str]:
    d = _inicio_ciclo(conn)
    hoy = _dt.date.today()
    out = []
    while d <= hoy:
        out.append(d.strftime("%Y-%m-%d"))
        d += _dt.timedelta(days=1)
    return out


def dia_del_ciclo(conn=None) -> int:
    """Día del ciclo de facturación (1 = día de renovación)."""
    return (_dt.date.today() - _inicio_ciclo(conn)).days + 1


def llamadas_del_ciclo(conn) -> int:
    """Llamadas contadas en lo que va del ciclo (dato informativo)."""
    total = 0
    for f in _fechas_ciclo(conn):
        try:
            total += int(float(get_setting(conn, f"api_helius_{f}", 0) or 0))
        except (TypeError, ValueError):
            pass
    return total


def creditos_usados(conn) -> int:
    """Créditos EXACTOS del ciclo, tal como los apunta el cliente RPC.
    Es la cifra que cuadra con el panel de Helius (con margen pequeño por
    lo que el cliente no ve, p. ej. eventos del websocket)."""
    total = 0
    for f in _fechas_ciclo(conn):
        try:
            total += int(float(
                get_setting(conn, f"api_helius_credits_{f}", 0) or 0))
        except (TypeError, ValueError):
            pass
    return total


def restantes(conn) -> int:
    return max(0, CUOTA_MENSUAL - creditos_usados(conn))


def pct_usado(conn) -> float:
    return 100.0 * creditos_usados(conn) / CUOTA_MENSUAL if CUOTA_MENSUAL else 0


def puede_llamar(conn=None) -> bool:
    """¿Queda margen para seguir llamando a Helius este ciclo?"""
    propia = conn is None
    try:
        if propia:
            conn = get_conn()
        return pct_usado(conn) < FRENO_PCT
    except Exception:
        return True          # ante la duda, no bloquear el sistema
    finally:
        if propia and conn:
            try:
                conn.close()
            except Exception:
                pass


def estado_line(conn) -> str:
    usados = creditos_usados(conn)
    pct = pct_usado(conn)
    dia = dia_del_ciclo(conn)
    proy = (usados / dia * 30) if dia else usados
    ico = "🟢" if pct < 60 else "🟡" if pct < FRENO_PCT else "🔴"
    return (f"{ico} Helius: {usados:,} créditos en {dia} día(s) de ciclo "
            f"({pct:.0f}% de {CUOTA_MENSUAL:,}) · ritmo → ~{proy:,.0f}/mes "
            f"({proy / CUOTA_MENSUAL * 100:.0f}%) · el panel de Helius manda")
