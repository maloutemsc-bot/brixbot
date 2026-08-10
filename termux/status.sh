#!/usr/bin/env bash
# =========================================================================== #
#  status.sh — État des services BrixBot (Termux / Android)
#  Usage : bash termux/status.sh
# =========================================================================== #

# --- IP locale (plusieurs méthodes, la première qui marche l'emporte) ---
get_ip() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ -n "$ip" ] && [ "$ip" != "127.0.0.1" ]; then echo "$ip"; return; fi
  ip="$(ip -4 addr show scope global 2>/dev/null | grep -oE 'inet [0-9.]+' | head -1 | cut -d' ' -f2)"
  if [ -n "$ip" ] && [ "$ip" != "127.0.0.1" ]; then echo "$ip"; return; fi
  echo "?"
}

echo "=== Services ==="
if curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1; then
  echo "✅ Backend (5000)  : actif"
else
  echo "❌ Backend (5000)  : arrêté"
fi
if curl -s --max-time 2 http://localhost:3000/health >/dev/null 2>&1; then
  echo "✅ Bot (3000)      : actif"
else
  echo "❌ Bot (3000)      : arrêté"
fi

echo ""
echo "=== Connexion WhatsApp ==="
# /api/whatsapp/status peut demander un mot de passe admin : on tolère l'échec.
curl -s --max-time 3 http://localhost:5000/api/whatsapp/status 2>/dev/null | head -c 300 || true
echo ""

echo ""
echo "=== Pinterest (.pin) ==="
# pinscrape est optionnel : le check passe par le service (stubs cv2/numpy).
if python -c "import sys; sys.path.insert(0, 'backend'); import pinscrape_service as ps; assert ps.available()" >/dev/null 2>&1; then
  echo "✅ Pinterest (pinscrape) : ACTIF — .pin l'utilisera en priorité"
else
  echo "⚠️  Pinterest : indisponible — .pin utilisera DuckDuckGo/Wikimedia"
  echo "    → bash termux/update.sh puis réessayez (ou pkg install python-pydantic)"
fi

echo ""
echo "=== IP locale ==="
IP="$(get_ip)"
if [ "$IP" != "?" ]; then
  echo "📶 $IP  →  panneau : http://$IP:5000/admin"
else
  echo "📶 IP locale introuvable — trouvez-la avec : ip -4 addr show"
fi
