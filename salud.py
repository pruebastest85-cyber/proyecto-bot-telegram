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
    """¿Llegan transacciones de las ⭐ vigiladas?

    (Ola 17-L, 25/8) Antes solo miraba `LAST_HOOK_TS`, que se escribe en
    UN sitio: la ruta Flask `helius_hook`. En el PC del dueño no hay
    `PUBLIC_URL` (era de Railway), asi que Helius no tiene a donde enviar
    y esa variable no se escribe NUNCA: el chequeo se quedaba amarillo
    para siempre con la pista equivocada ("normal si el bot acaba de
    arrancar") mientras la ingesta real, LaserStream, funcionaba. Ahora
    se juzga la via que de verdad esta ingiriendo.
    """
    try:
        from realtime import LAST_HOOK_TS, PUBLIC_URL, tracked_addresses
        n = len(tracked_addresses() or [])
        if n == 0:
            return _chk("Ingesta", WARN, "no hay ⭐ vigiladas todavía",
                        "el embudo aún no promociona billeteras")
        # ── Via 1: LaserStream ──
        ls_activo = ls_con = False
        ls_rec, ls_err = 0, ""
        try:
            from laserstream import activo as _ls_act, estado as _ls_est
            ls_activo = bool(_ls_act())
            if ls_activo:
                e = _ls_est() or {}
                ls_con = bool(e.get("conectado"))
                ls_rec = int(e.get("recibidas") or 0)
                ls_err = str(e.get("error") or "")
        except Exception:
            ls_activo = False        # modulo ausente: no es via disponible
        # ── Via 2: webhook HTTP de Helius ──
        hook_conf = bool(PUBLIC_URL)
        hook_h = _horas(LAST_HOOK_TS) if LAST_HOOK_TS else None
        hook_vivo = hook_h is not None and hook_h <= 12

        if ls_con:
            _extra = ("" if hook_conf else
                      " · el webhook HTTP no aplica aquí (sin PUBLIC_URL)")
            return _chk("Ingesta", OK,
                        f"LaserStream conectado · {ls_rec} transacciones "
                        f"· {n} ⭐ vigiladas{_extra}")
        if hook_vivo:
            return _chk("Ingesta", OK,
                        f"webhook HTTP activo (hace {hook_h:.0f} h) · "
                        f"{n} ⭐ vigiladas"
                        + (" · LaserStream caído" if ls_activo else ""))
        # Ninguna via confirmada: el aviso depende de cuantas HAY.
        if not ls_activo and not hook_conf:
            return _chk("Ingesta", CRIT,
                        "no hay vía de entrada: LaserStream apagado "
                        "(USE_LASERSTREAM=0) y sin PUBLIC_URL para el "
                        "webhook", "sin ingesta no entra ninguna señal")
        if ls_activo:
            _d = f"LaserStream desconectado{' — ' + ls_err if ls_err else ''}"
            if not hook_conf:
                return _chk("Ingesta", CRIT, _d + " y es la única vía",
                            "sin PUBLIC_URL el webhook no puede recibir; "
                            "reintenta solo, pero si sigue así reinicia")
            return _chk("Ingesta", WARN, _d + "; queda el webhook HTTP",
                        "vigila que sigan entrando señales")
        # Solo webhook configurado y sin datos recientes
        if hook_h is None:
            return _chk("Ingesta", WARN,
                        f"{n} ⭐ vigiladas, sin datos aún del webhook",
                        "normal si el bot acaba de arrancar")
        if hook_h > 24:
            return _chk("Ingesta", CRIT,
                        f"{hook_h:.0f} h sin transacciones",
                        "corre /ciclo para resincronizar el webhook")
        return _chk("Ingesta", WARN, f"{hook_h:.0f} h sin transacciones",
                    "puede ser normal si las ⭐ están inactivas")
    except Exception as e:
        return _chk("Ingesta", WARN, f"no se pudo comprobar ({e})")


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
        # (Ola 8, 21/8) Antes el denominador era TODA la historia de
        # señales de compra, pero el medidor solo mide por diseño las de
        # billeteras vigilables (⭐ o >=2 ganadores) CON precio base, y
        # solo dentro de su ventana. El % convergia a la tasa historica
        # acumulada (62%) y gritaba aviso aunque la medicion de HOY
        # funcionara al 94%. Ahora: misma poblacion que el medidor,
        # ventana movil de 7 dias, y solo señales cuya ventana ya vencio.
        ahora = int(time.time())
        # (Ola 17-H, auditoria 6) El `price_usd IS NOT NULL` estaba en el
        # WHERE, o sea que las señales que PERDIERON su precio de entrada
        # salian del numerador Y del denominador: /salud podia decir
        # "100% medidas" mientras un tercio de las señales se perdia sin
        # medir. Medido en la base del dueño: 34% de las compras sin
        # precio, de forma estable durante 10 dias, y /salud en verde.
        # Ahora se cuentan las tres cosas por separado.
        # (Ola 17-L, 25/8) DOS POBLACIONES, DOS NUMEROS. La consulta de
        # arriba agrupaba por el estado ACTUAL de la billetera
        # (`is_tracked=1 OR winning_tokens_count>=2`), y eso es un flag
        # que cambia varias veces por hora: una señal que alerto ayer,
        # emitida por una ⭐ que hoy ya no lo es, se caia del recuento.
        # Medido en la base del dueño el 25/8: de 123 compras ALERTADAS
        # en la ventana, solo 7 sobrevivian al filtro — se tiraba el 94%.
        # Resultado: /salud gritaba CRIT 33% mientras las alertas que el
        # dueño recibe de verdad perdian el 4%.
        #
        # Ahora se miden por separado, y con el criterio correcto en cada
        # caso:
        #   · ALERTADAS  → sin filtro de billetera. `alerted=1` ya prueba
        #     que en su momento era ⭐ y estaba en el top (realtime.py
        #     hace `continue` antes de enviar si no lo es), y es un hecho
        #     historico que no cambia. Un filtro por estado actual solo
        #     puede quitar señales legitimas: 8 de las 123 venian de
        #     billeteras marcadas como bot DESPUES de haber alertado.
        #   · SILENCIOSAS → se mantiene `is_bot=0` para no contar el
        #     ruido de los bots (1.031 de 14.235 en la ventana). Nunca
        #     alertaron, asi que no hay historia que preservar.
        _ini, _fin = ahora - 7 * 86400, ahora - 30 * 3600
        _cols = ("COUNT(*) v, "
                 "COALESCE(SUM(CASE WHEN s.price_usd IS NOT NULL "
                 "AND s.price_usd > 0 THEN 1 ELSE 0 END), 0) con_px, "
                 "COALESCE(SUM(CASE WHEN s.chg_24h IS NOT NULL "
                 "OR s.chg_1h IS NOT NULL THEN 1 ELSE 0 END), 0) m ")
        r_al = conn.execute(
            "SELECT " + _cols +
            "FROM signals s WHERE s.side='compra' AND s.alerted=1 "
            "AND s.ts BETWEEN ? AND ?", (_ini, _fin)).fetchone()
        r_si = conn.execute(
            "SELECT " + _cols +
            "FROM signals s JOIN wallets w ON w.address = s.wallet "
            "AND COALESCE(w.is_bot, 0) = 0 "
            "WHERE s.side='compra' AND s.alerted=0 "
            "AND s.ts BETWEEN ? AND ?", (_ini, _fin)).fetchone()
        al, al_px, al_m = r_al["v"], r_al["con_px"], r_al["m"]
        si, si_px = r_si["v"], r_si["con_px"]
        al_sin, si_sin = al - al_px, si - si_px
        si_pct = (si_sin * 100.0 / si) if si else 0.0
        # Frase de contexto: siempre se dice el numero de las silenciosas,
        # aunque el aviso lo dispare otra cosa. Son las que deciden que
        # candidata asciende a ⭐, asi que perderlas no es cosmetico.
        _ctx = (f"; aparte, {si_sin} de {si} señales silenciosas "
                f"({si_pct:.0f}%) sin precio" if si else "")
        _accion_si = ("las silenciosas son las que deciden qué candidata "
                      "asciende a ⭐: con esa parte sin medir, el ranking "
                      "se decide con menos evidencia")

        if al == 0:
            # Sin alertas en 7 dias no se puede juzgar la medicion del
            # dueño; se informa de lo que si hay.
            if si == 0:
                return _chk("Medición", OK,
                            "sin señales medibles vencidas en 7 días")
            return _chk("Medición", WARN,
                        f"ninguna alerta en 7 días{_ctx}", _accion_si)
        if al_sin * 100.0 / al >= 15:
            return _chk("Medición", CRIT,
                        f"{al_sin} de {al} señales ALERTADAS "
                        f"({al_sin*100.0/al:.0f}%) se quedaron SIN precio "
                        f"de entrada: no se pueden medir nunca{_ctx}",
                        "revisa si DexScreener está fallando o limitando; "
                        "sin precio base no hay win rate real")
        if al_px == 0:
            return _chk("Medición", CRIT,
                        f"ninguna de las {al} alertas tenía precio de "
                        f"entrada", "la medición está parada del todo")
        # El % de medicion se calcula sobre las que SI tenian precio
        # (las medibles de verdad); las que lo perdieron ya salen arriba.
        pct = 100.0 * al_m / al_px
        if pct < 30:
            return _chk("Medición", CRIT,
                        f"solo {pct:.0f}% de las alertas medibles de 7 días "
                        f"medidas ({al_m}/{al_px}){_ctx}",
                        "sin medición no hay aprendizaje ni backtest fiable")
        if pct < 70:
            return _chk("Medición", WARN,
                        f"{pct:.0f}% de las alertas medidas "
                        f"({al_m}/{al_px} con precio){_ctx}", _accion_si)
        if si_pct >= 25:
            return _chk("Medición", WARN,
                        f"alertas OK ({pct:.0f}% medidas, {al_m}/{al_px})"
                        f"{_ctx}", _accion_si)
        return _chk("Medición", OK,
                    f"{pct:.0f}% de las alertas medidas ({al_m}/{al_px} "
                    f"con precio"
                    + (f", {al_sin} sin precio de entrada" if al_sin else "")
                    + f" · 7 días){_ctx}")
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
        # Desglose REAL del embudo (Ola 7): "16.104 candidatas" a secas
        # asustaba — el 93% son billeteras de UN solo ganador que esperan
        # reincidir, no una cola de trabajo. Aquí va la cola de examen de
        # verdad (los mismos filtros de la evaluación) y el enfriamiento.
        try:
            from datetime import datetime, timedelta, timezone
            _c3 = (datetime.now(timezone.utc) - timedelta(days=3)
                   ).isoformat(timespec="seconds")
            _c14 = (datetime.now(timezone.utc) - timedelta(days=14)
                    ).isoformat(timespec="seconds")
            en_cola = conn.execute(
                """SELECT COUNT(*) c FROM wallets w
                   WHERE COALESCE(is_bot,0)=0 AND winning_tokens_count >= 2
                     AND (ai_class IS NULL OR pnl_updated IS NULL
                          OR (is_tracked=1 AND pnl_updated < ?)
                          OR (is_tracked=0 AND pnl_updated < ?))
                     AND EXISTS (SELECT 1 FROM appearances a
                                 WHERE a.wallet = w.address
                                   AND COALESCE(a.buy_sol, 0) >= 1
                                   AND (a.entry_multiple IS NULL
                                        OR a.entry_multiple >= 3))""",
                (_c3, _c14)).fetchone()["c"]
            enfriando = conn.execute(
                """SELECT COUNT(*) c FROM wallets
                   WHERE COALESCE(is_bot,0)=0 AND is_tracked=0
                     AND ai_class IS NOT NULL AND pnl_updated >= ?""",
                (_c14,)).fetchone()["c"]
            return _chk("Embudo", OK,
                        f"{wallets} conocidas · {en_cola} en cola de "
                        f"examen · {enfriando} en enfriamiento · "
                        f"{estrellas} ⭐")
        except Exception:
            return _chk("Embudo", OK,
                        f"{wallets} candidatas · {evaluadas} evaluadas · "
                        f"{estrellas} ⭐")
    except Exception as e:
        return _chk("Embudo", WARN, f"no se pudo comprobar ({e})")


