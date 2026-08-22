"""
Paper trading: simula operaciones con las señales alertadas para medir
si el sistema ganaría dinero SIN arriesgar capital real.

Reglas:
  - Cada señal de COMPRA alertada abre una posición simulada. El monto
    es el de la billetera ⭐, con TOPE configurable por señal
    (settings 'paper_max_sol').
  - Solo una posición abierta por token, y un máximo de posiciones
    abiertas a la vez ('paper_max_abiertas').
  - Cierre por: take-profit ('paper_tp_pct', +100% por defecto),
    stop-loss ('paper_sl_pct', 50 → cierra a -50%), tiempo
    ('paper_timeout_h', 48 h), o porque la billetera ⭐ que dio la
    señal vendió (seguimos su salida).
  - PnL nocional: pnl = monto × variación % del precio del token.
    El monto se guarda en SOL *y* en dólares al cambio del momento de
    entrar, y el PnL se publica en dólares, que es la unidad en la que
    está medida la variación del precio. El PnL en SOL se sigue
    guardando (pnl_sol) para no invalidar el histórico ya cerrado, pero
    ojo: asume que el precio de SOL no se movió, así que es una
    aproximación, no una cifra exacta.
    No simula slippage ni fees; es una medida optimista pero comparable.

Settings (tabla settings, editables con /paper o desde el chat):
  paper_enabled       "1"/"0"  (default 1)
  paper_max_sol       tope de monto por señal, en SOL (default 1.0)
  paper_max_abiertas  máximo de posiciones abiertas (default 10)
  paper_tp_pct        take-profit % (default 100 = x2)
  paper_sl_pct        stop-loss % positivo (default 50 → -50%)
  paper_timeout_h     horas máximas de una posición (default 48)
"""

import time

from db import get_conn, get_setting

HOUR = 3600


def _g(conn, key: str, default):
    """get_setting con tolerancia (el modulo ya importa db abajo)."""
    try:
        from db import get_setting
        return get_setting(conn, key, default)
    except Exception:
        return default


