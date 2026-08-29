"""
Radar de pares recién nacidos (Ola 14, 24/8/2026).

El descubrimiento clásico llega DESPUÉS de la subida: busca tokens que ya
explotaron y reconstruye quién compró temprano. Este radar mira la otra
punta: los tokens recién lanzados (GeckoTerminal new_pools), les pasa el
semáforo de seguridad y comprueba si alguna billetera QUE EL SISTEMA YA
CONOCE como buena (⭐, graduada o candidata con historial) está comprando.
Si la smart money de tu propia base entra en un token de minutos de vida,
eso es una señal que ningún ranking externo te da.

Diseño anti-ruido: NO alerta tokens "prometedores" (serían cientos al
día); alerta SOLO cuando hay billeteras conocidas dentro. Todo lo demás
queda registrado en la tabla radar_tokens para el resumen de /radar.

Costo Helius: ~RADAR_TXS/10 créditos por token examinado (getTransactions
del inicio del token). Con los defaults: ~8 tokens × 96 pasadas/día ≈
30-40k créditos/día como máximo. Respeta el freno del presupuesto.

Ajustes por variable de entorno (sin tocar código):
  RADAR_ACTIVO=1            apagar con 0
  RADAR_MAX_EDAD_MIN=90     edad máxima del pool para examinarlo
  RADAR_MIN_LIQ=8000        liquidez mínima (USD) para molestarse
  RADAR_TOKENS_PASADA=8     tokens examinados por pasada (cada 15 min)
  RADAR_TXS=500             transacciones del inicio a revisar por token
  RADAR_MIN_CONOCIDAS=1     billeteras conocidas mínimas para alertar
  RADAR_SILENCIOSO=1        MODO OCULTO (por defecto): el radar trabaja
                            igual pero NO escribe en Telegram

MODO OCULTO (Ola 18-P, 29/8/2026, pedido del dueño). Medido en su base
antes de tocarlo: 53 avisos de smart money en 24 h más 2-3 de "hizo xN".
Ese caudal tapaba las alertas de las ⭐, que son las que se copian. En
silencio el radar hace EXACTAMENTE lo mismo — examina, pasa el semáforo
de seguridad, registra en `radar_tokens` y promueve al embudo los que
hacen xN, que es de donde salen candidatas horas antes — pero no manda
mensajes. Lo que vio se consulta con /radar, y el resumen diario lleva
una línea con los números del día. Se enciende y se apaga en caliente
con /radarsilencio, sin reiniciar y sin tocar el entorno.
"""

import os
import time
from datetime import datetime, timezone

from db import get_conn


def _int_env(n, d):
    try:
        return int(float(os.getenv(n, d)))
    except (TypeError, ValueError):
        return d


ACTIVO = _int_env("RADAR_ACTIVO", 1)
MAX_EDAD_MIN = _int_env("RADAR_MAX_EDAD_MIN", 90)
MIN_LIQ = _int_env("RADAR_MIN_LIQ", 8000)
TOKENS_PASADA = _int_env("RADAR_TOKENS_PASADA", 8)
RADAR_TXS = _int_env("RADAR_TXS", 500)
MIN_CONOCIDAS = _int_env("RADAR_MIN_CONOCIDAS", 1)
# Valor de arranque del modo oculto. Manda el ajuste guardado en la base
# (`radar_silencioso`), que el dueño cambia con /radarsilencio; esto solo
# decide qué pasa cuando ese ajuste todavía no existe.
SILENCIOSO_DEF = _int_env("RADAR_SILENCIOSO", 1)
# Conexion con el embudo (14b): si el token del radar hace xN, se promueve
# al catalogo de ganadores y el ciclo analiza a TODOS sus compradores
# tempranos — los desconocidos que compraron junto a tus conocidas entran
# al embudo horas antes que con el descubrimiento clasico.
GANADOR_X = float(os.getenv("RADAR_GANADOR_X", "3.0") or 3.0)
SEG_MAX_H = _int_env("RADAR_SEG_MAX_H", 48)


def _plural(n: int, uno: str, varios: str) -> str:
    return uno if n == 1 else varios


