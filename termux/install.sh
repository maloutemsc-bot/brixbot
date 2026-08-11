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