def _c_presupuesto(conn):
    """Presupuesto de la NUBE. Desde el 18/8 la local es titular: que la
    nube este agotada ya no es un problema del sistema, solo un dato."""
    try:
        from ia_puente import _setting
        orden = str(_setting("ia_proveedor", "local_primero", conn)
                    or "local_primero")
        if orden == "local" or not os.getenv("ANTHROPIC_API_KEY"):
            return _chk("Presupuesto IA nube", OK,
                        "nube fuera de juego · titular: IA local")
        from ai_budget import budget_left, used_today, _cap
        quedan, usadas, cap = budget_left(conn), used_today(conn), _cap()
        if quedan <= 0:
            # Solo es un problema si la nube es la titular.
            nivel = WARN if orden == "nube" else OK
            return _chk("Presupuesto IA nube", nivel,
                        f"agotado hoy ({usadas}/{cap})",
                        "la IA local sigue trabajando gratis")
        if quedan < cap * 0.1:
            return _chk("Presupuesto IA nube", OK,
                        f"quedan {quedan} de {cap} (respaldo)")
        return _chk("Presupuesto IA nube", OK, f"{usadas}/{cap} usadas hoy")
    except Exception as e:
        return _chk("Presupuesto IA nube", WARN, f"no se pudo comprobar ({e})")


