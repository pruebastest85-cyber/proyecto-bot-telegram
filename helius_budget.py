"""
Presupuesto de créditos de Helius.

Cada llamada a la Enhanced Transactions API cuesta 100 CRÉDITOS (no 1).
Con el plan Developer (10M créditos/mes) eso son ~100.000 llamadas al mes.

En vez de adivinar topes conservadores, se mide el consumo real y se frena
solo cuando de verdad hay riesgo. Así los topes pueden ser generosos: si
hay material que analizar se aprovecha la cuota, y si un mes hay
muchísima actividad el freno evita quedarse ciego a mitad de mes.

El contador se apoya en api_usage (que ya cuenta las llamadas por día).
"""

import os
import time

from db import get_conn, get_setting


def _int_env(n, d):
    try:
        return int(os.getenv(n, d))
    except (TypeError, ValueError):
        return d


CREDITOS_POR_LLAMADA = _int_env("HELIUS_CREDITS_PER_CALL", 100)
CUOTA_MENSUAL = _int_env("HELIUS_MONTHLY_CREDITS", 10_000_000)
FRENO_PCT = _int_env("HELIUS_STOP_AT_PCT", 85)   # frenar al 85% de la cuota


def _dia_ciclo() -> int:
    """Día del ciclo de facturación (aprox: día del mes)."""
    return int(time.strftime("%d"))


def llamadas_del_mes(conn) -> int:
    """Suma las llamadas a Helius contadas en lo que va de mes."""
    total = 0
    mes = time.strftime("%Y-%m")
    try:
        for d in range(1, 32):
            v = get_setting(conn, f"api_helius_{mes}-{d:02d}", None)
            if v:
                try:
                    total += int(float(v))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return total


def creditos_reales_del_mes(conn) -> int:
    """Créditos contabilizados exactamente (los apunta el cliente RPC)."""
    total = 0
    mes = time.strftime("%Y-%m")
    try:
        for d in range(1, 32):
            v = get_setting(conn, f"api_helius_credits_{mes}-{d:02d}", None)
            if v:
                try:
                    total += int(float(v))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return total


def creditos_usados(conn) -> int:
    """Créditos del mes: los exactos del RPC + los estimados de la API vieja
    (100 por llamada). Así el freno sigue siendo correcto durante la
    transición entre ambos métodos."""
    return (creditos_reales_del_mes(conn)
            + llamadas_del_mes(conn) * CREDITOS_POR_LLAMADA)


def restantes(conn) -> int:
    return max(0, CUOTA_MENSUAL - creditos_usados(conn))


def pct_usado(conn) -> float:
    return 100.0 * creditos_usados(conn) / CUOTA_MENSUAL if CUOTA_MENSUAL else 0


def puede_llamar(conn=None) -> bool:
    """¿Queda margen para seguir llamando a Helius este mes?"""
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
    dia = _dia_ciclo()
    proy = (usados / dia * 30) if dia else usados
    ico = "🟢" if pct < 60 else "🟡" if pct < FRENO_PCT else "🔴"
    return (f"{ico} Helius: {usados:,} de {CUOTA_MENSUAL:,} créditos "
            f"({pct:.0f}%) · proyección fin de mes ~{proy:,.0f} "
            f"({proy / CUOTA_MENSUAL * 100:.0f}%)")