def _f(conn, key: str, default: float) -> float:
    try:
        return float(get_setting(conn, key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _enabled(conn) -> bool:
    return (get_setting(conn, "paper_enabled", "1") or "1").strip() != "0"


def _tg(text: str):
    try:
        from realtime import tg_send
        tg_send(text)
    except Exception as e:
        print(f"· Paper: aviso TG falló: {e}")


# ── Formato de importes ───────────────────────────────────────────────────
# Los tres se importan dentro de la función a propósito: card_image tira de
# Pillow y unrealized_pnl de requests, y no queremos que un fallo de esos
# módulos impida abrir una posición.

def _precio(x) -> str:
    """Precio en dólares legible. $0.00008342, no $8.342e-05."""
    try:
        from card_image import _fmt_price
        return _fmt_price(x)
    except Exception:
        try:
            return f"{float(x):.10f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return "?"


def _sol_a_usd() -> float | None:
    """Precio SOL/USD ahora mismo (cache de 5 min en unrealized_pnl)."""
    try:
        from unrealized_pnl import _sol_usd
        return _sol_usd()
    except Exception:
        return None


def _usd(x) -> str:
    """Importe en dólares con separador de miles."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    return f"${x:,.2f}" if abs(x) >= 1 else f"${x:,.4f}"


def _usd_firmado(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    return ("+" if x >= 0 else "-") + _usd(abs(x))


# ───────────────────────── Apertura ───────────────────────────────────────

def _symbol_db(conn, mint: str) -> str | None:
    """(21/8, restaurado 22/8: el commit 3761eaf lo piso sin querer)
    Ticker desde la PROPIA base: señales previas del mint o el catalogo
    de ganadores ya lo traen casi siempre. Cero red, cero espera.
    Descarta los placebos guardados que son el trozo del contrato."""
    try:
        for sql in ("SELECT symbol FROM signals WHERE mint=? AND symbol "
                    "IS NOT NULL AND symbol<>'' ORDER BY ts DESC LIMIT 1",
                    "SELECT symbol FROM winning_tokens WHERE mint=? "
                    "AND symbol IS NOT NULL AND symbol<>''"):
            r = conn.execute(sql, (mint,)).fetchone()
            if r and r["symbol"] and not mint.startswith(r["symbol"]):
                return str(r["symbol"]).strip()
    except Exception:
        pass
    return None


def _symbol_helius(mint: str) -> str | None:
    """(22/8) Ultimo eslabon: Helius DAS conoce el ticker desde el
    SEGUNDO CERO del mint. Un token recien nacido en pump.fun aun no
    esta en DexScreener (caso real: "Doge2" salio como "EjAuFt")."""
    try:
        from helius_rpc import simbolo_token
        return simbolo_token(mint)
    except Exception:
        return None


def _symbol_rapido(mint: str) -> str | None:
    """Ticker del token en una consulta corta a DexScreener (para que la
    tarjeta del camino caliente no salga con el pedazo de contrato)."""
    try:
        import config
        import requests
        r = requests.get(config.DEXSCREENER_TOKEN.format(address=mint),
                         timeout=4)
        pares = (r.json() or {}).get("pairs") or []
        if pares:
            s = ((pares[0].get("baseToken") or {}).get("symbol") or "")
            return s.strip() or None
    except Exception:
        pass
    return None


def open_trade(conn, trade: dict, token: dict, score,
               origen: str = "top") -> bool:
    """Abre una posición simulada a partir de una señal de compra alertada.
    Devuelve True si se abrió."""
    if not _enabled(conn):
        return False
    price = token.get("price")
    if not price or price <= 0:
        print("· Paper: sin precio del token; no se abre posición")
        return False

    # Una posición abierta por token
    ya = conn.execute(
        "SELECT id FROM paper_trades WHERE mint=? AND status='abierta'",
        (trade["mint"],)).fetchone()
    if ya:
        return False

    # ── Enfriamiento por token ────────────────────────────────────────
    # La posicion de un token la abre la PRIMERA ⭐ que lo compra y la
    # cierra su propia venta. Sin este bloqueo, si otra ⭐ compraba el
    # mismo token minutos despues del cierre, se abria una posicion
    # NUEVA a otro market cap: re-compras en cadena del mismo token.
    # Un token ya jugado no se vuelve a jugar hasta pasar el plazo
    # ("paper_reentrada_h"; 0 = sin bloqueo).
    reent_h = _f(conn, "paper_reentrada_h", 24.0)
    if reent_h > 0:
        ult = conn.execute(
            "SELECT MAX(exit_ts) t FROM paper_trades "
            "WHERE mint=? AND status<>'abierta'",
            (trade["mint"],)).fetchone()
        if ult and ult["t"] and time.time() - ult["t"] < reent_h * 3600:
            print(f"· Paper: {trade['mint'][:8]}… ya se jugó hace "
                  f"{(time.time()-ult['t'])/3600:.1f}h (enfriamiento "
                  f"{reent_h:g}h); no se reabre")
            return False

    # Máximo de posiciones abiertas
    max_abiertas = int(_f(conn, "paper_max_abiertas", 10))
    n = conn.execute(
        "SELECT COUNT(*) c FROM paper_trades WHERE status='abierta'"
    ).fetchone()["c"]
    if n >= max_abiertas:
        print(f"· Paper: {n} posiciones abiertas (máx {max_abiertas}); "
              "no se abre otra")
        return False

    # Tope de monto por señal
    max_sol = _f(conn, "paper_max_sol", 1.0)
    stake = min(float(trade.get("sol") or 0) or max_sol, max_sol)
    if stake <= 0:
        return False

    # El camino caliente abre antes de conocer el ticker y la tarjeta
    # salia con el pedazo de contrato ("7xKq4B"). Consulta relampago del
    # simbolo (19/8): ~300 ms que no frenan la copia; si falla, el
    # backfill posterior corrige la base igual que siempre.
    # (21/8, restaurado 22/8) El camino caliente manda el PEDAZO DE
    # CONTRATO como "symbol" (mint[:6]) y el `or` lo daba por bueno: el
    # buscador rapido nunca corria. Cadena: placebo descartado → propia
    # base → DexScreener → Helius DAS (tokens recien nacidos) → prefijo.
    _sym_tok = (token.get("symbol") or "").strip()
    if _sym_tok and trade["mint"].startswith(_sym_tok):
        _sym_tok = None                      # es el trozo del contrato
    sym = (_sym_tok or _symbol_db(conn, trade["mint"])
           or _symbol_rapido(trade["mint"])
           or _symbol_helius(trade["mint"]) or trade["mint"][:6])
    # Importe en dólares al cambio de AHORA. Se guarda, no se recalcula
    # al cerrar: lo que quieres saber es cuánto dinero habrías puesto.
    su = _sol_a_usd()
    stake_usd = stake * su if su and su > 0 else None

    # ── Ejecucion simulada: cotizacion REAL de Jupiter (sin ejecutar) ──
    # ¿Cuantos tokens darian AHORA por este monto, con la ruta real y su
    # impacto? Eso captura el slippage y los fees de pool que el paper
    # clasico ignoraba. Si Jupiter no responde o no hay ruta, la posicion
    # se abre igual que siempre (solo que sin la parte "real").
    cot = None
    if stake_usd:
        try:
            from ejecucion_simulada import cotizar_compra
            cot = cotizar_compra(trade["mint"], stake_usd, su)
        except Exception as e:
            print(f"· Paper: cotización de entrada falló ({e})")
    fee_sol = _f(conn, "paper_fee_sol", 0.0005)
    costo_entrada = fee_sol * su if su and su > 0 else None

    # Cuanto tardamos en copiar: desde que la ⭐ opero en la cadena
    # hasta este instante. Con el camino caliente deberia rondar 1-3 s;
    # por la via normal (analisis completo + IA) eran 5-15 s.
    demora = max(0.0, time.time() - (trade.get("ts") or time.time()))
    # A/B de gestion de salidas: alterna reglas/ia por orden de llegada.
    # Solo se asigna "ia" si el experimento esta encendido (/ialocal);
    # apagado, todo es "reglas" y el comportamiento es el de siempre.
    gestion = "reglas"
    try:
        if int(float(_g(conn, "ia_local_activa", "0") or 0)):
            n_previas = conn.execute(
                "SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
            gestion = "ia" if n_previas % 2 else "reglas"
    except Exception:
        pass
    conn.execute(
        """INSERT INTO paper_trades
           (signature, wallet, mint, symbol, stake_sol, stake_usd,
            entry_price, entry_ts, signal_score, status,
            tokens_raw, slippage_entrada_pct, costos_usd, demora_s,
            gestion, origen)
           VALUES (?,?,?,?,?,?,?,?,?, 'abierta', ?,?,?,?,?,?)""",
        (trade["signature"], trade["wallet"], trade["mint"], sym,
         stake, stake_usd, price, trade["ts"], score,
         str(cot["tokens_raw"]) if cot else None,
         cot.get("slippage_pct") if cot else None,
         costo_entrada, round(demora, 2), gestion, origen))
    conn.commit()
    # (Ola 12, restaurado 22/8: el commit 3761eaf lo piso sin querer)
    # Resolver el creador del token EN FONDO (1 llamada RPC): la
    # vigilancia dev-sell necesita saber quien es el dev, y el camino
    # caliente no espera a nadie.
    try:
        _fila = conn.execute(
            "SELECT id FROM paper_trades WHERE signature=? "
            "ORDER BY id DESC LIMIT 1", (trade["signature"],)).fetchone()
        if _fila:
            import threading as _th
            from dev_watch import guardar_dev
            _th.Thread(target=guardar_dev,
                       args=(_fila["id"], trade["mint"]),
                       daemon=True).start()
    except Exception as e:
        print(f"· dev_watch: no pude lanzar la resolución del dev: {e}")
    monto = (f"{_usd(stake_usd)} ({stake:.2f} SOL)" if stake_usd is not None
             else f"{stake:.2f} SOL")
    print(f"🧪 Paper: compra simulada {monto} en {sym} "
          f"@ ${_precio(price)} · demora {demora:.1f}s")
    # La jugada de la ⭐ visible en el aviso (pedido del 17/8): las copias
    # del camino caliente pueden nacer de señales bajo el umbral, que no
    # mandan tarjeta — sin esto, aparecian copias "de la nada".
    linea_star = ""
    try:
        _rw = conn.execute("SELECT alias FROM wallets WHERE address=?",
                           (trade["wallet"],)).fetchone()
        _nom = (_rw["alias"] if _rw and _rw["alias"]
                else trade["wallet"][:8] + "…")
        _pos = None
        try:
            from wallet_ident import posicion
            _pos = posicion(conn, trade["wallet"], 30)
        except Exception:
            pass
        linea_star = (f"\n⭐ Copiando a *{_nom}*"
                      + (f" (#{_pos} del top)" if _pos else "")
                      + (f" · compró {float(trade.get('sol') or 0):.2f} SOL"
                         if trade.get("sol") else ""))
        if origen == "consenso":
            linea_star += ("\n🤝 *Copia por CONSENSO*: varias ⭐ entraron "
                           "a este token; se imita a la LÍDER (la primera)")
    except Exception:
        pass
    extra_cot = ""
    if cot and cot.get("slippage_pct") is not None:
        extra_cot = (f"\n📉 Slippage real de entrada: "
                     f"{cot['slippage_pct']:.1f}% (cotización Jupiter)")
    _tg(f"🧪 *Paper:* compra simulada{linea_star}\n"
        f"💵 Monto: *{monto}*\n"
        f"🪙 Token: *{sym}*  ·  entrada ${_precio(price)}{extra_cot}\n"
        f"📂 {n + 1}/{max_abiertas} abiertas\nVer: /paper")

    # ── Filtro de entrada IA (modo SOMBRA, pedido del 17/8) ──────────
    # La IA opina si esta compra valia la pena; la posicion corre igual
    # (es simulada) y /paper mide cuanto habria ahorrado rechazar.
    # EN HILO DE FONDO (arreglo del mismo dia): _proc procesa las
    # transacciones en bucle, y una consulta de hasta 8 s aqui retenia a
    # la SIGUIENTE señal del lote — que si es camino caliente. El hilo
    # abre su propia conexion (las conexiones no se comparten entre
    # hilos) y muere solo. Si la IA no esta, no viaja veredicto y ya.
    try:
        if int(float(_g(conn, "ia_local_activa", "0") or 0)):
            import threading as _th
            _tr, _tok, _sym = dict(trade), dict(token or {}), sym

            def _veredicto_en_fondo():
                try:
                    from decision_ia import decidir_entrada
                    from db import get_conn as _gc
                    c2 = _gc()
                    try:
                        v = decidir_entrada(c2, _tr, _tok)
                        if v:
                            c2.execute(
                                "UPDATE paper_trades SET ia_entrada=?, "
                                "ia_entrada_razon=? WHERE signature=?",
                                (v["entrada"], v.get("razon", ""),
                                 _tr["signature"]))
                            c2.commit()
                            if v["entrada"] == "rechazar":
                                _tg(f"🚫 *La IA habría rechazado* esta "
                                    f"compra en {_sym}: "
                                    f"_{v.get('razon','')}_\n"
                                    f"(la posición corre igual: al cierre "
                                    f"sabremos quién tenía razón)")
                    finally:
                        c2.close()
                except Exception as e:
                    print(f"· Filtro de entrada IA falló: {e}")

            _th.Thread(target=_veredicto_en_fondo, daemon=True).start()
    except Exception as e:
        print(f"· Filtro de entrada IA no arrancó: {e}")
    return True


# ───────────────────────── Cierres ────────────────────────────────────────

def _campo(row, nombre):
    """row[nombre] tolerante: las filas viejas no tienen las columnas
    nuevas, y sqlite3.Row lanza IndexError en vez de devolver None."""
    try:
        return row[nombre]
    except (IndexError, KeyError, TypeError):
        return None


# ── Libro de eventos (Ola 4, auditoria 19/8) ─────────────────────────────
# Cada salida es UNA fila inmutable en paper_fills. La idempotencia sale
# del esquema — UNIQUE(trade_id, firma) — y no de una columna-recuerdo:
# ultima_venta_sig solo guardaba la ULTIMA firma, asi que la re-entrega
# de un evento ANTERIOR (Helius reintenta fuera de orden) pasaba la
# guardia. El INSERT OR IGNORE atrapa todos, para siempre.

def _fill_nuevo(conn, trade_id: int, firma) -> bool:
    """Reclama el evento para este trade. True = primera vez (procesar);
    False = ya procesado (el otro camino llego antes). Sin firma (jobs
    TP/SL/tiempo) siempre True: esos solo ocurren una vez por diseño."""
    if not firma:
        return True
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO paper_fills (trade_id, firma, ts) "
            "VALUES (?,?,?)", (trade_id, firma, int(time.time())))
        conn.commit()
        return bool(cur.rowcount)
    except Exception as e:
        print(f"· paper_fills no disponible ({e}); sigo sin guardia")
        return True


def _fill_resultado(conn, trade_id: int, firma, tipo: str, motivo: str,
                    fraccion, precio, usd_cotizado, fee_usd) -> None:
    """Completa (o crea, si no hay firma) la fila del evento con lo que
    realmente paso. Best-effort: un fallo aqui no frena el paper."""
    try:
        if firma:
            conn.execute(
                """UPDATE paper_fills SET tipo=?, motivo=?, fraccion=?,
                   precio=?, usd_cotizado=?, fee_usd=?
                   WHERE trade_id=? AND firma=?""",
                (tipo, motivo, fraccion, precio, usd_cotizado, fee_usd,
                 trade_id, firma))
        else:
            conn.execute(
                """INSERT INTO paper_fills
                   (trade_id, firma, ts, tipo, motivo, fraccion, precio,
                    usd_cotizado, fee_usd)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (trade_id, None, int(time.time()), tipo, motivo, fraccion,
                 precio, usd_cotizado, fee_usd))
        conn.commit()
    except Exception as e:
        print(f"· fill no registrado ({e})")


def _close(conn, row, price: float, reason: str, icon: str, firma=None):
    pct = (price / row["entry_price"] - 1) * 100

    # PnL en dólares sobre el importe que se guardó al entrar. Si la fila
    # es vieja y no lo tiene, se reconstruye al cambio de ahora; es una
    # aproximación, pero mejor que no dar la cifra.
    stake_usd = _campo(row, "stake_usd")
    if stake_usd is None:
        su = _sol_a_usd()
        stake_usd = (row["stake_sol"] or 0) * su if su and su > 0 else None
    # Ventas parciales espejo: el cierre liquida solo la FRACCION que
    # queda viva, y al total se le suma lo ya realizado por los trozos.
    # En filas sin parciales frac=1 y realizado=0: identico a siempre.
    frac = _campo(row, "fraccion_restante")
    frac = 1.0 if frac is None else frac
    realizado = _campo(row, "pnl_realizado_usd") or 0
    pnl_usd = (stake_usd * frac * pct / 100 + realizado
               if stake_usd is not None else None)
    # pnl_sol tambien respeta la fraccion viva (Ola 4): antes aplicaba el
    # pct final al stake COMPLETO — con parciales de por medio, el win
    # rate (que contaba por pnl_sol) podia contradecir al PnL en dolares.
    # Sigue sin incluir lo realizado por los trozos (eso vive en USD);
    # es la cifra legada, el resumen decide por pnl_usd.
    pnl = (row["stake_sol"] or 0) * frac * pct / 100

    # stake_usd se guarda también cuando se ha reconstruido. Si no, el
    # SUM(stake_usd) del resumen se saltaría esa fila y el ROI saldría
    # calculado sobre un total demasiado pequeño — lo detectó la prueba:
    # dos operaciones cerradas y solo una contada en «invertido».
    # ── Salida REAL: cotizar en Jupiter la venta de los tokens crudos
    # que la cotizacion de entrada dijo que recibiriamos. El PnL neto =
    # dolares que darian de verdad - invertido - fees de prioridad de las
    # dos transacciones. Es la cifra que decide si el copy trading real
    # seria rentable; pnl_usd (el clasico) queda para comparar.
    pnl_neto = None
    usd_salida = _campo(row, "usd_salida_real")   # acumulado de parciales
    costos = _campo(row, "costos_usd")
    tokens_raw = _campo(row, "tokens_raw")
    _usd_fill = _fee_fill = None
    if tokens_raw and stake_usd is not None and int(tokens_raw) > 0:
        try:
            from ejecucion_simulada import cotizar_venta
            su2 = _sol_a_usd()
            v = cotizar_venta(row["mint"], int(tokens_raw), su2) \
                if su2 and su2 > 0 else None
            if v:
                fee_sol = _f(conn, "paper_fee_sol", 0.0005)
                costos = (costos or 0) + fee_sol * su2
                usd_salida = (usd_salida or 0) + v["usd_salida"]
                pnl_neto = usd_salida - stake_usd - costos
                _usd_fill = v["usd_salida"]
                _fee_fill = fee_sol * su2
        except Exception as e:
            print(f"· Paper: cotización de salida falló ({e})")
    elif usd_salida is not None and stake_usd is not None:
        # todo se vendio ya por trozos: el neto sale de lo acumulado
        pnl_neto = usd_salida - stake_usd - (costos or 0)
    # Cierres "sin liquidez" (rug / par muerto): Jupiter no cotiza JAMAS
    # esos tokens, asi que el neto quedaba NULL y las PEORES perdidas se
    # excluian de "Realidad vs papel" — sesgo de supervivencia que
    # inflaba el neto real justo donde ejecutar pierde mas (Ola 4). Aqui
    # el remanente vale 0 con certeza: el neto es lo acumulado por los
    # trozos (o 0) menos lo invertido.
    if pnl_neto is None and stake_usd is not None \
            and reason == "sin liquidez":
        pnl_neto = (usd_salida or 0) - stake_usd - (costos or 0)

    conn.execute(
        """UPDATE paper_trades SET status='cerrada', exit_price=?,
           exit_ts=?, exit_reason=?, pnl_pct=?, pnl_sol=?, pnl_usd=?,
           stake_usd=?, costos_usd=?, usd_salida_real=?, pnl_usd_neto=?
           WHERE id=?""",
        (price, int(time.time()), reason, pct, pnl, pnl_usd,
         stake_usd, costos, usd_salida, pnl_neto, row["id"]))
    conn.commit()
    _fill_resultado(conn, row["id"], firma, "total", reason,
                    round(frac, 4), price, _usd_fill, _fee_fill)

    # (22/8) El icono decide por la MISMA cifra que se muestra: antes
    # usaba pnl (SOL) y el texto pnl_usd — con ventas parciales previas
    # podian discrepar y salia "🔴 PnL: +$18.50" (caso real TEST, 22/8).
    _cifra = pnl_usd if pnl_usd is not None else pnl
    res = "🟢" if _cifra >= 0 else "🔴"
    # Con parciales previos, el % del precio no cuenta la historia
    # completa: se dice.
    nota_parciales = ("\n_(el PnL incluye ventas parciales previas; el % "
                      "es solo del precio de entrada a salida)_"
                      if frac < 1 else "")
    if pnl_usd is not None:
        linea_pnl = (f"{res} PnL: *{_usd_firmado(pnl_usd)}*  "
                     f"sobre {_usd(stake_usd)} invertidos")
    else:
        linea_pnl = (f"{res} PnL: *{pnl:+.3f} SOL*  "
                     f"sobre {row['stake_sol']:.2f} SOL")
    linea_neto = ""
    if pnl_neto is not None:
        linea_neto = (f"\n⚖️ Neto real (Jupiter, con slippage y fees): "
                      f"*{_usd_firmado(pnl_neto)}*")
    # (Ola 8, 21/8) Al cerrar "sin liquidez" el precio de salida es un
    # SUPUESTO (par muerto, -99%), no una cotizacion: se dice.
    nota_precio = ("\n_(precio de salida asumido: el par ya no cotiza)_"
                   if reason == "sin liquidez" else "")
    _tg(f"{icon} *Paper cerrada* ({reason}): *{row['symbol']}*\n"
        f"💵 Precio: ${_precio(row['entry_price'])} → "
        f"*${_precio(price)}*  ({pct:+.0f}%){nota_precio}{nota_parciales}\n"
        f"{linea_pnl}{linea_neto}\n"
        f"Resumen: /paper")
    print(f"🧪 Paper cerrada {row['symbol']} por {reason}: "
          f"{_usd_firmado(pnl_usd) if pnl_usd is not None else f'{pnl:+.3f} SOL'}")


def _venta_parcial(conn, row, price: float, pct: float, firma=None):
    """La ⭐ vendio el pct% de SU posicion: el paper vende el mismo pct%
    de la SUYA. La fila sigue abierta con la fraccion restante; el PnL del
    trozo vendido se acumula en pnl_realizado_usd y se suma al cierre."""
    frac = _campo(row, "fraccion_restante")
    frac = 1.0 if frac is None else frac
    vendida = frac * pct / 100.0
    nueva = max(0.0, frac - vendida)

    pct_precio = (price / row["entry_price"] - 1) * 100
    # stake_usd se reconstruye si la fila es vieja (Ola 4): antes solo
    # _close lo hacia — un parcial sobre fila sin stake_usd reducia la
    # fraccion pero su PnL se perdia para siempre.
    stake_usd = _campo(row, "stake_usd")
    if stake_usd is None:
        _su0 = _sol_a_usd()
        stake_usd = ((row["stake_sol"] or 0) * _su0
                     if _su0 and _su0 > 0 else None)
    pnl_trozo = (stake_usd * vendida * pct_precio / 100
                 if stake_usd is not None else None)
    realizado = (_campo(row, "pnl_realizado_usd") or 0) + (pnl_trozo or 0)

    # Ejecucion simulada del trozo: cotizar en Jupiter la venta de la
    # parte proporcional de los tokens crudos. Los tokens se descuentan
    # SOLO si la cotizacion respondio (Ola 4, auditoria 19/8): antes se
    # descontaban tambien cuando Jupiter fallaba — el trozo desaparecia
    # de tokens_raw sin que sus dolares entraran a usd_salida_real, y el
    # pnl_usd_neto quedaba subestimado para siempre. Si falla, el trozo
    # queda en tokens_raw y lo cotiza el cierre.
    tokens_raw = _campo(row, "tokens_raw")
    nuevos_tokens = tokens_raw
    usd_real = _campo(row, "usd_salida_real")
    costos = _campo(row, "costos_usd")
    _fee_fill = None
    _usd_fill = None
    if tokens_raw:
        try:
            from ejecucion_simulada import cotizar_venta
            su = _sol_a_usd()
            trozo = int(int(tokens_raw) * pct / 100.0)
            v = cotizar_venta(row["mint"], trozo, su) \
                if su and su > 0 and trozo > 0 else None
            if v:
                usd_real = (usd_real or 0) + v["usd_salida"]
                fee_sol = _f(conn, "paper_fee_sol", 0.0005)
                costos = (costos or 0) + fee_sol * su
                nuevos_tokens = str(int(tokens_raw) - trozo)
                _usd_fill = v["usd_salida"]
                _fee_fill = fee_sol * su
        except Exception as e:
            print(f"· Paper: cotización parcial falló ({e}); el trozo "
                  "queda para el cierre")

    conn.execute(
        """UPDATE paper_trades SET fraccion_restante=?, pnl_realizado_usd=?,
           tokens_raw=?, usd_salida_real=?, costos_usd=?, stake_usd=?
           WHERE id=?""",
        (nueva, realizado, nuevos_tokens, usd_real, costos,
         stake_usd if stake_usd is not None else _campo(row, "stake_usd"),
         row["id"]))
    conn.commit()
    _fill_resultado(conn, row["id"], firma, "parcial",
                    f"espejo {pct:.0f}% de la ⭐", round(vendida, 4),
                    price, _usd_fill, _fee_fill)

    txt_pnl = (f" · PnL del trozo {_usd_firmado(pnl_trozo)}"
               if pnl_trozo is not None else "")
    try:
        _rw = conn.execute("SELECT alias FROM wallets WHERE address=?",
                           (row["wallet"],)).fetchone()
        _nom = (_rw["alias"] if _rw and _rw["alias"]
                else (row["wallet"] or "")[:8] + "…")
    except Exception:
        _nom = "la ⭐"
    _tg(f"✂️ *Venta parcial copiada* en *{row['symbol']}*: *{_nom}* vendió "
        f"el {pct:.0f}% y el paper vende su {pct:.0f}%"
        f" (queda {nueva*100:.0f}% de la posición){txt_pnl}")
    print(f"✂️ Paper: venta parcial {pct:.0f}% en {row['symbol']} "
          f"(queda {nueva*100:.0f}%)")


def close_on_wallet_sell(conn, trade: dict, token: dict,
                         pos: dict | None = None, sigue_estrella: bool = True):
    """La billetera que origino la señal vendio → cerramos con ella.
    Solo si es LA MISMA wallet que abrio la señal y la venta es
    significativa (≥50% de su posicion o cierre total); antes cualquier
    venta parcial de cualquier ⭐ cerraba la simulada y sesgaba el PnL."""
    row = conn.execute(
        "SELECT * FROM paper_trades WHERE mint=? AND status='abierta'",
        (trade["mint"],)).fetchone()
    if not row:
        return
    # IDEMPOTENCIA POR LIBRO DE EVENTOS (Ola 4): el camino caliente y la
    # via normal procesan el MISMO evento de venta. Chequeo barato aqui
    # (evita las consultas de quorum en duplicados); el reclamo atomico
    # va mas abajo, cuando la decision de actuar ya esta tomada.
    _firma = trade.get("signature")
    try:
        if _firma and conn.execute(
                "SELECT 1 FROM paper_fills WHERE trade_id=? AND firma=?",
                (row["id"], _firma)).fetchone():
            return
    except Exception:
        pass
    if row["wallet"] and trade.get("wallet") \
            and row["wallet"] != trade["wallet"]:
        # SALIDA DE MANADA CON QUORUM (19/8): en posiciones por CONSENSO
        # la lider manda sola (flujo normal), pero si ella se duerme, la
        # manada puede sacarte SOLO con quorum: hacen falta al menos
        # `consenso_salida_n` (2) ⭐ DISTINTAS vendiendo este token desde
        # la entrada. Una sola mano debil que compro tarde y salio sin
        # ganancia no arrastra la posicion. Para las posiciones clasicas
        # del top se mantiene la regla: solo quien abrio.
        try:
            if _campo(row, "origen") != "consenso":
                return
            quorum = int(_f(conn, "consenso_salida_n", 2))
            ent = row["entry_ts"] or 0
            # Solo cuentan vendedoras que ESTUVIERON en la manada
            # compradora (compraron el mint en la ventana de consenso,
            # 45 min = la CONSENSUS_WINDOW_MIN de realtime, antes de la
            # entrada). Una ⭐ ajena que liquida una bolsa vieja no suma.
            vendedoras = conn.execute(
                "SELECT COUNT(DISTINCT s.wallet) c FROM signals s "
                "JOIN wallets w ON w.address=s.wallet AND w.is_tracked=1 "
                "WHERE s.mint=? AND s.side='venta' AND s.ts>=? "
                "AND s.wallet IN (SELECT s2.wallet FROM signals s2 "
                "  WHERE s2.mint=? AND s2.side='compra' "
                "  AND s2.ts BETWEEN ? AND ?)",
                (trade["mint"], ent, trade["mint"],
                 ent - 45 * 60, ent)).fetchone()["c"]
            if vendedoras < max(1, quorum):
                return
            print(f"🤝 Salida de manada: {vendedoras} ⭐ de la manada "
                  f"vendieron {_campo(row, 'symbol') or trade['mint'][:8]} "
                  f"(quórum {quorum})")
            # Quorum cumplido = señal de salida TOTAL (nada de espejo
            # parcial: la fraccion de un miembro cualquiera no es la de
            # la lider). OJO deliberado: pasa por la decision de cierre
            # normal — IA/reglas pueden holdear con trailing si el perfil
            # lo amerita, y TP/SL siguen supremos. No siempre vende YA.
            pos = None
        except Exception:
            return
    price = token.get("price")
    if not price or price <= 0:
        return
    # El evento se RECLAMA aqui, en cuanto la decision de actuar esta
    # tomada: el "return por precio ausente" de arriba es transitorio y
    # SI se quiere reintentar; de aqui en adelante, todo pase repetido
    # del mismo evento (caliente + via normal) muere en el UNIQUE del
    # libro de eventos.
    if not _fill_nuevo(conn, row["id"], _firma):
        return

    # ── HOLD YA DECIDIDO (Ola 4, auditoria 19/8) ─────────────────────
    # Si la posicion ya esta en hold (la ⭐ vendio y se decidio aguantar
    # con trailing), un evento de venta NUEVO no re-decide: antes volvia
    # a consultar a la IA, reiniciaba el reloj del hold y PISABA el pico
    # con el precio actual — el trailing perdia el maximo ya alcanzado
    # justo durante la caida que debia atraparlo. La salida de un hold
    # es de TP/SL/trailing/tiempo, que siguen supremos en el job.
    if _campo(row, "politica") == "holdear":
        _fill_resultado(conn, row["id"], _firma, "hold", "venta durante "
                        "hold: no se re-decide", 0.0, price, None, None)
        print(f"🕐 Paper: venta nueva en {row['symbol']} durante el hold; "
              "mando el trailing, no se re-decide")
        return

    # ── Espejo proporcional (implementado a pedido, 13/8/2026) ────────
    # Antes: venta <50% se ignoraba y >=50% cerraba TODO. Ahora el paper
    # copia el porcentaje: si la ⭐ vendio el 15%, vendemos nuestro 15%.
    # Solo cuenta como cierre total si ella vendio (casi) todo.
    #   - < paper_parcial_min_pct (5%): polvo, se ignora.
    #   - entre medio: venta parcial espejo, la posicion sigue viva.
    #   - >= paper_total_pct (95%) o fully_sold: cierre total (abajo).
    if pos and pos.get("known") and not pos.get("fully_sold") \
            and pos.get("pct_sold") is not None:
        pct_v = float(pos["pct_sold"])
        tope_total = _f(conn, "paper_total_pct", 95.0)
        min_parcial = _f(conn, "paper_parcial_min_pct", 5.0)
        if pct_v < min_parcial:
            return                      # venta de polvo: no se copia
        if pct_v < tope_total and row["status"] == "abierta":
            _venta_parcial(conn, row, price, pct_v, firma=_firma)
            return

    # ── Salida inteligente (solo reglas, sin IA) ──────────────────────
    # Si el perfil de deriva post-venta dice que esta billetera "vende
    # temprano" (el token sigue subiendo despues de sus ventas), NO se
    # cierra con ella: se holdea con trailing stop y tope de tiempo.
    # El stop-loss y el take-profit normales SIGUEN activos durante el
    # hold. Todo queda registrado (politica, precio al vender la lider)
    # para medir si el hold extra gano dinero de verdad.
    # Solo si sigue siendo ⭐: si perdio la estrella, se cierra y punto.
    if sigue_estrella and row["status"] == "abierta":
        try:
            from salidas import perfil_salida
            perfil = perfil_salida(conn, row["wallet"])
        except Exception:
            perfil = None

        # ── Mitad "ia" del A/B: decide la IA local (con barandillas) ──
        if _campo(row, "gestion") == "ia":
            try:
                from decision_ia import decidir_salida, armar_contexto
                d = decidir_salida(
                    conn, armar_contexto(conn, row, price, perfil, token))
                conn.execute(
                    "UPDATE paper_trades SET decidido_por=? WHERE id=?",
                    (d.get("decidido_por"), row["id"]))
                conn.commit()
                if d["salida"] == "holdear":
                    hasta = int(time.time() + d["max_min"] * 60)
                    conn.execute(
                        "UPDATE paper_trades SET politica='holdear', "
                        "precio_venta_lider=?, pico=?, hold_hasta=? "
                        "WHERE id=?", (price, price, hasta, row["id"]))
                    conn.commit()
                    _tg(f"🤖 *IA local* en {row['symbol']}: la ⭐ vendió y "
                        f"la IA decide holdear hasta {d['max_min']:.0f} min "
                        f"con trailing. _{d.get('razon','')}_")
                    print(f"🤖 Paper[ia]: hold {d['max_min']:.0f}min "
                          f"en {row['symbol']}")
                    return
                # decidio vender (o fallback): cierre normal
                _close(conn, row, price,
                       "venta de la ⭐ (decisión IA)"
                       if d.get("decidido_por") == "ia_local"
                       else "venta de la ⭐", "🚪", firma=_firma)
                return
            except Exception as e:
                print(f"· Decisión IA falló ({e}); reglas de siempre")

        if perfil and perfil.get("clase") == "vende temprano":
            try:
                conn.execute("UPDATE paper_trades SET decidido_por='reglas' "
                             "WHERE id=? AND decidido_por IS NULL",
                             (row["id"],))
            except Exception:
                pass
            extra_min = _f(conn, "paper_hold_extra_min", 60.0)
            hasta = int(time.time() + extra_min * 60)
            conn.execute(
                "UPDATE paper_trades SET politica='holdear', "
                "precio_venta_lider=?, pico=?, hold_hasta=? WHERE id=?",
                (price, price, hasta, row["id"]))
            conn.commit()
            deriva = perfil.get("deriva_24h")
            if deriva is None:
                deriva = perfil.get("deriva_1h")
            _tg(f"🕐 *Hold extra* en {row['symbol']}: la ⭐ vendió, pero "
                f"su perfil dice que vende temprano "
                f"({deriva:+.0f}% de deriva tras sus ventas). "
                f"Mantengo hasta {extra_min:.0f} min con trailing stop.")
            print(f"🕐 Paper: hold extra en {row['symbol']} "
                  f"(deriva {deriva:+.0f}%)")
            return

    motivo = ("venta de la ⭐" if sigue_estrella
              else "venta (la billetera ya no es ⭐)")
    _close(conn, row, price, motivo, "🚪", firma=_firma)


def update_open_trades() -> int:
    """Job periódico: revisa TP / SL / tiempo de las posiciones abiertas.
    Devuelve cuántas cerró."""
    conn = get_conn()
    if not _enabled(conn):
        conn.close()
        return 0
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status='abierta' "
        "ORDER BY entry_ts").fetchall()
    if not rows:
        conn.close()
        return 0
    tp = _f(conn, "paper_tp_pct", 100.0)
    sl = -abs(_f(conn, "paper_sl_pct", 50.0))
    timeout = _f(conn, "paper_timeout_h", 48.0) * HOUR
    now = time.time()

    import config
    from signal_tracker import _price
    cerradas = 0
    for row in rows:
        price = _price(row["mint"])
        time.sleep(config.DEXSCREENER_DELAY)
        if not price:
            # Sin precio (¿par muerto?): si además venció el tiempo,
            # cerramos como pérdida total para no dejarla zombi.
            if now - row["entry_ts"] > timeout:
                _close(conn, row, row["entry_price"] * 0.01,
                       "sin liquidez", "💀")
                cerradas += 1
            continue
        pct = (price / row["entry_price"] - 1) * 100
        if pct >= tp:
            _close(conn, row, price, "take-profit", "🎯")
            cerradas += 1
        elif pct <= sl:
            _close(conn, row, price, "stop-loss", "🛑")
            cerradas += 1
        elif _campo(row, "politica") == "holdear":
            # Posicion en hold extra: la ⭐ ya vendio pero su perfil dice
            # "vende temprano". Trailing stop sobre el maximo alcanzado
            # desde su venta, y tope de tiempo. TP/SL de arriba siguen
            # mandando (por eso este bloque va despues).
            trail = abs(_f(conn, "paper_trail_pct", 15.0))
            pico = max(_campo(row, "pico") or price, price)
            if pico != _campo(row, "pico"):
                conn.execute("UPDATE paper_trades SET pico=? WHERE id=?",
                             (pico, row["id"]))
                conn.commit()
            caida = (price / pico - 1) * 100
            if caida <= -trail:
                _close(conn, row, price, "trailing del hold", "🪂")
                cerradas += 1
            elif now >= (_campo(row, "hold_hasta") or 0):
                _close(conn, row, price, "fin del hold extra", "🕐")
                cerradas += 1
        elif now - row["entry_ts"] > timeout:
            _close(conn, row, price, "tiempo", "⏰")
            cerradas += 1
    conn.close()
    # (Ola 12, restaurado 22/8) Vigilancia dev-sell de respaldo: ¿el
    # creador de algun token con posicion abierta vendio? Una alerta por
    # posicion, nunca cierra solo. (La via principal es en tiempo real.)
    try:
        from dev_watch import revisar_devs
        revisar_devs()
    except Exception as e:
        print(f"· dev_watch falló (no afecta al paper): {e}")
    return cerradas


