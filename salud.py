"""
Autodiagnóstico del sistema: ¿está todo funcionando como debe?

Dos capas, a propósito:

  1. DETECCIÓN determinista (este módulo, $0). Todo lo medible se comprueba
     con código: ciclos, webhook, señales, medición de resultados, embudo,
     presupuesto, errores, backup. Esto detecta la gran mayoría de los
     problemas reales sin gastar un centavo de IA.

  2. INTERPRETACIÓN por IA (opcional, 1 llamada al día). La IA no detecta:
     recibe este informe y explica en lenguaje claro qué pasa, por qué y
     qué hacer, priorizado. Ahí es donde aporta valor real.

Importante: la IA NO puede validar que la lógica sea correcta ni que un
umbral esté bien elegido. Detecta SÍNTOMAS (no entran señales, la API
falla, se acabó el presupuesto), no errores de criterio.
"""

import os
import time

from db import get_conn, get_setting

OK, WARN, CRIT = "ok", "warn", "crit"
_ICONO = {OK: "🟢", WARN: "🟡", CRIT: "🔴"}


def _chk(nombre, estado, detalle, accion=""):
    return {"nombre": nombre, "estado": estado, "detalle": detalle,
            "accion": accion}


def _horas(ts):
    if not ts:
        return None
    return (time.time() - float(ts)) / 3600


# ─────────────────────────── COMPROBACIONES ──────────────────────────

def _c_webhook():
    """¿Llegan transacciones de las ⭐ vigiladas?"""
    try:
        from realtime import LAST_HOOK_TS, tracked_addresses
        n = len(tracked_addresses() or [])
        if n == 0:
            return _chk("Webhook", WARN, "no hay ⭐ vigiladas todavía",
                        "el embudo aún no promociona billeteras")
        if LAST_HOOK_TS is None:
            return _chk("Webhook", WARN, f"{n} ⭐ vigiladas, sin datos aún",
                        "normal si el bot acaba de arrancar")
        h = _horas(LAST_HOOK_TS)
        if h > 24:
            return _chk("Webhook", CRIT, f"{h:.0f} h sin transacciones",
                        "corre /ciclo para resincronizar el webhook")
        if h > 12:
            return _chk("Webhook", WARN, f"{h:.0f} h sin transacciones",
                        "puede ser normal si las ⭐ están inactivas")
        return _chk("Webhook", OK, f"activo · {n} ⭐ vigiladas")
    except Exception as e:
        return _chk("Webhook", WARN, f"no se pudo comprobar ({e})")


def _c_senales(conn):
    """¿Se están generando señales?"""
    try:
        ahora = int(time.time())
        n24 = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE ts>=?",
            (ahora - 86400,)).fetchone()["c"]
        n7 = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE ts>=?",
            (ahora - 7 * 86400,)).fetchone()["c"]
        if n7 == 0:
            return _chk("Señales", CRIT, "ninguna en 7 días",
                        "revisa el webhook y que haya ⭐ activas")
        if n24 == 0:
            return _chk("Señales", WARN, f"0 en 24 h ({n7} en 7 días)",
                        "puede ser mercado tranquilo")
        return _chk("Señales", OK, f"{n24} en 24 h · {n7} en 7 días")
    except Exception as e:
        return _chk("Señales", WARN, f"no se pudo comprobar ({e})")


def _c_medicion(conn):
    """¿Se mide el resultado de las señales? (sin esto no hay aprendizaje)"""
    try:
        ahora = int(time.time())
        viejas = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE side='compra' AND ts<=?",
            (ahora - 86400,)).fetchone()["c"]
        medidas = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE side='compra' AND ts<=? "
            "AND (chg_24h IS NOT NULL OR chg_1h IS NOT NULL)",
            (ahora - 86400,)).fetchone()["c"]
        if viejas == 0:
            return _chk("Medición", OK, "aún no hay señales con 24 h")
        pct = 100.0 * medidas / viejas
        if pct < 30:
            return _chk("Medición", CRIT,
                        f"solo {pct:.0f}% de las señales medidas "
                        f"({medidas}/{viejas})",
                        "sin medición no hay aprendizaje ni backtest fiable")
        if pct < 70:
            return _chk("Medición", WARN,
                        f"{pct:.0f}% medidas ({medidas}/{viejas})")
        return _chk("Medición", OK, f"{pct:.0f}% medidas ({medidas}/{viejas})")
    except Exception as e:
        return _chk("Medición", WARN, f"no se pudo comprobar ({e})")


def _c_embudo(conn):
    """¿El embudo produce candidatas y estrellas?"""
    try:
        wallets = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE COALESCE(is_bot,0)=0"
        ).fetchone()["c"]
        estrellas = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE is_tracked=1"
        ).fetchone()["c"]
        evaluadas = conn.execute(
            "SELECT COUNT(*) c FROM wallets WHERE ai_class IS NOT NULL"
        ).fetchone()["c"]
        if wallets == 0:
            return _chk("Embudo", CRIT, "sin billeteras en la base",
                        "corre /ciclo para descubrir tokens y compradores")
        if evaluadas == 0:
            return _chk("Embudo", WARN,
                        f"{wallets} candidatas, ninguna evaluada aún",
                        "la IA las evalúa por ciclos; dale tiempo")
        if estrellas == 0:
            return _chk("Embudo", WARN,
                        f"{evaluadas} evaluadas pero 0 ⭐",
                        "puede que los umbrales estén muy exigentes")
        return _chk("Embudo", OK,
                    f"{wallets} candidatas · {evaluadas} evaluadas · "
                    f"{estrellas} ⭐")
    except Exception as e:
        return _chk("Embudo", WARN, f"no se pudo comprobar ({e})")


