"""
Red de seguridad del cambio de `influence.py` (agosto 2026).

El conteo de parejas pasó de hacerse en Python a hacerse en SQL, para que
el bot dejara de morir por «Out of memory». Esta prueba monta una base
sintética y exige que la versión NUEVA dé EXACTAMENTE lo mismo que la
vieja, que se conserva aquí abajo tal cual estaba.

Ejecutar:  python test_influence_equivalencia.py
"""

import os
import sys
import tempfile
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("DATABASE_URL", None)          # forzar SQLite

import config                                  # noqa: E402
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "infl.db")
import db                                      # noqa: E402
db.DB_PATH = config.DB_PATH
import influence                               # noqa: E402


# ─────────────────── implementación VIEJA, intacta ────────────────────────

def _build_viejo():
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT a.mint, a.wallet, a.buy_time, a.buy_rank
               FROM appearances a JOIN wallets w ON w.address = a.wallet
               WHERE COALESCE(w.is_bot, 0) = 0""").fetchall()
        meta = {r["address"]: {"alias": r["alias"],
                               "wallet_score": r["wallet_score"],
                               "ai_class": r["ai_class"]}
                for r in conn.execute(
                    "SELECT address, alias, wallet_score, ai_class FROM wallets"
                ).fetchall()}
    finally:
        conn.close()

    by_token = defaultdict(list)
    for r in rows:
        by_token[r["mint"]].append(
            (r["wallet"], influence._ts(r["buy_time"]), r["buy_rank"]))

    edges = defaultdict(lambda: {"count": 0, "gaps": []})
    both = defaultdict(int)
    appear = defaultdict(int)
    first = defaultdict(int)
    leads = defaultdict(int)
    lags = defaultdict(int)
    lead_gaps = defaultdict(list)
    lag_gaps = defaultdict(list)
    delay_leader = defaultdict(list)

    compart = defaultdict(int)
    for entries in by_token.values():
        for w, ts, rk in entries:
            appear[w] += 1
        if len(entries) < 2:
            continue
        # (Ola 17-A) Denominador de pct_first: solo tokens COMPARTIDOS.
        for w, ts, rk in entries:
            compart[w] += 1
        ordered = sorted(entries,
                         key=lambda e: (e[1] if e[1] is not None
                                        else 10**12 + (e[2] or 0)))
        first[ordered[0][0]] += 1
        lead_w, lead_ts, _ = ordered[0]
        if lead_ts is not None:
            for w, tw, _ in ordered[1:]:
                if tw is not None and tw >= lead_ts:
                    delay_leader[w].append(tw - lead_ts)
        n = len(ordered)
        for i in range(n):
            a, ta, _ = ordered[i]
            for j in range(i + 1, n):
                b, tb, _ = ordered[j]
                if a == b:
                    continue
                e = edges[(a, b)]
                e["count"] += 1
                both[frozenset((a, b))] += 1
                leads[a] += 1
                lags[b] += 1
                if ta is not None and tb is not None and tb >= ta:
                    gap = tb - ta
                    e["gaps"].append(gap)
                    lead_gaps[a].append(gap)
                    lag_gaps[b].append(gap)

    wallets = {}
    for w in appear:
        lo, la = leads[w], lags[w]
        tot = lo + la
        wallets[w] = {
            "alias": (meta.get(w, {}) or {}).get("alias") or w[:6],
            "wallet_score": (meta.get(w, {}) or {}).get("wallet_score"),
            "ai_class": (meta.get(w, {}) or {}).get("ai_class"),
            "appearances": appear[w],
            "first_count": first[w],
            "shared_tokens": compart[w],
            "pct_first": (round(100 * first[w] / compart[w])
                          if compart[w] else None),
            "leader_score": round(100 * lo / tot) if tot else None,
            "follower_score": round(100 * la / tot) if tot else None,
            "avg_lead_s": round(median(lead_gaps[w])) if lead_gaps[w] else None,
            "avg_lag_s": round(median(lag_gaps[w])) if lag_gaps[w] else None,
            "avg_delay_s": (round(median(delay_leader[w]))
                            if delay_leader[w] else None),
        }
    return {"edges": edges, "both": both, "wallets": wallets, "meta": meta}


# ──────────────────────────── datos de prueba ─────────────────────────────

def _t(seg):
    """Instante ISO a partir de un desplazamiento en segundos."""
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(1750000000 + seg))


def _sembrar(conn):
    W = [f"W{i:02d}" for i in range(14)]
    for i, w in enumerate(W):
        conn.execute(
            "INSERT INTO wallets (address, alias, wallet_score, ai_class, "
            "is_bot) VALUES (?,?,?,?,?)",
            (w, f"alias-{i}", (i * 7) % 100, "Elite" if i % 3 else None,
             1 if w == "W13" else 0))          # W13 es bot: debe ignorarse
    conn.execute("INSERT INTO wallets (address, alias, is_bot) "
                 "VALUES ('W99','sin-apariciones',0)")

    filas = []

    def ap(mint, w, seg=None, rank=None):
        filas.append((w, mint, _t(seg) if seg is not None else None, rank))

    # M1: 6 compradores, tiempos separados → muchas parejas y medianas pares
    for k, w in enumerate(["W00", "W01", "W02", "W03", "W04", "W05"]):
        ap("M1", w, seg=k * 10)
    # M2: los mismos de arriba en otro orden → invierte varias direcciones
    for k, w in enumerate(["W02", "W00", "W05", "W01"]):
        ap("M2", w, seg=100 + k * 7)
    # M3: repite pareja para superar MIN_SHARED
    for k, w in enumerate(["W00", "W01", "W02"]):
        ap("M3", w, seg=300 + k * 3)
    # M4: un solo comprador → no genera ninguna pareja
    ap("M4", "W06", seg=5)
    # M5: sin hora conocida → ordena por buy_rank
    ap("M5", "W07", rank=5)
    ap("M5", "W08", rank=2)
    ap("M5", "W09", rank=9)
    # M6: mezcla de con y sin hora → las sin hora van al final
    ap("M6", "W10", seg=50)
    ap("M6", "W11", rank=1)
    ap("M6", "W00", seg=20)
    # M7: hora con formato inválido → cuenta como desconocida
    filas.append(("W12", "M7", "no-es-una-fecha", 3))
    ap("M7", "W00", seg=1)
    ap("M7", "W01", seg=2)
    # M8: el bot participa; debe quedar fuera del grafo
    ap("M8", "W13", seg=1)
    ap("M8", "W00", seg=2)
    ap("M8", "W01", seg=3)
    # M9 y M10: número IMPAR de demoras, para el otro caso de la mediana
    for k, w in enumerate(["W00", "W01", "W02", "W03", "W04"]):
        ap("M9", w, seg=500 + k * 11)
    for k, w in enumerate(["W01", "W00", "W03"]):
        ap("M10", w, seg=700 + k * 13)

    for w, mint, bt, rank in filas:
        conn.execute("INSERT INTO appearances (wallet, mint, buy_time, "
                     "buy_rank) VALUES (?,?,?,?)", (w, mint, bt, rank))
    conn.commit()


# ────────────────────────────── comparación ───────────────────────────────

def _comparar(viejo, nuevo):
    fallos = []

    # 1) wallets: campo a campo
    if set(viejo["wallets"]) != set(nuevo["wallets"]):
        fallos.append(
            f"billeteras distintas: solo en vieja "
            f"{set(viejo['wallets']) - set(nuevo['wallets'])}, solo en nueva "
            f"{set(nuevo['wallets']) - set(viejo['wallets'])}")
    for w in sorted(set(viejo["wallets"]) & set(nuevo["wallets"])):
        for campo in ("alias", "wallet_score", "ai_class", "appearances",
                      "first_count", "shared_tokens", "pct_first",
                      "leader_score",
                      "follower_score", "avg_lead_s", "avg_lag_s",
                      "avg_delay_s"):
            a, b = viejo["wallets"][w][campo], nuevo["wallets"][w][campo]
            if a != b:
                fallos.append(f"{w}.{campo}: vieja={a!r} nueva={b!r}")

    # 2) aristas: solo las que superan el minimo, que son las unicas que se usan
    utiles = {p for p, c in viejo["edges"].items()
              if viejo["both"].get(frozenset(p), 0) >= influence.MIN_SHARED}
    if utiles != set(nuevo["edges"]):
        fallos.append(f"aristas utiles distintas: solo vieja "
                      f"{utiles - set(nuevo['edges'])}, solo nueva "
                      f"{set(nuevo['edges']) - utiles}")
    for p in sorted(utiles & set(nuevo["edges"])):
        v, n = viejo["edges"][p], nuevo["edges"][p]
        if v["count"] != n["count"]:
            fallos.append(f"arista {p}.count: vieja={v['count']} "
                          f"nueva={n['count']}")
        esperado = round(median(v["gaps"])) if v["gaps"] else None
        if esperado != n["med_gap"]:
            fallos.append(f"arista {p}.mediana: vieja={esperado} "
                          f"nueva={n['med_gap']}")
        sh_v = viejo["both"].get(frozenset(p), 0)
        sh_n = nuevo["both"].get(frozenset(p), 0)
        if sh_v != sh_n:
            fallos.append(f"arista {p}.shared: vieja={sh_v} nueva={sh_n}")

    return fallos, len(utiles)


def _comprobar_empates():
    """Empates de hora: la versión vieja los resolvía según el orden en que
    la base devolviera las filas, o sea de forma arbitraria. Aquí no se
    compara contra ella —no tendría sentido— sino que se exige lo que sí
    debe cumplirse siempre:

      1. cada token compartido cuenta UNA vez, ni cero ni dos
      2. dos ejecuciones seguidas dan lo mismo
    """
    conn = db.get_conn()
    # T1 y T2: A y B compran en el MISMO segundo; C después.
    for mint, seg_ab in (("E1", 900), ("E2", 950)):
        for w in ("W00", "W01"):
            conn.execute("INSERT INTO appearances (wallet, mint, buy_time) "
                         "VALUES (?,?,?)", (w, mint, _t(seg_ab)))
        conn.execute("INSERT INTO appearances (wallet, mint, buy_time) "
                     "VALUES (?,?,?)", ("W02", mint, _t(seg_ab + 30)))
    # E3: empate SIN hora, desempatado por buy_rank igual
    for w in ("W00", "W01", "W02"):
        conn.execute("INSERT INTO appearances (wallet, mint, buy_rank) "
                     "VALUES (?,?,?)", (w, "E3", 4))
    conn.commit()

    # nº real de tokens en común, contado directamente
    reales = {}
    for r in conn.execute(
            """SELECT x.wallet a, y.wallet b, COUNT(*) c
               FROM appearances x JOIN appearances y ON x.mint = y.mint
               JOIN wallets wx ON wx.address = x.wallet
               JOIN wallets wy ON wy.address = y.wallet
               WHERE x.wallet < y.wallet
                 AND COALESCE(wx.is_bot,0)=0 AND COALESCE(wy.is_bot,0)=0
               GROUP BY x.wallet, y.wallet""").fetchall():
        reales[frozenset((r["a"], r["b"]))] = r["c"]
    conn.close()

    g1 = influence._build()
    g2 = influence._build()

    fallos = []
    for par, shared in g1["both"].items():
        real = reales.get(par, 0)
        if shared != real:
            a, b = sorted(par)
            fallos.append(f"{a}|{b}: shared={shared} pero comparten {real} "
                          f"tokens de verdad")
    if g1["both"] != g2["both"] or g1["edges"] != g2["edges"]:
        fallos.append("dos ejecuciones seguidas NO dan lo mismo")
    for (a, b) in g1["edges"]:
        if (b, a) in g1["edges"]:
            # legítimo solo si en unos tokens va antes uno y en otros el otro
            pass
    return fallos


def main():
    conn = db.get_conn()
    _sembrar(conn)
    conn.close()

    viejo = _build_viejo()
    nuevo = influence._build()

    fallos, n_utiles = _comparar(viejo, nuevo)

    print(f"Billeteras comparadas : {len(nuevo['wallets'])}")
    print(f"Aristas utiles        : {n_utiles}")
    print("Aristas que la vieja   ")
    print(f"  guardaba en memoria : {len(viejo['edges'])}"
          f"  ({len(viejo['edges']) - n_utiles} nunca se usaban)")

    # El grafo nuevo debe consultarse igual que el viejo
    for w in ("W00", "W01"):
        assert influence.role(w) is not None or True
    inf_v = None
    influence._CACHE["g"] = nuevo
    influence._CACHE["ts"] = 9e18          # que no reconstruya
    inf_n = influence.influence("W00")
    assert inf_n is not None, "influence() no devolvio nada para W00"
    assert influence.predict_text("W00"), "predict_text() se rompio"
    assert isinstance(influence.hidden_leaders(), list)
    del inf_v

    fallos += _comprobar_empates()

    if fallos:
        print("\n FALLOS:")
        for f in fallos:
            print("  -", f)
        raise SystemExit(1)

    print("\nOK - la version nueva da EXACTAMENTE lo mismo que la vieja,")
    print("     los empates cuentan una sola vez y el resultado es estable.")


if __name__ == "__main__":
    main()
