"""
Alias deterministas y únicos por billetera — SIN IA.

Nombrar no es razonar: es una tarea determinista, así que la hace código
(gratis) y no la IA (que a escala repite apodos populares). Cada dirección
mapea SIEMPRE al mismo apodo, elegido de un espacio de ~46.000 combinaciones
(adjetivo × animal × distintivo), así que las repeticiones son prácticamente
imposibles y el nombre es estable en el tiempo.
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
    "Marta", "Tejón", "Gato", "Águila",
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
