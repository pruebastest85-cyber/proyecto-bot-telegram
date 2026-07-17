"""
Detección de billeteras hermanas: direcciones que probablemente
pertenecen al mismo dueño porque se transfieren SOL directamente.

v1: revisa el historial reciente de cada billetera ⭐ y busca
transferencias nativas de SOL entre billeteras rastreadas.
"""

import time

import requests

import config
from db import get_conn

MIN_SOL_LINK = 0.01   # ignorar polvo
TX_LIMIT = 100        # txs recientes a revisar por billetera


def _txs(address: str, limit: int = TX_LIMIT):
    url = config.HELIUS_PARSED_TX.format(address=address)
    r = requests.get(url, params={"api-key": config.HELIUS_API_KEY,
                                  "limit": limit}, timeout=30)
    r.raise_for_status()
    return r.json()


def find_links() -> str:
    """Busca transferencias directas entre billeteras ⭐ y devuelve
    un informe listo para Telegram."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT address, alias FROM wallets WHERE is_tracked=1").fetchall()
    conn.close()
    tracked = {r["address"]: (r["alias"] or r["address"][:8])
               for r in rows}
    if len(tracked) < 2:
        return "🔗 Hacen falta al menos 2 billeteras ⭐ para buscar vínculos."

    links = {}
    revisadas = 0
    for addr in tracked:
        try:
            txs = _txs(addr)
            revisadas += 1
        except Exception as e:
            print(f"· hermanas: fallo con {addr[:8]}: {e}")
            continue
        time.sleep(config.HELIUS_DELAY)
        for tx in txs or []:
            for nt in tx.get("nativeTransfers") or []:
                a = nt.get("fromUserAccount")
                b = nt.get("toUserAccount")
                if a in tracked and b in tracked and a != b:
                    sol = (nt.get("amount") or 0) / 1e9
                    if sol >= MIN_SOL_LINK:
                        k = tuple(sorted((a, b)))
                        links[k] = links.get(k, 0.0) + sol

    if not links:
        return (f"🔗 Revisé {revisadas} billeteras ⭐ y no encontré "
                "transferencias directas entre ellas.\n"
                "_Cada una parece ser de un dueño distinto (o usan "
                "intermediarios que aún no detecto)._")

    def _n(a):
        return str(tracked[a]).replace("*", "").replace("_", " ")

    out = ["🔗 *Posibles billeteras del mismo dueño:*\n"]
    for (a, b), sol in sorted(links.items(), key=lambda x: -x[1]):
        out.append(f"• *{_n(a)}* ↔ *{_n(b)}*: {sol:.2f} SOL entre ellas")
        out.append(f"  `{a[:10]}…` / `{b[:10]}…`")
    out.append("\n_Transferencias directas de SOL en su historial "
               f"reciente (últimas {TX_LIMIT} txs por billetera)._")
    return "\n".join(out)
"""
Detección de billeteras hermanas: direcciones que probablemente
pertenecen al mismo dueño porque se transfieren SOL directamente.

v1: revisa el historial reciente de cada billetera ⭐ y busca
transferencias nativas de SOL entre billeteras rastreadas.
"""

import time

import requests

import config
from db import get_conn

MIN_SOL_LINK = 0.01   # ignorar polvo
TX_LIMIT = 100        # txs recientes a revisar por billetera


def _txs(address: str, limit: int = TX_LIMIT):
    url = config.HELIUS_PARSED_TX.format(address=address)
    r = requests.get(url, params={"api-key": config.HELIUS_API_KEY,
                                  "limit": limit}, timeout=30)
    r.raise_for_status()
    return r.json()


def find_links() -> str:
    """Busca transferencias directas entre billeteras ⭐ y devuelve
    un informe listo para Telegram."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT address, alias FROM wallets WHERE is_tracked=1").fetchall()
    conn.close()
    tracked = {r["address"]: (r["alias"] or r["address"][:8])
               for r in rows}
    if len(tracked) < 2:
        return "🔗 Hacen falta al menos 2 billeteras ⭐ para buscar vínculos."

    links = {}
    revisadas = 0
    for addr in tracked:
        try:
            txs = _txs(addr)
            revisadas += 1
        except Exception as e:
            print(f"· hermanas: fallo con {addr[:8]}: {e}")
            continue
        time.sleep(config.HELIUS_DELAY)
        for tx in txs or []:
            for nt in tx.get("nativeTransfers") or []:
                a = nt.get("fromUserAccount")
                b = nt.get("toUserAccount")
                if a in tracked and b in tracked and a != b:
                    sol = (nt.get("amount") or 0) / 1e9
                    if sol >= MIN_SOL_LINK:
                        k = tuple(sorted((a, b)))
                        links[k] = links.get(k, 0.0) + sol

    if not links:
        return (f"🔗 Revisé {revisadas} billeteras ⭐ y no encontré "
                "transferencias directas entre ellas.\n"
                "_Cada una parece ser de un dueño distinto (o usan "
                "intermediarios que aún no detecto)._")

    def _n(a):
        return str(tracked[a]).replace("*", "").replace("_", " ")

    out = ["🔗 *Posibles billeteras del mismo dueño:*\n"]
    for (a, b), sol in sorted(links.items(), key=lambda x: -x[1]):
        out.append(f"• *{_n(a)}* ↔ *{_n(b)}*: {sol:.2f} SOL entre ellas")
        out.append(f"  `{a[:10]}…` / `{b[:10]}…`")
    out.append("\n_Transferencias directas de SOL en su historial "
               f"reciente (últimas {TX_LIMIT} txs por billetera)._")
    return "\n".join(out)