# ───────────────────────── Resumen (/paper) ───────────────────────────────

def resumen_text() -> str:
    conn = get_conn()
    tp = _f(conn, "paper_tp_pct", 100.0)
    sl = abs(_f(conn, "paper_sl_pct", 50.0))
    timeout = _f(conn, "paper_timeout_h", 48.0)
    max_sol = _f(conn, "paper_max_sol", 1.0)
    estado = "🟢 activo" if _enabled(conn) else "🔴 apagado"

    abiertas = conn.execute(
        "SELECT * FROM paper_trades WHERE status='abierta' "
        "ORDER BY entry_ts DESC").fetchall()
    # wins por pnl_usd (Ola 4): pnl_sol ignoraba los parciales y una
    # operacion podia salir 🟢 en dolares pero contar como derrota.
    # pnl_sol solo decide cuando no hay cifra en dolares (filas viejas).
    cer = conn.execute(
        "SELECT COUNT(*) n, SUM(pnl_sol) pnl, SUM(pnl_usd) pnl_usd, "
        "SUM(stake_usd) invertido, "
        "SUM(CASE WHEN stake_usd IS NULL THEN 1 ELSE 0 END) sin_usd, "
        "SUM(CASE WHEN pnl_usd IS NULL AND pnl_sol IS NULL "
        "THEN 1 ELSE 0 END) sin_pnl, "
        "SUM(CASE WHEN pnl_usd IS NULL THEN 1 ELSE 0 END) sin_usd_pnl, "
        "SUM(CASE WHEN COALESCE(pnl_usd, pnl_sol) > 0 THEN 1 ELSE 0 END) "
        "wins FROM paper_trades WHERE status='cerrada'").fetchone()
    # Comparacion optimista vs REAL, solo sobre las cerradas que tienen
    # ambas cifras: la brecha entre las dos es el costo verdadero de
    # ejecutar (slippage + fees) y decide si el copy trading real da.
    real = conn.execute(
        "SELECT COUNT(*) n, SUM(pnl_usd) opt, SUM(pnl_usd_neto) neto, "
        "AVG(slippage_entrada_pct) slip "
        "FROM paper_trades WHERE status='cerrada' "
        "AND pnl_usd_neto IS NOT NULL AND pnl_usd IS NOT NULL").fetchone()
    demora = conn.execute(
        "SELECT AVG(demora_s) d, COUNT(demora_s) n FROM paper_trades "
        "WHERE demora_s IS NOT NULL").fetchone()
    ab = conn.execute(
        "SELECT gestion, COUNT(*) n, SUM(pnl_usd) pnl "
        "FROM paper_trades WHERE status<>'abierta' AND gestion IS NOT NULL "
        "GROUP BY gestion").fetchall()
    filtro = conn.execute(
        "SELECT ia_entrada, COUNT(*) n, SUM(pnl_usd) pnl "
        "FROM paper_trades WHERE status<>'abierta' "
        "AND ia_entrada IS NOT NULL GROUP BY ia_entrada").fetchall()
    por_motivo = conn.execute(
        "SELECT exit_reason r, COUNT(*) n, SUM(pnl_sol) pnl, "
        "SUM(pnl_usd) pnl_usd "
        "FROM paper_trades WHERE status='cerrada' "
        "GROUP BY exit_reason ORDER BY n DESC").fetchall()
    # Top vs consenso: consultado ANTES del close (la v1 de este bloque
    # quedo despues del conn.close() y el except se comio el error en
    # silencio — leccion repetida: los except anchos imprimen SIEMPRE).
    try:
        org = conn.execute(
            "SELECT COALESCE(origen,'top') o, COUNT(*) n, "
            "SUM(pnl_usd) pnl, "
            "SUM(CASE WHEN pnl_usd IS NOT NULL THEN 1 ELSE 0 END) n_dato, "
            "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) wins "
            "FROM paper_trades WHERE status<>'abierta' "
            "GROUP BY COALESCE(origen,'top')").fetchall()
    except Exception as e:
        print(f"· Resumen origen falló: {e}")
        org = []
    conn.close()

    out = [f"🧪 *Paper trading*  ·  {estado}",
           f"Config: tope {max_sol:g} SOL/señal · TP +{tp:.0f}% · "
           f"SL -{sl:.0f}% · máx {timeout:g}h",
           ""]
    n_c = cer["n"] or 0
    if n_c:
        pnl_t = cer["pnl"] or 0.0
        pnl_usd_t = cer["pnl_usd"]
        invertido = cer["invertido"]
        # (Ola 8, 21/8) Filas legadas sin NINGUN pnl no pueden "ganar":
        # dejarlas en el denominador hundia el win rate; y el PnL total
        # en $ las excluye del SUM sin decirlo — ahora ambas cosas se ven.
        sin_pnl = cer["sin_pnl"] or 0
        con_dato = n_c - sin_pnl
        wr = 100.0 * (cer["wins"] or 0) / con_dato if con_dato else 0.0
        res = "🟢" if (pnl_usd_t if pnl_usd_t is not None
                       else pnl_t) >= 0 else "🔴"
        total = (_usd_firmado(pnl_usd_t) if pnl_usd_t is not None
                 else f"{pnl_t:+.2f} SOL")
        wr_txt = (f"win rate {wr:.0f}% (de {con_dato} con dato)"
                  if sin_pnl else f"win rate {wr:.0f}%")
        nota_usd = ""
        if pnl_usd_t is not None and (cer["sin_usd_pnl"] or 0) > sin_pnl:
            nota_usd = f"  _($ sin dato en {cer['sin_usd_pnl']})_"
        out.append(f"{res} *Cerradas:* {n_c} · {wr_txt} · "
                   f"PnL total *{total}*{nota_usd}")
        if invertido:
            roi = 100.0 * (pnl_usd_t or 0) / invertido
            # Aviso honesto: si alguna cerrada no tiene importe en
            # dólares, el ROI está calculado sobre menos operaciones.
            faltan = cer["sin_usd"] or 0
            nota = f"  _(sin dato en {faltan})_" if faltan else ""
            out.append(f"   Invertido {_usd(invertido)} → "
                       f"ROI *{roi:+.1f}%*{nota}")
        for m in por_motivo:
            cifra = (_usd_firmado(m["pnl_usd"]) if m["pnl_usd"] is not None
                     else f"{(m['pnl'] or 0):+.2f} SOL")
            out.append(f"   · {m['r']}: {m['n']}  ({cifra})")
        if real and (real["n"] or 0) > 0:
            brecha = (real["opt"] or 0) - (real["neto"] or 0)
            out.append(
                f"⚖️ *Realidad vs papel* ({real['n']} con cotización "
                f"Jupiter):\n"
                f"   papel {_usd_firmado(real['opt'] or 0)} → "
                f"neto real *{_usd_firmado(real['neto'] or 0)}*  "
                f"(costo de ejecutar: {_usd(brecha)})"
                + (f"\n   slippage medio de entrada "
                   f"{real['slip']:.1f}%" if real["slip"] is not None
                   else ""))
        if demora and (demora["n"] or 0) > 0:
            out.append(f"⚡ Demora señal→copia: {demora['d']:.1f}s de media "
                       f"({demora['n']} medidas)")
        if ab and len(ab) > 1:
            # (Ola 8) NULL ya no se disfraza de "+$0.00": se dice "s/d".
            trozos = [f"{r['gestion']}: {r['n']} ops "
                      + (_usd_firmado(r["pnl"]) if r["pnl"] is not None
                         else "s/d") for r in ab]
            out.append("🤖 A/B de salidas · " + "  vs  ".join(trozos))
        # ¿El consenso gana o pierde frente a la copia clasica del top?
        # (19/8) La columna origen existe justamente para contestar esto.
        # (la consulta corre arriba, ANTES del conn.close()).
        try:
            omap = {r["o"]: r for r in org}
            if "consenso" in omap:
                trozos_o = []
                for o in ("top", "consenso"):
                    r = omap.get(o)
                    if r and r["n"]:
                        # (Ola 8) wr solo sobre filas con pnl (las legadas
                        # sin dato no pueden ganar) y NULL como s/d.
                        nd = r["n_dato"] or 0
                        wr_o = (f"wr {100 * (r['wins'] or 0) / nd:.0f}%"
                                + (f" de {nd}" if nd < r["n"] else "")
                                if nd else "wr s/d")
                        trozos_o.append(
                            f"{o}: {r['n']} ops "
                            + (_usd_firmado(r["pnl"])
                               if r["pnl"] is not None else "s/d")
                            + f" ({wr_o})")
                if trozos_o:
                    out.append("🤝 Origen · " + "  vs  ".join(trozos_o))
        except Exception as e:
            print(f"· Resumen origen (formato) falló: {e}")
        if filtro:
            fmap = {r["ia_entrada"]: r for r in filtro}
            rech = fmap.get("rechazar")
            cop = fmap.get("copiar")
            if rech or cop:
                linea = "🚪 Filtro de entrada IA · "
                if cop:
                    linea += (f"copiaría: {cop['n']} ("
                              + (_usd_firmado(cop["pnl"])
                                 if cop["pnl"] is not None else "s/d") + ")")
                if rech:
                    linea += (f"  ·  rechazaría: {rech['n']} ("
                              + (_usd_firmado(rech["pnl"])
                                 if rech["pnl"] is not None else "s/d") + ")")
                out.append(linea)
                if rech and (rech["pnl"] or 0) < 0:
                    out.append(f"   → rechazar habría ahorrado "
                               f"{_usd(-(rech['pnl'] or 0))}")
    else:
        out.append("Aún no hay operaciones cerradas.")
    out.append("")
    if abiertas:
        out.append(f"📂 *Abiertas ({len(abiertas)}):*")
        now = time.time()
        try:
            from card_image import _ago
        except Exception:
            # Pillow roto no puede tumbar /paper (politica del modulo).
            def _ago(hs):
                return (f"hace {hs:.1f}h" if hs < 24
                        else f"hace {hs / 24:.1f}d")
        for r in abiertas[:15]:
            hs = (now - r["entry_ts"]) / HOUR
            su = _campo(r, "stake_usd")
            monto = _usd(su) if su is not None else f"{r['stake_sol']:.2f} SOL"
            out.append(f"   · *{r['symbol']}* {monto} "
                       f"@ ${_precio(r['entry_price'])} · {_ago(hs)}")
    else:
        out.append("📂 Sin posiciones abiertas.")
    out.append("\nComandos: /paper on · /paper off · /paper max <SOL> · "
               "/paper reset")
    return "\n".join(out)


