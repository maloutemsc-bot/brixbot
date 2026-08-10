#!/usr/bin/env bash
# =========================================================================== #
#  setup-env.sh — Crée les fichiers .env depuis les modèles (si absents)
#  Puis éditez-les avec : nano backend/.env  et  nano whatsapp-bot/.env
#  (copiez le contenu des .env de votre PC Windows).
# =========================================================================== #
cd "$(dirname "$0")/.."

for f in backend/.env whatsapp-bot/.env; do
  if [ -f "$f" ]; then
    echo "✓ $f existe déjà (inchangé)."
  else
    cp "$f.example" "$f"
    echo "➕ $f créé depuis le modèle — éditez-le : nano $f"
  fi
done

echo ""
echo "💡 Rappel : BOT_API_KEY doit être IDENTIQUE dans backend/.env et whatsapp-bot/.env"
