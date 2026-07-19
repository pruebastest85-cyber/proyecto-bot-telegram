"""
Descubrimiento de clusters de co-compra — la ventaja competitiva.

En vez de analizar billeteras sueltas, busca REDES: grupos de
billeteras que compran temprano LOS MISMOS tokens ganadores una y otra
vez. Si A, B y C aparecen juntas en varios ganadores, cuando una compra
las otras suelen ir detrás → señal colectiva de alto valor.

Fuente: la tabla `appearances` (wallet, mint, buy_rank) que ya llena el
pipeline. No requiere APIs nuevas.

Método:
  1. Por cada token ganador, toma las billeteras que lo compraron
     TEMPRANO (buy_rank <= EARLY_RANK).
  2. Cuenta co-apariciones por par de billeteras.
  3. Une los pares fuertes (>= MIN_SHARED tokens en común) en clusters
     (union-find) y puntúa cada cluster.
"""

from collections import defaultdict

from db import get_conn

EARLY_RANK = 30       # "temprano" = entre los primeros 30 compradores
MIN_SHARED = 2        # nº mínimo de tokens ganadores compartidos por par


def _early_buyers_by_token(conn) -> dict[str, list[str]]:
    rows = conn.execute(
        """SELECT a.mint, a.wallet
           FROM appearances a
           JOIN wallets w ON w.address = a.wallet
           WHERE (a.buy_rank IS NULL OR a.buy_rank <= ?)
             AND COALESCE(w.is_bot, 0) = 0
           ORDER BY a.mint""",
        (EARLY_RANK,)).fetchall()
    by_token = defaultdict(set)
    for r in rows:
        by_token[r["mint"]].add(r["wallet"])
    return {m: list(ws) for m, ws in by_token.items() if len(ws) >= 2}


def find_clusters(min_shared: int = MIN_SHARED) -> list[dict]:
    """Devuelve clusters ordenados por fuerza (nº de miembros × tokens)."""
    conn = get_conn()
    try:
        by_token = _early_buyers_by_token(conn)
        # 1) co-apariciones por par + tokens compartidos
        pair_tokens = defaultdict(set)
        for mint, wallets in by_token.items():
            ws = sorted(wallets)
            for i in range(len(ws)):
                for j in range(i + 1, len(ws)):
                    pair_tokens[(ws[i], ws[j])].add(mint)

        # 2) union-find sobre pares fuertes
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        strong = {p: t for p, t in pair_tokens.items() if len(t) >= min_shared}
        for (a, b) in strong:
            union(a, b)

        # 3) agrupar y describir
        groups = defaultdict(set)
        group_tokens = defaultdict(set)
        for (a, b), toks in strong.items():
            root = find(a)
            groups[root].update((a, b))
            group_tokens[root].update(toks)

        # alias por billetera para mostrar
        alias = {r["address"]: r["alias"] for r in conn.execute(
            "SELECT address, alias FROM wallets WHERE alias IS NOT NULL"
        ).fetchall()}

        clusters = []
        for root, members in groups.items():
            toks = group_tokens[root]
            clusters.append({
                "members": sorted(members),
                "aliases": [alias.get(m) or m[:6] for m in sorted(members)],
                "size": len(members),
                "shared_tokens": len(toks),
                "strength": len(members) * len(toks),
            })
        clusters.sort(key=lambda c: c["strength"], reverse=True)
        return clusters
    finally:
        conn.close()


def clusters_text(limit: int = 6) -> str:
    clusters = find_clusters()
    if not clusters:
        return ("🕸 Aún no hay clusters: hacen falta más billeteras ⭐ que "
                "coincidan temprano en varios tokens ganadores. Corre más "
                "ciclos de descubrimiento y vuelve a intentar.")
    out = [f"🕸 *Clusters de co-compra* ({len(clusters)} detectados)\n"]
    for i, c in enumerate(clusters[:limit], 1):
        nombres = ", ".join(c["aliases"][:6])
        if c["size"] > 6:
            nombres += f" +{c['size'] - 6}"
        out.append(
            f"*{i}. Cluster de {c['size']} billeteras* · "
            f"{c['shared_tokens']} tokens en común\n{nombres}")
    out.append("\n_Billeteras que entran temprano juntas en los mismos "
               "ganadores. Cuando una compra, vigila a las demás._")
    return "\n".join(out)


def cluster_for(address: str) -> dict | None:
    """El cluster (si existe) al que pertenece una billetera."""
    for c in find_clusters():
        if address in c["members"]:
            return c
    return None
