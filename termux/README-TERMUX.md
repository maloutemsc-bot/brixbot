# 📱 BrixBot sur un vieux téléphone Android (Termux) — 24/7 gratuit

Faites tourner **tout le bot** (backend Flask + bot WhatsApp + panneau) sur un
**vieux téléphone Android** qui reste branché : 100 % gratuit, aucune carte
bancaire, et surtout **IP résidentielle = risque de bannissement WhatsApp quasi
nul** (contrairement à Render/VPS dont les IP de datacenter font bannir le
numéro en quelques semaines).

---

## ✅ Téléphone recommandé

| Critère | Recommandation |
|---|---|
| Android | 7.0 ou plus récent |
| RAM | **2 Go minimum** (Node + Python + Baileys ≈ 500 Mo) |
| Stockage | 1 Go de libre suffit |
| Alimentation | Branché en permanence (surtout la nuit) |
| Réseau | Wi-Fi stable à la maison (ou un forfait data illimité) |

---

## 1. 🛠 Préparer le téléphone

1. Installez **F-Droid** : téléchargez l'APK sur https://f-droid.org puis ouvrez-le.
2. Depuis F-Droid, installez ces 3 applications :
   - **Termux** (l'application principale)
   - **Termux:API** (permet le *wake lock* : le téléphone ne s'endort pas)
   - **Termux:Boot** (permet le démarrage automatique au redémarrage du téléphone)
3. Ouvrez Termux et autorisez le stockage :
   ```
   termux-setup-storage
   ```
   (acceptez la demande d'autorisation de stockage)
4. **Paramètres Android** (varie selon la marque) :
   - Désactivez **l'optimisation de la batterie** pour Termux
     (Paramètres → Batterie → Optimisation → Termux → « Ne pas optimiser »)
   - Autorisez **le démarrage automatique** de Termux au boot
     (Xiaomi/Redmi : *Autostart* · Samsung : *Apps jamais en veille* ·
     Huawei : *Gestionnaire du démarrage* · OnePlus : *Démarrage automatique*)

---

## 2. 📤 Pousser le projet sur GitHub (depuis votre PC)

Les fichiers sensibles (`.env`, `auth_info/`, `instance/`, logs…) sont déjà
dans `.gitignore` : ils ne seront **jamais** envoyés. Vous pouvez donc créer un
dépôt **privé** et pousser :

```bash
# Sur votre PC, dans le dossier du projet :
git remote add origin https://github.com/VOTRE_COMPTE/brixbot.git
git branch -M main
git push -u origin main
```

> 💡 Si vous n'avez pas de compte GitHub, vous pouvez aussi transférer le dossier
> du projet sur le téléphone autrement (câble USB, Google Drive…). L'important
> est d'avoir le dossier du projet sur le téléphone.

---

## 3. 🏗 Installer Termux (sur le téléphone)

Ouvrez Termux et copiez-collez :

```bash
pkg update && pkg upgrade -y
pkg install -y python nodejs-lts git nano termux-api yt-dlp curl
```

> ⚠️ **N'installez PAS `termux-boot` avec `pkg`** : ce paquet n'existe pas dans
> les dépôts Termux (vous verriez « Unable to locate package termux-boot »).
> **Termux:Boot est une application Android** — elle s'installe depuis F-Droid,
> exactement comme Termux et Termux:API (étape 1). Vérifiez qu'elle est bien
> installée sur le téléphone avant de continuer.
>
> `termux-api` (la commande `termux-wake-lock`…), lui, est bien un paquet `pkg`.

Puis récupérez le projet :

```bash
git clone https://github.com/VOTRE_COMPTE/brixbot.git
cd brixbot
```

> 📁 Le dossier du projet sera dans `~/brixbot` (chez vous). Si vous l'avez
> transféré ailleurs, placez-vous dedans avec `cd`.

---

## 4. 🔑 Configurer les clés (copiez depuis votre PC)

Sur votre PC, ouvrez `backend\.env` avec le Bloc-notes (il contient vos clés
BrixHub / GROQ / propriétaire). Copiez son **contenu complet**.

Sur le téléphone :

```bash
# Crée les .env depuis les modèles (une seule fois) :
bash termux/setup-env.sh

# Collez le contenu de votre .env Windows dans chaque fichier :
nano backend/.env          # → Ctrl+O pour enregistrer, Ctrl+X pour quitter
nano whatsapp-bot/.env     # → idem
```

> ⚠️ `BOT_API_KEY` doit être **identique** dans les deux fichiers.

---

## 5. 📦 Installer les dépendances (une seule fois)

```bash
bash termux/install.sh
```

> ℹ️ Sur Android, `sharp` (conversion d'images en stickers) n'est pas
> installable : le script l'ignore et la commande `.sticker` est simplement
> désactivée proprement. Tout le reste fonctionne (IA, .yt, .ocr, .tts,
> .translate, .resume, transcription vocale…).

---

## 6. 🚀 Lancer le bot

```bash
bash termux/start.sh
```

Ce qui se passe :
- 🔒 Le **wake lock** est activé (le téléphone ne dort plus)
- 🌐 Votre **IP locale** s'affiche → le panneau sera accessible sur le PC via
  `http://IP_DU_TELEPHONE:5000/admin`
- 🤖 Le backend puis le bot démarrent
- 📱 Le **QR code** s'affiche dans le terminal (et dans le panneau)

**Scannez le QR avec VOTRE autre téléphone** (celui avec WhatsApp) :
WhatsApp → Paramètres → Appareils connectés → Connecter un appareil.

Une fois connecté : `✅ WhatsApp connecté`.

> 💡 Le terminal affiche les logs du bot en direct. Pour le quitter sans arrêter
> le bot, voir la section 7 (démarrage automatique) — c'est LA vraie solution
> 24/7.

---

## 7. 🔁 Démarrage automatique au boot (la vraie solution 24/7)

> ⚠️ **À faire impérativement AVANT tout le reste : ouvrez l'application
> Termux:Boot UNE FOIS** (juste la lancer, puis la quitter). Sans ce premier
> lancement, Android ne lui donne jamais l'autorisation de se déclencher au
> boot — c'est la cause n°1 du « ça ne démarre pas ». Au premier lancement,
> elle crée aussi le dossier `~/.termux/boot`.

Puis, dans Termux :

```bash
# 1) Crée le dossier attendu par Termux:Boot (si pas déjà fait)
mkdir -p ~/.termux/boot

# 2) Copie le script de boot
cp termux/boot.sh ~/.termux/boot/brixbot.sh

# 3) Rend le script exécutable (sécurité)
chmod +x ~/.termux/boot/brixbot.sh

# 4) Vérifie qu'il est bien là
ls -la ~/.termux/boot/
```

**Redémarrez le téléphone**, puis vérifiez :

```bash
bash termux/status.sh          # ports 5000 et 3000 à l'écoute ?
cat ~/brixbot-boot.log         # le journal du démarrage automatique
```

Le bot se connecte tout seul avec la session sauvegardée (pas de QR à
rescanner tant que la session reste valide).

> 💡 Le script `boot.sh` **trouve tout seul le dossier du projet** (peu importe
> s'il s'appelle `brixbot`, `wtspbot`, etc.) et écrit un **journal détaillé**
> dans `~/brixbot-boot.log` : si ça ne démarre pas, ce fichier vous dira
> exactement où ça bloque.

---

## 8. 📺 Accéder au panneau depuis votre PC

- Sur le téléphone : `bash termux/status.sh` ou regardez l'IP affichée au
  démarrage (`ip -4 addr show` dans Termux).
- Sur le PC : ouvrez **http://IP_DU_TELEPHONE:5000/admin**
- Pensez à définir `ADMIN_PASSWORD` dans `backend/.env` puisque le panneau est
  maintenant accessible sur votre réseau local.

> ℹ️ L'IP du téléphone peut changer (Wi-Fi) : pour éviter ça, réservez une IP
> fixe au téléphone dans l'interface de votre routeur (DHCP reservation).

---

## 9. 🛠 Commandes utiles

| Commande | Effet |
|---|---|
| `bash termux/start.sh` | Démarre le bot (terminal, avec QR) |
| `bash termux/stop.sh` | Arrête tout proprement |
| `bash termux/status.sh` | Affiche les ports actifs |
| `bash termux/update.sh` | Met à jour depuis GitHub + réinstalle les dépendances |
| `bash termux/reset-session.sh` | Supprime la session → **nouveau QR** au prochain démarrage |
| `termux-wake-lock` / `termux-wake-unlock` | Active / désactive le verrou de veille |

---

## 10. 🔧 Dépannage

| Problème | Solution |
|---|---|
| Rien ne démarre au boot | 1) **Ouvrez l'app Termux:Boot une fois** puis redémarrez. 2) `cat ~/brixbot-boot.log` pour voir où ça bloque. 3) Vérifiez `ls -la ~/.termux/boot/` (le script doit être là). 4) Autorisez le démarrage auto de Termux ET Termux:Boot (Xiaomi : Autostart · Samsung : Apps jamais en veille). |
| `brixbot-boot.log` dit « Dossier du projet introuvable » | Le dossier ne s'appelle ni `brixbot` ni `wtspbot` : renommez-le avec `mv ~/ancien_nom ~/brixbot` (ou modifiez la liste dans `~/.termux/boot/brixbot.sh`). |
| `brixbot-boot.log` dit « Pas encore de réseau… » 12 fois | Le Wi-Fi met trop de temps à se connecter, ou il n'y a pas d'accès internet (ex: pas de data). Le bot a besoin d'internet pour WhatsApp. |
| Le bot démarre puis meurt après quelques secondes | Optimisation batterie encore active sur Termux, ou le téléphone se met en veille : désactivez l'optimisation pour Termux + Termux:Boot, et vérifiez `termux-wake-lock`. |
| `pkg install … termux-boot` → « Unable to locate package » | Normal : **Termux:Boot est une application F-Droid**, pas un paquet `pkg`. Installez-la depuis F-Droid (étape 1). |
| `.sticker` ne marche pas | Normal sur Android (sharp indisponible). Tout le reste fonctionne. |
| Le bot ne se reconnecte pas après coupure Wi-Fi | Baileys se reconnecte tout seul ; vérifiez avec `bash termux/status.sh`. |
| Téléphone endormi, plus de réponses | `termux-wake-lock` + désactivez l'optimisation de batterie de Termux. |
| QR à rescanner régulièrement | Normal si le téléphone a été éteint longtemps. Rescandez simplement. |
| Erreur `Key used already` / `Bad MAC` | Deux instances tournent → `bash termux/stop.sh` puis relancez UNE seule fois. |
| Panneau inaccessible depuis le PC | Vérifiez l'IP (même réseau Wi-Fi), et que le port 5000 est écouté. |
| Mémoire insuffisante | Fermez les autres apps ; un téléphone 2 Go est le minimum. |
| Numéro banni | 🛡️ N'utilisez **jamais** le numéro principal avec le bot — prenez une SIM dédiée. |

---

## 11. 💡 Conseils de sécurité

- Utilisez un **numéro dédié** pour le bot (pas votre numéro principal).
- Mettez un `ADMIN_PASSWORD` solide dans `backend/.env`.
- Ne partagez jamais `backend/.env` ni le dossier `auth_info/` (session = accès
  complet au compte WhatsApp connecté).