def silencioso(conn) -> bool:
    """¿El radar trabaja en silencio? Se lee EN CADA pasada, no al
    importar: así /radarsilencio hace efecto al instante y no hay que
    reiniciar el bot. Ante cualquier duda (ajuste ilegible, base rara)
    manda el valor de arranque, que por defecto es SÍ."""
    try:
        from db import get_setting
        v = get_setting(conn, "radar_silencioso", None)
    except Exception:
        return bool(SILENCIOSO_DEF)
    if v is None or str(v).strip() == "":
        return bool(SILENCIOSO_DEF)
    try:
        return bool(int(float(v)))
    except (TypeError, ValueError):
        return bool(SILENCIOSO_DEF)


def resumen_linea(conn) -> str | None:
    """Una línea con lo que hizo el radar en 24 h, para el resumen diario.

    Con el radar apagado del todo devuelve None (no hay nada que contar).
    Encendido devuelve SIEMPRE algo: si no examinó ni un token en 24 h,
    la línea lo dice como aviso — desde que el radar no escribe, esa es
    la única señal de que se haya parado.
    """
    if not ACTIVO:
        return None            # apagado del todo: no hay nada que contar
    ahora = int(time.time())
    corte = ahora - 86400
    try:
        tot = conn.execute(
            "SELECT COUNT(*) AS c FROM radar_tokens WHERE ts >= ?",
            (corte,)).fetchone()["c"]
        # Se cuenta la columna `smart` (un HECHO que no se pisa), no el
        # estado `resultado`: el seguimiento lo reescribe a
        # murio/expirado/ganador_promovido, y con tokens de minutos de
        # vida eso hacía que la cifra ENCOGIERA sola dentro de las mismas
        # 24 h. Justo la cifra que compensa el silencio.
        con_smart = conn.execute(
            "SELECT COUNT(*) AS c FROM radar_tokens WHERE ts >= ? "
            "AND (COALESCE(smart, 0) > 0 "
            "     OR SUBSTR(COALESCE(resultado, ''), 1, 9) = 'alertado:')",
            (corte,)).fetchone()["c"]
        # Los promovidos van a 7 días, como en /radar: una promoción
        # ocurre entre 1 h y 48 h DESPUÉS de ver el token, así que
        # contarla en la ventana de 24 h del descubrimiento se dejaría
        # fuera la mitad. Se dice la ventana para no confundir.
        prom = conn.execute(
            "SELECT COUNT(*) AS c FROM radar_tokens WHERE ts >= ? "
            "AND resultado = 'ganador_promovido'",
            (ahora - 7 * 86400,)).fetchone()["c"]
    except Exception:
        return None
    if not tot:
        # NO se calla. Con el radar encendido, 24 h sin examinar ni un
        # token no es "nada que contar": es que el radar está parado, y
        # antes eso se notaba porque dejaban de llegar los 53 avisos.
        # Ahora esta línea es la única alarma que queda.
        return ("  ⚠️ 0 tokens nuevos examinados en 24 h — el radar "
                "debería ver decenas; revisa /radar y /errores")
    return (f"  • {tot} {_plural(tot, 'token nuevo examinado', 'tokens nuevos examinados')} · {con_smart} con "
            f"billeteras de tu base dentro · {prom} "
            f"{_plural(prom, 'promovido', 'promovidos')} al embudo "
            f"(7 días)")