def _c_ia_local(conn):
    """¿Responde la IA local? Ahora es la pieza critica: sin nube con
    creditos, si esto se cae el sistema se queda sin IA."""
    try:
        from decision_ia import _url, _modelo
        url = _url(conn)
        if not url:
            return _chk("IA local", WARN, "sin URL configurada",
                        "usa /ialocal <url>")
        import requests as _rq
        r = _rq.get(f"{url}/v1/models", timeout=4)
        if r.status_code >= 400:
            return _chk("IA local", WARN, f"HTTP {r.status_code} en {url}")
        modelos = [m.get("id") for m in r.json().get("data", [])]
        modelo = _modelo(conn)
        if modelo and modelos and modelo not in modelos:
            return _chk("IA local", WARN,
                        f"conectada pero sin el modelo {modelo}",
                        "cárgalo en LM Studio (y revisa el auto-unload)")
        return _chk("IA local", OK, f"responde · modelo {modelo}")
    except Exception as e:
        grave = CRIT if not os.getenv("ANTHROPIC_API_KEY") else WARN
        return _chk("IA local", grave, f"no responde ({e})",
                    "¿PC apagada o túnel caído? repite /ialocal <url>")


def _c_apis():
    """¿Están configuradas las claves necesarias?"""
    faltan = [n for n, v in (("HELIUS_API_KEY", os.getenv("HELIUS_API_KEY")),
                             ("TELEGRAM_BOT_TOKEN",
                              os.getenv("TELEGRAM_BOT_TOKEN")))
              if not v]
    if faltan:
        return _chk("Claves API", CRIT, "faltan: " + ", ".join(faltan),
                    "configúralas en las variables de Railway")
    sin_nube = not os.getenv("ANTHROPIC_API_KEY")
    try:
        from ia_puente import hay_ia
        ia_ok = hay_ia()
    except Exception:
        ia_ok = not sin_nube
    if not ia_ok:
        return _chk("Claves API", WARN, "sin IA disponible (ni local ni nube)",
                    "configura /ialocal <url> o una ANTHROPIC_API_KEY")
    if sin_nube:
        return _chk("Claves API", OK,
                    "todas presentes · IA: local titular, sin nube")
    return _chk("Claves API", OK, "todas presentes")


