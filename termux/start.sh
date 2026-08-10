#!/usr/bin/env bash
# =========================================================================== #
#  start.sh — Démarre le backend Flask + le bot WhatsApp (Termux / Android)
#  Usage : bash termux/start.sh
# =========================================================================== #
cd "$(dirname "$0")/.."

# --- IP locale (plusieurs méthodes, la première qui marche l'emporte) ---
get_ip() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ -n "$ip" ] && [ "$ip" != "127.0.0.1" ]; then echo "$ip"; return; fi
  ip="$(ip -4 addr show scope global 2>/dev/null | grep -oE 'inet [0-9.]+' | head -1 | cut -d' ' -f2)"
  if [ -n "$ip" ] && [ "$ip" != "127.0.0.1" ]; then echo "$ip"; return; fi
  echo "?"
}
IP="$(get_ip)"

# 1) Wake lock : empêche Android de mettre le CPU en veille
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  echo "🔒 Wake lock activé — le téléphone ne s'endormira pas."
fi

# 2) Adresse du panneau
if [ "$IP" != "?" ]; then
  echo "📶 IP du téléphone : $IP"
  echo "🌐 Panneau (sur votre PC) : http://$IP:5000/admin"
else
  echo "📶 IP locale introuvable — trouvez-la avec : ip -4 addr show"
fi
echo ""

# 3) Anti double-instance : port 3000 qui répond OU processus déjà actif
if curl -s --max-time 2 http://localhost:3000/health >/dev/null 2>&1 \
   || pgrep -f 'node whatsapp-bot.js' >/dev/null 2>&1; then
  echo "⚠️ Le bot tourne déjà (port 3000 occupé ou processus actif)."
  echo "   Pour tout arrêter : bash termux/stop.sh  puis relancez."
  exit 1
fi

# 4) Backend : déjà actif ? sinon on le démarre et on attend qu'il soit prêt
if curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1; then
  echo "ℹ️ Backend déjà actif (port 5000) — lancement du bot uniquement."
else
  echo "[1/2] Démarrage du backend Flask…"
  nohup python backend/app.py > backend.log 2>&1 &
  for i in $(seq 1 25); do
    if curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1; then
      echo "      ✅ Backend prêt (http://localhost:5000)"
      break
    fi
    sleep 1
  done
fi

# 5) Bot WhatsApp en avant-plan (logs + QR dans le terminal)
echo "[2/2] Démarrage du bot WhatsApp — scandez le QR avec votre autre téléphone…"
echo ""
cd whatsapp-bot
node whatsapp-bot.js