def _c_presupuesto(conn):
    """¿Queda presupuesto de IA y créditos?"""
    try:
        from ai_budget import budget_left, used_today, _cap
        quedan, usadas, cap = budget_left(conn), used_today(conn), _cap()
        if quedan <= 0:
            return _chk("Presupuesto IA", WARN,
                        f"agotado ({usadas}/{cap} hoy)",
                        "el sistema sigue con el respaldo por grading")
        if quedan < cap * 0.1:
            return _chk("Presupuesto IA", WARN,
                        f"quedan {quedan} de {cap}")
        return _chk("Presupuesto IA", OK, f"{usadas}/{cap} usadas hoy")
    except Exception as e:
        return _chk("Presupuesto IA", WARN, f"no se pudo comprobar ({e})")


def _c_apis():
    """¿Están configuradas las claves necesarias?"""
    faltan = [n for n, v in (("HELIUS_API_KEY", os.getenv("HELIUS_API_KEY")),
                             ("TELEGRAM_BOT_TOKEN",
                              os.getenv("TELEGRAM_BOT_TOKEN")))
              if not v]
    if faltan:
        return _chk("Claves API", CRIT, "faltan: " + ", ".join(faltan),
                    "configúralas en las variables de Railway")
    sin_ia = not os.getenv("ANTHROPIC_API_KEY")
    if sin_ia:
        return _chk("Claves API", WARN, "sin ANTHROPIC_API_KEY",
                    "el sistema funciona con grading, pero sin IA")
    return _chk("Claves API", OK, "todas presentes")


def _c_helius(conn):
    """¿Se está gastando la cuota de Helius a buen ritmo o de más?"""
    try:
        from helius_budget import (creditos_usados, pct_usado, CUOTA_MENSUAL,
                                   FRENO_PCT, _dia_ciclo)
        usados = creditos_usados(conn)
        pct = pct_usado(conn)
        dia = max(1, _dia_ciclo())
        proy = usados / dia * 30
        proy_pct = 100.0 * proy / CUOTA_MENSUAL if CUOTA_MENSUAL else 0
        det = (f"{usados:,} créditos ({pct:.0f}%) · proyección "
               f"{proy_pct:.0f}% a fin de mes")
        if pct >= FRENO_PCT:
            return _chk("Créditos Helius", CRIT, det,
                        "descargas pausadas para no quedarte sin cuota")
        if proy_pct > 90:
            return _chk("Créditos Helius", WARN, det,
                        "vas camino de agotar la cuota; baja los topes")
        if proy_pct < 25:
            return _chk("Créditos Helius", OK,
                        det + " — sobra cuota, se puede analizar más")
        return _chk("Créditos Helius", OK, det)
    except Exception as e:
        return _chk("Créditos Helius", WARN, f"no se pudo comprobar ({e})")


def _c_laserstream():
    """¿Está viva la escucha en tiempo real de menor latencia?"""
    try:
        from laserstream import activo, estado
        if not activo():
            return _chk("LaserStream", OK, "desactivado (solo webhook)")
        e = estado()
        if e.get("conectado"):
            return _chk("LaserStream", OK,
                        f"conectado · {e.get('recibidas', 0)} transacciones")
        return _chk("LaserStream", WARN,
                    f"desconectado{' — ' + e['error'] if e.get('error') else ''}",
                    "el webhook sigue funcionando como respaldo")
    except Exception as e:
        return _chk("LaserStream", WARN, f"no se pudo comprobar ({e})")


def _c_errores():
    """¿Hubo errores registrados?"""
    try:
        from errores import total, resumen
        n = total(24)
        if n == 0:
            return _chk("Errores", OK, "ninguno en 24 h")
        filas = resumen(24)
        peor = filas[0] if filas else {}
        if n >= 50:
            return _chk("Errores", CRIT, f"{n} en 24 h · el más frecuente: "
                        f"{peor.get('modulo')} ({peor.get('tipo')})",
                        "mira /errores para el detalle")
        return _chk("Errores", WARN, f"{n} en 24 h · más frecuente: "
                    f"{peor.get('modulo')} ({peor.get('tipo')})",
                    "mira /errores para el detalle")
    except Exception as e:
        return _chk("Errores", WARN, f"no se pudo comprobar ({e})")


def _c_backup(conn):
    try:
        ts = get_setting(conn, "last_backup_ts", None)
        if not ts:
            return _chk("Backup", WARN, "sin registro de backup todavía")
        h = _horas(ts)
        if h > 48:
            return _chk("Backup", WARN, f"último hace {h:.0f} h")
        return _chk("Backup", OK, f"hace {h:.0f} h")
    except Exception as e:
        return _chk("Backup", WARN, f"no se pudo comprobar ({e})")


