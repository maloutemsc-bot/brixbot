# 🤖 BrixBot — Bot WhatsApp + Panneau d'administration

Un bot WhatsApp complet avec un panneau d'administration moderne, capable de :

- 🔍 **Commande `.search`** : recherche de personnes via l'API **BrixHub**
  (`.search nom`, `.search nom prénom`, `.search nom prénom ville`)
- 🧠 **IA automatique (GROQ)** : l'IA répond en français à tous les messages
  WhatsApp qui ne sont pas des commandes `.search`
- 📱 **Connexion WhatsApp** via **Baileys** (QR Code scannable, session persistante)
- 🎛️ **Panneau d'administration** : dashboard, configuration, WhatsApp, IA, logs,
  test API — le tout dans un thème sombre élégant

---

## 🏗️ Architecture

```
┌─────────────────────┐        ┌──────────────────────┐        ┌──────────────┐
│  WhatsApp (téléphone)│◄──────►│  whatsapp-bot (Node) │◄──────►│   backend    │
│                     │  QR /   │  Baileys + Express   │  API   │   Flask      │
│                     │  messages│  port 3000          │  REST  │   port 5000  │
└─────────────────────┘        └──────────────────────┘        └──────┬───────┘
                                                                      │
                                                   ┌──────────────────┼──────────────┐
                                                   │                  │              │
                                              ┌────▼─────┐      ┌─────▼─────┐   ┌────▼──────┐
                                              │ BrixHub  │      │   GROQ    │   │  SQLite   │
                                              │  API     │      │   IA      │   │  (logs +  │
                                              └──────────┘      └───────────┘   │  config)  │
                                                                                └───────────┘
```

- **Backend (Python/Flask)** : API REST + panneau d'administration + appels BrixHub/GROQ.
  Les clés API ne sont **jamais** exposées côté bot.
- **Bot (Node.js/Baileys)** : gère uniquement la connexion WhatsApp (QR Code,
  réception/envoi de messages) et communique avec le backend via l'API REST.
- **SQLite** : stocke la configuration et les logs (commandes + conversations IA).

---

## 📁 Structure du projet

```
whatsapp-brixhub-bot/
├── backend/                      # Backend Flask
│   ├── app.py                    # Application principale + API REST
│   ├── database.py               # Modèles SQLAlchemy (config, logs)
│   ├── brixhub_service.py        # Service d'appel à l'API BrixHub
│   ├── ai_service.py             # Service d'appel à l'API GROQ
│   ├── whatsapp_handler.py       # Gestionnaire des commandes WhatsApp
│   ├── requirements.txt
│   ├── .env.example
│   ├── templates/
│   │   └── admin.html            # Panneau d'administration
│   └── static/
│       ├── css/admin.css
│       └── js/admin.js
├── whatsapp-bot/                 # Bot WhatsApp (Node.js)
│   ├── package.json
│   ├── whatsapp-bot.js
│   └── .env.example
├── Dockerfile.backend            # Image Docker du backend
├── Dockerfile.bot                # Image Docker du bot
├── docker-compose.yml            # Orchestration locale
├── smoke_test.py                 # Test rapide de l'API Flask
└── README.md
```

---

## ✅ Prérequis

