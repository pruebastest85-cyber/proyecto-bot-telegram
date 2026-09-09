"""
Descubrimiento dirigido: buscar a quien MULTIPLICA metiendo poco.
(Embudo v2, fase 8 — 08/09/2026)

POR QUE EXISTE
--------------
La nota de calidad de la fase 6 solo puede puntuar a las billeteras que el
embudo VIEJO ya perfilo: 4.009, elegidas por "llegar pronto". Ese es el
sesgo que el dueño rechaza — francotiradores que compran el minuto cero y
sueltan a los cuarenta segundos. Medido: la nota media de las ⭐ actuales
es 45,2 y la de las que NO son ⭐, 46,4. Ser estrella hoy no guarda
ninguna relacion con aguantar y ganar.

Aqui se sale a buscar gente NUEVA, con el criterio del dueño en sus
palabras: no quien gano mas dinero, sino **quien gano bastante metiendo
poco capital**.

DE DONDE SALE LA BANDA DE CAPITAL (medido, no elegido a ojo)
------------------------------------------------------------
Sobre las 2.555 posiciones cerradas y medidas de la base, por cuanto
metieron:

    capital        n     mediana   p90     x>=3    ganadoras
    0-0,5 SOL     286     x0,94    x3,7     14 %      37 %
    0,5-1 SOL     327     x0,96    x3,8     13 %      45 %
    1-3 SOL       799     x1,10    x8,5     25 %      57 %   <-- el mejor
    3-10 SOL      660     x1,30    x5,6     24 %      69 %
    10-30 SOL     337     x1,18    x5,0     17 %      67 %
    30-100 SOL    126     x1,17    x2,8      9 %      68 %
    >100 SOL       20     x1,13    x2,6      5 %      75 %

Dos cosas saltan a la vista. La cola gorda —las operaciones que de verdad
multiplican— esta entre **1 y 3 SOL**: uno de cada cuatro pasa de x3 y el
percentil 90 llega a x8,5. Y por debajo de medio SOL no hay señal, hay
polvo: solo el 37 % acaba en ganancia. Las ballenas (>30 SOL) ganan a
menudo pero multiplican poquisimo, que es justo lo contrario de lo que el
dueño busca.

Por eso la banda por defecto es 0,5-5 SOL, y los dos extremos se pueden
mover por configuracion.

EL POLVO MIENTE (regla 54)
--------------------------
En la base hay una posicion que "multiplico x1.358": metio 0,004 SOL y
saco 1,37. Con capitales minusculos el multiplo se dispara sin significar
nada. El suelo de la banda existe para eso, y no es un adorno.

CUANTO CUESTA
-------------
Bajar el arranque de un token cuesta ~200 creditos (ruta RPC barata, 10
por cada 100 transacciones). Perfilar una billetera, ~230 todo incluido.
Con tres tokens y diez candidatas por token, una pasada son ~7.500
creditos. Ademas hay un TOPE DURO por pasada que se comprueba contra el
libro de cuentas de la fase 7: si se pasa, se corta y se sigue en la
siguiente. Nunca puede repetirse lo del 3 de septiembre.
"""

import math as _math
import time

import config
from db import get_conn
from avisos import aviso as _avisar_ex


