"""
brat_service.py — Génération de stickers "brat" (esthétique Charli XCX).

Style : fond BLANC pur, texte noir tout en minuscules, police condensée
(Arial Narrow — la VRAIE police du générateur officiel, embarquée en TTF
via fonts/brat.ttf), flou + grain "sale" pour le rendu authentique basse
résolution (mêmes réglages que le projet open source du générateur).

Entrée : texte libre. Sortie : image WebP 512×512 (sticker WhatsApp).
Aucune dépendance autre que Pillow (déjà utilisée pour .sticker).
"""

import io
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Police condensée du style brat : Arial Narrow officielle si fetch_brat_font.py
# a pu la télécharger à l'installation, sinon la police libre Roboto Condensed
# embarquée par défaut. .brat fonctionne dans tous les cas.
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "brat.ttf")

CANVAS = 512          # taille du sticker (WhatsApp)
MARGIN = 30           # marge intérieure
MAX_TEXT_CHARS = 600  # garde-fou longueur du texte

# Tailles de police testées de la plus grande à la plus petite : on choisit la
# plus grande qui fait tenir tout le texte sur le canvas (comme le rendu brat).
FONT_SIZES = (52, 44, 38, 32, 27, 23, 20, 18)


def _wrap(text, font, draw, max_width):
    """Découpe le texte en lignes qui tiennent dans max_width (par mots)."""
    words = text.split(" ")
    lines, current = [], ""
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _pick_font(text, draw):
    """Choisit la plus grande taille de police qui fait tout tenir."""
    for size in FONT_SIZES:
        font = ImageFont.truetype(FONT_PATH, size)
        lines = _wrap(text, font, draw, CANVAS - 2 * MARGIN)
        total_h = len(lines) * (size + 8)
        if total_h <= CANVAS - 2 * MARGIN:
            return font, lines
    # Dernier recours : la plus petite taille (texte très long)
    font = ImageFont.truetype(FONT_PATH, FONT_SIZES[-1])
    return font, _wrap(text, font, draw, CANVAS - 2 * MARGIN)


def render(text):
    """
    Génère le sticker brat (WebP 512×512) et renvoie les octets.

    Ne lève jamais : retourne None en cas de problème (le bot affichera
    un message d'erreur propre).
    """
    if not text:
        return None
    # Style brat : tout en minuscules, sans sauts de ligne sauvages
    cleaned = " ".join(str(text).split()).lower()[:MAX_TEXT_CHARS]
    if not cleaned:
        return None

    try:
        img = Image.new("RGB", (CANVAS, CANVAS), "white")
        draw = ImageDraw.Draw(img)

        font, lines = _pick_font(cleaned, draw)
        line_h = font.size + 8

        # Position verticale centrée (légèrement haut, comme le style)
        y = max(MARGIN, (CANVAS - len(lines) * line_h) // 2 - 10)
        for line in lines:
            draw.text((MARGIN, y), line, font=font, fill="black")
            y += line_h

        # Effet brat : flou + grain "sale" (rendu authentique basse résolution,
        # mêmes réglages que le générateur open source : blur ~4px sur grand format)
        img = img.filter(ImageFilter.GaussianBlur(1.2))
        pixels = img.load()
        for _ in range(2800):
            x = random.randrange(CANVAS)
            y = random.randrange(CANVAS)
            noise = random.choice((-10, -5, 5, 10))
            r, g, b = pixels[x, y]
            pixels[x, y] = (
                max(0, min(255, r + noise)),
                max(0, min(255, g + noise)),
                max(0, min(255, b + noise)),
            )

        out = io.BytesIO()
        img.save(out, "WEBP", quality=92)
        return out.getvalue()
    except Exception:
        return None
