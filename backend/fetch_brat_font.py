"""
fetch_brat_font.py — Police authentique pour les stickers .brat.

La police Arial Narrow (celle du générateur officiel bratgenerator.com) est
PROPRIÉTAIRE : on ne la committe PAS dans le dépôt. Comme le projet open
source du générateur, on référence l'URL officielle du woff et on le convertit
en TTF à l'installation (script appelé par install.sh / demarrer-bot.bat).

Si le téléchargement échoue, brat.ttf reste la police libre embarquée
(Roboto Condensed, Apache 2.0) : .brat fonctionne dans tous les cas.

Utilisation : python fetch_brat_font.py
"""

import io
import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
TARGET = os.path.join(FONTS_DIR, "brat.ttf")

# URL officielle du woff Arial Narrow (référencée par bratgenerator.com /
# le projet open source Jalpan04/brat-text-generator).
ARIAL_NARROW_WOFF = (
    "https://www.bratgenerator.com/sites/g/files/g2000017981/files/"
    "2024-03/arial_narrow-webfont.woff"
)

FALLBACK_MSG = "Police libre Roboto Condensed utilisée (fonctionne toujours)."


def main():
    os.makedirs(FONTS_DIR, exist_ok=True)

    try:
        import requests
        from fontTools.ttLib import TTFont
    except ImportError:
        print(f"  [WARN] fonttools/requests absents. {FALLBACK_MSG}")
        return 1

    # 0) Déjà installée ? On évite un appel réseau à chaque démarrage.
    if os.path.exists(TARGET):
        try:
            if (TTFont(TARGET)["name"].getDebugName(1) or "") == "Arial Narrow":
                print("  [OK] Police brat authentique déjà présente.")
                return 0
        except Exception:
            pass  # fichier corrompu : on retélécharge

    # 1) Téléchargement du woff officiel
    try:
        r = requests.get(ARIAL_NARROW_WOFF, timeout=30)
        r.raise_for_status()
        woff = r.content
        # Vérifie que c'est bien un woff (en-tête "wOFF")
        if not woff[:4] == b"wOFF":
            print(f"  [WARN] Réponse inattendue (pas un woff). {FALLBACK_MSG}")
            return 1
    except Exception as exc:
        print(f"  [WARN] Téléchargement impossible ({exc}). {FALLBACK_MSG}")
        return 1

    # 2) Conversion woff → ttf (écrite dans un fichier temporaire)
    fd, tmp_path = tempfile.mkstemp(suffix=".ttf", dir=FONTS_DIR)
    os.close(fd)
    try:
        font = TTFont(io.BytesIO(woff))
        font.save(tmp_path)
        name = font["name"].getDebugName(1) or "?"
        font.close()
        if not os.path.getsize(tmp_path):
            raise ValueError("conversion vide")
    except Exception as exc:
        os.unlink(tmp_path)
        print(f"  ⚠️  Conversion impossible ({exc}). {FALLBACK_MSG}")
        return 1

    # 3) Remplacement de brat.ttf par la police authentique
    try:
        os.replace(tmp_path, TARGET)
    except Exception:
        os.unlink(tmp_path)
        return 1

    print(f"  [OK] Police brat authentique installée : {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