def _int(nombre, defecto):
    try:
        return int(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return int(defecto)


def _float(nombre, defecto):
    try:
        return float(getattr(config, nombre, defecto))
    except (TypeError, ValueError):
        return float(defecto)


# ── a que tokens ir ───────────────────────────────────────────────────

def tokens_a_explorar(conn, limite: int) -> list[dict]:
    """Tokens que cruzaron MIN_WINNER_MC y que todavia no se han
    explorado. Se descartan los que SABEMOS que murieron; los de
    supervivencia desconocida entran, porque "no se sabe" no es "no"
    (fase 4) y descartarlos dejaria fuera casi todo el historial.

    Primero los que mas alto llegaron: donde hubo mas dinero que ganar.
    """
    nivel = _float("MIN_WINNER_MC", 500_000)
    try:
        filas = conn.execute(
            """SELECT m.mint, m.first_reached_ts,
                      COALESCE(w.ath_mc, 0) ath,
                      w.survival_24h surv, w.symbol
               FROM token_milestones m
               LEFT JOIN winning_tokens w ON w.mint = m.mint
               WHERE m.milestone_usd = ?
                 AND COALESCE(w.survival_24h, 1) <> 0
                 AND NOT EXISTS (
                       SELECT 1 FROM analysis_events e
                        WHERE e.entity_id = m.mint
                          AND e.stage = 'descubrimiento')
               ORDER BY ath DESC, m.first_reached_ts DESC
               LIMIT ?""", (nivel, int(limite))).fetchall()
        return [dict(f) for f in filas]
    except Exception as _ex:
        _avisar_ex("descubrimiento:tokens_a_explorar", _ex)
        return []


# ── quien es candidata ────────────────────────────────────────────────

def elegir_candidatas(buys: list[dict], sells: list[dict],
                      ya_conocidas: set | None = None,
                      limite: int | None = None) -> list[dict]:
    """De las compras del arranque, las que cumplen el criterio del dueño.

    Se pide:
      · haber metido DENTRO DE LA BANDA (poco capital, pero no polvo),
      · NO haber vendido dentro de la ventana que hemos mirado — quien
        ya solto en el arranque es un volteador, no alguien a quien
        copiar,
      · y no ser una billetera que ya tenemos medida (buscamos gente
        nueva; a las conocidas ya las puntua la fase 6).

    Ordenadas por CERCANIA AL PUNTO DULCE medido, no por "la mas
    pequeña". La diferencia importa y la enseño la primera caceria real:
    ordenando por capital ascendente, las 30 candidatas salieron todas
    entre 0,50 y 0,77 SOL — el fondo de la banda. Y el fondo de la banda
    es justo su peor tramo: entre 0,5 y 1 SOL solo el 45 % acaba en
    ganancia y el percentil 90 se queda en x3,8, mientras que entre 1 y
    3 SOL gana el 57 % y el percentil 90 llega a x8,5.

    "Poco capital" no quiere decir "el minimo posible": quiere decir la
    banda donde de verdad se multiplica. Por eso se ordena por distancia
    (en escala logaritmica, que es como se comparan los multiplos) al
    centro de ese tramo.
    """
    minimo = _float("DESCUBRIMIENTO_MIN_SOL", 0.5)
    maximo = _float("DESCUBRIMIENTO_MAX_SOL", 5.0)
    if limite is None:
        limite = _int("DESCUBRIMIENTO_CANDIDATOS_POR_TOKEN", 10)
    ya = ya_conocidas or set()
    # Quien vendio en la ventana queda descartado, con su primera venta.
    vendieron = {}
    for s in sells or []:
        w, ts = s.get("wallet"), s.get("time")
        if not w:
            continue
        if w not in vendieron or (ts and ts < vendieron[w]):
            vendieron[w] = ts

    # Una billetera puede comprar varias veces: cuenta lo que metio EN
    # TOTAL, no cada compra suelta (si no, quien parte 6 SOL en tres
    # trozos pasaria por pequeña).
    total = {}
    primera = {}
    for b in buys or []:
        w, sol, ts = b.get("wallet"), float(b.get("sol") or 0), b.get("time")
        if not w or sol <= 0:
            continue
        total[w] = total.get(w, 0.0) + sol
        if w not in primera or (ts and ts < primera[w]):
            primera[w] = ts

    out = []
    for w, sol in total.items():
        if w in ya:
            continue
        if not (minimo <= sol <= maximo):
            continue
        if w in vendieron:
            continue
        out.append({"wallet": w, "sol": sol, "ts": primera.get(w)})
    optimo = max(1e-9, _float("DESCUBRIMIENTO_OPTIMO_SOL", 2.0))
    out.sort(key=lambda d: abs(_math.log(max(1e-9, d["sol"]) / optimo)))
    return out[:int(limite)]


def es_bot(conn, wallet: str) -> bool:
    """¿Esta billetera ya esta marcada como bot en NUESTRA base?

    Importa porque `trades_store.guardar` se niega a guardar el historial
    de una billetera con `is_bot=1`: la descarga entera acaba en la
    basura. Pagarla no devuelve ni un dato — el veredicto ya lo teniamos.
    """
    try:
        f = conn.execute(
            "SELECT COALESCE(is_bot, 0) b FROM wallets WHERE address = ?",
            (wallet,)).fetchone()
        return bool(f and f["b"])
    except Exception as _ex:
        _avisar_ex("descubrimiento:es_bot", _ex)
        return False          # ante la duda, no bloquear la caceria


def _ya_medidas(conn) -> set:
    """Billeteras que NO hay que volver a pagar.

    Dos motivos distintos, los dos salidos de lo ya medido:

    1. Ya tenemos su historial propio (`trades`): pagarlo otra vez es
       comprar lo mismo dos veces.
    2. Ya esta marcada como bot: `trades_store.guardar` tira su historial
       nada mas llegar (mira `is_bot` y devuelve 0 sin guardar), asi que
       la descarga se paga entera y no deja NI UNA fila.

    MEDIDO en la base del dueño el 09/09/2026: de las 125 billeteras que
    la caceria habia perfilado, **44 estaban marcadas como bot desde
    julio** — semanas antes de encolarlas. Ninguna dejo una sola
    operacion. Son ~10.000 creditos comprando una respuesta que ya
    estaba escrita en la base.

    OJO, y esto es decision del dueño, no mia: la marca `is_bot` del
    embudo VIEJO se aplico de mas (ver `wallet_profiler`: ~3.190
    marcadas por un calculo de horas que ya esta corregido). Saltarlas
    aqui no las esconde mas de lo que ya estan —hoy se pagan y se tiran
    igual—, y el dia que el dueño desmarque a las falsas volveran solas
    a la caceria sin tocar este codigo.
    """
    fuera = set()
    try:
        fuera |= {f["wallet"] for f in conn.execute(
            "SELECT DISTINCT wallet FROM trades").fetchall()}
    except Exception as _ex:
        _avisar_ex("descubrimiento:_ya_medidas", _ex)
    try:
        fuera |= {f["address"] for f in conn.execute(
            "SELECT address FROM wallets "
            "WHERE COALESCE(is_bot, 0) = 1").fetchall()}
    except Exception as _ex:
        _avisar_ex("descubrimiento:_ya_medidas:bots", _ex)
    return fuera


# ── la cola ───────────────────────────────────────────────────────────

def encolar(conn, wallet: str, prioridad: int, motivo: str) -> bool:
    """Mete una billetera en la cola de perfilado. Idempotente: la clave
    (entity_type, entity_id, tarea) impide duplicados."""
    ahora = int(time.time())
    try:
        cur = conn.execute(
            """UPDATE helius_queue SET priority = ?, reason = ?,
                      updated_ts = ?
                WHERE entity_type = 'wallet' AND entity_id = ?
                  AND tarea = 'perfilar' AND estado = 'pendiente'""",
            (int(prioridad), str(motivo)[:200], ahora, wallet))
        if not cur.rowcount:
            conn.execute(
                """INSERT INTO helius_queue
                   (entity_type, entity_id, tarea, priority, reason,
                    estado, intentos, created_ts, updated_ts, next_try_ts)
                   VALUES ('wallet', ?, 'perfilar', ?, ?, 'pendiente',
                           0, ?, ?, ?)""",
                (wallet, int(prioridad), str(motivo)[:200], ahora, ahora,
                 ahora))
        return True
    except Exception as _ex:
        # Puede fallar legitimamente si ya existe con otro estado (hecha):
        # no se vuelve a encolar y no es un error.
        _avisar_ex("descubrimiento:encolar", _ex)
        return False


def pendientes(conn, limite: int) -> list[dict]:
    try:
        return [dict(f) for f in conn.execute(
            """SELECT entity_id, priority, reason, intentos
               FROM helius_queue
               WHERE entity_type = 'wallet' AND tarea = 'perfilar'
                 AND estado = 'pendiente' AND COALESCE(next_try_ts, 0) <= ?
               ORDER BY priority DESC, created_ts ASC LIMIT ?""",
            (int(time.time()), int(limite))).fetchall()]
    except Exception as _ex:
        _avisar_ex("descubrimiento:pendientes", _ex)
        return []


def marcar(conn, wallet: str, estado: str, intentos: int = 0) -> None:
    try:
        conn.execute(
            """UPDATE helius_queue SET estado = ?, intentos = ?,
                      updated_ts = ?
                WHERE entity_type = 'wallet' AND entity_id = ?
                  AND tarea = 'perfilar'""",
            (estado, int(intentos), int(time.time()), wallet))
        conn.commit()
    except Exception as _ex:
        _avisar_ex("descubrimiento:marcar", _ex)


# ── el tope de gasto ──────────────────────────────────────────────────

def _gastado_desde(conn, desde_ts: int) -> int:
    """Creditos apuntados en el libro desde ese instante. Se vuelca antes
    para que el bufer en memoria no deje el tope corto."""
    try:
        from helius_ledger import volcar, gastado
        volcar()
        return int(gastado(conn, desde_ts))
    except Exception as _ex:
        _avisar_ex("descubrimiento:_gastado_desde", _ex)
        return 0


def _hay_presupuesto() -> bool:
    """El freno global del 85 % manda por encima de todo lo de aqui."""
    try:
        from helius_budget import puede_llamar
        return bool(puede_llamar())
    except Exception as _ex:
        _avisar_ex("descubrimiento:_hay_presupuesto", _ex)
        return True


# ── la pasada ─────────────────────────────────────────────────────────

def explorar(conn, tokens: list[dict], tope_creditos: int,
             inicio_ts: int) -> tuple:
    """Baja el arranque de cada token y encola a sus candidatas.
    Devuelve (tokens_mirados, candidatas_encoladas)."""
    from wallet_analyzer import (fetch_earliest_txs, extract_buys,
                                 extract_sells, motivo_fallo_descarga)
    from helius_ledger import contexto
    from analysis_events import registrar

    ya = _ya_medidas(conn)
    mirados = encoladas = 0
    for t in tokens:
        if _gastado_desde(conn, inicio_ts) >= tope_creditos:
            print(f"  ⛔ Descubrimiento: tope de {tope_creditos} créditos "
                  f"alcanzado; el resto queda para la próxima pasada")
            break
        if not _hay_presupuesto():
            print("  ⛔ Descubrimiento: freno global de Helius activo")
            break
        mint = t["mint"]
        simbolo = t.get("symbol") or mint[:8]
        with contexto("descubrimiento", "token", mint):
            txs, completo = fetch_earliest_txs(mint, con_estado=True)
        fallo = motivo_fallo_descarga()
        if fallo:
            # No se marca el token como explorado: se reintenta.
            print(f"  ⚠️ {simbolo}: no pude bajar el arranque ({fallo}); "
                  f"se reintentará")
            continue
        mirados += 1
        buys = extract_buys(txs, mint)
        sells = extract_sells(txs, mint)
        cands = elegir_candidatas(buys, sells, ya)
        for i, c in enumerate(cands):
            # Prioridad = el orden en que salieron de `elegir_candidatas`,
            # que ya es "lo mas cerca del punto dulce, primero". Se
            # convierte a entero porque la columna lo es.
            prio = 1000 - i
            if encolar(conn, c["wallet"], prio,
                       f"{simbolo}: metió {c['sol']:.2f} SOL y no vendió "
                       f"en el arranque"):
                encoladas += 1
                ya.add(c["wallet"])
        registrar(conn, "token", mint, "descubrimiento", "explorado",
                  (f"{len(txs)} txs · {len(buys)} compras · "
                   f"{len(sells)} ventas tempranas · {len(cands)} "
                   f"candidatas"
                   + ("" if completo else " · historial incompleto")),
                  score=len(cands), data_source="helius:primeras_txs")
        conn.commit()
        print(f"  · {simbolo}: {len(buys)} compras → {len(cands)} "
              f"candidatas de poco capital que no vendieron")
    return (mirados, encoladas)


def atender_cola(conn, tope_creditos: int, inicio_ts: int,
                 limite: int | None = None) -> int:
    """Perfila a las candidatas encoladas, dentro del tope. Devuelve
    cuantas se perfilaron."""
    if limite is None:
        limite = _int("DESCUBRIMIENTO_PERFILES_POR_PASADA", 25)
    from analysis_events import registrar
    hechas = saltadas = 0
    for fila in pendientes(conn, limite):
        if _gastado_desde(conn, inicio_ts) >= tope_creditos:
            print(f"  ⛔ Descubrimiento: tope de {tope_creditos} créditos "
                  f"alcanzado con {hechas} perfiles; el resto sigue en cola")
            break
        if not _hay_presupuesto():
            print("  ⛔ Descubrimiento: freno global de Helius activo")
            break
        w = fila["entity_id"]
        # Verja tardia: una candidata puede haberse marcado como bot
        # DESPUES de entrar en la cola (o venir de una pasada anterior al
        # arreglo). Perfilarla cuesta lo mismo y `trades_store` tira la
        # descarga entera, asi que se cierra sin gastar.
        if es_bot(conn, w):
            marcar(conn, w, "hecha", int(fila.get("intentos") or 0))
            saltadas += 1
            registrar(conn, "wallet", w, "descubrimiento", "saltada",
                      "ya marcada como bot: su historial se tiraría al "
                      "guardarlo, así que no se paga",
                      data_source="wallets.is_bot")
            continue
        try:
            # `profile_wallet` ya declara su sobre ("billeteras") y guarda
            # las operaciones en `trades`: de ahi salen solas las
            # posiciones (fase 5) y la nota (fase 6).
            from wallet_profiler import profile_wallet
            p = profile_wallet(w, with_holdings=False)
        except Exception as e:
            print(f"  · {w[:8]}…: el perfilado falló ({e})")
            marcar(conn, w, "pendiente", int(fila.get("intentos") or 0) + 1)
            continue
        if not p or not p.get("historial_entero", True):
            # Media historia da metricas equivocadas: se reintenta.
            marcar(conn, w, "pendiente", int(fila.get("intentos") or 0) + 1)
            continue
        marcar(conn, w, "hecha", int(fila.get("intentos") or 0) + 1)
        hechas += 1
        registrar(conn, "wallet", w, "descubrimiento", "perfilada",
                  (fila.get("reason") or "")[:200],
                  score=p.get("tx_sampled"), data_source="helius:historial")
    if saltadas:
        print(f"  · {saltadas} candidatas saltadas por estar ya marcadas "
              f"como bot (0 créditos)")
    return hechas


def ciclo(limite_tokens: int | None = None) -> dict:
    """Una pasada completa: explorar tokens, encolar candidatas,
    perfilarlas y refrescar sus posiciones. Con tope duro de creditos."""
    if limite_tokens is None:
        limite_tokens = _int("DESCUBRIMIENTO_TOKENS_POR_PASADA", 3)
    tope = _int("DESCUBRIMIENTO_CREDITOS_POR_PASADA", 30_000)
    inicio = int(time.time())
    conn = get_conn()
    try:
        if not _hay_presupuesto():
            print("· Descubrimiento: freno de Helius activo; no se explora")
            return {"tokens": 0, "encoladas": 0, "perfiladas": 0,
                    "creditos": 0}
        tokens = tokens_a_explorar(conn, limite_tokens)
        mirados, encoladas = explorar(conn, tokens, tope, inicio)
        perfiladas = atender_cola(conn, tope, inicio)
        # Cero creditos: rehace las posiciones de los tokens explorados
        # con las operaciones que acaban de llegar.
        try:
            from posiciones import reconstruir_mint
            for t in tokens[:mirados]:
                reconstruir_mint(conn, t["mint"])
        except Exception as _ex:
            _avisar_ex("descubrimiento:ciclo:posiciones", _ex)
        gasto = _gastado_desde(conn, inicio)
        print(f"✓ Descubrimiento: {mirados} tokens · {encoladas} candidatas "
              f"nuevas · {perfiladas} perfiladas · {gasto} créditos "
              f"(tope {tope})")
        return {"tokens": mirados, "encoladas": encoladas,
                "perfiladas": perfiladas, "creditos": gasto}
    finally:
        conn.close()


# ── lectura ───────────────────────────────────────────────────────────

def resumen_text(conn=None) -> str:
    """Texto para /descubrir."""
    propia = conn is None
    if propia:
        conn = get_conn()
    try:
        L = ["🔎 *Descubrimiento de billeteras nuevas*", ""]
        try:
            est = conn.execute(
                """SELECT estado, COUNT(*) n FROM helius_queue
                   WHERE tarea = 'perfilar' GROUP BY estado""").fetchall()
        except Exception as _ex:
            _avisar_ex("descubrimiento:resumen_text:cola", _ex)
            return "🔎 *Descubrimiento*\n\nTodavía no ha corrido ninguna vez."
        if not est:
            L.append("Todavía no ha corrido. Busca billeteras que metieron "
                     f"entre {_float('DESCUBRIMIENTO_MIN_SOL', 0.5):g} y "
                     f"{_float('DESCUBRIMIENTO_MAX_SOL', 5.0):g} SOL en "
                     "tokens que llegaron alto y NO vendieron en el "
                     "arranque.")
            return "\n".join(L)
        L.append("*Cola:* " + " · ".join(
            f"{f['estado']} {f['n']}" for f in est))
        try:
            r = conn.execute(
                """SELECT COUNT(*) n FROM analysis_events
                   WHERE stage = 'descubrimiento'
                     AND decision = 'explorado'""").fetchone()
            L.append(f"*Tokens explorados:* {r['n']}")
        except Exception as _ex:
            _avisar_ex("descubrimiento:resumen_text:tokens", _ex)
        # Lo que de verdad importa: de las descubiertas, cuantas ya tienen
        # nota y como es.
        try:
            filas = conn.execute(
                """SELECT w.address, w.q_score, w.estrategia,
                          w.mult_realizado, w.hold_median_h
                   FROM wallets w
                   WHERE w.q_score IS NOT NULL AND EXISTS (
                         SELECT 1 FROM helius_queue q
                          WHERE q.entity_id = w.address
                            AND q.tarea = 'perfilar')
                   ORDER BY w.q_score DESC LIMIT 10""").fetchall()
            if filas:
                L.append("")
                L.append("*Ya puntuadas de las descubiertas*")
                for f in filas:
                    mult = (f"x{f['mult_realizado']:.1f}"
                            if f["mult_realizado"] is not None else "?")
                    L.append(f"  `{f['address'][:10]}…` nota "
                             f"{(f['q_score'] or 0):.0f} · {f['estrategia']}"
                             f" · {mult} en "
                             f"{(f['hold_median_h'] or 0):.0f} h")
            else:
                L.append("")
                L.append("_Ninguna tiene nota todavía: hacen falta 3 "
                         "posiciones cerradas para puntuar._")
        except Exception as _ex:
            _avisar_ex("descubrimiento:resumen_text:puntuadas", _ex)
        return "\n".join(L)
    finally:
        if propia:
            conn.close()


if __name__ == "__main__":
    ciclo()