# ───────────────────────── Reinicio ───────────────────────────────────────

def contar() -> tuple[int, int]:
    """(abiertas, cerradas) — para avisar de lo que se va a borrar."""
    conn = get_conn()
    try:
        a = conn.execute("SELECT COUNT(*) c FROM paper_trades "
                         "WHERE status='abierta'").fetchone()["c"]
        c = conn.execute("SELECT COUNT(*) c FROM paper_trades "
                         "WHERE status='cerrada'").fetchone()["c"]
        return a, c
    except Exception:
        return 0, 0
    finally:
        conn.close()


def reset() -> tuple[int, int]:
    """Borra TODO el historial simulado y empieza de cero.

    Devuelve cuántas abiertas y cerradas había. Es irreversible: por eso
    el botón de Telegram pide confirmación antes de llamar aquí.

    También limpia las marcas 'mult_alert:*' de settings, que son los
    múltiplos ya avisados por token. Si no se borran, tras el reinicio un
    token que ya hizo x5 no volvería a avisar hasta x6.
    """
    abiertas, cerradas = contar()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM paper_trades")
        try:
            conn.execute("DELETE FROM paper_fills")
        except Exception as e:
            print(f"· Reset: no pude limpiar paper_fills: {e}")
        try:
            conn.execute("DELETE FROM settings WHERE key LIKE 'mult_alert:%'")
        except Exception as e:
            print(f"· Reset: no pude limpiar mult_alert: {e}")
        conn.commit()
    finally:
        conn.close()
    print(f"🧪 Paper trading reiniciado: {abiertas} abiertas y "
          f"{cerradas} cerradas borradas")
    return abiertas, cerradas
