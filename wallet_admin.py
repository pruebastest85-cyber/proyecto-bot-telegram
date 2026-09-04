"""
Gestión manual de billeteras y armado del mensaje /top.

  - discard_wallet / restore_wallet: descartar una billetera (deja de
    rastrearse y sale del top) o revertir el descarte. Ambas
    resincronizan el webhook de Helius al instante.
  - build_top_message: arma el texto del /top con alias IA, PnL y
    teclado inline (❌ para descartar, tamaños 10/20/30).
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from db import get_conn, top_wallets
from realtime import sync_helius_webhook

TOP_SIZES = (10, 20, 30)


def discard_wallet(address: str) -> str:
    """Marca la billetera como bot, le quita la ⭐ y resincroniza el webhook."""
    conn = get_conn()
    try:                       # (Ola 15 - M7) sin fugas de conexión
        row = conn.execute("SELECT address FROM wallets WHERE address=?",
                           (address,)).fetchone()
        if not row:
            return "No existe esa dirección en la base."
        conn.execute(
            """UPDATE wallets SET is_bot=1, is_tracked=0, confirmada=0,
               prueba_desde=NULL, turno_desde=NULL,
               ai_class='descartada', ai_follow=0,
               ai_reason='Descartada manualmente por el admin'
               WHERE address=?""", (address,))
        conn.commit()
    finally:
        conn.close()
    # (Ola 15 - B5) Invalidar el conjunto operativo YA: sin esto, la
    # billetera recién descartada seguía alertando y disparando copias
    # hasta 60 s (el TTL de la caché).
    try:
        from db import invalidar_copiables
        invalidar_copiables()
        from realtime import invalidar_vigiladas
        invalidar_vigiladas()
    except Exception as e:
        print(f"· No pude invalidar cachés tras descartar: {e}")
    hook = sync_helius_webhook()
    return f"❌ {address[:8]}… descartada. {hook}"


def _y(items) -> str:
    """'a', 'a y b', 'a, b y c' — para que el aviso se lea como español."""
    if len(items) <= 2:
        return " y ".join(items)
    return ", ".join(items[:-1]) + " y " + items[-1]


def restore_wallet(address: str) -> str:
    """Revierte un descarte: vuelve a rastrear y la IA la reevaluará."""
    # (Ola 16) Mismo patrón que discard_wallet: try/finally SIN except.
    # Un fallo de base sube y se ve como lo que es; antes un except lo
    # disfrazaba de "no existe esa dirección" y mandaba al dueño a buscar
    # un problema que no era.
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT address, is_tracked, confirmada, grade, "
            "hold_median_min FROM wallets WHERE address=?",
            (address,)).fetchone()
        if not row:
            return "No existe esa dirección en la base."
        _ya_tenia = bool(row["is_tracked"])
        # ai_follow=1: sin esto, recompute_scores retiraba la ⭐ restaurada
        # en el siguiente ciclo antes de que la IA la reevaluara.
        # ai_class=NULL fuerza la reevaluacion igualmente.
        # (18-L) confirmada=0 + reloj de prueba: restaurar da la estrella,
        # no el altavoz — las puertas deciden si alerta, como a todas.
        # (18-O) A la que estaba FUERA se le ponen grade y
        # hold_median_min en NULL: los dos venían del perfilado anterior
        # al descarte y los dos bloques de cabeza de `depurar_estrellas`
        # los leen tal cual, así que le quitaban la ⭐ en el ciclo
        # siguiente por la misma nota que el dueño ACABA de revertir. En
        # NULL ninguno de los dos la toca. (Si la IA no vuelve a
        # perfilarla —su cola tiene sus propios requisitos— se queda sin
        # nota, que es preferible a perder la estrella por una vieja.)
        import time as _t
        _ahora = int(_t.time())
        # RESTAURAR ES PARA LAS QUE ESTÁN FUERA. Si la billetera YA tenía
        # la ⭐ (el dueño puede escribir /rastrear de más, o sobre una que
        # ya sigue), esto no puede castigarla: conservaría su reloj de
        # turno, su confirmación —quitársela la saca del altavoz hasta el
        # siguiente ciclo, hasta 2 h muda— y su nota y su retención, que
        # solo se recuperan si vuelve a entrar en la cola del perfilado,
        # cosa que no está garantizada. Solo se reinicia lo que hay que
        # reiniciar cuando de verdad estaba fuera.
        # `/rastrear` SIEMPRE estrena el reloj del turno, la tuviera ya
        # o no, y el mensaje lo dice con todas las letras. Es el sentido
        # entero del mando: "a esta la quiero, empieza de cero". Si se
        # conservara el reloj de una ⭐ viva —que hoy es NULL en TODAS,
        # porque la columna acaba de nacer— el mando no protegería de
        # nada justo en la población real: la racha la degradaría 15 min
        # después con el expediente de siempre, que es EL fallo que esta
        # ola viene a arreglar. Que se pueda repetir no es un agujero:
        # es una orden del dueño, y se le informa cada vez.
        _turno = _ahora
        # Lo que SÍ se respeta de una ⭐ viva es su estado del embudo:
        # quitarle la confirmación la deja muda hasta el próximo ciclo, y
        # borrarle nota y retención la castiga por un mando que no pedía
        # castigo. A la que estaba fuera se le limpian las dos porque
        # vienen del perfilado anterior al descarte.
        _conf = (row["confirmada"] or 0) if _ya_tenia else 0
        _grade = row["grade"] if _ya_tenia else None
        _hold = row["hold_median_min"] if _ya_tenia else None
        # Copias para el mensaje: la fila se lee dentro del `with` de la
        # conexión y fuera ya no está disponible.
        row_grade, row_hold = _grade, _hold
        # El plazo de inactividad se reinicia igual en los dos casos: la
        # billetera acaba de recibir la atención del dueño.
        _prueba = _ahora
        # La ficha se ANTEPONE, no se borra: es el único rastro que tiene
        # el dueño de por qué esa billetera llegó hasta aquí (mismo
        # criterio que la racha y la revisión desde 18-O).
        conn.execute(
            """UPDATE wallets SET is_bot=0, is_tracked=1,
               confirmada=?, prueba_desde=?, turno_desde=?,
               ai_class=NULL, ai_follow=1, grade=?,
               hold_median_min=?,
               ai_reason=SUBSTR(? || COALESCE(ai_reason,''), 1, 500)
               WHERE address=?""",
            (_conf, _prueba, _turno, _grade, _hold,
             "⭐ restaurada a mano por el admin · ", address))
        conn.commit()
        # (18-O) Se le dice al dueño la VERDAD en el momento: si su
        # historial no pasa las puertas 1-2, el ciclo le va a quitar la
        # estrella y es mejor que lo sepa ahora que descubrirlo mañana.
        # (18-O, ronda 3) Se miran las TRES puertas, no solo el
        # historial: el caso más frecuente de /rastrear es una billetera
        # a la que se degradó por su RENDIMIENTO MEDIDO, y esas señales
        # no caducan (ver `medidas`). Avisando solo de las puertas 1-2 el
        # dueño recibía un mensaje limpio y la ⭐ se quedaba muda sin que
        # nada le dijera por qué.
        _pasa, _motivo, _pasa12 = True, "", True
        _embudo = True
        try:
            from filtro_calidad import (_cfg, historial, medidas, puertas,
                                        puertas_historial)
            # Con FILTRO_TRES_PUERTAS=0 la clasificación confirma a TODAS
            # y /reembudo se niega a correr: avisar de que "no pasa el
            # embudo" sería mentira por partida doble.
            _embudo = bool(_cfg()["activo"])
            if _embudo:
                _h = historial(conn, address).get(address)
                _pasa, _motivo = puertas(
                    _h, medidas(conn, address).get(address))
                # Se distingue en qué se queda, porque el remedio no es el
                # mismo: /reembudo solo retira a quien falla el HISTORIAL.
                _pasa12 = puertas_historial(_h)[0]
        except Exception as _e:
            print(f"· aviso del embudo omitido: {_e}")
            _pasa, _motivo, _pasa12 = True, "", True
        # La regla de CREADORA DE MERCADO del dueño no perdona ni con
        # /rastrear (dar vueltas al mismo token no deja de ser
        # incopiable porque el dueño la restaure), así que el ciclo se la
        # quitará igual: mejor decírselo aquí que dejar que desaparezca.
        try:
            from performance_review import creadora_de_mercado
            _mm = creadora_de_mercado(conn, address)
        except Exception as _e:
            print(f"· aviso de creadora de mercado omitido: {_e}")
            _mm = None
        # A la ⭐ VIVA se le conservan nota y retención (borrárselas la
        # castigaría por un mando que no pidió castigo), pero entonces
        # los dos primeros bloques de `depurar_estrellas` sí pueden
        # quitarle la estrella por ellas: se avisa.
        _herencia = []
        if _ya_tenia:
            try:
                from ai_analyst import nota_vinculante
                from grading import MIN_HOLD_MIN
                _g = row["grade"]
                _malo = (_g == "Descartada") or (
                    nota_vinculante(conn) and _g is not None
                    and _g not in ("Elite", "Seguimiento"))
                if _malo:
                    _herencia.append("su nota del embudo es "
                                     f"«{_g or 'sin nota'}»")
                _hm = row["hold_median_min"]
                if _hm is not None and _hm < MIN_HOLD_MIN:
                    _herencia.append(f"retiene {_hm:.1f} min de mediana "
                                     f"(mínimo {MIN_HOLD_MIN:.0f})")
            except Exception as _e:
                print(f"· aviso de herencia omitido: {_e}")
    finally:
        conn.close()
    try:                       # (Ola 15 - B5) que alerte desde YA
        from db import invalidar_copiables
        invalidar_copiables()
        from realtime import invalidar_vigiladas
        invalidar_vigiladas()
    except Exception as e:
        print(f"· No pude invalidar cachés tras restaurar: {e}")
    hook = sync_helius_webhook()
    # EL MENSAJE DICE LA VERDAD, TODA Y SOLO LA VERDAD. Es lo único que
    # el dueño ve de este mando, y la ola nació justamente porque el bot
    # deshacía en silencio lo que él acababa de pedir.
    partes = []
    partes.append("Desde ahora se la juzga por lo que haga a partir de "
                  "este momento: sus señales medidas anteriores dejan de "
                  "contar en su contra (racha y revisión de rendimiento).")
    if _ya_tenia:
        # Se enumera lo que DE VERDAD conserva: decirle "se le conserva
        # la confirmación" a una que estaba en prueba sería mentira.
        _conserva = []
        if _conf:
            _conserva.append("la confirmación")
        if row_grade:
            _conserva.append(f"la nota ({row_grade})")
        if row_hold is not None:
            _conserva.append("la retención medida")
        partes.append(
            "Ya la tenías, así que no se le quita nada de lo suyo"
            + (": se le conservan " + _y(_conserva) if _conserva
               else " (no tenía confirmación, ni nota, ni retención)")
            + ". Sí se le borra la clasificación anterior de la IA para "
              "que la vuelva a evaluar (si entra en la cola del perfilado: "
              "necesita al menos una entrada temprana registrada en un "
              "token ganador).")
    if not _pasa:
        _que_pasa = ("Conserva la confirmación, así que de momento puede "
                     "alertar y copiarse, pero la próxima clasificación "
                     "(como mucho un ciclo) se la quitará por esto."
                     if _conf else
                     "Se la vigila y se miden sus señales, pero no alerta "
                     "ni se copia mientras no pase.")
        partes.append(f"⚠️ Hoy NO pasa el embudo ({_motivo}). {_que_pasa}")
        partes.append("Su historial sí pasa: lo que falla son sus señales "
                      "medidas, así que /reembudo NO se la llevaría."
                      if _pasa12 else
                      "Es el historial lo que falla, así que /reembudo se "
                      "la llevaría junto a las demás que no pasan.")
    elif _embudo and not _conf and not _mm and not _herencia:
        # OJO con el `and`: si abajo se avisa de que el ciclo le va a
        # quitar la ⭐ (creadora de mercado o nota/retención heredadas),
        # prometer que "empezará a alertar" sería falso — esos bloques
        # de la depuración corren ANTES que la clasificación, así que no
        # llega a confirmarse nunca.
        partes.append("Pasa el embudo, así que empezará a alertar y a "
                      "copiarse en cuanto el bot la clasifique (como "
                      "mucho un ciclo); hasta entonces está en silencio.")
    if _mm:
        partes.append(f"⛔ Es CREADORA DE MERCADO ({_mm}). Esa regla tuya "
                      "no la levanta /rastrear: el próximo ciclo le "
                      "quitará la ⭐ igualmente.")
    if _herencia:
        partes.append("⚠️ Arrastra del perfilado anterior: "
                      + " y ".join(_herencia)
                      + ". Por eso el próximo ciclo puede quitarle la ⭐.")
    partes.append("(Hay más reglas que pueden retirarla en el ciclo: que "
                  "al re-perfilarla la IA la rechace o le ponga una nota "
                  "mala, que la vuelva a ver como bot o como entidad "
                  "conocida, que una hermana de su familia puntúe mejor, "
                  "o que no quepa en el presupuesto de atención.)")
    aviso = "\n\n" + "\n\n".join(f"· {p}" for p in partes)
    return f"⭐ {address[:8]}… vuelve a rastrearse. {hook}{aviso}"


def _campo(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def build_top_message(limit: int = 10):
    """Arma el texto y el teclado inline del /top."""
    conn = get_conn()
    rows = top_wallets(conn, limit)
    conn.close()
    if not rows:
        return ("Aún no hay billeteras. Espera el próximo ciclo o corre /ciclo.",
                None)
    lines = [f"🏆 *Top {len(rows)} billeteras candidatas:*\n"
             "_Orden: primero las ⭐ cuyo historial de COPIA gana "
             "(📐 copiable), luego las aún sin medir, al final las que "
             "copiarlas pierde._\n"]
    buttons, row_btns = [], []
    for i, w in enumerate(rows, 1):
        flag = " ⭐" if w["is_tracked"] else ""
        ai_class = _campo(w, "ai_class")
        ai = f" · 🧠 {ai_class}" if ai_class else ""
        alias = _campo(w, "alias")
        ws = _campo(w, "wallet_score")
        etiqueta_ws = f" · 🧮 {ws:.0f}/100" if ws is not None else ""
        nombre = f"👤 *{alias}*{etiqueta_ws}\n   " if alias else ""
        partes = []
        p30, ptot = _campo(w, "pnl_30d"), _campo(w, "pnl_total")
        if p30 is not None:
            partes.append(f"30d: {p30:+.1f}")
        if ptot is not None:
            partes.append(f"hist: {ptot:+.1f}")
        # (Ola 8, 21/8) pnl_30d/pnl_total son de la ULTIMA evaluacion
        # de la billetera (se refrescan cada 3-14 dias), no de hoy.
        pnl = ("\n   💰 PnL (SOL, últ. evaluación) "
               + " · ".join(partes)) if partes else ""
        # (19-AH) Lo MEDIDO al copiarla: es lo que ordena el top.
        try:
            from copiabilidad import linea_top
            copi = "\n" + linea_top(w) if w["is_tracked"] else ""
        except Exception as e:
            print(f"· /top: sin línea copiable ({e})")
            copi = ""
        lines.append(
            f"{i}. {nombre}`{w['address']}`\n"
            f"   ganadores: {w['winning_tokens_count']} · "
            f"score: {w['score']:.1f}{flag}{ai}{pnl}{copi}\n")
        row_btns.append(InlineKeyboardButton(
            f"❌ {i}", callback_data=f"d:{limit}:{w['address']}"))
        if len(row_btns) == 5:
            buttons.append(row_btns)
            row_btns = []
    if row_btns:
        buttons.append(row_btns)
    buttons.append([
        InlineKeyboardButton(("· " if n == limit else "") + f"Top {n}",
                             callback_data=f"t:{n}")
        for n in TOP_SIZES
    ])
    lines.append("\n❌ n = descartar la billetera nº n (deja de rastrearse "
                 "y no vuelve al top).\nUsa /evidencia <address> para ver el porqué.")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)
