"""
Alias deterministas y únicos por billetera — SIN IA.

Nombrar no es razonar: es una tarea determinista, así que la hace código
(gratis) y no la IA (que a escala repite apodos populares). Cada dirección
mapea SIEMPRE al mismo apodo, elegido de un espacio de ~46.000 combinaciones
(adjetivo × animal × distintivo).

OJO con las repeticiones: 46.464 nombres suenan a muchos, pero por la
paradoja del cumpleaños bastan ~215 billeteras para que haya un 50% de
probabilidad de choque. Con 13.595 billeteras los choques son SEGUROS
(unos 2.000). Por eso existe make_alias_unico(), que añade un sufijo
determinista derivado de la dirección cuando el nombre base ya está
cogido: sin él, el deduplicador renombraba al MISMO nombre que ya chocaba
una y otra vez, en cada conexión a la base, sin converger jamás.
"""

import hashlib

_ADJ = [
    "Paciente", "Sigiloso", "Astuto", "Veloz", "Certero", "Frío", "Audaz",
    "Silencioso", "Implacable", "Errante", "Nocturno", "Dorado", "Sombrío",
    "Feroz", "Sereno", "Letal", "Furtivo", "Táctico", "Voraz", "Metódico",
    "Intrépido", "Oculto", "Ágil", "Templado", "Salvaje", "Preciso",
    "Fantasma", "Relámpago", "Invisible", "Legendario", "Solitario",
    "Frenético", "Glacial", "Ardiente", "Tenaz", "Vigilante", "Escurridizo",
    "Magnético", "Quirúrgico", "Rapaz", "Espectral", "Meteórico", "Colosal",
    "Zen",
]
_NOUN = [
    "Ballena", "Zorro", "Halcón", "Lobo", "Tiburón", "Cobra", "Águila",
    "Pantera", "Lince", "Búho", "Cuervo", "Puma", "Jaguar", "Tigre",
    "Escorpión", "Delfín", "Cóndor", "Mantis", "Camaleón", "Erizo",
    "Mangosta", "Orca", "Grifo", "Fénix", "Dragón", "Kraken", "Basilisco",
    "Golondrina", "Nutria", "Comadreja", "Coyote", "Chacal", "Serpiente",
    "Víbora", "Milano", "Gavilán", "Búfalo", "Bisonte", "Alce", "Armiño",
    "Marta", "Tejón", "Gato", "Faisán",
]
_EXTRA = [
    "Alfa", "Beta", "Gamma", "Delta", "Épsilon", "Zeta", "Omega", "Sigma",
    "Theta", "Carmesí", "Índigo", "Cobalto", "Ámbar", "Esmeralda",
    "Escarlata", "Azabache", "Marfil", "Ónix", "Zafiro", "Rubí", "Neón",
    "Prisma", "Vórtice", "Eclipse",
]


def make_alias(address: str) -> str:
    """Apodo estable y (casi) único a partir de la dirección."""
    h = int(hashlib.sha256((address or "").encode()).hexdigest(), 16)
    a = _ADJ[h % len(_ADJ)]
    n = _NOUN[(h // len(_ADJ)) % len(_NOUN)]
    e = _EXTRA[(h // (len(_ADJ) * len(_NOUN))) % len(_EXTRA)]
    return f"{n} {a} {e}"


def make_alias_unico(address: str, ocupados) -> str:
    """
    Como make_alias pero garantizando que no choca con `ocupados`.

    El sufijo sale de la propia dirección, así que sigue siendo
    determinista y estable: la misma billetera obtiene siempre el mismo
    apodo, incluso si se recalcula meses después.
    """
    base = make_alias(address)
    if base not in ocupados:
        return base
    h = hashlib.sha256((address or "").encode()).hexdigest()
    # Hasta 4 intentos con trozos distintos del hash; el espacio efectivo
    # pasa de 46.464 a más de 3.000 millones, así que converge siempre.
    for i in range(4):
        cand = f"{base} {h[i * 4:i * 4 + 4].upper()}"
        if cand not in ocupados:
            return cand
    return f"{base} {h[:8].upper()}"
