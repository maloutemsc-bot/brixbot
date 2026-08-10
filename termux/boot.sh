#!/usr/bin/env bash
# =========================================================================== #
#  boot.sh — Démarrage automatique au redémarrage du téléphone (Termux:Boot)
#
#  Installation (UNE SEULE FOIS, dans Termux) :
#      cp termux/boot.sh ~/.termux/boot/brixbot.sh
#
#  Prérequis :
#      - Application "Termux:Boot" installée depuis F-Droid
#      - Démarrage automatique de Termux autorisé dans les réglages Android
#        (Xiaomi : Autostart · Samsung : Apps jamais en veille · etc.)
# =========================================================================== #
sleep 15   # attendre que le réseau Wi-Fi soit opérationnel

cd "$HOME/brixbot" || exit 1

# Wake lock : le téléphone ne s'endort plus
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
fi

# Backend : déjà actif ? sinon démarrage + attente
if ! curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1; then
  nohup python backend/app.py > backend.log 2>&1 &
  for i in $(seq 1 25); do
    curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1 && break
    sleep 1
  done
fi

# Bot WhatsApp : déjà actif ? sinon démarrage en arrière-plan
if ! curl -s --max-time 2 http://localhost:3000/health >/dev/null 2>&1; then
  cd whatsapp-bot
  nohup node whatsapp-bot.js > bot.log 2>&1 &
fi