def _c_helius(conn):
    """¿Se está gastando la cuota de Helius a buen ritmo o de más?"""
    try:
        # v2 (Ola 7): créditos EXACTOS del CICLO de facturación (los que
        # cuadran con el panel de Helius), no la vieja suma doble sobre
        # el mes calendario que llegó a mostrar 2,2 M con 328 k reales.
        from helius_budget import (creditos_usados, pct_usado, CUOTA_MENSUAL,
                                   FRENO_PCT, dia_del_ciclo)
        usados = creditos_usados(conn)
        pct = pct_usado(conn)
        dia = max(1, dia_del_ciclo(conn))
        proy = usados / dia * 30
        proy_pct = 100.0 * proy / CUOTA_MENSUAL if CUOTA_MENSUAL else 0
        det = (f"{usados:,} créditos en {dia} día(s) de ciclo ({pct:.0f}%) "
               f"· ritmo → {proy_pct:.0f}% del plan · el panel manda")
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


def _c_base_datos(conn):
    """Tamaño de la base: si crece de más, el backup por Telegram falla."""
    try:
        import config as _cfg
        ruta = getattr(_cfg, "DB_PATH", None)
        if not ruta or not os.path.exists(ruta):
            return _chk("Base de datos", OK, "en Postgres o sin archivo local")
        mb = os.path.getsize(ruta) / 1e6
        try:
            from trades_store import estadisticas
            ops = estadisticas().get("operaciones", 0)
        except Exception:
            ops = 0
        det = f"{mb:.0f} MB · {ops:,} operaciones guardadas"
        # El backup va comprimido (~5x), así que el margen real es mayor
        if mb > 300:
            return _chk("Base de datos", CRIT, det,
                        "demasiado grande incluso comprimida: pasa a "
                        "Postgres (DATABASE_URL) o baja MAX_TRADES_TOTAL")
        if mb > 200:
            return _chk("Base de datos", WARN, det,
                        "considera pasar a Postgres para crecer sin límite")
        return _chk("Base de datos", OK, det)
    except Exception as e:
        return _chk("Base de datos", WARN, f"no se pudo comprobar ({e})")


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
        # (Ola 16) El automático (copia verificada en disco, con rotación)
        # y el manual (/backup, que solo manda el archivo y lo borra) son
        # cosas distintas: mezclarlos escondía un job roto durante días.
        ts = get_setting(conn, "last_backup_ts", None)
        tsm = get_setting(conn, "last_backup_manual_ts", None)
        extra = ""
        if tsm:
            extra = f" · manual hace {_horas(tsm):.0f} h"
        # (Ola 17-C, auditoría 4) Antes esto NUNCA pasaba de WARN, y
        # `revisar_y_avisar` solo manda aviso ante CRIT: por eso el dueño
        # estuvo 70 h sin ningún respaldo y solo se enteró al escribir
        # /salud a mano. Un backup roto es exactamente el fallo que hay
        # que contar antes de necesitarlo, no después.
        if not ts:
            return _chk("Backup", WARN,
                        "sin backup automático todavía" + extra,
                        "si sigue así mañana, algo va mal en el job")
        h = _horas(ts)
        if h > 72:
            return _chk("Backup", CRIT,
                        f"SIN RESPALDO desde hace {h:.0f} h" + extra,
                        "mira el log por '· Backup falló:' y /errores; "
                        "el histórico es lo único irrecuperable")
        if h > 48:
            return _chk("Backup", WARN,
                        f"automático hace {h:.0f} h" + extra,
                        "revisa la carpeta backups/ junto a la base")
        return _chk("Backup", OK, f"hace {h:.0f} h" + extra)
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
                   _c_presupuesto(conn), _c_ia_local(conn), _c_helius(conn),
                   _c_base_datos(conn), _c_backup(conn)]
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
    if not __import__("ia_puente").hay_ia():
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
        # Puente de IA (18/8/2026): la LOCAL es titular; la nube, opcional.
        from ia_puente import completar
        txt = (completar(prompt, max_tokens=500, timeout=90) or "").strip()
        if not txt:
            raise RuntimeError("IA no disponible (ni local ni nube)")
    except Exception as e:
        try:
            from errores import record
            record("salud", e, "interpretar")
        except Exception:
            pass
        return None
    if not txt:
        return None
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
