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
    aprueba = not (t.get("mint_auth") or t.get("freeze_auth")
                   or len(riesgos) >= 3)
    linea = ("🟢 seguridad ok" if not problemas
             else ("🟡 " if aprueba else "🔴 ") + " · ".join(problemas))
    return aprueba, linea


def escanear() -> int:
    """Una pasada del radar. Devuelve cuántas alertas mandó."""
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
    alertas = 0
    try:
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
            if not aprueba:
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
                    "UPDATE radar_tokens SET resultado=? WHERE mint=?",
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
            try:
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
                "UPDATE radar_tokens SET resultado=? WHERE mint=?",
                (f"alertado:{len(conocidas)}", c["mint"]))
            conn.commit()
            alertas += 1
    finally:
        conn.close()
    if alertas:
        print(f"📡 Radar: {alertas} alertas de smart money temprana")
    return alertas


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
        alertados = conn.execute(
            "SELECT mint, symbol, resultado, ts FROM radar_tokens "
            "WHERE ts >= ? AND resultado LIKE 'alertado%' "
            "ORDER BY ts DESC LIMIT 10", (corte,)).fetchall()
    finally:
        conn.close()
    if not ACTIVO:
        return "📡 El radar está APAGADO (RADAR_ACTIVO=0)."
    out = ["📡 *Radar de pares recién nacidos* (24 h)\n",
           f"Tokens examinados: {tot} · descartados por seguridad: {seg}"]
    if alertados:
        out.append("\n🎯 Con smart money de tu base:")
        for r in alertados:
            n = r["resultado"].split(":")[-1]
            hace = (time.time() - r["ts"]) / 3600
            out.append(f"  • {r['symbol']} · {n} conocida(s) · "
                       f"hace {hace:.1f}h\n    `{r['mint']}`")
    else:
        out.append("\nSin coincidencias con tu base todavía — el radar "
                   "solo alerta cuando billeteras conocidas entran a un "
                   "token nuevo, no por 'pinta prometedora'.")
    return "\n".join(out)
