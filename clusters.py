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

# (Ola 17-A) Mínimo de comparaciones para poder decir quién lidera.
# Sin esto, una billetera que coincidió con otra en UN token y compró
# antes salía con "100% adelanta" y se coronaba 👑 por delante de una
# con 95% sobre 400 comparaciones — y el bot llegaba a decir "vigila
# esta billetera para adelantarte" a partir de una sola coincidencia.
MIN_COMPS_LIDER = 6


def _early_data(conn):
    """Devuelve (by_token: {mint:set(wallets)}, ranks: {(mint,wallet):rank})."""
    # Rank NULL EXCLUIDO (Ola 5, auditoria 19/8 - C16): wallet_analyzer
    # escribe rank_real=None a proposito cuando la historia del token
    # esta incompleta ("mejor sin dato que con un dato falso") — y aqui
    # ese NULL pasaba el filtro de "comprador temprano", asi que TODOS
    # los compradores de esos tokens contaban como tempranos: clusters
    # inventados de co-compradores tardios. Sin dato no es evidencia.
    rows = conn.execute(
        """SELECT a.mint, a.wallet, a.buy_rank
           FROM appearances a
           JOIN wallets w ON w.address = a.wallet
           WHERE a.buy_rank <= ?
             AND COALESCE(w.is_bot, 0) = 0
           ORDER BY a.mint""",
        (EARLY_RANK,)).fetchall()
    by_token = defaultdict(set)
    ranks = {}
    for r in rows:
        by_token[r["mint"]].add(r["wallet"])
        if r["buy_rank"] is not None:
            ranks[(r["mint"], r["wallet"])] = r["buy_rank"]
    by_token = {m: ws for m, ws in by_token.items() if len(ws) >= 2}
    return by_token, ranks


def _leadership(members, shared_tokens, ranks):
    """Ordena a los miembros por quién compra ANTES (líder → seguidores).

    Para cada token compartido compara los buy_rank de los miembros
    presentes: quien tiene menor rank compró primero. Acumula:
      - lead_pct: % de comparaciones en que el miembro fue el más temprano,
      - avg_rank: puesto medio de compra,
      - follows: a quién sigue más a menudo (quién le precede).
    """
    leads = {m: 0 for m in members}
    comps = {m: 0 for m in members}
    rank_sum = {m: 0.0 for m in members}
    appears = {m: 0 for m in members}
    precede = {m: defaultdict(int) for m in members}
    for t in shared_tokens:
        present = [(m, ranks.get((t, m))) for m in members
                   if ranks.get((t, m)) is not None]
        for m, r in present:
            rank_sum[m] += r
            appears[m] += 1
        for a, ra in present:
            for b, rb in present:
                if a == b:
                    continue
                comps[a] += 1
                if ra < rb:
                    leads[a] += 1
                elif rb < ra:
                    precede[a][b] += 1
    order = []
    for m in members:
        # Muestra insuficiente = NO se sabe, no "100%".
        lead_pct = (round(100 * leads[m] / comps[m])
                    if comps[m] >= MIN_COMPS_LIDER else None)
        avg_rank = round(rank_sum[m] / appears[m], 1) if appears[m] else None
        follows_w = (max(precede[m].items(), key=lambda kv: kv[1])[0]
                     if precede[m] else None)
        order.append({"wallet": m, "lead_pct": lead_pct, "comps": comps[m],
                      "avg_rank": avg_rank, "appears": appears[m],
                      "follows": follows_w})
    # líder primero: mayor lead_pct, luego menor rank medio
    order.sort(key=lambda o: (-(o["lead_pct"] or -1),
                              o["avg_rank"] if o["avg_rank"] is not None else 1e9))
    return order


def find_clusters(min_shared: int = MIN_SHARED) -> list[dict]:
    """Devuelve clusters ordenados por fuerza (nº de miembros × tokens)."""
    conn = get_conn()
    try:
        by_token, ranks = _early_data(conn)
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

        def nm(w):
            return alias.get(w) or w[:6]

        clusters = []
        for root, members in groups.items():
            toks = group_tokens[root]
            order = _leadership(members, toks, ranks)
            for o in order:
                o["alias"] = nm(o["wallet"])
                o["follows_alias"] = nm(o["follows"]) if o["follows"] else None
            clusters.append({
                "members": sorted(members),
                "aliases": [nm(m) for m in sorted(members)],
                "size": len(members),
                "shared_tokens": len(toks),
                "strength": len(members) * len(toks),
                "order": order,
                # (Ola 17-A) Solo hay 👑 si el primero tiene muestra
                # suficiente; si no, el cluster existe pero SIN líder
                # declarado, que es la verdad.
                "leader": (order[0]["alias"] if order
                           and order[0]["lead_pct"] is not None else None),
                "leader_wallet": (order[0]["wallet"] if order
                                  and order[0]["lead_pct"] is not None
                                  else None),
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
        out.append(f"*{i}. Cluster de {c['size']} billeteras* · "
                   f"{c['shared_tokens']} tokens en común")
        if c.get("leader"):
            _o0 = c["order"][0]
            out.append(f"   👑 Líder: *{c['leader']}* (compra primero — "
                       f"{_o0['lead_pct']}% sobre {_o0['comps']} comparaciones)")
        else:
            out.append(f"   ⚪ Sin líder claro todavía (hacen falta "
                       f"{MIN_COMPS_LIDER}+ comparaciones por billetera)")
        seguidores = [o for o in c.get("order", [])
                      if o["wallet"] != c.get("leader_wallet")][:5]
        for o in seguidores:
            lp = (f"{o['lead_pct']}% adelanta ({o['comps']} comp.)"
                  if o["lead_pct"] is not None else "adelanto s/d")
            sig = f" · sigue a {o['follows_alias']}" if o.get("follows_alias") else ""
            out.append(f"   • {o['alias']} (rank medio {o['avg_rank']}{('; ' + lp) if lp else ''}{sig})")
        out.append("")
    out.append("_Cuando el 👑 líder compra, los seguidores suelen entrar "
               "detrás. Ésa es tu ventana._")
    return "\n".join(out)


def cluster_for(address: str) -> dict | None:
    """El cluster (si existe) al que pertenece una billetera."""
    for c in find_clusters():
        if address in c["members"]:
            return c
    return None
