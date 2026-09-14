"""
Las tres puertas: quien merece que le copiemos.
(Embudo v2, fase 9 — 09/09/2026)

POR QUE EXISTE
--------------
Las fases anteriores MIDEN. Esta DECIDE — o mejor dicho, prepara la
decision: pone a cada billetera en una etapa (`wallets.wallet_stage`) y
deja escrito por que, para que el dueño pueda discutirlo con
`/porque <billetera>` en vez de tragarse un numero.

Se llaman puertas y no "puntuacion" a proposito: son tres condiciones que
hay que pasar EN ORDEN, y basta fallar una para quedarse fuera. Una nota
media alta puede esconder un cero en algo que importa; una puerta no.

LAS TRES, EN LAS PALABRAS DEL DUEÑO
------------------------------------
1. **¿Se le puede copiar?** No es un bot, y no entra y sale tan rapido
   que cuando nuestra alerta llegue ya se haya ido. Da igual lo bien que
   opere: si no se le puede seguir, no sirve.
2. **¿Gana de verdad?** Con material suficiente para saberlo (no una
   operacion con suerte), multiplo tipico por encima de 1, HABER PEGADO
   ALGUNA VEZ UN x10, y ganar mas SOL de los que pierde.
3. **¿Es la estrategia que quiero?** Aguanta al menos unas horas y mueve
   entre 1 y 100 SOL por operacion.

QUE CAMBIO EL 14/09 Y POR QUE (19-BS)
--------------------------------------
El embudo se quedo mudo: 0 alertas en 4 dias, 6 billeteras con permiso
para alertar de 34.131 en la base. La auditoria de ese dia encontro que
el 79 % de la actividad venia de billeteras que el embudo no podia
juzgar, y que las `copiable` producian 31 señales en 4 dias entre 5.

El dueño reescribio el criterio, y lo medido le da la razon:

  criterio viejo (0,5-5 SOL · 24 h · acierta >=50 %) →  47 billeteras, 10 activas
  criterio nuevo (1-100 SOL · algun x10 · PF >=2 · 5 h) → 59 billeteras, 14 activas

Los tres cambios, con sus palabras:

· **Capital 1-100 SOL** (antes 0,5-5). "No poner un minimo de inversion
  de 1 solana y el maximo de inversion 100 solanas". La banda vieja daba
  por hecho que solo multiplica quien entra con poco; dejaba fuera
  billeteras con x231 y x145 medidos que mueven 12 o 20 SOL.

· **Algun x10** (nuevo). "Que hayan obtenido buenas ganancias, se hayan
  marcado minimo un x10". En memecoins lo que paga es el pelotazo, no
  ganar poquito muchas veces.

· **Cuanto gana, no cuantas veces acierta.** "Si gano 3 y perdio 6 pero
  en esas 3 que gano se marco un x30, es muy rentable la billetera".
  Medido: 51 billeteras aciertan menos veces de las que fallan y aun asi
  tienen un x20 dentro y ganan de sobra. El winrate las echaba a todas.
  Ahora manda el profit factor y el winrate queda APAGADO de fabrica
  (`PUERTA_MIN_CONSISTENCIA = 0`), encendible sin desplegar.

Se retiro ademas la puerta doble de aguante de la 19-BP, que nunca llego
a subirse: medida sobre la base del dueño se llevaba 2 de las 4
copiables que de verdad operaban, una de ellas por seis segundos (29,9
min contra un minimo de 30). Apretar un embudo ahogado iba en direccion
contraria.

ETAPAS
------
- `copiable`     — pasa las tres.
- `observacion`  — pasa 1 y 2, falla la 3 (gana, pero no como el dueño
                   quiere: voltea rapido o mueve demasiado dinero).
- `candidata`    — pasa la 1, falla la 2 (se le podria copiar, pero
                   todavia no esta demostrado que gane).
- `descartada`   — falla la 1.
- `sin_datos`    — no hay con que juzgarla; NO es un suspenso.

QUE MANDA Y QUE NO (fase 10, 09/09/2026)
-----------------------------------------
El dueño lo encendio: "descartar todo lo que no queremos, que se quede lo
bueno". Desde la fase 10, con el embudo al mando:
- solo alertan y se copian las `copiable` (lo aplica `db._operativas`);
- una `copiable` que no era ⭐ pasa a serlo (`ascender`), porque el bot
  solo vigila en tiempo real a las ⭐ y si nadie la escucha no puede
  generar ni una señal;
- y desde la FASE 11 (10/09) una ⭐ ya juzgada que NO pasa las puertas
  deja de ser ⭐ (`degradar`), porque re-perfilarla cada 3 dias costaba
  ~7.500 creditos al dia en gente que el embudo ya habia descartado. No
  se degrada a quien todavia no tiene etapa ni a quien tiene una posicion
  de papel abierta. El historial NUNCA se borra: solo se apaga la
  escucha.

Sigue en pie la regla 55: esto NO mueve dinero real. Todo termina en
señales y en paper trading.

El interruptor vive en `db.embudo_manda()`, que mira primero el ajuste
`embudo_v2_activo` de la base — el que escribe `/embudo` — y se cae a
`config.EMBUDO_V2_ACTIVO`. Asi apagarlo es un mensaje de Telegram y no un
despliegue. Hay pruebas de que con el interruptor APAGADO nada cambia de
sitio, que es el modo al que se vuelve con `/embudo off`.

DE DONDE SALEN LOS UMBRALES
---------------------------
De lo medido en la base, no de mi cabeza — y cada uno se puede mover por
configuracion:
- El capital tipico se compara con la banda que fijo el dueño el 14/09:
  DESCUBRIMIENTO_MIN_SOL (1) a DESCUBRIMIENTO_MAX_SOL (100).
- "Aguantar" es PUERTA_MIN_HOLD_H (5 h). OJO: NO es `HOLD_MIN_HOURS`,
  que sigue en 24 h y define `q_hold` en `wallet_quality`. Son dos cosas
  distintas a proposito: mover la puerta no debe cambiar la NOTA de
  todas las billeteras de la base.
- El pelotazo minimo es PUERTA_MIN_MULT_MAX (x10) y el factor de
  ganancia minimo PUERTA_MIN_PF (2).
- El minimo de posiciones medidas es el mismo de la fase 6 (3): con una
  sola operacion no se distingue el acierto de la suerte.
"""