def _frescos() -> list[dict]:
    """Pools recién creados que valen un examen: jóvenes y con liquidez."""
    from discovery import fetch_new_pools
    out = []
    ahora = datetime.now(timezone.utc)
    for pool in fetch_new_pools(pages=2):
        a = pool.get("attributes", {})
        try:
            liq = float(a.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            continue
        if liq < MIN_LIQ:
            continue
        created = a.get("pool_created_at")
        edad_min = None
        if created:
            try:
                dt = datetime.fromisoformat(
                    str(created).replace("Z", "+00:00"))
                edad_min = (ahora - dt).total_seconds() / 60
            except (ValueError, TypeError):
                pass
        if edad_min is None or edad_min > MAX_EDAD_MIN:
            continue
        rel = pool.get("relationships", {}).get(
            "base_token", {}).get("data", {})
        raw_id = rel.get("id", "")
        mint = raw_id.split("_", 1)[1] if "_" in raw_id else None
        if not mint:
            continue
        name = a.get("name", "")
        symbol = name.split("/")[0].strip() if "/" in name else name
        out.append({"mint": mint, "symbol": symbol or mint[:6],
                    "liq": liq, "edad_min": round(edad_min)})
    return out


def _compradores(mint: str) -> set[str]:
    """Billeteras que RECIBIERON el token en sus primeras transacciones."""
    from helius_rpc import primeras_txs
    txs, _completo = primeras_txs(mint, max_txs=RADAR_TXS)
    buyers: set[str] = set()
    for tx in txs or []:
        for tt in tx.get("tokenTransfers") or []:
            if tt.get("mint") == mint and tt.get("toUserAccount"):
                buyers.add(tt["toUserAccount"])
    return buyers


def _conocidas(conn, buyers: set[str]) -> list[dict]:
    """Cruce con la base: cuáles de esos compradores ya son de los buenos."""
    if not buyers:
        return []
    out = []
    lote = list(buyers)
    for i in range(0, len(lote), 200):        # IN (...) por tandas
        trozo = lote[i:i + 200]
        marcas = ",".join("?" * len(trozo))
        out.extend(conn.execute(
            f"""SELECT address, alias, is_tracked, grade
                FROM wallets
                WHERE address IN ({marcas})
                  AND COALESCE(is_bot, 0) = 0
                  AND (is_tracked = 1
                       OR grade IN ('Elite', 'Seguimiento')
                       OR winning_tokens_count >= 2)""",
            trozo).fetchall())
    return out


def _semaforo(t: dict) -> tuple[bool, str]:
    """(aprueba, línea legible) a partir del chequeo de seguridad real."""
    problemas = []
    if t.get("mint_auth"):
        problemas.append("mint ACTIVA")
    if t.get("freeze_auth"):
        problemas.append("freeze ACTIVA")
    lp = t.get("lp_locked_pct")
    if lp is not None and lp < 50:
        problemas.append(f"LP lock {lp:.0f}%")
    top10 = t.get("top10_pct")
    if top10 is not None and top10 > 60:
        problemas.append(f"top10 {top10:.0f}%")
    riesgos = t.get("risks") or []
    if riesgos:
        problemas.append("riesgos: " + ", ".join(str(r) for r in riesgos[:2]))
    # (Ola 15, corregido en Ola 16) Si el chequeo NO se hizo, no se puede
    # aprobar. El indicador es `rug_ok` — que la fuente RESPONDIO —, no la
    # ausencia de valores: RugCheck devuelve null en mint/freeze cuando la
    # autoridad esta REVOCADA (el mejor estado), y un token de minutos aun
    # no tiene topHolders ni markets. La version anterior rechazaba
    # exactamente a los tokens limpios.
    if not t.get("rug_ok"):
        return False, "⚪ sin datos de seguridad (chequeo falló)"
    aprueba = not (t.get("mint_auth") or t.get("freeze_auth")
                   or len(riesgos) >= 3)
    linea = ("🟢 seguridad ok" if not problemas
             else ("🟡 " if aprueba else "🔴 ") + " · ".join(problemas))
    return aprueba, linea


def escanear() -> int:
    """Una pasada del radar. Devuelve cuántos HALLAZGOS hubo (tokens
    nuevos con billeteras conocidas dentro), se hayan avisado o no: en
    modo oculto el radar encuentra lo mismo, solo que en silencio."""
    if not ACTIVO:
        return 0
    try:
        from helius_budget import puede_llamar
        if not puede_llamar():
            print("· Radar: freno de presupuesto Helius activo; pasada "
                  "omitida")
            return 0
    except Exception:
        pass
    try:
        candidatos = _frescos()
    except Exception as e:
        print(f"· Radar: new_pools falló ({e})")
        return 0
    if not candidatos:
        return 0

    conn = get_conn()
    hallazgos = 0
    # (Ola 17-I) Tokens cuya seguridad NO se pudo comprobar (la fuente no
    # respondio). No se queman: vuelven a la cola y se cuentan aqui.
    _sin_chequear = 0
    _mudo = True
    try:
        # Una lectura por pasada, no una por token. Va DENTRO del try que
        # cierra la conexión: si fallara aquí fuera, la conexión quedaría
        # abierta reteniendo el candado de SQLite.
        _mudo = silencioso(conn)
        # Poda de registros viejos (14 días): la tabla no crece sin tope.
        conn.execute("DELETE FROM radar_tokens WHERE ts < ?",
                     (int(time.time()) - 14 * 86400,))
        conn.commit()
        examinados = 0
        for c in candidatos:
            if examinados >= TOKENS_PASADA:
                break
            # Dedup por esquema: el segundo INSERT del mismo mint es no-op.
            cur = conn.execute(
                "INSERT OR IGNORE INTO radar_tokens "
                "(mint, ts, symbol, liq, resultado) VALUES (?,?,?,?,?)",
                (c["mint"], int(time.time()), c["symbol"], c["liq"],
                 "examinando"))
            conn.commit()
            if not cur.rowcount:
                continue                      # ya visto en otra pasada
            examinados += 1

            # 1. Semáforo de seguridad (DexScreener + RugCheck)
            try:
                from token_check import analyze_token
                t = analyze_token(c["mint"])
            except Exception as e:
                print(f"· Radar: seguridad de {c['mint'][:8]} falló: {e}")
                t = {}
            aprueba, linea_seg = _semaforo(t)
            try:
                _p0 = float(t.get("price") or 0) or None
            except (TypeError, ValueError):
                _p0 = None
            conn.execute(
                "UPDATE radar_tokens SET price0=? WHERE mint=?",
                (_p0, c["mint"]))
            conn.commit()
            if not aprueba:
                if "sin datos" in linea_seg:
                    # (Ola 17-I, auditoria 6) El chequeo NO SE HIZO (la
                    # fuente no respondio). Antes esto escribia
                    # `sin_seguridad` y el token quedaba QUEMADO: la fila
                    # ya existe, asi que el INSERT OR IGNORE de arriba
                    # impide volver a entrar, y el seguimiento excluye
                    # `sin_seguridad`. Un rato de RugCheck caido podia
                    # tirar cientos de tokens sanos sin que /radar lo
                    # contara siquiera. Ahora se BORRA la reserva: el
                    # token vuelve a la cola y se examina en otra pasada
                    # mientras siga siendo fresco.
                    conn.execute("DELETE FROM radar_tokens WHERE mint=?",
                                 (c["mint"],))
                    conn.commit()
                    # (Ola 17-K) NO se resta a `examinados`. En la 17-I
                    # puse `examinados -= 1` para que "no gastara turno",
                    # y con RugCheck devolviendo 404 —lo normal en un
                    # token de minutos— el contador no crecia nunca: la
                    # pasada procesaba TODOS los candidatos en vez de
                    # TOKENS_PASADA, con 3 peticiones cada uno, y podia
                    # solaparse con la pasada siguiente (radar_job no
                    # tiene candado). El intento SI cuenta; lo que no se
                    # pierde es el token, que vuelve a la cola.
                    _sin_chequear += 1
                    continue
                conn.execute(
                    "UPDATE radar_tokens SET resultado=? WHERE mint=?",
                    ("descartado_seguridad", c["mint"]))
                conn.commit()
                continue

            # 2. ¿Está comprando gente que ya conocemos?
            buyers = _compradores(c["mint"])
            conocidas = _conocidas(conn, buyers)
            if len(conocidas) < MIN_CONOCIDAS:
                conn.execute(
                    "UPDATE radar_tokens SET resultado=?, smart=0 "
                    "WHERE mint=?",
                    (f"sin_conocidas:{len(buyers)}", c["mint"]))
                conn.commit()
                continue

            # 3. Alerta: smart money de TU base en un token de minutos
            nombres = []
            for w in conocidas[:5]:
                icono = "⭐" if w["is_tracked"] else (
                    "🏆" if w["grade"] in ("Elite", "Seguimiento") else "👁")
                nombres.append(f"{icono} {w['alias'] or w['address'][:8]}")
            sym = t.get("symbol") if t.get("symbol") not in (None, "?") \
                else c["symbol"]
            # MODO OCULTO: el hallazgo se registra igual (la fila queda
            # con `smart` y sale en /radar y en el resumen diario), pero
            # no se escribe en Telegram. El contador cuenta HALLAZGOS, no
            # mensajes: apagar el aviso no puede cambiar lo que el radar
            # dice haber encontrado.
            try:
                if not _mudo:
                    from realtime import tg_send
                    tg_send(
                        f"📡 *RADAR: smart money en token recién nacido*\n"
                        f"💎 *{sym}* · {c['edad_min']} min de vida · "
                        f"liq ${c['liq']:,.0f}\n"
                        f"{linea_seg}\n"
                        f"👥 De tu base ({len(conocidas)}): "
                        + ", ".join(nombres) + "\n"
                        f"`{c['mint']}`\n"
                        f"📊 [DexScreener](https://dexscreener.com/solana/"
                        f"{c['mint']})")
            except Exception as e:
                print(f"· Radar: alerta falló: {e}")
            conn.execute(
                "UPDATE radar_tokens SET resultado=?, smart=? WHERE mint=?",
                (f"alertado:{len(conocidas)}", len(conocidas), c["mint"]))
            conn.commit()
            hallazgos += 1
        # ── 14b: seguimiento de los ya examinados → promover ganadores ──
        try:
            _seguimiento(conn)
        except Exception as e:
            print(f"· Radar: seguimiento falló: {e}")
    finally:
        conn.close()
    if hallazgos:
        print(f"📡 Radar: {hallazgos} hallazgo(s) de smart money temprana"
              + (" (en silencio)" if _mudo else ""))
    if _sin_chequear:
        # (Ola 17-I) Se DICE. Antes esto no aparecia en ningun sitio: ni
        # en el log ni en /radar, asi que un rato de RugCheck caido era
        # completamente invisible.
        print(f"📡 Radar: {_sin_chequear} token(s) sin poder comprobar la "
              f"seguridad; vuelven a la cola")
        try:                    # (Ola 17-K) para que /radar lo enseñe
            _c2 = get_conn()
            try:
                from db import set_setting
                set_setting(_c2, "radar_sin_chequear", _sin_chequear)
                set_setting(_c2, "radar_sin_chequear_ts", int(time.time()))
            finally:
                _c2.close()
        except Exception:
            pass
    return hallazgos


def _seguimiento(conn) -> int:
    """Re-visita los tokens examinados (1-48 h): el que hizo xN se
    promueve al catalogo de ganadores para que el ciclo analice a sus
    compradores tempranos con la semantica de siempre. Devuelve cuantos
    promovio."""
    ahora = int(time.time())
    _mudo_seg = silencioso(conn)   # una lectura por pasada
    # (Ola 15) Muestreo ALEATORIO, no "los 15 mas viejos": con volumen,
    # el orden fijo hacia que un token que hizo x5 a la hora 3 no se
    # re-visitara hasta casi expirar (inanicion). RANDOM() existe igual
    # en SQLite y en Postgres.
    filas = conn.execute(
        """SELECT mint, symbol, ts, price0 FROM radar_tokens
           WHERE price0 IS NOT NULL AND price0 > 0
             AND resultado NOT IN ('descartado_seguridad', 'sin_seguridad',
                 'ganador_promovido', 'expirado', 'murio', 'examinando')
             AND ts BETWEEN ? AND ?
           ORDER BY RANDOM() LIMIT 15""",
        (ahora - SEG_MAX_H * 3600, ahora - 3600)).fetchall()
    promovidos = 0
    for r in filas:
        try:
            from signal_tracker import _price_mc_ex
            px, mc, muerto, liq = _price_mc_ex(r["mint"])
        except Exception:
            continue
        if muerto:
            conn.execute("UPDATE radar_tokens SET resultado=? "
                         "WHERE mint=?", ("murio", r["mint"]))
            conn.commit()
            continue
        if not px:
            continue
        mult = px / r["price0"]
        if mult >= GANADOR_X:
            # (14c) El x3 solo abre la puerta; la vara para "ganador" es
            # LA MISMA del descubrimiento clasico: volumen, liquidez y MC
            # minimos de config. Un token de $8k de MC hace x3 con dos
            # compras — eso no es un ganador, es ruido chico. Si aun no
            # da la talla, se queda en seguimiento (puede crecer hasta
            # las 48 h); solo se promueve cuando el tamaño acompana.
            import config as _cfg
            try:
                from token_check import analyze_token
                t = analyze_token(r["mint"])
            except Exception:
                t = {}
            vol = t.get("vol24") or 0
            liq_full = t.get("liq") or liq or 0
            mc_full = t.get("mc") or mc or 0
            if not (vol >= _cfg.MIN_VOLUME_24H_USD
                    and liq_full >= _cfg.MIN_LIQUIDITY_USD
                    and mc_full >= _cfg.MIN_MC_USD):
                print(f"  · Radar: {r['symbol']} hizo x{mult:.1f} pero "
                      f"no da la talla (vol ${vol:,.0f} / liq "
                      f"${liq_full:,.0f} / MC ${mc_full:,.0f}); sigue "
                      f"en observación")
                continue
            from db import save_winning_token
            save_winning_token(conn, {
                "mint": r["mint"], "symbol": r["symbol"],
                "name": r["symbol"],
                "price_change_24h": round((mult - 1) * 100, 1),
                "volume_24h_usd": vol, "liquidity_usd": liq_full,
                "pair_address": t.get("pair")})
            conn.execute("UPDATE radar_tokens SET resultado=? "
                         "WHERE mint=?", ("ganador_promovido", r["mint"]))
            conn.commit()
            promovidos += 1
            # MODO OCULTO: la promoción al embudo SE HACE igual (es lo
            # valioso del radar: mete al ciclo a los compradores
            # tempranos de un token que ya hizo xN); lo único que se
            # calla es el mensaje.
            if not _mudo_seg:
                try:
                    from realtime import tg_send
                    tg_send(f"🏆 *Radar → embudo*: *{r['symbol']}* hizo "
                            f"x{mult:.1f} desde el radar Y da la talla de "
                            f"ganador (vol ${vol:,.0f} · liq "
                            f"${liq_full:,.0f} · MC ${mc_full:,.0f}). "
                            f"El próximo ciclo analizará a sus compradores "
                            f"tempranos.\n`{r['mint']}`")
                except Exception:
                    pass
        elif ahora - r["ts"] > SEG_MAX_H * 3600 - 3600:
            conn.execute("UPDATE radar_tokens SET resultado=? "
                         "WHERE mint=?", ("expirado", r["mint"]))
            conn.commit()
    if promovidos:
        print(f"📡 Radar: {promovidos} token(s) promovidos a ganadores")
    return promovidos


def radar_text() -> str:
    """Resumen para /radar: qué vio el radar en las últimas 24 h."""
    conn = get_conn()
    try:
        corte = int(time.time()) - 86400
        tot = conn.execute(
            "SELECT COUNT(*) c FROM radar_tokens WHERE ts >= ?",
            (corte,)).fetchone()["c"]
        seg = conn.execute(
            "SELECT COUNT(*) c FROM radar_tokens WHERE ts >= ? "
            "AND resultado='descartado_seguridad'", (corte,)).fetchone()["c"]
        # (Ola 17-I) El informe solo contaba los descartados por
        # seguridad: con 229 filas decia "examinados 229 · descartados 89"
        # y las otras 140 desaparecian. Ahora se desglosa todo.
        otros = conn.execute(
            "SELECT resultado, COUNT(*) c FROM radar_tokens WHERE ts >= ? "
            "GROUP BY resultado", (corte,)).fetchall()
        # (18-P) Por `smart`, no por el estado: el que se alertó y luego
        # murió seguía siendo un hallazgo. Y sin LIKE '...%' con
        # parámetros: en Postgres ese `%` literal revienta la consulta
        # (psycopg2 interpola en cliente y esperaría `%%`).
        alertados = conn.execute(
            "SELECT mint, symbol, resultado, ts, smart FROM radar_tokens "
            "WHERE ts >= ? AND (COALESCE(smart, 0) > 0 "
            "     OR SUBSTR(COALESCE(resultado, ''), 1, 9) = 'alertado:') "
            "ORDER BY ts DESC LIMIT 10", (corte,)).fetchall()
    finally:
        conn.close()
    if not ACTIVO:
        return "📡 El radar está APAGADO (RADAR_ACTIVO=0)."
    conn2 = get_conn()
    try:
        _mudo = silencioso(conn2)
        # `smart` O el estado: si el relleno de arranque no llegó a una
        # fila (falló, o la escribió una versión anterior entre
        # despliegues), el total y el desglose de arriba se
        # contradecirían. SUBSTR en vez de LIKE '...%' porque esta
        # consulta SÍ lleva parámetros y en Postgres ese `%` reventaría.
        _hallazgos = conn2.execute(
            "SELECT COUNT(*) AS c FROM radar_tokens WHERE ts >= ? "
            "AND (COALESCE(smart, 0) > 0 "
            "     OR SUBSTR(COALESCE(resultado, ''), 1, 9) = 'alertado:')",
            (corte,)).fetchone()["c"]
        prom = conn2.execute(
            "SELECT COUNT(*) c FROM radar_tokens WHERE ts >= ? "
            "AND resultado='ganador_promovido'",
            (int(time.time()) - 7 * 86400,)).fetchone()["c"]
    finally:
        conn2.close()
    # (Ola 17-K) Los que no se pudieron comprobar se BORRAN de la tabla
    # (para poder reintentarlos), asi que no salen en `tot`. Se guarda el
    # contador de la ultima pasada para poder decirlo: si no, el commit
    # que decia "ningun token desaparece" habria creado justo eso.
    _res = {r["resultado"]: r["c"] for r in otros}
    _murio = _res.get("murio", 0)
    _sinc = sum(v for k, v in _res.items() if str(k).startswith("sin_conocidas"))
    # OJO: esto es el ESTADO actual, no el total de hallazgos. En cuanto
    # el seguimiento pisa la fila (murio/expirado/ganador_promovido) el
    # token sale de aquí, así que este número solo sirve para que el
    # desglose de abajo sume `tot`. El total de verdad va aparte, por
    # `smart`, que no lo pisa nadie.
    _alert = sum(v for k, v in _res.items() if str(k).startswith("alertado"))
    _exam = _res.get("examinando", 0)
    _resto = tot - seg - _murio - _sinc - _alert - _exam
    out = ["📡 *Radar de pares recién nacidos* (24 h)\n",
           ("🔇 *Modo oculto*: trabaja igual pero no avisa por Telegram "
            "(encender los avisos: `/radarsilencio off`)"
            if _mudo else
            "🔔 *Avisa por Telegram* cuando entra smart money "
            "(silenciar: `/radarsilencio on`)"),
           "",
           f"Tokens examinados: {tot}",
           f"  ⛔ descartados por seguridad: {seg}",
           f"  💀 murieron: {_murio}",
           f"  👤 sin billeteras conocidas: {_sinc}",
           f"  🎯 con smart money, aún en seguimiento: {_alert}"
           + (f"  ·  ⏳ en curso: {_exam}" if _exam else "")
           + (f"  ·  otros: {_resto}" if _resto > 0 else ""),
           f"🎯 *Hallazgos de smart money en 24 h: {_hallazgos}* "
           f"(cuenta los de arriba y también los que ya murieron o se "
           f"promovieron)",
           f"🏆 Promovidos a ganadores (7 días): {prom} — sus compradores "
           f"tempranos entran al embudo"]
    # (Ola 17-K) Los que no se pudieron comprobar no estan en la tabla
    # (se borran para reintentarlos), asi que hay que decirlo aparte.
    try:
        _c3 = get_conn()
        try:
            from db import get_setting as _gs
            _sc = int(float(_gs(_c3, "radar_sin_chequear", 0) or 0))
            _sc_ts = float(_gs(_c3, "radar_sin_chequear_ts", 0) or 0)
        finally:
            _c3.close()
        if _sc and time.time() - _sc_ts < 3600:
            out.append(f"  ⚪ sin poder comprobar la seguridad en la última "
                       f"pasada: {_sc} (vuelven a la cola, no se pierden)")
    except Exception:
        pass
    if alertados:
        out.append("\n🎯 Con smart money de tu base:")
        for r in alertados:
            # Mismo repliegue que en los conteos: si a la fila no le llegó
            # el relleno de arranque, el número sigue en su `resultado`
            # ('alertado:3'). Sin esto la lista imprimía "None conocida(s)".
            n = r["smart"]
            if n is None:
                n = str(r["resultado"] or "").split(":")[-1] or "?"
            hace = (time.time() - r["ts"]) / 3600
            # El ESTADO va en la línea: desde que la lista se saca de
            # `smart` (un hecho) también salen los que ya murieron, y sin
            # decirlo el dueño podría copiar el mint de un token muerto.
            _e = str(r["resultado"] or "")
            _ico = ("💀 murió" if _e == "murio" else
                    "🏆 promovido al embudo" if _e == "ganador_promovido"
                    else "⌛ expiró" if _e == "expirado"
                    # "en seguimiento", no "vivo": el seguimiento revisa
                    # 15 filas al azar por pasada y ninguna de menos de
                    # 1 h, así que un token puede llevar horas muerto y
                    # todavía no haberse comprobado. Decir "vivo" sería
                    # afirmar más de lo que se sabe.
                    else "🟢 en seguimiento")
            out.append(f"  • {r['symbol']} · {n} conocida(s) · "
                       f"hace {hace:.1f}h · {_ico}\n    `{r['mint']}`")
    else:
        out.append("\nSin coincidencias con tu base todavía — solo "
                   "cuentan como hallazgo los tokens nuevos donde entran "
                   "billeteras conocidas, no los de 'pinta prometedora'.")
    return "\n".join(out)
