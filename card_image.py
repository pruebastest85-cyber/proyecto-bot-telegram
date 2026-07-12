"""
Generador de tarjetas para las alertas de múltiplo (x2, x3, x4…).

Toma uno de los diseños (card_1.png … card_9.png), al azar de un grupo
configurable, y le encima el texto dinámico en el lado oscuro:
el x{múltiplo} en grande, el token, la subida y el precio.
Devuelve los bytes JPEG listos para enviar por Telegram (sendPhoto).

Si algo falla, el llamador debe caer de vuelta al mensaje de texto.
"""

import io
import os
import random

from PIL import Image, ImageDraw, ImageFont

_DIR = os.path.dirname(os.path.abspath(__file__))
_FONT = os.path.join(_DIR, "card_font.ttf")

# Todos los diseños disponibles (1..9). El grupo activo se puede acotar
# pasando pool=[...] (ej. [1, 5, 9]).
ALL_CARDS = list(range(1, 10))

# Colores
GREEN = (52, 224, 122)
WHITE = (240, 246, 250)
MUTED = (156, 176, 196)
SHADOW = (0, 0, 0)


def _font(size: int):
    try:
        return ImageFont.truetype(_FONT, size)
    except Exception:
        return ImageFont.load_default()


def _card_path(n: int) -> str:
    return os.path.join(_DIR, f"card_{n}.png")


def _rtext(draw, right, y, text, font, fill, shadow=True):
    """Texto alineado a la derecha, con una sombra sutil para legibilidad."""
    w = draw.textlength(text, font=font)
    x = right - w
    if shadow:
        draw.text((x + 2, y + 2), text, font=font, fill=SHADOW)
    draw.text((x, y), text, font=font, fill=fill)


def make_multiple_card(mult: int, symbol: str, pct: float, base: float,
                       price: float, alias: str, hace_h: float,
                       pool=None) -> bytes:
    """Genera la tarjeta y devuelve los bytes JPEG."""
    candidates = [n for n in (pool or ALL_CARDS) if os.path.exists(_card_path(n))]
    if not candidates:
        candidates = [n for n in ALL_CARDS if os.path.exists(_card_path(n))]
    n = random.choice(candidates)

    img = Image.open(_card_path(n)).convert("RGB")
    W = 1000
    H = int(round(img.height * W / img.width))
    img = img.resize((W, H), Image.LANCZOS)

    # Scrim oscuro en el lado derecho para que el texto siempre se lea
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    start = int(W * 0.40)
    for x in range(start, W):
        a = int(210 * (x - start) / (W - start))
        sd.line([(x, 0), (x, H)], fill=(6, 9, 15, min(210, a + 30)))
    img = Image.alpha_composite(img.convert("RGBA"), scrim).convert("RGB")

    d = ImageDraw.Draw(img)
    right = W - 46

    f_tag = _font(26)
    f_sym = _font(50)
    f_mult = _font(168)
    f_pct = _font(60)
    f_price = _font(32)
    f_small = _font(26)

    sym = (symbol or "?").upper()
    if len(sym) > 12:
        sym = sym[:12] + "…"

    y = int(H * 0.10)
    _rtext(d, right, y, "SMART MONEY", f_tag, MUTED); y += 40
    _rtext(d, right, y, sym, f_sym, WHITE); y += 66
    _rtext(d, right, y, f"x{mult}", f_mult, GREEN); y += int(f_mult.size * 0.98)
    _rtext(d, right, y, f"+{pct:.0f}%", f_pct, GREEN); y += 76
    _rtext(d, right, y, f"${base:.6g}  →  ${price:.6g}", f_price, WHITE); y += 46
    _rtext(d, right, y, f"{alias} · hace {hace_h:.1f}h", f_small, MUTED)

    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()
