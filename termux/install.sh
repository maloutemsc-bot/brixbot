#!/usr/bin/env bash
# =========================================================================== #
#  install.sh — Installation des dépendances BrixBot sur Termux (Android)
#  À exécuter UNE SEULE FOIS, dans le dossier du projet (~/brixbot).
# =========================================================================== #
set -e
cd "$(dirname "$0")/.."

echo "[1/3] Dépendances Python (Flask, SQLAlchemy, requests…)…"
python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install -r backend/requirements.txt
# pinscrape (Pinterest, priorité de .pin) SANS opencv (~350 Mo) : search()
# n'en a pas besoin, le service le shime. Échec = .pin garde DuckDuckGo/Wikimedia.
echo "  → pinscrape (Pinterest pour .pin, léger sans opencv)…"
python -m pip install --no-deps pinscrape==5.1.0 2>/dev/null || echo "  ⚠️ pinscrape non installé (repli automatique sur DuckDuckGo/Wikimedia)"

echo "[2/3] Dépendances Node (Baileys, Express, axios…)…"
cd whatsapp-bot
# --omit=optional : ignore sharp (binaires natifs indisponibles sur Android).
# La commande .sticker est alors désactivée proprement (le bot le gère) :
# tout le reste fonctionne (IA, .yt, .ocr, .tts, transcription vocale…).
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
