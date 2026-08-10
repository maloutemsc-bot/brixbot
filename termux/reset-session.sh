#!/usr/bin/env bash
# =========================================================================== #
#  reset-session.sh — Supprime la session WhatsApp (→ nouveau QR code)
#  Usage : bash termux/reset-session.sh
#  À utiliser si vous voulez reconnecter un AUTRE numéro, ou si la session
#  est corrompue ("Key used already" en boucle).
# =========================================================================== #
cd "$(dirname "$0")/.."

echo "🧹 Arrêt des services…"
bash termux/stop.sh
sleep 1

rm -rf whatsapp-bot/auth_info
echo "✅ Session WhatsApp supprimée."
echo ""
echo "   Relancez : bash termux/start.sh"
echo "   → un NOUVEAU QR code s'affichera dans le terminal et le panneau."
