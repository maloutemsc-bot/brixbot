#!/usr/bin/env bash
# =========================================================================== #
#  boot.sh — Démarrage automatique au redémarrage du téléphone (Termux:Boot)
#
#  Installation (UNE SEULE FOIS, dans Termux) :
#      mkdir -p ~/.termux/boot
#      cp termux/boot.sh ~/.termux/boot/brixbot.sh
#
#  Prérequis :
#      - Application "Termux:Boot" installée depuis F-Droid
#      - ⚠️ OUVREZ Termux:Boot UNE FOIS après l'installation (il crée le
#        dossier ~/.termux/boot et s'autorise au démarrage) PUIS redémarrez
#      - Démarrage automatique autorisé dans les réglages Android
#        (Xiaomi : Autostart · Samsung : Apps jamais en veille · etc.)
#      - Optimisation batterie désactivée pour Termux ET Termux:Boot
#
#  Journal : tout ce que fait ce script est écrit dans ~/brixbot-boot.log
# =========================================================================== #

# Tout ce qui suit est enregistré dans le journal (consultable après boot)
LOG="$HOME/brixbot-boot.log"
exec >> "$LOG" 2>&1

echo ""
echo "===== Boot BrixBot : $(date '+%Y-%m-%d %H:%M:%S') ====="

# 1) Attendre que le réseau soit opérationnel (jusqu'à 60 s)
for i in $(seq 1 12); do
  if curl -s --max-time 3 https://example.com >/dev/null 2>&1; then
    echo "[boot] Réseau OK (essai $i)"
    break
  fi
  echo "[boot] Pas encore de réseau… (essai $i/12)"
  sleep 5
done

# 2) Wake lock : le téléphone ne s'endort plus
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock && echo "[boot] Wake lock activé"
fi

# 3) Trouver le dossier du projet (peu importe son nom : brixbot, wtspbot, bot…)
PROJECT=""
for cand in "$HOME/brixbot" "$HOME/wtspbot" "$HOME/bot" "$HOME/brixbot-bot" "$HOME/whatsapp-bot"; do
  if [ -f "$cand/backend/app.py" ] && [ -f "$cand/whatsapp-bot/whatsapp-bot.js" ]; then
    PROJECT="$cand"
    break
  fi
done
# Repli : recherche automatique dans tous les sous-dossiers de $HOME
if [ -z "$PROJECT" ]; then
  for d in "$HOME"/*/; do
    if [ -f "${d}backend/app.py" ] && [ -f "${d}whatsapp-bot/whatsapp-bot.js" ]; then
      PROJECT="${d%/}"
      break
    fi
  done
fi

if [ -z "$PROJECT" ]; then
  echo "[boot] ❌ Dossier du projet introuvable (cherché : brixbot, wtspbot, bot, + scan de ~)."
  echo "[boot]    Vérifiez le nom du dossier avec : ls ~   puis renommez-le en brixbot,"
  echo "[boot]    ou corrigez la liste des noms dans ce script."
  exit 1
fi
echo "[boot] Projet trouvé : $PROJECT"
cd "$PROJECT" || exit 1

# 4) Backend : déjà actif ? sinon démarrage + attente
if curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1; then
  echo "[boot] Backend déjà actif (5000)."
else
  echo "[boot] Démarrage du backend Flask…"
  nohup python backend/app.py > backend.log 2>&1 &
  for i in $(seq 1 25); do
    if curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1; then
      echo "[boot] ✅ Backend prêt (5000)"
      break
    fi
    sleep 1
  done
  if ! curl -s --max-time 2 http://localhost:5000/health >/dev/null 2>&1; then
    echo "[boot] ⚠️ Backend pas prêt après 25 s — voir backend.log"
  fi
fi

# 5) Bot WhatsApp : déjà actif ? sinon démarrage en arrière-plan
if curl -s --max-time 2 http://localhost:3000/health >/dev/null 2>&1 \
   || pgrep -f 'node whatsapp-bot.js' >/dev/null 2>&1; then
  echo "[boot] Bot WhatsApp déjà actif (3000)."
else
  echo "[boot] Démarrage du bot WhatsApp…"
  cd whatsapp-bot
  nohup node whatsapp-bot.js > bot.log 2>&1 &
  sleep 10
  if curl -s --max-time 2 http://localhost:3000/health >/dev/null 2>&1; then
    echo "[boot] ✅ Bot actif (3000)."
  else
    echo "[boot] ⚠️ Bot pas encore prêt après 10 s — voir whatsapp-bot/bot.log"
  fi
fi

echo "[boot] Terminé : $(date '+%H:%M:%S')"
