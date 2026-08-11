#!/usr/bin/env bash
# =========================================================================== #
#  update.sh — Met à jour le bot depuis GitHub (Termux / Android)
#  Usage : bash termux/update.sh
# =========================================================================== #
cd "$(dirname "$0")/.."

echo "🔄 Récupération des nouveautés depuis GitHub…"

# --- Nettoyage d'un éventuel état git bloquant ("unmerged files") ---------- #
# Un pull interrompu laisse des fichiers en conflit qui bloquent tout pull
# suivant. Les données sensibles (.env, session WhatsApp, base de données,
# transcript, médias) sont dans .gitignore : un reset est donc SANS RISQUE.
# On ne touche QUE aux fichiers suivis par git.
if git ls-files -u | grep -q .; then
  echo "  ⚠️ Conflits git détectés (pull précédent interrompu)…"
  echo "  🧹 Nettoyage automatique — vos .env, session et données sont intacts."
  git reset --hard HEAD
  echo "  ✅ État git nettoyé."
fi

# --autostash : préserve d'éventuelles petites modifications locales
# (les .env et la session restent de toute façon intouchés).
git pull --autostash

echo "📦 Réinstallation des dépendances (si nécessaire)…"
bash termux/install.sh

echo ""
echo "✅ Mise à jour terminée !"
echo "   Relancez : bash termux/start.sh"
