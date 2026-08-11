#!/usr/bin/env bash
# =========================================================================== #
#  install.sh — Installation des dépendances BrixBot sur Termux (Android)
#  À exécuter UNE SEULE FOIS, dans le dossier du projet (~/brixbot).
# =========================================================================== #
set -e
cd "$(dirname "$0")/.."

echo "[1/3] Dépendances Python (Flask, SQLAlchemy, requests…)…"
python -m pip install --upgrade pip >/dev/null 2>&1 || true
# Pinterest (.pin) fonctionne via l'API directe dans pinterest_service.py :
# requests seul suffit (déjà dans requirements.txt), AUCUNE dépendance lourde
# (ni pinscrape, ni pydantic, ni opencv) — rien de plus à installer.
# Pillow (.sticker via le backend) : le paquet Termux précompilé inclut
# l'encodage WebP. Non-fatal : le bot retombe sur sharp s'il est disponible.
if command -v pkg >/dev/null 2>&1; then
  pkg install -y python-pillow >/dev/null 2>&1 || echo "  ⚠️ python-pillow (Termux) : échec — .sticker utilisera le fallback si dispo"
fi
python -m pip install -r backend/requirements.txt 2>/dev/null || echo "  ⚠️ Dépendances Python de base : échec (voir messages ci-dessus)"
# Police authentique du style brat (Arial Narrow) : téléchargée et convertie
# à l'installation. NON FATAL : si le site officiel est indisponible, la police
# libre embarquée (Roboto Condensed) reste utilisée et .brat fonctionne quand même.
echo "[1b/3] Police brat authentique (Arial Narrow)…"
python backend/fetch_brat_font.py || true

# Site bratify en LOCAL (self-host) : le générateur brat est copié et servi
# par notre propre backend → rendu 100% identique SANS dépendance réseau.
echo "[1c/3] Site bratify local (self-host)…"
if python backend/fetch_bratify_site.py; then
  echo "  ✅ Site bratify copié en local : .brat fonctionne sans Internet"
else
  echo "  ⚠️ Copie impossible : .brat utilisera le site distant (si dispo)"
fi

# Navigateur pour le scraping .brat (rendu 100% identique au site bratify).
# Chromium Termux ~200 Mo : NON FATAL, .brat retombe sur la génération locale.
echo "[1d/3] Navigateur pour .brat (scraping authentique)…"
if command -v pkg >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
  pkg install -y chromium >/dev/null 2>&1 || echo "  ⚠️ chromium : échec — .brat utilisera le rendu local (fallback)"
fi
if command -v chromium >/dev/null 2>&1; then
  echo "  ✅ Navigateur présent : .brat utilisera le VRAI générateur (scraping local)"
else
  echo "  ⚠️ Aucun navigateur : .brat utilisera le rendu local (fallback)"
fi

# ffmpeg pour .shazam (conversion vocal ogg/opus → WAV avant reconnaissance).
# Le paquet Termux fournit le vrai binaire dans le PATH : le bot le détecte
# automatiquement (aucun binaire npm requis — ffmpeg-static n'existe pas sur
# toutes les architectures Android). NON FATAL : .shazam explique la commande.
echo "[1e/3] ffmpeg pour .shazam (reconnaissance musicale)…"
if command -v pkg >/dev/null 2>&1 && ! command -v ffmpeg >/dev/null 2>&1; then
  pkg install -y ffmpeg >/dev/null 2>&1 || echo "  ⚠️ ffmpeg : échec — .shazam affichera la marche à suivre"
fi
if command -v ffmpeg >/dev/null 2>&1; then
  echo "  ✅ ffmpeg présent : .shazam fonctionne"
else
  echo "  ⚠️ ffmpeg absent : .shazam indisponible (lancez pkg install ffmpeg)"
fi

echo "  ✅ Pinterest (.pin) : API directe intégrée — aucune installation supplémentaire"
echo "  ✅ Stickers (.sticker) : conversion via le backend (Pillow + WebP)"

echo "[2/3] Dépendances Node (Baileys, Express, axios…)…"
cd whatsapp-bot
# --omit=optional : ignore sharp (binaires natifs indisponibles sur Android).
# .sticker fonctionne quand même via le backend (Pillow → WebP), tout le
# reste fonctionne aussi (IA, .yt, .ocr, .tts, transcription vocale…).
npm install --omit=optional
cd ..

echo "[3/3] Vérification des outils…"
for c in python node npm yt-dlp git curl; do
  if command -v "$c" >/dev/null 2>&1; then
    echo "  ✓ $c : $(command -v "$c")"
  else
    echo "  ⚠️ $c manquant → pkg install $c"
  fi
done

echo ""
echo "✅ Installation terminée !"
echo "   → Clés :   bash termux/setup-env.sh  puis  nano backend/.env"
echo "   → Lancer : bash termux/start.sh"
