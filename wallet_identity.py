"""
Identidad de billeteras (Wallet API de Helius).

Helius mantiene 32.500 cuentas etiquetadas y millones de etiquetas
categóricas: exchanges, protocolos, market makers, validadores, tesorerías,
puentes… y también estafadores conocidos.

POR QUÉ IMPORTA AQUÍ
--------------------
El embudo busca TRADERS REALES. Sin esto, la cartera caliente de un exchange
o la bóveda de un protocolo pueden colarse como "billetera muy rentable":
mueven muchísimo dinero y su PnL aparente es enorme, pero no son alguien a
quien copiar. Consultar la identidad ANTES de perfilar evita ese ruido y
además ahorra créditos (no se gasta historial en una cuenta de Binance).

Coste: 100 créditos por lote de hasta 100 direcciones = 1 crédito por
billetera. El dato apenas cambia, así que se guarda en caché para siempre.
"""

import json
import os
import time

import requests

from db import get_conn
from avisos import aviso as _avisar_ex   # (19-AE)

API = "https://api.helius.xyz/v1/wallet/batch-identity"

# Categorías que NO son traders individuales: infraestructura, empresas,
# programas y cuentas de servicio. Nada de esto es replicable por una persona.
NO_TRADER = {
    "Centralized Exchange", "Cross-chain Bridge", "DeFi", "Market Maker",
    "Trading Firm", "Validator", "Treasury", "DAO", "Stake Pool", "Multisig",
    "Oracle", "Payments", "Governance", "Authority", "Jito",
    "Transaction Sending", "System", "Fees", "Vault", "Restaking",
    "Non-Circulating Supply", "Genesis Block Distribution", "Airdrop",
    "Fundraise", "Proprietary AMM", "DePIN", "Casino & Gambling",
}

# Categorías directamente peligrosas: además de excluir, conviene avisar.
PELIGROSAS = {
    "Exploiter, Hackers & Scams", "Hacker", "Rugger", "Scammer", "Spam",
}


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS wallet_identity (
                      address TEXT PRIMARY KEY, tipo TEXT, nombre TEXT,
                      categoria TEXT, etiquetas TEXT, ts REAL)""")
    conn.commit()


def _cacheadas(conn, direcciones):
    """Lo que ya sabemos, para no volver a preguntarlo."""
    out = {}
    if not direcciones:
        return out
    try:
        for a in direcciones:
            r = conn.execute(
                "SELECT tipo, nombre, categoria, etiquetas FROM "
                "wallet_identity WHERE address=?", (a,)).fetchone()
            if r:
                out[a] = {"tipo": r["tipo"], "nombre": r["nombre"],
                          "categoria": r["categoria"],
                          "etiquetas": json.loads(r["etiquetas"] or "[]")}
    except Exception as _ex:
        _avisar_ex("wallet_identity:_cacheadas:68", _ex)
        pass
    return out


def _guardar(conn, addr, d):
    try:
        conn.execute(
            """INSERT OR IGNORE INTO wallet_identity
               (address, tipo, nombre, categoria, etiquetas, ts)
               VALUES (?,?,?,?,?,?)""",
            (addr, d.get("tipo"), d.get("nombre"), d.get("categoria"),
             json.dumps(d.get("etiquetas") or []), time.time()))
        conn.commit()
    except Exception as e:
        # (19-AA, auditoria M17) Igual que en wallet_funding: sin la
        # cache se vuelve a pagar la consulta, y antes no se decia.
        print(f"· Identidad: no pude guardar la caché de "
              f"{str(addr)[:8]} ({e}); se volverá a consultar")


def identificar(direcciones: list[str]) -> dict:
    """
    Devuelve {direccion: {tipo, nombre, categoria, etiquetas}} para las que
    Helius conoce. Las desconocidas quedan con categoria=None (y también se
    cachean, para no volver a preguntar por ellas).
    """
    direcciones = [a for a in dict.fromkeys(direcciones or []) if a]
    if not direcciones:
        return {}
    api_key = os.getenv("HELIUS_API_KEY", "")
    if not api_key:
        return {}

    conn = get_conn()
    try:
        _ensure(conn)
        conocidas = _cacheadas(conn, direcciones)
        faltan = [a for a in direcciones if a not in conocidas]

        for i in range(0, len(faltan), 100):     # el lote admite 100
            trozo = faltan[i:i + 100]
            try:
                r = requests.post(API, params={"api-key": api_key},
                                  json={"addresses": trozo}, timeout=30)
                if r.status_code == 403:
                    print("  · Identidad de billeteras: requiere plan de pago")
                    return conocidas
                r.raise_for_status()
                filas = r.json() or []
            except requests.RequestException as e:
                print(f"  · Identidad no disponible: {e}")
                break

            vistas = set()
            for f in filas:
                a = f.get("address")
                if not a:
                    continue
                d = {"tipo": f.get("type"), "nombre": f.get("name"),
                     "categoria": f.get("category"),
                     "etiquetas": f.get("tags") or []}
                conocidas[a] = d
                _guardar(conn, a, d)
                vistas.add(a)
            # Las que Helius no conoce: se cachean como desconocidas
            for a in trozo:
                if a not in vistas:
                    d = {"tipo": None, "nombre": None, "categoria": None,
                         "etiquetas": []}
                    conocidas[a] = d
                    _guardar(conn, a, d)
        return conocidas
    finally:
        conn.close()


def motivo_exclusion(ident: dict) -> str | None:
    """Si esta identidad NO es un trader replicable, devuelve el porqué."""
    if not ident:
        return None
    cat = ident.get("categoria")
    if not cat:
        return None
    nombre = ident.get("nombre") or cat
    if cat in PELIGROSAS:
        return f"marcada como {cat} ({nombre})"
    if cat in NO_TRADER:
        return f"no es un trader: {cat} ({nombre})"
    return None


def etiqueta(address: str) -> str | None:
    """Nombre conocido de una dirección, si Helius lo tiene (para mostrarlo)."""
    try:
        conn = get_conn()
        try:
            _ensure(conn)
            r = conn.execute("SELECT nombre, categoria FROM wallet_identity "
                             "WHERE address=?", (address,)).fetchone()
            if r and r["nombre"]:
                return f"{r['nombre']} ({r['categoria']})"
        finally:
            conn.close()
    except Exception as _ex:
        _avisar_ex("wallet_identity:etiqueta:172", _ex)
        pass
    return None
