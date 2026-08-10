#!/usr/bin/env bash
# =========================================================================== #
#  update.sh — Met à jour le bot depuis GitHub (Termux / Android)
#  Usage : bash termux/update.sh
# =========================================================================== #
cd "$(dirname "$0")/.."

echo "🔄 Récupération des nouveautés depuis GitHub…"
# --autostash : préserve d'éventuelles petites modifications locales
# (les .env et la session restent de toute façon intouchés).
git pull --autostash

echo "📦 Réinstallation des dépendances (si nécessaire)…"
bash termux/install.sh

echo ""
echo "✅ Mise à jour terminée !"
echo "   Relancez : bash termux/start.sh"