# ──────────────────────────── INFORME ────────────────────────────────

def diagnostico() -> list[dict]:
    """Ejecuta todas las comprobaciones y devuelve la lista de resultados."""
    checks = [_c_apis(), _c_webhook()]
    conn = None
    try:
        conn = get_conn()
        checks += [_c_senales(conn), _c_medicion(conn), _c_embudo(conn),
                   _c_presupuesto(conn), _c_helius(conn), _c_backup(conn)]
    except Exception as e:
        checks.append(_chk("Base de datos", CRIT, f"no accesible: {e}",
                           "revisa el volumen/DATABASE_URL en Railway"))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    checks.append(_c_laserstream())
    checks.append(_c_errores())
    return checks


def peor_estado(checks) -> str:
    if any(c["estado"] == CRIT for c in checks):
        return CRIT
    if any(c["estado"] == WARN for c in checks):
        return WARN
    return OK


def salud_text(checks=None, con_ia: bool = False) -> str:
    checks = checks or diagnostico()
    peor = peor_estado(checks)
    cabecera = {OK: "🟢 *Todo funcionando correctamente*",
                WARN: "🟡 *Funcionando con avisos*",
                CRIT: "🔴 *Hay problemas que requieren atención*"}[peor]
    out = [cabecera, ""]
    for c in checks:
        out.append(f"{_ICONO[c['estado']]} *{c['nombre']}* — {c['detalle']}")
        if c["accion"] and c["estado"] != OK:
            out.append(f"   ↳ _{c['accion']}_")
    if con_ia:
        txt = interpretar(checks)
        if txt:
            out += ["", "🧠 *Lectura de la IA*", txt]
    return "\n".join(out)


def interpretar(checks) -> str | None:
    """
    Capa 2: la IA explica y prioriza. Solo se llama si hay algo que
    interpretar y queda presupuesto. Devuelve None si no aplica.
    """
    problemas = [c for c in checks if c["estado"] != OK]
    if not problemas:
        return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        conn = get_conn()
        try:
            from ai_budget import can_call
            if not can_call(conn):
                return None
            # máximo una interpretación al día
            hoy = time.strftime("%Y-%m-%d")
            if get_setting(conn, "salud_ia_dia", "") == hoy:
                return None
        finally:
            conn.close()
    except Exception:
        return None

    import json
    import requests
    from errores import resumen as _res
    prompt = (
        "Eres el ingeniero de guardia de un bot de trading on-chain en "
        "Solana. Te paso el diagnóstico automático y los errores recientes. "
        "Explica en español, breve y claro:\n"
        "1. Qué está fallando de verdad y qué es ruido.\n"
        "2. La causa más probable de cada problema real.\n"
        "3. Qué hacer, ordenado por prioridad (máximo 3 acciones).\n"
        "No inventes causas: si los datos no alcanzan, dilo.\n\n"
        f"DIAGNÓSTICO: {json.dumps(problemas, ensure_ascii=False)}\n\n"
        f"ERRORES 24h: {json.dumps(_res(24)[:10], ensure_ascii=False, default=str)}")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.getenv("ANTHROPIC_API_KEY"),
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60)
        r.raise_for_status()
        txt = "".join(b.get("text", "") for b in
                      r.json().get("content", [])).strip()
    except Exception as e:
        try:
            from errores import record
            record("salud", e, "interpretar")
        except Exception:
            pass
        return None
    if not txt:
        return None
    try:
        conn = get_conn()
        try:
            from ai_budget import record_call
            record_call(conn)
            from db import set_setting
            set_setting(conn, "salud_ia_dia", time.strftime("%Y-%m-%d"))
        finally:
            conn.close()
    except Exception:
        pass
    return txt[:1500]


def revisar_y_avisar() -> str | None:
    """
    Job periódico: SOLO avisa si hay algo crítico y accionable.
    Anti-ruido: no repite el mismo aviso en 12 h.
    """
    checks = diagnostico()
    criticos = [c for c in checks if c["estado"] == CRIT]
    if not criticos:
        return None
    firma = "|".join(sorted(c["nombre"] for c in criticos))
    try:
        conn = get_conn()
        try:
            prev = get_setting(conn, "salud_ultimo_aviso", "") or ""
            ts_prev = float(get_setting(conn, "salud_ultimo_aviso_ts", 0) or 0)
            if prev == firma and (time.time() - ts_prev) < 12 * 3600:
                return None            # mismo problema, ya avisado
            from db import set_setting
            set_setting(conn, "salud_ultimo_aviso", firma)
            set_setting(conn, "salud_ultimo_aviso_ts", time.time())
        finally:
            conn.close()
    except Exception:
        pass
    texto = salud_text(checks, con_ia=True)
    try:
        from realtime import tg_send
        tg_send("⚠️ *Autodiagnóstico*\n\n" + texto)
    except Exception:
        pass
    return texto
