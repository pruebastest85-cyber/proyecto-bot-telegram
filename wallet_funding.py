"""
Fuente de fondeo de una billetera (Wallet API de Helius).

Devuelve quién le envió su PRIMER SOL. Con eso se responden dos preguntas
que el embudo necesita y que hasta ahora solo se intuían:

  1. ¿Es una billetera RECIÉN CREADA para esta operación?
     Si se fondeó minutos antes de comprar, huele a bundle / wallet
     desechable, no a un trader con historial. Es justo el patrón que
     queremos fuera.

  2. ¿Hay varias billeteras del MISMO operador?
     Si comparten la dirección que las fondeó, son del mismo dueño. Eso
     evita contar como "consenso de varias Elite" lo que en realidad es
     una sola persona con cinco carteras.

El dato NUNCA cambia (el primer fondeo es histórico), así que se cachea
para siempre. Coste: 100 créditos por billetera, una única vez.
"""

import os
import time

import requests

from db import get_conn

API = "https://api.helius.xyz/v1/wallet/{addr}/funded-by"

# Por debajo de esto, la billetera se creó prácticamente para operar
HORAS_RECIEN_CREADA = 24


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS wallet_funding (
                      address TEXT PRIMARY KEY, funder TEXT, funder_nombre TEXT,
                      funder_tipo TEXT, monto REAL, ts_fondeo REAL,
                      consultado REAL)""")
    conn.commit()


def fondeo(address: str) -> dict | None:
    """{funder, nombre, tipo, monto, ts} o None. Cacheado permanentemente."""
    if not address:
        return None
    api_key = os.getenv("HELIUS_API_KEY", "")
    conn = get_conn()
    try:
        _ensure(conn)
        r = conn.execute(
            "SELECT funder, funder_nombre, funder_tipo, monto, ts_fondeo "
            "FROM wallet_funding WHERE address=?", (address,)).fetchone()
        if r:
            if not r["funder"]:
                return None          # ya consultada: sin datos de fondeo
            return {"funder": r["funder"], "nombre": r["funder_nombre"],
                    "tipo": r["funder_tipo"], "monto": r["monto"],
                    "ts": r["ts_fondeo"]}
        if not api_key:
            return None

        try:
            resp = requests.get(API.format(addr=address),
                                params={"api-key": api_key}, timeout=25)
            if resp.status_code == 404:
                conn.execute(
                    "INSERT OR IGNORE INTO wallet_funding "
                    "(address, consultado) VALUES (?,?)",
                    (address, time.time()))
                conn.commit()
                return None
            if resp.status_code == 403:
                print("  · Fuente de fondeo: requiere plan de pago")
                return None
            resp.raise_for_status()
            d = resp.json() or {}
        except requests.RequestException as e:
            print(f"  · Fondeo no disponible: {e}")
            return None

        out = {"funder": d.get("funder"), "nombre": d.get("funderName"),
               "tipo": d.get("funderType"), "monto": d.get("amount"),
               "ts": d.get("timestamp")}
        try:
            conn.execute(
                """INSERT OR IGNORE INTO wallet_funding
                   (address, funder, funder_nombre, funder_tipo, monto,
                    ts_fondeo, consultado) VALUES (?,?,?,?,?,?,?)""",
                (address, out["funder"], out["nombre"], out["tipo"],
                 out["monto"], out["ts"], time.time()))
            conn.commit()
        except Exception:
            pass
        return out if out["funder"] else None
    finally:
        conn.close()


def recien_creada(address: str, ts_referencia: float | None = None) -> tuple:
    """
    ¿Se fondeó justo antes de operar? Devuelve (es_reciente, horas, detalle).
    ts_referencia = momento de su compra; si falta, se compara con ahora.
    """
    f = fondeo(address)
    if not f or not f.get("ts"):
        return (False, None, None)
    ref = ts_referencia or time.time()
    horas = (float(ref) - float(f["ts"])) / 3600.0
    if horas < 0:
        return (False, None, None)
    if horas < HORAS_RECIEN_CREADA:
        origen = f.get("nombre") or (f.get("funder") or "")[:8]
        return (True, horas, f"fondeada {horas:.1f} h antes por {origen}")
    return (False, horas, None)


def hermanas(address: str, limite: int = 12) -> list[str]:
    """Otras billeteras de la base fondeadas por la MISMA dirección."""
    f = fondeo(address)
    if not f or not f.get("funder"):
        return []
    conn = get_conn()
    try:
        _ensure(conn)
        filas = conn.execute(
            "SELECT address FROM wallet_funding WHERE funder=? AND address<>? "
            "LIMIT ?", (f["funder"], address, limite)).fetchall()
        return [r["address"] for r in filas]
    except Exception:
        return []
    finally:
        conn.close()


def resumen(address: str) -> str | None:
    """Línea legible para el DNA o la ficha de una billetera."""
    f = fondeo(address)
    if not f:
        return None
    origen = f.get("nombre") or (f.get("funder") or "")[:8] + "…"
    tipo = f" · {f['tipo']}" if f.get("tipo") else ""
    partes = [f"💰 Fondeada por {origen}{tipo}"]
    if f.get("ts"):
        dias = (time.time() - float(f["ts"])) / 86400
        partes.append(f"hace {dias:.0f} días")
    # (Ola 8, 21/8) Antes esto usaba hermanas(), que NO filtra fondeadores
    # impersonales (hot-wallets de exchange, pump.fun) ni el tamaño del
    # grupo: media Solana sale de la hot-wallet de un exchange y aparecia
    # como "⚠️ mismo origen" insinuando mismo dueño. familia() existe
    # justo para eso; si el fondeador es un servicio, se dice sin alarma.
    fam = familia(address)
    if fam:
        partes.append(f"⚠️ {len(fam)} billetera(s) del mismo origen")
    elif _es_impersonal(f):
        partes.append("origen impersonal (exchange/servicio; "
                      "no implica mismo dueño)")
    return "  ·  ".join(partes)


# ── Familias: una sola ⭐ por dueño ────────────────────────────────────────
# Dos billeteras fondeadas por la MISMA dirección suelen ser del mismo
# dueño. Sin esto, una persona con 4 billeteras ocupa 4 estrellas y te
# manda 4 alertas del mismo movimiento: parece consenso y es una sola
# opinión. Medido en los datos: 3 de los 6 falsos positivos del modelo
# eran la misma estrategia repetida en billeteras distintas.
#
# DOS PROTECCIONES, porque "mismo fondeador" NO siempre significa hermanas:
#   1. Si el fondeador es un exchange/servicio, no son hermanas: media
#      Solana se fondea desde la cartera caliente de Binance.
#   2. Si un fondeador aparece en demasiadas billeteras, es un servicio
#      aunque no sepamos su nombre. Una persona no tiene 30 billeteras.
MAX_FAMILIA = int(os.getenv("MAX_FAMILIA", "20"))

_FUNDER_IMPERSONAL = (
    "exchange", "cex", "binance", "coinbase", "okx", "bybit", "kraken",
    "kucoin", "gate", "mexc", "bitget", "htx", "bridge", "wormhole",
    "protocol", "program", "validator", "treasury", "market maker",
    "jupiter", "raydium", "orca", "pump", "moonshot",
)


def _es_impersonal(f: dict) -> bool:
    txt = f"{f.get('tipo') or ''} {f.get('nombre') or ''}".lower()
    return any(k in txt for k in _FUNDER_IMPERSONAL)


def familia(address: str) -> list[str]:
    """
    Hermanas REALES de `address` (sin incluirla). Lista vacía si no se
    puede afirmar que compartan dueño.
    """
    f = fondeo(address)
    if not f or not f.get("funder") or _es_impersonal(f):
        return []
    conn = get_conn()
    try:
        _ensure(conn)
        filas = conn.execute(
            "SELECT address FROM wallet_funding WHERE funder=? AND address<>?",
            (f["funder"], address)).fetchall()
        otras = [r["address"] for r in filas]
        # Grupo demasiado grande = servicio, no una persona
        if len(otras) + 1 > MAX_FAMILIA:
            return []
        return otras
    except Exception:
        return []
    finally:
        conn.close()


def hermana_con_estrella(conn, address: str, mi_score) -> str | None:
    """
    ¿Hay ya una hermana con ⭐ igual o mejor que esta? Devuelve su
    dirección (para explicar el porqué) o None si esta puede llevarse la
    estrella de la familia.
    """
    hs = familia(address)
    if not hs:
        return None
    marcas = ",".join("?" * len(hs))
    try:
        filas = conn.execute(
            f"""SELECT address, COALESCE(wallet_score, -1) sc FROM wallets
                WHERE address IN ({marcas}) AND is_tracked = 1
                ORDER BY sc DESC""", hs).fetchall()
    except Exception:
        return None
    if not filas:
        return None
    mejor = filas[0]
    mio = mi_score if mi_score is not None else -1
    return mejor["address"] if (mejor["sc"] or -1) >= mio else None


def destronar_hermanas(conn, address: str) -> list[str]:
    """
    Esta billetera es la mejor de su familia: quita la ⭐ a las hermanas.
    Devuelve a quiénes se la quitó.
    """
    hs = familia(address)
    if not hs:
        return []
    marcas = ",".join("?" * len(hs))
    try:
        quitadas = [r["address"] for r in conn.execute(
            "SELECT address FROM wallets WHERE address IN (%s) "
            "AND is_tracked = 1" % marcas, hs).fetchall()]
        if not quitadas:
            return []
        motivo = (" · 🔗 sin ⭐: su hermana %s… representa a la familia"
                  % address[:8])
        for a in quitadas:
            conn.execute(
                """UPDATE wallets
                   SET is_tracked = 0, ai_follow = 0, confirmada = 0,
                       prueba_desde = NULL, turno_desde = NULL,
                       ai_reason = SUBSTR(COALESCE(ai_reason,'') || ?, 1, 500)
                   WHERE address = ?""", (motivo, a))
        conn.commit()
        return quitadas
    except Exception as e:
        print(f"· destronar hermanas falló: {e}")
        return []
