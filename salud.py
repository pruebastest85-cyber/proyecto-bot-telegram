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


def _md_plano(txt) -> str:
    """Neutraliza los símbolos que rompen el Markdown de Telegram.

    (Ola 18-Q) `salud_text` envuelve cada consejo en cursiva: `_{accion}_`.
    Si dentro se cuela una ruta o el texto de una excepción con un número
    IMPAR de `_`, Telegram rechaza el mensaje ENTERO con un 400 y /salud
    sale sin ningún formato por el reintento en texto plano. Y no es
    hipotético: un `ImportError` cualquiera de este módulo dice
    `No module named 'trades_store'`, con dos `_` y una comilla — el
    aviso que avisa de los fallos se rompía justo cuando hay un fallo.

    Los caracteres se QUITAN en vez de escaparse con `\\`, y la tabla es
    la misma que usa `telegram_bot._MD_FUERA`, por la razón que allí está
    escrita: el Markdown legacy no des-escapa la barra en todos los
    contextos y acaban viéndose barras sueltas.

    Solo se usa en los trozos que vienen de FUERA (rutas, textos de
    excepción); el texto que escribimos nosotros conserva sus `backticks`
    a propósito.
    """
    s = str(txt)
    for _c, _r in (("*", ""), ("_", " "), ("`", ""), ("[", "("), ("]", ")")):
        s = s.replace(_c, _r)
    return s


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
        ls_rec, ls_err, ls_h, ls_desc = 0, "", None, 0
        try:
            from laserstream import activo as _ls_act, estado as _ls_est
            ls_activo = bool(_ls_act())
            if ls_activo:
                e = _ls_est() or {}
                ls_con = bool(e.get("conectado"))
                ls_rec = int(e.get("recibidas") or 0)
                ls_err = str(e.get("error") or "")
                ls_desc = int(e.get("descartadas") or 0)
                # (Ola 17-N, auditoria de la 17-M) `ls_h` medía desde
                # `desde`, la ultima conexion CON EXITO, y el watchdog
                # reconecta cada 10 min: nunca pasaba de 0,17 h, asi que
                # el CRIT de "conectado pero mudo" era codigo muerto y el
                # margen de reconexion se volvia un WARN permanente. Lo
                # que hay que medir es el SILENCIO: cuanto lleva sin
                # llegar un dato real. `ultimo` solo avanza con datos;
                # si nunca llego ninguno, se cuenta desde el arranque.
                ls_h = _horas(e.get("ultimo") or e.get("arranque")
                              or e.get("desde"))
        except Exception:
            ls_activo = False        # modulo ausente: no es via disponible
        # ── Via 2: webhook HTTP de Helius ──
        hook_conf = bool(PUBLIC_URL)
        hook_h = _horas(LAST_HOOK_TS) if LAST_HOOK_TS else None
        hook_vivo = hook_h is not None and hook_h <= 12
        # (Ola 17-M) `conectado` se pone a True en cuanto abre el socket,
        # ANTES de recibir nada, y una suscripcion rechazada guarda el
        # motivo en `error` sin tocar `conectado`: el chequeo se quedaba
        # en verde diciendo "conectado · 0 transacciones".
        # (17-N) La condicion era `ls_rec == 0`, que no vale: `recibidas`
        # es acumulado desde el arranque del proceso, asi que en cuanto
        # llegaba UNA transaccion el detector quedaba desarmado para
        # siempre. Ahora manda el silencio, que es lo que de verdad
        # delata una suscripcion muerta.
        # (Ola 17-O) El umbral era 0,25 h (15 min), copiado del watchdog
        # interno de LaserStream — y estaba mal, porque ese watchdog solo
        # RE-SUSCRIBE (gratis) mientras esto enciende una alarma roja que
        # dice "reinicia el bot". El socket solo emite cuando una
        # billetera vigilada opera de verdad: no hay latido. Medido en la
        # base del dueño, 34.573 señales de 7 dias:
        #   mediana 6 s · p99 2,7 min · p99,9 7,1 min
        #   huecos > 15 min:  6 en 7 dias  → ~1 falso rojo AL DIA
        #   huecos > 30 min:  2 en 7 dias
        #   huecos > 90 min:  1 en 7 dias (uno de 226 min el 21/8, que
        #                     tiene toda la pinta de corte real)
        # Con 90 min no hay falsos y una suscripcion muerta se caza
        # igual: /salud corre cada 6 h, asi que 1,5 h de margen no
        # retrasa nada. El aviso naranja a los 30 min avisa sin gritar.
        MUDO_H = 1.5             # rojo: 90 min sin un solo dato
        FLOJO_H = 0.5            # aviso: 30 min
        ls_silencio = ls_con and ls_h is not None
        ls_mudo = ls_silencio and ls_h > MUDO_H
        ls_flojo = ls_silencio and FLOJO_H < ls_h <= MUDO_H

        if ls_con and not ls_mudo:
            _extra = ("" if hook_conf else
                      " · el webhook HTTP no aplica aquí (sin "
                      "`PUBLIC_URL`)")
            # (17-O) `descartadas` no lo miraba nadie: si la cola de
            # workers se llena, LaserStream sigue contando "recibidas" y
            # el chequeo daba verde mientras los datos se tiraban.
            if ls_desc > 0:
                return _chk("Ingesta", WARN,
                            f"LaserStream conectado pero ha DESCARTADO "
                            f"{ls_desc} transacciones (cola llena) · "
                            f"{ls_rec} recibidas",
                            "esas señales no se han procesado; si el "
                            "número sube, reinicia el bot")
            if ls_flojo:
                return _chk("Ingesta", WARN,
                            f"LaserStream conectado pero lleva "
                            f"{ls_h * 60:.0f} min sin recibir nada · "
                            f"{ls_rec} transacciones{_extra}",
                            "puede ser mercado tranquilo; se avisa en "
                            "rojo si pasa de 90 min")
            return _chk("Ingesta", OK,
                        f"LaserStream conectado · {ls_rec} transacciones "
                        f"· {n} ⭐ vigiladas{_extra}")
        if hook_vivo:
            return _chk("Ingesta", OK,
                        f"webhook HTTP activo (hace {hook_h:.0f} h) · "
                        f"{n} ⭐ vigiladas"
                        + ((" · LaserStream sin datos" if ls_mudo
                            else " · LaserStream caído") if ls_activo
                           else ""))
        if ls_mudo:
            return _chk("Ingesta", CRIT,
                        f"LaserStream dice conectado pero lleva "
                        f"{ls_h * 60:.0f} min sin recibir nada"
                        f"{' — ' + _md_plano(ls_err) if ls_err else ''}",
                        "suele ser una suscripción rechazada: revisa la "
                        "clave de Helius y reinicia el bot")
        # Ninguna via confirmada: el aviso depende de cuantas HAY.
        if not ls_activo and not hook_conf:
            return _chk("Ingesta", CRIT,
                        "no hay vía de entrada: LaserStream apagado "
                        "(`USE_LASERSTREAM`=0) y sin `PUBLIC_URL` para "
                        "el webhook",
                        "sin ingesta no entra ninguna señal")
        if ls_activo:
            _d = ("LaserStream desconectado"
                  + (" — " + _md_plano(ls_err) if ls_err else ""))
            if not hook_conf:
                # (17-M) Sin webhook posible es la unica via, pero un
                # corte de segundos es NORMAL: el backoff de reconexion
                # llega a 300 s y el arranque espera 120 s si aun no hay
                # ⭐. Se da margen antes de gritar; pasado eso, es rojo.
                # (17-N) El margen se mide sobre el SILENCIO, no sobre
                # `desde`: con `desde` la reconexion cada 10 min dejaba
                # el aviso en amarillo permanente aunque no entrara nada.
                # (17-O) El margen aqui es FLOJO_H, no MUDO_H: estar
                # DESCONECTADO no es mercado tranquilo, es un estado
                # duro, y el backoff de reconexion no pasa de 5 min. 30
                # min desconectado ya es un problema de verdad.
                if ls_h is not None and ls_h < FLOJO_H:
                    return _chk("Ingesta", WARN, _d + " (reconectando)",
                                "normal tras un corte; si sigue en el "
                                "próximo /salud, reinicia el bot")
                return _chk("Ingesta", CRIT,
                            _d + " y es la única vía (sin `PUBLIC_URL` "
                            "el webhook no puede recibir)",
                            "reintenta solo, pero si sigue así reinicia "
                            "el bot")
            # (17-M) Con webhook configurado el viejo chequeo daba CRIT a
            # las 24 h de silencio. Devolver WARN aqui sin mirar el reloj
            # degradaba un apagon TOTAL (LaserStream caido + webhook mudo
            # 50 h) de rojo a amarillo — y `revisar_y_avisar` solo avisa
            # por Telegram ante CRIT, asi que el apagon pasaba callado.
            # (17-N) `hook_h is None` NO es prueba de apagon: LAST_HOOK_TS
            # es un global de proceso que vuelve a None en cada reinicio,
            # asi que entre arrancar y el primer POST de Helius este
            # camino gritaba "las dos vías están caídas; reinicia el bot"
            # justo despues de un reinicio, recomendando lo que acababa
            # de pasar. Solo es rojo con un reloj que lo demuestre.
            if hook_h is not None and hook_h > 24:
                return _chk("Ingesta", CRIT,
                            f"{_d} y el webhook lleva {hook_h:.0f} h "
                            f"mudo: sin ingesta",
                            "las dos vías están caídas; reinicia el bot")
            if hook_h is None:
                # (17-O) Este WARN no tenia techo: un apagon total con
                # PUBLIC_URL puesta se quedaba en amarillo para siempre,
                # porque LAST_HOOK_TS vuelve a None en cada reinicio y
                # nunca vuelve a llenarse. El reloj que faltaba es el del
                # proceso: si lleva mas de una hora arriba y NINGUNA de
                # las dos vias ha traido nada, ya no es un arranque.
                if ls_h is not None and ls_h > 1.0:
                    return _chk("Ingesta", CRIT,
                                f"{_d} y el webhook tampoco ha recibido "
                                f"nada en {ls_h:.0f} h: sin ingesta",
                                "las dos vías están caídas; reinicia el "
                                "bot")
                return _chk("Ingesta", WARN,
                            _d + " y el webhook no ha recibido nada aún",
                            "normal si el bot acaba de arrancar; si sigue "
                            "en el próximo /salud, no hay ingesta")
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
        return _chk("Ingesta", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
        return _chk("Señales", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
        # (Ola 17-M, auditoria de la 17-L) Dos correcciones sobre lo de
        # arriba, ambas medidas:
        #  a) El SQL se escribia concatenando `"SELECT " + _cols + ...`.
        #     concatenando la lista de columnas en una variable aparte.
        #     `auditoria.py` solo extrae consultas cuando el argumento de
        #     execute() es una cadena LITERAL (ast.Constant), asi que
        #     decia "Sin hallazgos" sin haber mirado estas dos. Ahora van
        #     literales, aunque se repita texto: la red de seguridad vale
        #     mas que ahorrar seis lineas.
        #  b) El PORCENTAJE MEDIDO usaba de denominador todas las
        #     alertadas con precio, pero `chg_1h`/`chg_24h` solo las
        #     escribe `track_outcomes`, y ese SELECT sigue filtrando por
        #     `is_bot=0 AND (is_tracked=1 OR winning_tokens_count>=2)`
        #     (signal_tracker.py). Una ⭐ degradada DENTRO de la ventana
        #     de medicion deja su señal inmedible para siempre, y estaba
        #     cayendo en el denominador: el % bajaba solo con el tiempo.
        #     Medido en la base del dueño: 3 de 118 a 7 dias (97%), 17 de
        #     338 a 30 dias (95%) — poco hoy, pero deriva sin suelo.
        #     Reparto correcto: el "sin precio" se juzga sobre TODAS las
        #     alertadas (poblacion estable), y el "% medido" solo sobre
        #     las que el medidor todavia puede tocar; las que quedan
        #     fuera de su alcance se dicen aparte en vez de esconderse.
        _ini, _fin = ahora - 7 * 86400, ahora - 30 * 3600
        r_al = conn.execute(
            "SELECT COUNT(*) v, "
            "COALESCE(SUM(CASE WHEN s.price_usd IS NOT NULL "
            "AND s.price_usd > 0 THEN 1 ELSE 0 END), 0) con_px "
            "FROM signals s WHERE s.side='compra' AND s.alerted=1 "
            "AND s.ts BETWEEN ? AND ?", (_ini, _fin)).fetchone()
        # Poblacion que el medidor PUDO tocar. Ojo con el matiz, que me
        # costo una pasada: filtrar solo por `EXISTS(billetera vigente)`
        # es igual de erroneo que el filtro que la 17-L quito, porque
        # mira el estado de HOY sobre un trabajo que se hizo AYER. Una
        # señal ya medida cuya ⭐ se degrado despues salia del
        # denominador aunque su medicion existiera: probado contra la
        # base del dueño, el denominador caia de 118 a 5 y el chequeo
        # decia "100% medidas (5/5)", un numero cierto e inutil.
        # La condicion correcta es: YA MEDIDA (prueba de que se pudo)
        # O todavia al alcance del medidor. Solo queda fuera lo que no
        # se midio y ya no se podra medir nunca.
        r_med = conn.execute(
            "SELECT COUNT(*) v, "
            "COALESCE(SUM(CASE WHEN s.chg_24h IS NOT NULL "
            "OR s.chg_1h IS NOT NULL THEN 1 ELSE 0 END), 0) m "
            "FROM signals s WHERE s.side='compra' AND s.alerted=1 "
            "AND s.price_usd IS NOT NULL AND s.price_usd > 0 "
            "AND (s.chg_1h IS NOT NULL OR s.chg_24h IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM wallets w WHERE w.address = s.wallet "
            "AND COALESCE(w.is_bot, 0) = 0 "
            "AND (w.is_tracked = 1 OR w.winning_tokens_count >= 2))) "
            "AND s.ts BETWEEN ? AND ?", (_ini, _fin)).fetchone()
        r_si = conn.execute(
            "SELECT COUNT(*) v, "
            "COALESCE(SUM(CASE WHEN s.price_usd IS NOT NULL "
            "AND s.price_usd > 0 THEN 1 ELSE 0 END), 0) con_px "
            "FROM signals s JOIN wallets w ON w.address = s.wallet "
            "AND COALESCE(w.is_bot, 0) = 0 "
            "WHERE s.side='compra' AND s.alerted=0 "
            "AND s.ts BETWEEN ? AND ?", (_ini, _fin)).fetchone()
        al, al_px = r_al["v"], r_al["con_px"]
        med_px, al_m = r_med["v"], r_med["m"]
        si, si_px = r_si["v"], r_si["con_px"]
        al_sin, si_sin = al - al_px, si - si_px
        si_pct = (si_sin * 100.0 / si) if si else 0.0
        fuera = al_px - med_px          # alertadas que el medidor ya no ve
        # Frase de contexto: siempre se dice el numero de las silenciosas,
        # aunque el aviso lo dispare otra cosa. Son las que deciden que
        # candidata asciende a ⭐, asi que perderlas no es cosmetico.
        _ctx_si = (f"; aparte, {si_sin} de {si} señales silenciosas "
                   f"({si_pct:.0f}%) sin precio" if si else "")
        _ctx_fuera = (f"; {fuera} alertadas quedaron fuera del alcance "
                      f"del medidor (su billetera se degradó)"
                      if fuera > 0 else "")
        _ctx = _ctx_si + _ctx_fuera
        _accion_si = ("las silenciosas son las que deciden qué candidata "
                      "asciende a ⭐: con esa parte sin medir, el ranking "
                      "se decide con menos evidencia")

        if al == 0:
            # (Ola 17-M) Sin alertas NO se puede quedar mudo. `alerted`
            # significa desde la 17-L "Telegram la acepto": si el token
            # se rota o el chat se bloquea, esta columna deja de
            # escribirse y los tres CRIT de abajo se volvian
            # inalcanzables — justo mientras el medidor sigue perdiendo
            # datos. Sin alertas se juzga la poblacion silenciosa con los
            # mismos umbrales, que es la unica evidencia que queda.
            if si == 0:
                return _chk("Medición", OK,
                            "sin señales medibles vencidas en 7 días")
            _nota = "ninguna alerta en 7 días"
            if si_pct >= 15:
                return _chk("Medición", CRIT,
                            f"{_nota}; {si_sin} de {si} señales "
                            f"({si_pct:.0f}%) se quedaron SIN precio de "
                            f"entrada: no se pueden medir nunca",
                            "revisa si DexScreener está fallando o "
                            "limitando; y por qué no sale ninguna alerta")
            return _chk("Medición", WARN, f"{_nota}{_ctx}",
                        "comprueba que las alertas de Telegram salen: "
                        "desde la 17-L solo se marcan si se entregaron")
        if al_sin * 100.0 / al >= 15:
            return _chk("Medición", CRIT,
                        f"{al_sin} de {al} señales ALERTADAS "
                        f"({al_sin*100.0/al:.0f}%) se quedaron SIN precio "
                        f"de entrada: no se pueden medir nunca{_ctx}",
                        "revisa si DexScreener está fallando o limitando; "
                        "sin precio base no hay win rate real")
        # (17-N) Aqui habia un `if al_px == 0` con su propio CRIT: era
        # codigo muerto. Si `al > 0` y `al_px == 0`, entonces `al_sin ==
        # al` y el 100% supera el umbral del 15% de arriba, que ya ha
        # devuelto. Se quita en vez de dejar un aviso que no puede salir.
        if med_px == 0:
            # Ninguna alertada con precio esta medida NI sigue al alcance
            # del medidor. (17-N) Devolvia WARN, y eso era una regresion:
            # con la 17-L este mismo estado daba pct=0 → CRIT, y
            # `revisar_y_avisar` solo manda Telegram ante CRIT. Que el
            # medidor este parado y que las billeteras se hayan degradado
            # producen el mismo sintoma, asi que se nombran los dos.
            # (17-O) La 17-N lo puso en CRIT diciendo "o el medidor esta
            # parado, o las billeteras se degradaron". La primera causa
            # es IMPOSIBLE aqui: un medidor parado con billeteras
            # vigentes las mete en `med_px` por el EXISTS, y acaba en el
            # CRIT de `pct < 30`. Llegar aqui exige que TODAS esten fuera
            # de alcance, que es el caso benigno y ya documentado (8 de
            # 123 venian de billeteras marcadas bot despues de alertar).
            # En una semana floja con una sola alerta eso daba un rojo
            # por Telegram por un unico dato perdido. Vuelve a WARN, con
            # la frase que de verdad explica lo que paso.
            return _chk("Medición", WARN,
                        f"las {al_px} alertas con precio quedaron fuera "
                        f"del alcance del medidor{_ctx_si}",
                        "sus billeteras se degradaron antes de que el "
                        "medidor llegara; si se repite, mira /errores")
        # El % de medicion se calcula sobre las que SI tenian precio Y el
        # medidor todavia alcanza; las demas ya salen arriba.
        pct = 100.0 * al_m / med_px
        if pct < 30:
            return _chk("Medición", CRIT,
                        f"solo {pct:.0f}% de las alertas medibles de 7 días "
                        f"medidas ({al_m}/{med_px}){_ctx}",
                        "sin medición no hay aprendizaje ni backtest fiable")
        if pct < 70:
            return _chk("Medición", WARN,
                        f"{pct:.0f}% de las alertas medidas "
                        f"({al_m}/{med_px} con precio){_ctx}",
                        "el medidor está perdiendo señales dentro de su "
                        "propia ventana")
        if si_pct >= 25:
            return _chk("Medición", WARN,
                        f"alertas OK ({pct:.0f}% medidas, {al_m}/{med_px})"
                        f"{_ctx}", _accion_si)
        return _chk("Medición", OK,
                    f"{pct:.0f}% de las alertas medidas ({al_m}/{med_px} "
                    f"con precio"
                    + (f", {al_sin} sin precio de entrada" if al_sin else "")
                    + f" · 7 días){_ctx}")
    except Exception as e:
        return _chk("Medición", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
            # (19-P) Los umbrales salen de la MISMA configuracion que usa
            # `evaluate_tracked`, no escritos a mano.
            #
            # Estaban a pelo (`winning_tokens_count >= 2`, `buy_sol >= 1`,
            # `entry_multiple >= 3`) y se habian separado de sus mandos:
            # el bot usa MIN_WINNING_TOKENS=1 y el dueño tiene
            # MIN_BUY_SOL=0.5. Resultado medido en su base el 1/9:
            # /salud decia "85 en cola de examen" cuando la cola real que
            # recorre el bot era de 11.786 — un factor de 138. El dueño
            # (y yo) tomamos decisiones sobre el gasto de Helius con ese
            # numero. Un panel de salud que miente es peor que no tenerlo.
            #
            # Mismo patron que la 19-L (AUTO_CYCLE_HOURS) y la 19-O
            # (FILTRO_PF_MIN): un valor duplicado a mano que se separa de
            # su fuente sin que nada avise.
            import os as _os
            try:
                import config as _cfg_e
                _min_gan = int(getattr(_cfg_e, "MIN_WINNING_TOKENS", 1))
                _min_sol = float(getattr(_cfg_e, "MIN_BUY_SOL", 1.0))
                _min_mul = float(getattr(_cfg_e, "MIN_ENTRY_MULTIPLE", 3.0))
            except Exception:
                _min_gan, _min_sol, _min_mul = 1, 1.0, 3.0
            try:
                from ai_analyst import REEVAL_DAYS as _rd
            except Exception:
                _rd = 3
            try:
                _rech = int(float(_os.getenv("REEVAL_RECHAZADAS_DIAS", "14")))
            except (TypeError, ValueError):
                _rech = 14
            _c3 = (datetime.now(timezone.utc) - timedelta(days=_rd)
                   ).isoformat(timespec="seconds")
            _c14 = (datetime.now(timezone.utc) - timedelta(days=_rech)
                    ).isoformat(timespec="seconds")
            en_cola = conn.execute(
                """SELECT COUNT(*) c FROM wallets w
                   WHERE COALESCE(is_bot,0)=0 AND winning_tokens_count >= ?
                     AND (ai_class IS NULL OR pnl_updated IS NULL
                          OR (is_tracked=1 AND pnl_updated < ?)
                          OR (is_tracked=0 AND pnl_updated < ?))
                     AND EXISTS (SELECT 1 FROM appearances a
                                 WHERE a.wallet = w.address
                                   AND COALESCE(a.buy_sol, 0) >= ?
                                   AND (a.entry_multiple IS NULL
                                        OR a.entry_multiple >= ?))""",
                (_min_gan, _c3, _c14, _min_sol, _min_mul)).fetchone()["c"]
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
        return _chk("Embudo", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
        return _chk("Presupuesto IA nube", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
            return _chk("IA local", WARN,
                        f"HTTP {r.status_code} en {_md_plano(url)}")
        modelos = [m.get("id") for m in r.json().get("data", [])]
        modelo = _modelo(conn)
        if modelo and modelos and modelo not in modelos:
            return _chk("IA local", WARN,
                        f"conectada pero sin el modelo {_md_plano(modelo)}",
                        "cárgalo en LM Studio (y revisa el auto-unload)")
        return _chk("IA local", OK,
                    f"responde · modelo {_md_plano(modelo)}")
    except Exception as e:
        grave = CRIT if not os.getenv("ANTHROPIC_API_KEY") else WARN
        return _chk("IA local", grave, f"no responde ({_md_plano(e)})",
                    "¿PC apagada o túnel caído? repite /ialocal <url>")


def _c_apis():
    """¿Están configuradas las claves necesarias?"""
    faltan = [n for n, v in (("HELIUS_API_KEY", os.getenv("HELIUS_API_KEY")),
                             ("TELEGRAM_BOT_TOKEN",
                              os.getenv("TELEGRAM_BOT_TOKEN")))
              if not v]
    if faltan:
        return _chk("Claves API", CRIT,
                    "faltan: " + ", ".join(f"`{_v}`" for _v in faltan)
                    + " (van en `bot_local.env`)",
                    "configúralas en el archivo de entorno del PC y "
                    "reinicia")
    sin_nube = not os.getenv("ANTHROPIC_API_KEY")
    try:
        from ia_puente import hay_ia
        ia_ok = hay_ia()
    except Exception:
        ia_ok = not sin_nube
    if not ia_ok:
        return _chk("Claves API", WARN, "sin IA disponible (ni local ni nube)",
                    "configura /ialocal <url> o una clave de la nube")
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
        return _chk("Créditos Helius", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


def _c_base_datos(conn):
    """Salud del archivo de la base.

    (Ola 18-Q, 30/8/2026) Antes esto daba ROJO por el TAMAÑO en MB y
    mandaba "pasa a Postgres o baja MAX_TRADES_TOTAL". Los tres consejos
    estaban mal medidos, comprobado en la base del dueño con 330 MB:

      · SQLite mueve bases de varios GB sin despeinarse. El tamaño, por
        sí solo, no pone en peligro nada.
      · `trades` eran 154.821 filas con el tope en 300.000, así que bajar
        MAX_TRADES_TOTAL no habría borrado ni una fila.
      · Y el respaldo NO estaba en peligro: desde la Ola 16 la copia
        verificada se guarda SIEMPRE en `backups/`; lo único que el
        tamaño impide es mandarla por Telegram, que admite 50 MB. Que un
        backup falte de verdad ya lo vigila `_c_backup`, y en ROJO.

    Lo que sí puede matar al bot es que se llene el DISCO. Eso es lo que
    se mira ahora. El resto es información: cuánto ocupa, cuánta de esa
    cifra son huecos que SQLite ya reutiliza por su cuenta, y si la copia
    diaria cabe o no en Telegram — dicho como un hecho, no como una
    alarma. En el repo NO hay ningún VACUUM, así que aquí no se nombra
    "compactar": sería mandar al dueño a un botón que no existe.
    """
    try:
        import config as _cfg
        try:
            from db import USE_PG as _pg
        except Exception:
            _pg = False
        ruta = getattr(_cfg, "DB_PATH", None)
        if _pg or not ruta or not os.path.exists(ruta):
            return _chk("Base de datos", OK, "en Postgres o sin archivo local")
        # El `-wal` cuenta: la base se abre en WAL (db.py) y esos MB
        # ocupan disco igual. Sin sumarlos, la cifra que ve el dueño y
        # las varas de disco se quedan cortas justo cuando hay escritura
        # pendiente, que es cuando más apretado va todo.
        _bytes = os.path.getsize(ruta)
        _wal_bytes = 0
        for _sufijo in ("-wal", "-shm"):
            try:
                if os.path.exists(ruta + _sufijo):
                    _wal_bytes += os.path.getsize(ruta + _sufijo)
            except OSError:
                pass
        _bytes += _wal_bytes
        mb = _bytes / 1e6
        try:
            from trades_store import estadisticas
            ops = estadisticas().get("operaciones", 0)
        except Exception:
            ops = 0
        # Se DICE que el WAL va dentro: si no, el dueño ve aquí 480 MB y
        # en el explorador de archivos 330, y no hay nada que se lo
        # explique.
        _tam_base = f"{mb:.0f} MB" if mb >= 1 else f"{mb * 1e3:.0f} KB"
        det = (_tam_base
               + (f" (incluye {_wal_bytes / 1e6:.0f} MB de WAL/SHM)"
                  if _wal_bytes >= 1e6 else "")
               + f" · {ops:,} operaciones guardadas")

        # Huecos: filas ya borradas por las purgas cuyo espacio SQLite no
        # le devuelve al disco. No es un problema, es espacio recuperable.
        hueco_mb = 0.0
        try:
            _fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
            _ps = conn.execute("PRAGMA page_size").fetchone()[0]
            hueco_mb = (_fl or 0) * (_ps or 0) / 1e6
        except Exception:
            hueco_mb = 0.0
        if hueco_mb >= 20 and mb and hueco_mb / mb >= 0.15:
            # No es un aviso: esas páginas SQLite las reutiliza para los
            # datos nuevos. Se dicen para que el número de MB del archivo
            # no asuste (en la base del dueño eran 77 de 330).
            det += (f" · {hueco_mb:.0f} MB son huecos de purgas que se "
                    f"reutilizan")

        # La copia diaria: se guarda SIEMPRE en disco. Se dice si además
        # cabe en Telegram, mirando el tamaño REAL de la última copia en
        # vez de suponer una tasa de compresión.
        _ult_mb = 0.0
        try:
            _dir = os.path.join(os.path.dirname(os.path.abspath(ruta)),
                                "backups")
            _cs = [os.path.join(_dir, f) for f in os.listdir(_dir)
                   if "backup_" in f
                   and os.path.isfile(os.path.join(_dir, f))]
            if _cs:
                _ult = max(_cs, key=os.path.getmtime)
                _umb = os.path.getsize(_ult) / 1e6
                # `max(0, ...)`: si el reloj del PC se atrasa (o la copia
                # viene de un equipo con la hora adelantada) la resta sale
                # negativa y el mensaje diría "hace -2 h".
                _uh = max(0.0,
                          (time.time() - os.path.getmtime(_ult)) / 3600)
                # ¿Es una copia TERMINADA o un archivo que está a medias?
                # El job copia con `shutil.copyfile` al nombre definitivo
                # y solo DESPUÉS apunta la hora (`maintenance.py`). Si el
                # disco se llena a mitad queda un archivo truncado con la
                # fecha de hace un segundo, y sin este cruce /salud
                # anunciaba "última copia 0 MB hace 0 h" justo encima de
                # `_c_backup` gritando "SIN RESPALDO desde hace 90 h".
                # Se miran los DOS relojes, porque el /backup manual usa
                # el suyo.
                #
                # Pero el que no cuadre NO se dice como una acusación,
                # porque hay un camino honrado que también deja el reloj
                # sin marcar: el /backup manual guarda la copia, intenta
                # mandarla por Telegram y solo marca su reloj DESPUÉS; si
                # el envío revienta (el SSLError de siempre), la copia es
                # buena y el reloj se queda sin poner. Así que sin marca
                # se dice lo único que se sabe seguro —qué archivo hay
                # ahí y de cuándo— y no se le llama "copia".
                # El margen es de una hora: en el camino manual, entre
                # guardar y marcar hay dos viajes a Telegram, y subir 50
                # MB por una línea lenta pasa de sobra de unos minutos.
                _reg = None
                try:
                    _a = float(get_setting(conn, "last_backup_ts", 0) or 0)
                    _b = float(get_setting(
                        conn, "last_backup_manual_ts", 0) or 0)
                    _reg = max(_a, _b)
                except Exception:
                    _reg = None          # sin relojes: no se juzga
                _registrada = (_reg is None
                               or (_reg > 0
                                   and os.path.getmtime(_ult)
                                   <= _reg + 3600))
                # Nunca se dice "0 MB" de algo que existe: con `.0f`
                # una copia de 300 KB salía como "0 MB", que es justo la
                # cifra que la línea de al lado usa para decir VACÍO —
                # el mismo número para dos cosas opuestas. Por debajo de
                # 1 MB se habla en KB.
                _utam = (f"{_umb:.0f} MB" if _umb >= 1
                         else f"{_umb * 1e3:.0f} KB")
                if _umb <= 0:
                    # 0 bytes no admite interpretación: no es una copia.
                    det += (f" · en `backups/` el archivo más nuevo está "
                            f"VACÍO (0 bytes, hace {_uh:.0f} h)")
                elif not _registrada:
                    det += (f" · archivo más nuevo en `backups/`: "
                            f"{_utam} hace {_uh:.0f} h (ningún "
                            f"backup lo ha registrado todavía)")
                else:
                    # Se dice la EDAD: sin ella esta línea podía anunciar
                    # una copia de hace un mes como si fuera la de hoy. Y
                    # se dice si CABE en Telegram, no si se envió: eso no
                    # se sabe desde aquí (el POST puede haber muerto con
                    # un SSLError, que es justo lo que dejó al bot tres
                    # días sin respaldo).
                    # La tasa de compresión solo se aprende de un `.gz`.
                    # El /backup MANUAL guarda en `backups/` el `.db` SIN
                    # comprimir (`telegram_bot` pasa a
                    # `guardar_copia_en_disco` lo que devuelve
                    # `make_backup`, y quien comprime es `maintenance`,
                    # solo en el automático). Midiendo con ese `.db` la
                    # tasa sale ≈1, la vara del temporal se va al tope de
                    # 2× y /salud daría ROJO en el temporal durante 24 h
                    # por haber pedido una copia a mano: el mismo rojo
                    # falso que esta ola vino a quitar.
                    if _ult.endswith(".gz"):
                        _ult_mb = _umb
                    det += (f" · última copia {_utam} hace "
                            f"{_uh:.0f} h en `backups/`"
                            + (" (no cabe en Telegram, tope 50 MB)"
                               if _umb > 49 else " (cabe en Telegram)"))
        except Exception:
            pass

        # LO ÚNICO QUE DE VERDAD IMPORTA: que quede sitio en el disco.
        # Se miran los DOS sitios donde el backup necesita espacio, y con
        # una vara DISTINTA en cada uno, porque necesitan cosas distintas:
        #   · la carpeta de la base tiene que albergar la base Y la copia
        #     rotada que se guarda a su lado (hasta 5): 2× la base, con un
        #     suelo de 500 MB para que una base diminuta en un disco lleno
        #     también avise.
        #   · el temporal alberga, a la vez, la copia sin comprimir Y su
        #     .gz (`maintenance` borra el .db DESPUÉS de escribir el .gz):
        #     el pico es 1× + lo que comprima. Ese "lo que comprima" NO
        #     se supone: se mide con el tamaño real de la última copia
        #     (en la base del dueño, 142 de 330 MB → 1,48×). Sin copia
        #     todavía, 1,5×. Y NO lleva suelo: un /tmp en tmpfs de 64 MB
        #     es normal en contenedores, y medirlo con la vara de la otra
        #     carpeta habría devuelto el rojo permanente que esta ola
        #     vino a quitar, solo que en otro montaje y con un consejo
        #     ("borra `backups/`") que allí no libera ni un byte.
        # Se dice CUÁL de los dos es el que va justo: "GB libres" a secas
        # sería el mínimo de dos sistemas de ficheros distintos contado
        # como si fuera uno.
        try:
            import shutil as _sh
            import tempfile as _tmpf
            _base_dir = os.path.dirname(os.path.abspath(ruta)) or "."
            _tmp_dir = _tmpf.gettempdir()
            _libre_base = _sh.disk_usage(_base_dir).free
            # ¿Son el MISMO volumen? Se pregunta al sistema (`st_dev`; en
            # Windows, el número de serie del volumen), no se deduce de
            # que los bytes libres coincidan: en el PC del dueño la base
            # y `%TEMP%` están los dos en `C:`, las dos medidas se toman
            # con microsegundos de diferencia y el propio bot está
            # escribiendo el WAL — basta un bloque de deriva para que
            # /salud hable de "el temporal" como si fuera otro disco.
            try:
                _mismo = (os.stat(_base_dir).st_dev
                          == os.stat(_tmp_dir).st_dev)
            except Exception:
                _mismo = (os.path.normcase(os.path.abspath(_tmp_dir))
                          == os.path.normcase(os.path.abspath(_base_dir)))
            _libre_tmp = (_libre_base if _mismo
                          else _sh.disk_usage(_tmp_dir).free)
        except Exception as _e:
            # Ciego, no sano: antes esto devolvía VERDE y la línea del
            # disco desaparecía sin una palabra.
            return _chk("Base de datos", WARN,
                        det + " · no se pudo medir el disco",
                        "comprueba los permisos de la carpeta "
                        f"({_md_plano(_e)})")
        det += (f" · {_libre_base / 1e9:.1f} GB libres donde vive la base"
                + ("" if _mismo
                   else f" y {_libre_tmp / 1e9:.1f} GB en el temporal"))
        # La ruta del temporal se enseña SOLO si sale entera: `_md_plano`
        # cambia `_` por espacio, así que un `C:\Users\juan_perez\...`
        # saldría como una carpeta que no existe. Si lleva alguno de esos
        # símbolos se nombra la variable y el dueño la mira él.
        _tmp_txt = (f"({_md_plano(_tmp_dir)})"
                    if not any(_s in _tmp_dir for _s in "*_`[]")
                    else "(la carpeta de TMPDIR / %TEMP%)")
        _rojo_base = max(mb * 1e6 * 2, 500e6)
        _factor_tmp = 1.5
        if _ult_mb > 0 and mb > 0:
            _factor_tmp = min(2.0, max(1.3, 1.0 + _ult_mb / mb + 0.05))
        _rojo_tmp = mb * 1e6 * _factor_tmp
        _aviso_base = max(_rojo_base + min(mb * 1e6 * 4, 5e9), 1e9)
        # Un solo volumen = un solo sitio: se queda la vara más
        # exigente de las dos y el mensaje habla de un sitio solo. Si no,
        # /salud diría "y en el temporal" de una carpeta que está en el
        # mismo disco que acaba de nombrar, y mandaría a mirar TMPDIR
        # cuando lo que hay que hacer es liberar ese disco.
        if _mismo:
            # La primera línea es un SEGURO, no lógica viva: con el tope
            # de 2,0 en `_factor_tmp`, `_rojo_tmp` nunca pasa de
            # `_rojo_base`. Se deja por si algún día se sube ese tope.
            _rojo_base = max(_rojo_base, _rojo_tmp)
            _aviso_base = max(_aviso_base, _rojo_tmp * 1.5)
        _falta_base = _libre_base < _rojo_base
        _falta_tmp = (not _mismo) and _libre_tmp < _rojo_tmp
        if _falta_base or _falta_tmp:
            # Los dos montajes pueden ir justos a la vez (y van SIEMPRE
            # juntos cuando son el mismo disco). Decir solo uno dejaba al
            # dueño arreglando la mitad y volviendo a ver el mismo rojo.
            if _falta_base and _falta_tmp:
                _donde = ("la carpeta de la base y el temporal "
                          f"{_tmp_txt}")
                _como = ("libera espacio o borra copias viejas de la "
                         "carpeta de backups; el temporal necesita sitio "
                         "para la copia sin comprimir (cambia TMPDIR si "
                         "está en otro disco)")
            elif _falta_base:
                _donde = "la carpeta de la base"
                _como = ("libera espacio o borra copias viejas de la "
                         "carpeta de backups")
            else:
                _donde = f"el temporal {_tmp_txt}"
                _como = ("el backup escribe ahí su copia sin comprimir; "
                         "dale más espacio o cambia TMPDIR")
            return _chk("Base de datos", CRIT,
                        det, f"queda poco sitio en {_donde}: {_como}")
        # Aviso previo, en LOS DOS montajes. Va SIEMPRE por encima de su
        # línea roja: se suma un margen al umbral del rojo en vez de
        # calcular una cifra suelta. Antes era `max(min(6×, 5 GB), 1 GB)`
        # y para una base de 10 GB ese techo caía POR DEBAJO del rojo
        # (20 GB), así que se pasaba de verde a rojo sin amarillo. El
        # margen lleva su propio techo (5 GB) para que una base enorme no
        # deje /salud en amarillo eterno, y suelo de 1 GB para que una
        # base diminuta también tenga banda de aviso. El temporal lleva
        # su propio aviso (1,5× su rojo) y sin suelo, por lo mismo que su
        # rojo no lo lleva.
        _poco_base = _libre_base < _aviso_base
        _poco_tmp = (not _mismo) and _libre_tmp < _rojo_tmp * 1.5
        if _poco_base or _poco_tmp:
            if _poco_base and _poco_tmp:
                _aviso = ("queda poco disco donde vive la base y en el "
                          "temporal; vigila la carpeta de backups")
            elif _poco_base:
                _aviso = ("queda poco disco donde vive la base; vigila la "
                          "carpeta de backups")
            else:
                _aviso = (f"queda poco sitio en el temporal "
                          f"{_tmp_txt}, donde el backup escribe su copia "
                          f"antes de comprimirla")
            return _chk("Base de datos", WARN, det, _aviso)
        return _chk("Base de datos", OK, det)
    except Exception as e:
        return _chk("Base de datos", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


def _c_laserstream():
    """¿Está viva la escucha en tiempo real de menor latencia?"""
    try:
        from laserstream import activo, estado
        if not activo():
            return _chk("LaserStream", OK, "desactivado (solo webhook)")
        e = estado()
        if e.get("conectado"):
            return _chk("LaserStream", OK,
                        f"conectado · {int(e.get('recibidas', 0) or 0)} "
                        f"transacciones")
        return _chk("LaserStream", WARN,
                    "desconectado"
                    + (" — " + _md_plano(e["error"])
                       if e.get("error") else ""),
                    "el webhook sigue funcionando como respaldo")
    except Exception as e:
        return _chk("LaserStream", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
            return _chk("Errores", CRIT, f"{n} en 24 h · el más "
                        f"frecuente: {_md_plano(peor.get('modulo'))} "
                        f"({_md_plano(peor.get('tipo'))})",
                        "mira /errores para el detalle")
        return _chk("Errores", WARN, f"{n} en 24 h · más frecuente: "
                    f"{_md_plano(peor.get('modulo'))} "
                    f"({_md_plano(peor.get('tipo'))})",
                    "mira /errores para el detalle")
    except Exception as e:
        return _chk("Errores", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
        return _chk("Backup", WARN,
                    f"no se pudo comprobar ({_md_plano(e)})")


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
        checks.append(_chk(
            "Base de datos", CRIT, f"no accesible: {_md_plano(e)}",
            "si es el archivo local, mira el disco y los permisos de su "
            "carpeta; si va con Postgres, la base remota"))
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
    # (Ola 18-Q) El texto de la IA es el ÚNICO que entraba crudo en el
    # mensaje, y encima `revisar_y_avisar` lo pide SIEMPRE: un `_` impar
    # en la respuesta del modelo (y las hay: "falta PUBLIC_URL",
    # "el modulo wallet_analyzer") tumbaba el formato del /salud entero.
    # Se sanea ANTES de recortar, para no partir nada a mitad.
    return _md_plano(txt)[:1500]


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
    # (Ola 18-Q) Telegram corta en 4096 y `tg_send` NO recorta: pasado
    # ese tope devuelve 400 y el reintento en texto plano falla igual,
    # porque el problema es el tamaño, no el formato. Y como la firma ya
    # se marcó arriba, el aviso se perdía y no se volvía a intentar en
    # 12 h. Si no cabe, lo primero que se cae es la lectura de la IA,
    # que es lo prescindible: los chequeos son el aviso.
    _cab = "⚠️ *Autodiagnóstico*\n\n"

    def _cabe(_t):
        return len((_cab + _t).encode("utf-16-le")) // 2 <= 4000

    if not _cabe(texto):
        # Se cortan LÍNEAS ENTERAS por el final, no caracteres: cada
        # línea lleva sus `*...*` cerrados, así que cortar a mitad
        # devolvería otro 400. Como la lectura de la IA va al final, lo
        # primero que se cae es lo prescindible; los chequeos, que son
        # el aviso de verdad, se quedan.
        _ls = texto.split("\n")
        while _ls and not _cabe("\n".join(_ls) + "\n…(recortado)"):
            _ls.pop()
        texto = "\n".join(_ls) + "\n…(recortado)"
    try:
        from realtime import tg_send
        if not tg_send(_cab + texto):
            # No quemar las 12 h de silencio por un aviso que no salió.
            _c2 = get_conn()
            try:
                from db import set_setting as _ss
                _ss(_c2, "salud_ultimo_aviso", "")
            finally:
                _c2.close()
    except Exception:
        pass
    return texto