| Outil      | Version minimale |
|------------|------------------|
| Node.js    | 20+              |
| Python     | 3.10+            |
| npm        | 9+               |
| Docker     | (optionnel)      |
| Compte GROQ| [console.groq.com](https://console.groq.com) — clé `gsk_...` |
| Compte BrixHub | Clé API `brix_...` |

---

## 🚀 Installation locale

### 1. Backend (Flask)

```bash
cd backend
python -m venv .venv                 # crée un environnement virtuel
# Windows :
.venv\Scripts\activate
# macOS / Linux :
# source .venv/bin/activate

pip install -r requirements.txt
```

Créez le fichier `.env` :

```bash
cp .env.example .env
```

Renseignez au minimum `BRIX_API_KEY` et `SECRET_KEY`. Puis démarrez :

```bash
python app.py
# → API disponible sur http://localhost:5000
# → Panneau : http://localhost:5000/admin
```

### 2. Bot WhatsApp (Node.js)

```bash
cd whatsapp-bot
npm install
cp .env.example .env
node whatsapp-bot.js
```

Le QR code s'affiche **dans le terminal** et **dans le panneau** (onglet WhatsApp).
Scannez-le avec votre téléphone : **WhatsApp → Paramètres → Appareils connectés →
Connecter un appareil**.

### Démarrage en 1 clic (Windows) 🪟

Double-cliquez sur **`demarrer-bot.bat`** : il vérifie/installe les dépendances
si nécessaire, démarre le backend Flask et le bot WhatsApp dans deux fenêtres
séparées, puis ouvre automatiquement le panneau dans votre navigateur.

> 🛡️ **Anti double-instance** : si le bot tourne déjà (port 3000 occupé), le
> lanceur **refuse de démarrer une seconde instance** et ouvre simplement le
> panneau. Ne lancez jamais le bot deux fois en même temps : cela provoque
> les erreurs `Key used already` / `Bad MAC` (messages illisibles).

Pour tout arrêter : double-cliquez sur **`arreter-bot.bat`**.

### 3. Vérification

Ouvrez http://localhost:5000/admin. Le statut WhatsApp doit passer à « ✅ Connecté »
après le scan. Envoyez ensuite un message à votre numéro :

```
.search Dupont Jean Paris
```

Vous devriez recevoir un message formaté avec les résultats BrixHub.

---

## ⚙️ Variables d'environnement

### Backend (`backend/.env`)

| Variable           | Description                                      | Défaut                      |
|--------------------|--------------------------------------------------|-----------------------------|
| `SECRET_KEY`       | Clé secrète Flask (obligatoire en prod)          | valeur de démo              |
| `ADMIN_PASSWORD`   | Mot de passe du panneau (vide = accès libre)     | vide                        |
| `BRIX_API_KEY`     | Clé API BrixHub (peut aussi être réglée dans le panel) | vide                  |
| `GROQ_API_KEY`     | Clé API GROQ (peut aussi être réglée dans le panel)     | vide                  |
| `BOT_API_KEY`      | Clé partagée bot ↔ backend (**identique partout**) | `changez-moi-bot`         |
| `BOT_INTERNAL_URL` | URL interne du bot (pour le redémarrage)         | `http://localhost:3000`     |
| `DATABASE_URL`     | Base SQLite (optionnel)                          | `sqlite:///instance/bot.db` |
| `FLASK_DEBUG`      | Mode debug (`true`/`false`)                      | `false`                     |

### Bot (`whatsapp-bot/.env`)

| Variable              | Description                              | Défaut                  |
|-----------------------|------------------------------------------|-------------------------|
| `FLASK_INTERNAL_URL`  | URL du backend Flask                     | `http://localhost:5000` |
| `BOT_API_KEY`         | Clé partagée (**identique au backend**)  | `changez-moi-bot`       |
| `BOT_PORT`            | Port du serveur interne du bot           | `3000`                  |
| `AUTH_DIR`            | Dossier de session WhatsApp              | `auth_info`             |
| `LOG_LEVEL`           | Logs pino (`silent`/`info`/`debug`…)     | `silent`                |

> ⚠️ **Important** : `BOT_API_KEY` doit être **identique** dans `backend/.env`
> et `whatsapp-bot/.env`. C'est la clé qui authentifie les échanges entre les deux.

---

## 🐳 Docker (déploiement local)

```bash
# 1. Créez le fichier .env racine
cp .env.example .env

# 2. Renseignez vos clés puis :
docker compose up --build
```

- Panneau : http://localhost:5000/admin
- Les données sont persistées dans les volumes `backend_data` et `bot_auth`
  (la session WhatsApp survit aux redémarrages).

---

## 📱 Déploiement 24/7 gratuit : vieux téléphone Android (Termux)

> 🛡️ **Recommandé** : un vieux téléphone Android branché en permanence est la
> solution la plus fiable et la plus sûre pour un bot WhatsApp — IP résidentielle
> (risque de bannissement quasi nul, contrairement aux IP de datacenter) et 100 %
> gratuit, sans carte bancaire ni mise en veille.

Guide complet : [`termux/README-TERMUX.md`](termux/README-TERMUX.md)

**En résumé :**

```bash
# Sur le téléphone (Termux) :
pkg update && pkg upgrade -y
pkg install -y python nodejs-lts git nano termux-api termux-boot yt-dlp curl ffmpeg

git clone https://github.com/VOTRE_COMPTE/brixbot.git
cd brixbot

bash termux/setup-env.sh        # crée les .env (puis copiez vos clés)
nano backend/.env               # collez le contenu de votre .env Windows

bash termux/install.sh          # dépendances (une seule fois)
bash termux/start.sh            # démarre + QR code

# 24/7 : démarrage automatique au boot
cp termux/boot.sh ~/.termux/boot/brixbot.sh
```

Le panneau est accessible depuis un PC du même Wi-Fi : `http://IP_DU_TEL:5000/admin`

> ℹ️ Sur Android : `.sticker` est désactivé (bibliothèque native `sharp` non
> installable) — tout le reste fonctionne normalement. Pour `.shazam`, installez
> `pkg install ffmpeg` (fait automatiquement par `install.sh`) : le bot l'utilise
> pour convertir les vocaux, aucun binaire npm requis.

---

## ☁️ Déploiement sur Render.com

Render ne lit pas `docker-compose.yml` directement : on crée **deux services**
indépendants (un par Dockerfile). Deux méthodes :

- **Méthode rapide (Blueprint, recommandé)** : le fichier `render.yaml` crée
  et configure les deux services automatiquement.
- **Méthode manuelle** : tout à la main dans le dashboard Render.

> ⚠️ **Plan gratuit ou payant ?**
> Le plan **gratuit** de Render met les web services en veille après 15 min
> d'inactivité et **ne permet pas les disques persistants** : la session
> WhatsApp serait perdue et le bot déconnecté. Pour un bot 24/7 fiable, il faut
> le plan **Starter** (~7 $/mois par service, disque persistant inclus).

### Méthode rapide — Blueprint (`render.yaml`)

1. Poussez le code sur GitHub (voir *Étape 1* ci-dessous).
2. Sur [render.com](https://render.com) : **New + → Blueprint**.
3. Choisissez votre dépôt : Render détecte `render.yaml` et prépare les deux services.
4. Renseignez les valeurs secrètes demandées :
   - `ADMIN_PASSWORD`, `BRIX_API_KEY`, `GROQ_API_KEY` ;
   - `BOT_API_KEY` : ⚠️ entrez la **même valeur** dans les deux services.
5. Cliquez **Apply** : Render construit et démarre tout automatiquement.
6. Ouvrez `https://brixbot-backend.onrender.com/admin`, connectez-vous, puis
   onglet **WhatsApp** pour scanner le QR Code.

> Si vous renommez un service, mettez à jour `BOT_INTERNAL_URL` (backend) et
> `FLASK_INTERNAL_URL` (bot) dans le dashboard Render avec les vraies URLs.

### Méthode manuelle (sans blueprint)

### Étape 1 — Poussez le code sur GitHub

```bash
git init
git add .
git commit -m "BrixBot : bot WhatsApp + panel admin"
git remote add origin https://github.com/votre-compte/whatsapp-brixhub-bot.git
git push -u origin main
```

### Étape 2 — Service « backend » (Web Service)

Sur [render.com](https://render.com) → **New → Web Service** :

| Paramètre          | Valeur                                            |
|--------------------|---------------------------------------------------|
| Source             | Votre dépôt GitHub                                |
| Environnement      | **Docker**                                        |
| Dockerfile path    | `Dockerfile.backend`                              |
| Nom du service     | `brixbot-backend`                                 |
| Port               | `5000`                                            |
| Plan               | Starter (ou supérieur — voir note sur le disque)  |

**Variables d'environnement** :

| Variable              | Valeur                                             |
|-----------------------|----------------------------------------------------|
| `SECRET_KEY`          | une chaîne aléatoire longue                        |
| `ADMIN_PASSWORD`      | un mot de passe fort pour le panneau *(recommandé)* |
| `BRIX_API_KEY`        | votre clé `brix_...`                               |
| `GROQ_API_KEY`        | votre clé `gsk_...`                                |
| `BOT_API_KEY`         | une clé secrète **commune aux 2 services** (ex : `cle-tres-secrete-123`) |
| `BOT_INTERNAL_URL`    | `https://brixbot-bot.onrender.com` *(créé à l'étape 3)* |

**Disque persistant** (obligatoire pour SQLite et la session) :
Ajoutez un **Persistent Disk** de 1 Go monté sur `/app/instance`.

> ℹ️ **Note** : le plan gratuit de Render ne permet pas les disques persistants
> ni les workers actifs en continu. Pour un bot WhatsApp qui doit rester connecté,
> un plan **Starter** est recommandé. Vous pouvez aussi tester le déploiement
> gratuit mais la session WhatsApp sera perdue à chaque recyclage.

### Étape 3 — Service « bot » (Web Service)

**New → Web Service**, même dépôt :

| Paramètre          | Valeur                                            |
|--------------------|---------------------------------------------------|
| Environnement      | **Docker**                                        |
| Dockerfile path    | `Dockerfile.bot`                                  |
| Nom du service     | `brixbot-bot`                                     |
| Port               | `3000`                                            |
| Health Check Path  | `/health`                                         |
| Plan               | Starter (voir note ci-dessus)                     |

**Variables d'environnement** :

| Variable              | Valeur                                             |
|-----------------------|----------------------------------------------------|
| `FLASK_INTERNAL_URL`  | `https://brixbot-backend.onrender.com`             |
| `BOT_API_KEY`         | la **même** clé que le backend                     |
| `BOT_PORT`            | `3000`                                             |
| `AUTH_DIR`            | `/opt/render/project/src/auth_info` (voir ci-dessous) |

**Disque persistant** : ajoutez un **Persistent Disk** de 1 Go monté sur
`/opt/render/project/src` (dossier racine du service) — c'est là que le bot
écrira son dossier `auth_info`.

### Étape 4 — Connexion WhatsApp

1. Ouvrez https://brixbot-backend.onrender.com/admin → onglet **WhatsApp**.
2. Un **QR Code** doit s'afficher (actualisé toutes les ~60 s).
3. Scannez-le avec **WhatsApp → Appareils connectés**.
4. Le statut passe à « ✅ Connecté ».

> Si aucun QR n'apparaît, vérifiez les logs du service bot dans Render
> (le QR PNG est envoyé au backend à chaque génération).

---

## 🛠️ Commandes utiles

```bash
# Backend en local
cd backend && python app.py

# Bot en local
cd whatsapp-bot && npm start

# Test rapide de l'API Flask (sans démarrer de serveur)
python smoke_test.py

# Docker
docker compose up --build
docker compose logs -f bot
```

---

## 💬 Comportement du bot

**Priorité des messages reçus :**

1. `.search` ou `.tel` → recherche BrixHub (par nom ou par numéro)
2. `.ia` → gestion de la whitelist IA (réservée au propriétaire `OWNER_NUMBER`)
3. Sinon, si l'**IA est activée** **et** que la conversation est autorisée → GROQ (en français)
4. Sinon, si la **réponse automatique** est activée → message d'aide par défaut
5. Sinon → message ignoré

**Commandes disponibles :**

| Commande | Description |
|---|---|
| `.search nom [prénom] [ville]` | Recherche par nom sur BrixHub |
| `.tel 06 12 34 56 78` | Recherche par numéro de téléphone |
| `.ia oui` / `.ia non` | Active/désactive l'IA pour cette conversation (propriétaire) |
| `.ia liste` | Liste les conversations où l'IA est active |
| `.rev` (réponse à une photo ou légende `.rev`) | Recherche d'images inversée : upload anonyme (catbox.moe) + recherche Yandex des images similaires (titre + source), puis liens Google Lens / Yandex pour creuser. Gratuit, sans clé (`requests` seul) |

**Mémoire IA :** l'IA garde le contexte de chaque utilisateur (nombre d'échanges
réglable dans le panneau, onglet IA). **Whitelist IA :** vide = l'IA répond à tout
le monde ; sinon, elle ne répond que dans les conversations listées (numéros ou
identifiants de groupe, réglables dans le panneau ou via `.ia oui`).

**Commandes cachées** (présentes mais absentes du `.help`) :

| Commande | Description |
|---|---|
| `.nsfw [nombre] tags` | Envoie des images rule34.xxx (contenu adulte). Ex : `.nsfw 3 neko`, `.nsfw cat_girl blue_eyes` (espaces = +, `_` dans un tag) — parsing HTML direct (`requests` seul, aucune dépendance lourde) |
| `.xxx [nombre] mots` | Comme `.nsfw` mais avec des **photos réelles** : images pornpics.com (CDN cdni, pleine résolution 1280). Ex : `.xxx 3 big ass` — parsing HTML direct (`requests` seul, aucune dépendance lourde) |

**Formatage des réponses `.search` :**

```
🔍 *Recherche : Dupont Jean (Paris)*

*1. Jean Dupont* ⭐⭐⭐⭐⭐
📍 Paris
📧 jean.dupont@email.com
📱 0612345678
────────────────────
📊 3 résultat(s) · ⚡ 45 ms
```

**Recherche flexible :** si aucun résultat n'est trouvé, le bot réessaie
automatiquement avec moins de critères (sans ville, sans prénom, puis nom seul).

---

## 🔒 Sécurité

- Les clés API sont stockées côté backend uniquement (base + variables
  d'environnement), jamais exposées au bot ni au navigateur.
- **Panneau protégé par mot de passe** : définissez `ADMIN_PASSWORD` pour
  verrouiller l'accès au panel et à toutes ses API (session + cookie signé).
  Sans ce mot de passe, le panneau est accessible à tous — indispensable dès
  que le service est déployé publiquement.
- Toutes les routes internes bot ↔ backend exigent l'en-tête `X-Bot-Key`
  (clé partagée).
- **Rate limiting** sur l'API Flask (par IP) pour éviter les abus.
- Validation des entrées (max résultats borné à 1-100, températures bornées).
- En-têtes de sécurité de base (`X-Frame-Options`, `nosniff`…).
- Échappement HTML dans le panneau (anti-XSS).
- `auth_info/` (session WhatsApp) est ignoré par git.

---

## 🧰 Dépannage

| Problème                          | Solution                                                                 |
|-----------------------------------|--------------------------------------------------------------------------|
| Aucun QR Code affiché             | Vérifiez que le bot tourne et que `FLASK_INTERNAL_URL` pointe bien vers le backend. Consultez les logs du bot. |
| QR Code scanné mais rien ne se passe | Patientez 10-20 s (sync initiale). Vérifiez les logs. Si le code est `loggedOut`, scandez à nouveau. |
| `401` sur les appels internes     | `BOT_API_KEY` diffère entre backend et bot → alignez-les.                |
| L'IA répond « clé manquante »     | Renseignez la clé GROQ dans l'onglet IA (ou `GROQ_API_KEY`).             |
| Erreur 404 modèle GROQ            | Le modèle sélectionné est déprécié → choisissez-en un actif dans l'onglet IA. |
| Bot déconnecté en boucle          | Session corrompue → supprimez `whatsapp-bot/auth_info/` et rescandez le QR. |
| `Key used already` / `Bad MAC` en boucle | **Deux instances du bot tournent en même temps** (même session partagée). Fermez tout avec `arreter-bot.bat`, puis relancez **une seule fois** `demarrer-bot.bat`. Le lanceur bloque désormais les doubles lancements. |
| Bot rejeté par WhatsApp           | WhatsApp peut bannir les comptes utilisés avec des bots. Utilisez un numéro dédié. |
| Base perdue sur Render            | Le disque persistant n'est pas monté (ou plan gratuit) → ajoutez le Persistent Disk. |

---

## 📜 Licence

Projet personnel — libre de réutilisation. **Respectez les CGU de WhatsApp,
BrixHub et GROQ lors de l'utilisation.**