import config
from db import get_conn
from avisos import aviso as _avisar_ex

HORA = 3600

ETAPAS = ("copiable", "observacion", "candidata", "descartada", "sin_datos")


def _f(nombre, defecto):
    try:
        return float(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return float(defecto)


def _i(nombre, defecto):
    try:
        return int(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return int(defecto)


# ── las tres puertas ─────────────────────────────────────────────────

def puerta1(d: dict) -> tuple:
    """¿Se le puede copiar? (bool, motivo)"""
    if d.get("is_bot"):
        return (False, "marcada como bot: no es una persona operando")
    h = d.get("hold_median_h")
    minimo = _f("PUERTA_MIN_HOLD_COPIABLE_H", 1.0)
    if h is None:
        return (False, "no se sabe cuánto aguanta")
    if h < minimo:
        return (False, f"entra y sale en {h:.1f} h de media: cuando la "
                       f"alerta llegue ya se habrá ido")
    return (True, f"aguanta {h:.1f} h de media, da tiempo a seguirla")


def puerta2(d: dict) -> tuple:
    """¿Gana de verdad? (bool, motivo)"""
    n = int(d.get("posiciones") or 0)
    minimo = _i("CALIDAD_MIN_POSICIONES", 3)
    if n < minimo:
        return (False, f"solo {n} operaciones medidas: hacen falta "
                       f"{minimo} para distinguir el acierto de la suerte")
    mult = d.get("mult_realizado")
    if mult is None:
        return (False, "sin múltiplo medido")
    # (19-BS) Apagado de fabrica: ver la nota de PUERTA_MIN_MULTIPLO en
    # config. Exigir que la operacion TIPICA gane es el winrate con otro
    # nombre, y echaba a 41 billeteras con un x10 dentro que ganan mas de
    # lo que pierden.
    _minm = _f("PUERTA_MIN_MULTIPLO", 0.0)
    if _minm > 0 and mult <= _minm:
        return (False, f"su operación típica es x{mult:.2f}: no gana")
    # (19-BS) EL GOLPE GORDO. Palabras del dueño: "que se hayan marcado
    # mínimo un x10". No basta con ganar poquito muchas veces: lo que
    # paga en memecoins es haber acertado un pelotazo alguna vez.
    mejor = d.get("mejor_multiplo")
    minx = _f("PUERTA_MIN_MULT_MAX", 10.0)
    if mejor is None:
        return (False, "sin ninguna operación medida de punta a punta")
    if mejor < minx:
        return (False, f"su mejor operación fue x{mejor:.1f}: nunca ha "
                       f"pegado un x{minx:.0f}")
    # (19-BS) CUÁNTO gana por cada SOL que pierde, en vez de CUÁNTAS
    # veces acierta. Textual del dueño: "si ganó 3 y perdió 6 pero en
    # esas 3 que ganó se marcó un x30 de su inversión inicial, es muy
    # rentable la billetera". Medido en su base el 14/09: 51 billeteras
    # aciertan menos veces de las que fallan y aun asi tienen un x20
    # dentro y ganan de sobra. El filtro del winrate las echaba a todas.
    pf = d.get("profit_factor")
    minpf = _f("PUERTA_MIN_PF", 2.0)
    if pf is None:
        return (False, "sin operaciones cerradas para medir cuánto gana "
                       "por cada SOL que pierde")
    if pf < minpf:
        return (False, f"gana {pf:.2f} SOL por cada SOL que pierde "
                       f"(mínimo {minpf:g}): sus aciertos no compensan "
                       f"sus fallos")
    # El winrate deja de ser una puerta y pasa a ser un ajuste OPCIONAL,
    # apagado de fabrica (0). Mismo patron que `FILTRO_PF_MIN` en la
    # 19-N: el codigo se queda, la exigencia no, y encenderla es una
    # decision del dueño y no un despliegue.
    cons = d.get("q_consistency")
    minc = _f("PUERTA_MIN_CONSISTENCIA", 0.0)
    if minc > 0:
        if cons is None:
            return (False, "sin medida de cuántas veces acierta")
        if cons < minc:
            return (False, f"acierta el {cons:.0f} % de las veces, menos "
                           f"de {minc:.0f} %")
    return (True, f"mejor golpe x{mejor:.0f} · gana {pf:.1f} SOL por cada "
                  f"SOL que pierde · x{mult:.2f} típico en {n} operaciones")


def puerta3(d: dict) -> tuple:
    """¿Es la estrategia que el dueño quiere? (bool, motivo)"""
    h = d.get("hold_median_h")
    # (19-BS) La puerta tiene su PROPIO minimo (5 h, elegido por el dueño
    # el 14/09) y ya NO reutiliza `HOLD_MIN_HOURS`. Ese ajuste sigue
    # valiendo 24 h porque define `q_hold` en `wallet_quality` —"que
    # parte de sus posiciones aguanto mas de X"— y moverlo habria
    # cambiado la NOTA de todas las billeteras de la base como efecto
    # colateral de tocar una puerta. Dos cosas distintas, dos ajustes.
    #
    # Por que 5 h y no 24: medido sobre la base del dueño, exigir 24 h
    # dejaba 32 billeteras (11 activas) de las 110 que cumplen el resto;
    # con 5 h son 59 (14 activas). Y por debajo de 1 h entran los que
    # entran y salen en segundos, que no se pueden copiar.
    hold_h = _f("PUERTA_MIN_HOLD_H", 5.0)
    if h is None or h < hold_h:
        if h is None:
            return (False, "no se sabe cuánto aguanta")
        return (False, f"aguanta {h:.1f} h de media, menos de las "
                       f"{hold_h:g} h que pides")
    cap = d.get("capital_tipico")
    # (19-BS) Banda 1-100 SOL (antes 0,5-5). El dueño, textual: "no poner
    # un minimo de inversion de 1 solana y el maximo de inversion 100
    # solanas". La banda vieja daba por hecho que solo multiplica quien
    # entra con poco, y eso dejaba fuera a billeteras con x100 y x200
    # medidos que mueven 12, 20 o 35 SOL por operacion.
    lo = _f("DESCUBRIMIENTO_MIN_SOL", 1.0)
    hi = _f("DESCUBRIMIENTO_MAX_SOL", 100.0)
    if cap is None:
        return (False, "no se sabe con cuánto capital opera")
    if cap > hi:
        return (False, f"mueve {cap:.1f} SOL por operación, más de los "
                       f"{hi:g} que pides")
    if cap < lo:
        return (False, f"mueve {cap:.2f} SOL por operación: con menos de "
                       f"{lo:g} SOL el múltiplo no significa nada")
    return (True, f"aguanta {h:.0f} h con {cap:.1f} SOL típicos")


def evaluar(d: dict) -> dict:
    """Las tres puertas sobre una billetera. Devuelve etapa y motivos."""
    if not d or d.get("posiciones") is None:
        return {"wallet_stage": "sin_datos", "p1": None, "p2": None,
                "p3": None, "motivo": "todavía no hay nada medido",
                "motivos": []}
    ok1, m1 = puerta1(d)
    ok2, m2 = puerta2(d)
    ok3, m3 = puerta3(d)
    if not ok1:
        etapa, motivo = "descartada", m1
    elif not ok2:
        etapa, motivo = "candidata", m2
    elif not ok3:
        etapa, motivo = "observacion", m3
    else:
        etapa, motivo = "copiable", m3
    return {"wallet_stage": etapa, "p1": ok1, "p2": ok2, "p3": ok3,
            "motivo": motivo, "motivos": [m1, m2, m3]}


# ── datos de una billetera ───────────────────────────────────────────

def datos(conn, wallet: str) -> dict | None:
    """Lo que hace falta para las tres puertas, en una consulta."""
    try:
        f = conn.execute(
            """SELECT w.address, COALESCE(w.is_bot, 0) is_bot,
                      w.q_score, w.q_consistency, w.hold_median_h,
                      w.mult_realizado, w.estrategia, w.wallet_stage,
                      COALESCE(w.is_tracked, 0) is_tracked
               FROM wallets w WHERE w.address = ?""", (wallet,)).fetchone()
    except Exception as _ex:
        _avisar_ex("puertas:datos", _ex)
        return None
    d = dict(f) if f else {"address": wallet, "is_bot": 0, "q_score": None,
                           "q_consistency": None, "hold_median_h": None,
                           "mult_realizado": None, "estrategia": None,
                           "wallet_stage": None, "is_tracked": 0}
    try:
        # (19-BS) En la MISMA consulta salen el mejor golpe y el factor de
        # ganancia. `MAX(sol_out/sol_in)` con la guarda `sol_in > 0` para
        # no dividir por cero; las dos sumas son las dos patas del factor.
        r = conn.execute(
            """SELECT COUNT(*) n, AVG(sol_in) cap,
                      MAX(CASE WHEN sol_in > 0 THEN sol_out / sol_in END)
                          AS mejor,
                      SUM(CASE WHEN sol_out > sol_in
                               THEN sol_out - sol_in ELSE 0 END) AS gana,
                      SUM(CASE WHEN sol_out <= sol_in
                               THEN sol_in - sol_out ELSE 0 END) AS pierde
               FROM wallet_positions
               WHERE wallet = ? AND history_complete = 1
                 AND position_status = 'cerrada'""", (wallet,)).fetchone()
        d["posiciones"] = int(r["n"] or 0)
        d["capital_tipico"] = r["cap"]
        d["mejor_multiplo"] = r["mejor"]
        _g, _p = float(r["gana"] or 0.0), float(r["pierde"] or 0.0)
        if _p > 0:
            d["profit_factor"] = _g / _p
        elif _g > 0:
            # Sin una sola perdida. Se usa el mismo tope simbolico que
            # `wallet_metrics` y `filtro_calidad` (99.99) en vez de un
            # infinito inventado; la puerta de `posiciones` va delante y
            # evita que tres operaciones con suerte parezcan un genio.
            d["profit_factor"] = 99.99
        else:
            d["profit_factor"] = None
    except Exception as _ex:
        _avisar_ex("puertas:datos:posiciones", _ex)
        d["posiciones"] = 0
        d["capital_tipico"] = None
        d["mejor_multiplo"] = None
        d["profit_factor"] = None
    return d


def guardar(conn, wallet: str, veredicto: dict) -> bool:
    """Escribe la etapa. Crea la ficha si falta (las cazadas llegan por su
    historial, no por una aparicion) y dice False si no escribio nada —
    la leccion de la 19-BI."""
    if not wallet or not veredicto:
        return False
    try:
        from db import now_iso
        conn.execute(
            """INSERT OR IGNORE INTO wallets (address, first_seen,
                                              last_updated)
               VALUES (?,?,?)""", (wallet, now_iso(), now_iso()))
    except Exception as _ex:
        _avisar_ex("puertas:guardar:alta", _ex)
    try:
        cur = conn.execute(
            "UPDATE wallets SET wallet_stage = ? WHERE address = ?",
            (veredicto.get("wallet_stage"), wallet))
        return bool(cur.rowcount)
    except Exception as _ex:
        _avisar_ex("puertas:guardar", _ex)
        return False


def ascender(conn, wallet: str, veredicto: dict) -> bool:
    """(Fase 10) Con el embudo al mando, una `copiable` pasa a ⭐.

    POR QUE: el bot solo vigila en tiempo real a las ⭐. El 09/09 habia
    **24 billeteras que pasaban las tres puertas y no eran ⭐**: nadie las
    escuchaba, asi que no podian generar ni una señal y el embudo nuevo
    no tenia forma de demostrar si acierta. Decision del dueño ese dia:
    ascenderlas.

    Lo que esta funcion NO hace: **no degrada a nadie**. Las ⭐ que no
    pasan las puertas conservan su `is_tracked` y su historial; que no
    alerten ni se copien lo decide el conjunto operativo
    (`db._operativas`), que es reversible con apagar el embudo. Quitar
    `is_tracked` seria destructivo y no hace falta para lo que el dueño
    pidio.

    POR QUE TAMBIEN `ai_follow` (19-BM, mismo dia, fallo medido)
    -------------------------------------------------------------
    La primera version ponia SOLO `is_tracked = 1`. La pasada de las
    22:11 ascendio a 18 billeteras y a los pocos minutos estaban las 18
    otra vez sin ⭐. Causa raiz: `db.recompute_scores` barre a toda ⭐ que
    no tenga `ai_follow` puesto y le quita la estrella.

    O sea: **una ⭐ sin `ai_follow` no existe** en este sistema; es la
    regla que impide que una candidata sin aprobar acabe alertando en
    tiempo real. El promotor legitimo
    (`filtro_calidad`, "promovida por el embudo") pone las dos banderas
    juntas; aqui se ponia media, asi que cada pasada ascendia y cada
    barrida deshacia: un vaiven que no convergia nunca.

    Aqui `ai_follow = 1` significa exactamente lo que la bandera dice —
    "aprobada para vigilarla" — y quien aprueba son las tres puertas.
    Solo se pone con el embudo al mando; apagado, esta funcion no corre.
    """
    if not wallet or not veredicto:
        return False
    if veredicto.get("wallet_stage") != "copiable":
        return False
    try:
        cur = conn.execute(
            "UPDATE wallets SET is_tracked = 1, ai_follow = 1 "
            "WHERE address = ? AND COALESCE(is_tracked, 0) = 0", (wallet,))
        return bool(cur.rowcount)
    except Exception as _ex:
        _avisar_ex("puertas:ascender", _ex)
        return False


# etapas que NO justifican quitarle la ⭐ a nadie: `copiable` la merece, y
# `sin_datos` significa que TODAVIA no se la ha juzgado.
_NO_DEGRADABLES = ("copiable", "sin_datos")


def degradar(conn, wallet: str, veredicto: dict) -> bool:
    """(Fase 11, 10/09/2026) Con el embudo al mando, una ⭐ YA JUZGADA que
    no pasa las tres puertas deja de ser ⭐.

    POR QUE, y esto lo pidio el dueño con un motivo que resulto medible:
    "no nos sirven de nada, es acumular y gastar creditos en nada".
    Comprobado en su base el 10/09 — el bot re-perfila cada ⭐ cada 3 dias
    (`REEVAL_DAYS`) a ~230 creditos de Helius cada una, y la cola de
    re-perfilado estaba asi:

        98 que NO pasan las puertas   ← ~7.500 creditos/dia
        31 copiables
        43 sin evaluar

    O sea que el **70 % del presupuesto de perfilado** se iba en re-medir
    a gente que el embudo ya habia descartado, y encima les quitaba el
    turno a las candidatas nuevas de la caceria. Dejar de escucharlas no
    es solo cosmetica: es la mayor fuga de creditos del sistema.

    DOS EXCEPCIONES, y las dos importan
    ------------------------------------
    1. **`sin_datos` / sin etapa NO se degrada.** No ha sido juzgada
       todavia; quitarle la ⭐ seria condenarla sin juicio, y encima el
       perfilado es justo lo que le daria su etapa. Eran 44 el 10/09.
    2. **Con una posicion de papel ABIERTA tampoco.** Si el bot deja de
       escucharla no vera su venta, y esa posicion se quedaria colgada
       hasta que la recojan TP/SL/tiempo con un precio peor. Eran 12.

    LO QUE ESTO CUESTA, dicho claro
    --------------------------------
    Es casi definitivo. Al dejar de escucharla sus datos se congelan: sin
    operaciones nuevas no puede mejorar su nota, asi que la puerta de
    vuelta es estrecha — solo si la caceria vuelve a encontrarla por su
    cuenta, o si `filtro_calidad.promocion` (que mira a TODA la base, no
    solo a las ⭐) la re-promueve con lo que ya hay. El dueño lo sabe y
    lo pidio asi.

    El historial NO se borra: `signals`, `trades`, `appearances` y la
    ficha se quedan enteros. Lo unico que se apaga es la escucha.
    """
    if not wallet or not veredicto:
        return False
    etapa = veredicto.get("wallet_stage")
    if not etapa or etapa in _NO_DEGRADABLES:
        return False
    try:
        abierta = conn.execute(
            "SELECT 1 FROM paper_trades WHERE wallet = ? "
            "AND status = 'abierta' LIMIT 1", (wallet,)).fetchone()
        if abierta:
            return False
    except Exception as _ex:
        # Si no se puede comprobar, NO se degrada: un fallo tiene que
        # cerrar la puerta, no abrirla. Perder la venta de una posicion
        # abierta cuesta dinero de verdad.
        _avisar_ex("puertas:degradar:abiertas", _ex)
        return False
    motivo = (f" · 🚦 sin ⭐: no pasa las tres puertas ({etapa}): "
              f"{veredicto.get('motivo', '')}")
    try:
        cur = conn.execute(
            """UPDATE wallets SET is_tracked = 0, ai_follow = 0,
                   confirmada = 0, prueba_desde = NULL, turno_desde = NULL,
                   ai_reason = SUBSTR(COALESCE(ai_reason,'') || ?, 1, 500)
               WHERE address = ? AND COALESCE(is_tracked, 0) = 1""",
            (motivo, wallet))
        return bool(cur.rowcount)
    except Exception as _ex:
        _avisar_ex("puertas:degradar", _ex)
        return False


# ── la pasada ────────────────────────────────────────────────────────

def _a_evaluar(conn, limite: int) -> list[str]:
    """Billeteras con nota, LAS QUE TIENEN ALGO PENDIENTE PRIMERO.

    (19-BO, 10/09) El orden anterior era "sin etapa primero, luego la nota
    mas reciente". Servia en la fase 9, cuando la pasada solo ETIQUETABA:
    daba igual el orden porque tarde o temprano todas recibian su etapa.

    Desde que la pasada tambien ASCIENDE (fase 10) y DEGRADA (fase 11) ese
    orden mata el trabajo. MEDIDO en la base del dueño el 10/09, en la
    primera pasada con la degradacion viva:

        632 billeteras con nota · la pasada mira 300
        de esas 300, ⭐ pendientes de degradar:   4
        ⭐ pendientes que se quedaban FUERA:     76

    O sea que la limpieza avanzaba de 12 en 12, y las 76 restantes se
    quedaban detras de billeteras recien puntuadas. Y `q_ts` se refresca
    sin parar (148 re-puntuadas ese mismo dia), asi que la cola de delante
    NO SE VACIA NUNCA: la degradacion podia no converger jamas.

    Ahora delante va lo que tiene ACCION PENDIENTE, que es lo unico que
    cambia algo:

      0. sin etapa — nunca juzgada.
      1. ⭐ que no es `copiable`  → le toca perder la estrella.
         `copiable` que no es ⭐  → le toca ganarla.
      2. el resto, por nota mas reciente — eso es solo re-confirmar una
         etiqueta que ya esta puesta.

    Con las ~88 acciones pendientes que habia caben de sobra en una sola
    pasada de 300. Y el orden aguanta cuando la base crezca: lo pendiente
    va delante sea cual sea el tamaño.

    (19-BR, 11/09) El WHERE ya no es solo `q_score IS NOT NULL`. Ese
    filtro dejaba fuera PARA SIEMPRE a un grupo que el dueño veia en
    pantalla y no entendia. MEDIDO en su base el 11/09:

        98 ⭐ · 47 copiable · 4 observacion · 3 candidata
        44 SIN ETAPA — y las 44 con q_score NULL

    No era que les faltara la pasada: `wallet_quality.calcular` devuelve
    None por debajo de CALIDAD_MIN_POSICIONES (3) posiciones cerradas y
    completas, y NINGUNA de las 44 llegaba a 3. Sin nota nunca entraban
    aqui, sin entrar nunca recibian etapa, y sin etapa `degradar` no las
    tocaba (`_NO_DEGRADABLES` protege a la que no tiene etiqueta). ⭐
    eterna: no alertaban ni se copiaban —el conjunto operativo exige
    `copiable`— pero 37 de las 44 seguian entrando en la cola de
    re-perfilado de `ai_analyst` cada REEVAL_DAYS (3) dias, gastando
    creditos de Helius en billeteras que no podian llegar a copiable.

    Decision del dueño (11/09), con el numero delante: que entren y se
    resuelvan. Las que no se pueden medir fallan la puerta 1 ("no se
    sabe cuanto aguanta") y pierden la ⭐. No se pierde nada: el
    historial queda, y si algun dia reunen sus 3 posiciones el embudo
    puede volver a subirlas.

    La condicion es "o tiene nota, o lleva ⭐". Acotarla a "⭐ SIN ETAPA"
    era lo primero que escribi y esta MAL: en cuanto la primera pasada le
    pone `descartada`, la billetera deja de cumplirla y —sin nota— vuelve
    a ser invisible, con su estrella intacta. Lo cazo la prueba de la
    pasada completa, no la de la funcion. Quien lleva estrella puesta
    tiene que poder ser mirado SIEMPRE; y como las ⭐ se cuentan por
    decenas, no hay riesgo de inundar el LIMIT.
    """
    try:
        filas = conn.execute(
            """SELECT address FROM wallets
               WHERE q_score IS NOT NULL
                  OR COALESCE(is_tracked, 0) = 1
               ORDER BY CASE WHEN wallet_stage IS NULL THEN 0
                             WHEN COALESCE(is_tracked, 0) = 1
                                  AND wallet_stage <> 'copiable' THEN 1
                             WHEN COALESCE(is_tracked, 0) = 0
                                  AND wallet_stage = 'copiable' THEN 1
                             ELSE 2 END,
                        q_ts DESC LIMIT ?""", (int(limite),)).fetchall()
        return [f["address"] for f in filas]
    except Exception as _ex:
        _avisar_ex("puertas:_a_evaluar", _ex)
        return []


def revisar(limite: int | None = None) -> dict:
    """Pasada periodica. Cero creditos: todo sale de lo ya medido."""
    if limite is None:
        limite = _i("PUERTAS_POR_PASADA", 300)
    conn = get_conn()
    try:
        cuenta = {e: 0 for e in ETAPAS}
        cambios = []
        ascendidas = []
        degradadas = []
        _manda = manda(conn)
        for w in _a_evaluar(conn, limite):
            d = datos(conn, w)
            v = evaluar(d)
            antes = (d or {}).get("wallet_stage")
            if guardar(conn, w, v):
                cuenta[v["wallet_stage"]] = cuenta.get(
                    v["wallet_stage"], 0) + 1
                if antes != v["wallet_stage"]:
                    cambios.append((w, antes, v["wallet_stage"],
                                    v["motivo"]))
                if _manda:
                    if ascender(conn, w, v):
                        ascendidas.append(w)
                    elif degradar(conn, w, v):
                        degradadas.append(w)
        conn.commit()
        if ascendidas or degradadas:
            # El conjunto operativo se cachea 60 s: sin esto, los cambios
            # no se notarian hasta que caducara la cache.
            try:
                from db import invalidar_copiables
                invalidar_copiables()
            except Exception as _ex:
                _avisar_ex("puertas:revisar:cache", _ex)
        try:
            from analysis_events import registrar_lote, registrar
            registrar_lote(conn, [
                {"entity_type": "wallet", "entity_id": w,
                 "stage": "puertas", "decision": nueva,
                 "reason": f"{antes or 'sin etapa'} → {nueva}: {mot}"}
                for w, antes, nueva, mot in cambios[:200]])
            registrar(conn, "lote", "puertas", "puertas", "revisadas",
                      " · ".join(f"{k} {v}" for k, v in cuenta.items() if v),
                      score=sum(cuenta.values()),
                      data_source="wallet_positions+wallets")
            if ascendidas:
                registrar_lote(conn, [
                    {"entity_type": "wallet", "entity_id": w,
                     "stage": "puertas", "decision": "ascendida",
                     "reason": "pasa las tres puertas: pasa a ⭐ para que "
                               "el bot la vigile en tiempo real"}
                    for w in ascendidas[:200]])
            if degradadas:
                registrar_lote(conn, [
                    {"entity_type": "wallet", "entity_id": w,
                     "stage": "puertas", "decision": "degradada",
                     "reason": "no pasa las tres puertas: se deja de "
                               "escuchar para no gastar créditos en "
                               "re-perfilarla"}
                    for w in degradadas[:200]])
            if ascendidas or degradadas:
                conn.commit()
        except Exception as _ex:
            _avisar_ex("puertas:revisar:eventos", _ex)
        print("✓ Puertas: " + " · ".join(f"{k} {v}" for k, v in
                                         cuenta.items() if v)
              + f" ({len(cambios)} cambios"
              + (f", {len(ascendidas)} ascendidas a ⭐" if ascendidas else "")
              + (f", {len(degradadas)} sin ⭐" if degradadas else "")
              + ") · 0 créditos")
        return cuenta
    finally:
        conn.close()


# ── lo que decide (o no) ─────────────────────────────────────────────

def manda(conn=None) -> bool:
    """¿La etapa decide ya a quien alerta y se copia?

    (Fase 10, 09/09) SI, si el dueño no lo ha apagado. El interruptor
    vive en UN solo sitio de verdad —`db.embudo_manda`—, que mira primero
    el ajuste `embudo_v2_activo` de la base (lo que pone `/embudo`) y se
    cae a `config.EMBUDO_V2_ACTIVO` si no hay ajuste. Asi apagarlo es un
    mensaje de Telegram y no un despliegue: si el bot se queda sin
    alertas, el dueño lo revierte en segundos desde el movil.
    """
    try:
        from db import embudo_manda
        return embudo_manda(conn)
    except Exception as _ex:
        _avisar_ex("puertas:manda", _ex)
        return bool(_i("EMBUDO_V2_ACTIVO", 0))


def copiables(conn, limite: int = 50) -> list[dict]:
    try:
        return [dict(f) for f in conn.execute(
            """SELECT address, q_score, mult_realizado, hold_median_h,
                      pos_24h, estrategia, COALESCE(is_tracked,0) is_tracked
               FROM wallets WHERE wallet_stage = 'copiable'
               ORDER BY q_score DESC LIMIT ?""", (int(limite),)).fetchall()]
    except Exception as _ex:
        _avisar_ex("puertas:copiables", _ex)
        return []


# ── textos ───────────────────────────────────────────────────────────

def porque_text(conn, wallet: str) -> str:
    """`/porque <billetera>`: las tres puertas, una por una."""
    d = datos(conn, wallet)
    if d is None:
        return "No pude leer esa billetera."
    v = evaluar(d)
    if v["wallet_stage"] == "sin_datos":
        return (f"`{wallet[:12]}…` todavía no tiene nada medido. No es un "
                f"suspenso: es que aún no hay con qué juzgarla.")
    marca = {True: "✅", False: "❌", None: "·"}
    L = [f"🚦 `{wallet[:14]}…` → *{v['wallet_stage']}*", ""]
    for n, (etq, ok, mot) in enumerate((
            ("¿Se le puede copiar?", v["p1"], v["motivos"][0]),
            ("¿Gana de verdad?", v["p2"], v["motivos"][1]),
            ("¿Es tu estrategia?", v["p3"], v["motivos"][2])), 1):
        L.append(f"{marca.get(ok)} *Puerta {n} · {etq}*")
        L.append(f"    _{mot}_")
    L.append("")
    cap = d.get("capital_tipico")
    L.append(f"Operación típica: x{(d.get('mult_realizado') or 0):.2f} · "
             f"{(d.get('hold_median_h') or 0):.0f} h dentro · "
             f"{cap:.2f} SOL" if cap is not None else "")
    L.append("")
    if manda(conn):
        L.append("_Las puertas MANDAN: solo alertan y se copian las que "
                 "pasan las tres. Apagar: `/embudo off`._")
    else:
        L.append("_Las puertas todavía no deciden a quién se copia: eso "
                 "sigue mandándolo el top de siempre._")
    return "\n".join(x for x in L if x is not None)


def ranking_text(conn, limite: int = 15) -> str:
    """`/ranking`: las que pasan las tres puertas."""
    try:
        cuenta = conn.execute(
            "SELECT wallet_stage e, COUNT(*) n FROM wallets "
            "WHERE wallet_stage IS NOT NULL GROUP BY e ORDER BY n DESC"
        ).fetchall()
    except Exception as _ex:
        _avisar_ex("puertas:ranking_text", _ex)
        return "🚦 *Ranking*\n\nTodavía no se han evaluado las puertas."
    if not cuenta:
        return ("🚦 *Ranking*\n\nTodavía no se han evaluado las puertas. "
                "Se llenan solas con lo ya medido, sin gastar créditos.")
    L = ["🚦 *Las tres puertas*", ""]
    L.append(" · ".join(f"{f['e']} {f['n']}" for f in cuenta))
    filas = copiables(conn, limite)
    if filas:
        L.append("")
        L.append("*Pasan las tres* (ordenadas por nota)")
        for f in filas:
            est = " ⭐" if f["is_tracked"] else ""
            L.append(f"`{f['address'][:10]}…`{est} nota "
                     f"{(f['q_score'] or 0):.0f} · "
                     f"x{(f['mult_realizado'] or 0):.1f} en "
                     f"{(f['hold_median_h'] or 0):.0f} h · "
                     f"{f['pos_24h'] or 0} aguantes")
    else:
        L.append("")
        L.append("_Ninguna pasa las tres todavía. `/porque <billetera>` "
                 "dice en cuál se cae cada una._")
    L.append("")
    if manda(conn):
        L.append("_El embudo MANDA: solo estas alertan y se copian. "
                 "Apagar: `/embudo off`._")
    else:
        L.append("_Esto todavía no cambia a quién se copia._")
    return "\n".join(L)


if __name__ == "__main__":
    revisar()
