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
    hs = hermanas(address)
    if hs:
        partes.append(f"⚠️ {len(hs)} billetera(s) del mismo origen")
    return "  ·  ".join(partes)
