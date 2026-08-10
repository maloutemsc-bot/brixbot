#!/usr/bin/env bash
# =========================================================================== #
#  stop.sh — Arrête le bot WhatsApp et le backend Flask (Termux / Android)
#  Usage : bash termux/stop.sh
# =========================================================================== #
cd "$(dirname "$0")/.."

echo "🛑 Arrêt du bot WhatsApp…"
pkill -f 'node whatsapp-bot.js' 2>/dev/null || true
sleep 1

echo "🛑 Arrêt du backend Flask…"
pkill -f 'backend/app.py' 2>/dev/null || true
pkill -f 'python app.py' 2>/dev/null || true

if command -v termux-wake-unlock >/dev/null 2>&1; then
  termux-wake-unlock 2>/dev/null || true
fi

echo "✅ Tout est arrêté. Pour relancer : bash termux/start.sh"
