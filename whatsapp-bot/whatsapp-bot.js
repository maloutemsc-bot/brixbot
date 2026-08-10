/**
 * whatsapp-bot.js — Bot WhatsApp basé sur @whiskeysockets/baileys.
 *
 * Rôles :
 *   - Connecte un compte WhatsApp (session persistante dans auth_info/)
 *   - Génère un QR code (terminal + envoi au backend pour le panneau)
 *   - Transmet chaque message reçu au backend Flask via l'API /api/message
 *   - Envoie la réponse reçue sur WhatsApp
 *   - Expose un petit serveur Express interne (health + redémarrage)
 *
 * Variables d'environnement (voir .env.example) :
 *   FLASK_INTERNAL_URL  URL du backend Flask (ex: http://localhost:5000)
 *   BOT_API_KEY         clé partagée (identique à celle du backend)
 *   BOT_PORT            port du serveur interne Express (défaut : 3000)
 *   AUTH_DIR            dossier de session WhatsApp (défaut : auth_info)
 */

require('dotenv').config();

const fs = require('fs');
const path = require('path');
const express = require('express');
const axios = require('axios');
const pino = require('pino');
const qrcodeTerminal = require('qrcode-terminal');
const QRCode = require('qrcode');

// sharp (conversion d'images en stickers) est OPTIONNEL : les binaires natifs
// ne sont pas installables sur certains environnements (ex: Termux/Android).
// La commande .sticker est alors désactivée proprement au lieu de crasher.
let sharp = null;
try {
  sharp = require('sharp');
} catch (_) {
  console.warn('⚠️ sharp indisponible : la commande .sticker sera désactivée (environnement sans binaires natifs).');
}
// Téléchargement YouTube via yt-dlp (binaire Python) : ytdl-core ne peut plus
// décrypter les URLs de flux de YouTube actuel ("Could not parse decipher").
// yt-dlp est toujours à jour et gère vidéo ET audio sans clé API.
const { execFile } = require('child_process');

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
} = require('@whiskeysockets/baileys');

/* -------------------------------------------------------------------------- */
/*  Filtre du bruit de décryptage (Baileys / libsignal)                       */
/* -------------------------------------------------------------------------- */

// Les erreurs "Failed to decrypt", "MessageCounterError" et "Bad MAC" sont
// bénignes une fois qu'une seule instance tourne (messages redélivrés par
// WhatsApp, ancien client encore connecté…). On n'affiche la première qu'une
// seule fois pour ne pas noyer le terminal, sans cacher les vraies erreurs.
let decryptNoiseShown = false;
function isDecryptNoise(...args) {
  return args.some((a) => typeof a === 'string'
    && /decrypt message|MessageCounterError|Bad MAC|session_cipher|closing (open )?session|prekey bundle|incoming prekey/i.test(a));
}const _origError = console.error;
const _origWarn = console.warn;
console.error = (...args) => {
  if (isDecryptNoise(...args)) {
    if (!decryptNoiseShown) {
      decryptNoiseShown = true;
      _origError('ℹ️ Bruit de décryptage ignoré (messages redélivrés par WhatsApp — normal).');
    }
    return;
  }
  _origError(...args);
};
console.warn = (...args) => {
  if (isDecryptNoise(...args)) {
    if (!decryptNoiseShown) {
      decryptNoiseShown = true;
      _origWarn('ℹ️ Bruit de décryptage ignoré (messages redélivrés par WhatsApp — normal).');
    }
    return;
  }
  _origWarn(...args);
};
const _origInfo = console.info;
console.info = (...args) => {
  if (isDecryptNoise(...args)) {
    // dump de session libsignal ("Closing session: SessionEntry {...}") : ignoré
    if (!decryptNoiseShown) {
      decryptNoiseShown = true;
      _origInfo('ℹ️ Bruit de décryptage ignoré (messages redélivrés par WhatsApp — normal).');
    }
    return;
  }
  _origInfo(...args);
};

const FLASK_URL = (process.env.FLASK_INTERNAL_URL || 'http://localhost:5000').replace(/\/+$/, '');
const BOT_PORT = parseInt(process.env.BOT_PORT || '3000', 10);
const BOT_API_KEY = process.env.BOT_API_KEY || 'changez-moi-bot';
const AUTH_DIR = process.env.AUTH_DIR || 'auth_info';
const LOG_LEVEL = process.env.LOG_LEVEL || 'silent';
// Fichier du journal de conversations (transcript).
// Désactivable : TRANSCRIPT_ENABLED=0
const TRANSCRIPT_ENABLED = process.env.TRANSCRIPT_ENABLED !== '0';
const TRANSCRIPT_FILE = path.join(__dirname, process.env.TRANSCRIPT_FILE || 'transcript.txt');

// Dossier temporaire pour les médias téléchargés (.sticker / .yt)
const MEDIA_DIR = path.join(__dirname, 'media_cache');
try { fs.mkdirSync(MEDIA_DIR, { recursive: true }); } catch (_) { /* non bloquant */ }
// Durées maximales pour .yt (vidéo) et .audio (audio seul). WhatsApp limite la
// taille des vidéos (~60 Mo) : on limite donc la vidéo à 10 min (360p ≈ 3 Mo/min)
// tandis que l'audio peut rester plus long.
const YT_VIDEO_MAX_SECONDS = 600;
const YT_AUDIO_MAX_SECONDS = 3600;
const YT_MAX_BYTES = 60 * 1024 * 1024; // garde-fou taille fichier (env. limite WhatsApp)
const YT_DOWNLOAD_TIMEOUT_MS = 300000; // 5 min max par téléchargement

// Commande yt-dlp résolue au premier usage (exe ou python -m yt_dlp).
let ytDlpCmd = null; // [commande, ...args] ou null si introuvable
function resolveYtDlp() {
  if (ytDlpCmd) return ytDlpCmd;
  if (process.env.YTDLP_PATH) {
    ytDlpCmd = [process.env.YTDLP_PATH];
    return ytDlpCmd;
  }
  // yt-dlp dans le PATH (installation pip globale) — souvent yt-dlp.exe
  ytDlpCmd = ['yt-dlp'];
  return ytDlpCmd;
}

/** Supprime les fichiers yt-<préfixe>* restés dans media_cache (partiels/échecs). */
function cleanupYtPrefix(prefix) {
  try {
    for (const f of fs.readdirSync(MEDIA_DIR)) {
      if (f.startsWith(prefix)) {
        fs.unlink(path.join(MEDIA_DIR, f), () => {});
      }
    }
  } catch (_) { /* non bloquant */ }
}

// Cache des données de langue pour l'OCR (.ocr — tesseract.js).
// Les données fra.traineddata sont téléchargées une seule fois puis réutilisées.
const TESS_CACHE = path.join(__dirname, 'tessdata-cache');
try { fs.mkdirSync(TESS_CACHE, { recursive: true }); } catch (_) { /* non bloquant */ }
// Limite de longueur du texte synthétisé (.tts — contrainte Google TTS)
const TTS_MAX_CHARS = 200;
// Taille maximale d'un texte à résumer (.resume — contrainte du backend)
const RESUME_MAX_CHARS = 6000;

// Langues supportées par .translate (code ISO → nom français + drapeau).
// Le service de traduction est Google Translate (gratuit, sans clé), le même
// que celui du backend (.traduis).
const TRANSLATE_LANGS = {
  fr: { name: 'Français', flag: '🇫🇷' },
  en: { name: 'Anglais', flag: '🇬🇧' },
  es: { name: 'Espagnol', flag: '🇪🇸' },
  de: { name: 'Allemand', flag: '🇩🇪' },
  it: { name: 'Italien', flag: '🇮🇹' },
  pt: { name: 'Portugais', flag: '🇵🇹' },
  nl: { name: 'Néerlandais', flag: '🇳🇱' },
  ru: { name: 'Russe', flag: '🇷🇺' },
  ar: { name: 'Arabe', flag: '🇸🇦' },
  zh: { name: 'Chinois', flag: '🇨🇳' },
  ja: { name: 'Japonais', flag: '🇯🇵' },
  ko: { name: 'Coréen', flag: '🇰🇷' },
  tr: { name: 'Turc', flag: '🇹🇷' },
  pl: { name: 'Polonais', flag: '🇵🇱' },
  el: { name: 'Grec', flag: '🇬🇷' },
  sv: { name: 'Suédois', flag: '🇸🇪' },
  no: { name: 'Norvégien', flag: '🇳🇴' },
  da: { name: 'Danois', flag: '🇩🇰' },
  fi: { name: 'Finnois', flag: '🇫🇮' },
  cs: { name: 'Tchèque', flag: '🇨🇿' },
  hu: { name: 'Hongrois', flag: '🇭🇺' },
  ro: { name: 'Roumain', flag: '🇷🇴' },
  uk: { name: 'Ukrainien', flag: '🇺🇦' },
  vi: { name: 'Vietnamien', flag: '🇻🇳' },
  id: { name: 'Indonésien', flag: '🇮🇩' },
  th: { name: 'Thaïlandais', flag: '🇹🇭' },
  hi: { name: 'Hindi', flag: '🇮🇳' },
  he: { name: 'Hébreu', flag: '🇮🇱' },
  sw: { name: 'Swahili', flag: '🇰🇪' },
};
// Limite de caractères par traduction (.translate — contrainte du service)
const TRANSLATE_MAX_CHARS = 4000;
// Alias anglais des langues (saisie naturelle : ".translate english")
const TRANSLATE_EN_ALIASES = {
  english: 'en', spanish: 'es', german: 'de', italian: 'it', portuguese: 'pt',
  dutch: 'nl', russian: 'ru', arabic: 'ar', chinese: 'zh', japanese: 'ja',
  korean: 'ko', turkish: 'tr', polish: 'pl', greek: 'el', swedish: 'sv',
  norwegian: 'no', danish: 'da', finnish: 'fi', czech: 'cs', hungarian: 'hu',
  romanian: 'ro', ukrainian: 'uk', vietnamese: 'vi', indonesian: 'id',
  thai: 'th', hindi: 'hi', hebrew: 'he', swahili: 'sw', french: 'fr',
};

// Membres muets par groupe (persistés dans mute-store.json) : les messages
// d'un membre muet sont supprimés automatiquement (le bot doit être admin).
const MUTE_STORE = path.join(__dirname, 'mute-store.json');
let mutedMap = {}; // { [groupeJid]: { [participantJid]: true } }
function loadMuteStore() {
  try {
    const parsed = JSON.parse(fs.readFileSync(MUTE_STORE, 'utf8'));
    mutedMap = (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : {};
  } catch (_) { mutedMap = {}; }
}
function saveMuteStore() {
  try { fs.writeFileSync(MUTE_STORE, JSON.stringify(mutedMap)); } catch (_) { /* non bloquant */ }
}
loadMuteStore();

// État persistant des fonctionnalités (avertissements, bienvenue, anti-lien, AFK).
const BOT_STORE = path.join(__dirname, 'bot-store.json');
let botState = { warns: {}, welcome: {}, antilink: {}, afk: {} };
function loadBotStore() {
  try {
    const parsed = JSON.parse(fs.readFileSync(BOT_STORE, 'utf8'));
    botState = (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : {};
  } catch (_) { botState = {}; }
  botState.warns = botState.warns || {};
  botState.welcome = botState.welcome || {};
  botState.antilink = botState.antilink || {};
  botState.afk = botState.afk || {};
}
function saveBotStore() {
  try { fs.writeFileSync(BOT_STORE, JSON.stringify(botState)); } catch (_) { /* non bloquant */ }
}
loadBotStore();

const logger = pino({ level: LOG_LEVEL });

/* -------------------------------------------------------------------------- */
/*  Journal de débogage (bot-messages.log)                                    */
/* -------------------------------------------------------------------------- */

// Trace chaque message reçu et son traitement : indispensable pour comprendre
// pourquoi "le message est arrivé mais le bot n'a rien répondu". Le fichier est
// tronqué automatiquement au-delà de 2 Mo pour ne jamais grossir indéfiniment.
const MSG_LOG = path.join(__dirname, 'bot-messages.log');
function debugLog(line) {
  try {
    if (fs.existsSync(MSG_LOG) && fs.statSync(MSG_LOG).size > 2 * 1024 * 1024) {
      fs.writeFileSync(MSG_LOG, '');
    }
    fs.appendFileSync(MSG_LOG, `${new Date().toISOString()} ${line}\n`);
  } catch (_) { /* le journal ne doit jamais bloquer le bot */ }
}

/* -------------------------------------------------------------------------- */
/*  Journal des conversations (transcript.txt)                                 */
/* -------------------------------------------------------------------------- */

// Le transcript enregistre TOUS les messages échangés au format lisible :
//     [2026-08-08 17:45] Marie : Bonjour !
//     [2026-08-08 17:45] 🤖 BrixBot : Salut Marie !
// Le fichier est tronqué automatiquement au-delà de 5 Mo.
function transcriptLog(line) {
  if (!TRANSCRIPT_ENABLED) return;
  try {
    if (fs.existsSync(TRANSCRIPT_FILE) && fs.statSync(TRANSCRIPT_FILE).size > 5 * 1024 * 1024) {
      fs.writeFileSync(TRANSCRIPT_FILE, '');
    }
    fs.appendFileSync(TRANSCRIPT_FILE, `${line}\n`, 'utf8');
  } catch (_) { /* le journal ne doit jamais bloquer le bot */ }
}

// Cache du nom des groupes (récupérés via groupMetadata, coûteux en réseau)
const groupNameCache = new Map();

/**
 * Formate une date en heure locale lisible : "2026-08-08 17:45".
 */
function fmtStamp(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * Nom lisible d'un numéro (jid) : contact ou numéro brut.
 */
function contactLabel(jid) {
  const local = (jid || '').split('@')[0].split(':')[0];
  if (!local) return 'Inconnu';
  if (/^\d+$/.test(local)) return local; // numéro
  return local; // identifiant non numérique (ex: groupe)
}

/**
 * Résout le nom affiché d'un expéditeur :
 *   - en groupe : nom du groupe (groupMetadata, mis en cache)
 *   - en privé   : pushName du contact (nom qu'il s'est donné sur WhatsApp)
 *   - sinon      : numéro de téléphone
 */
async function resolveSenderName(remoteJid, sender, pushName) {
  const isGroup = remoteJid && remoteJid.endsWith('@g.us');
  if (isGroup) {
    if (groupNameCache.has(remoteJid)) return groupNameCache.get(remoteJid);
    if (sock && typeof sock.groupMetadata === 'function') {
      try {
        const meta = await sock.groupMetadata(remoteJid);
        const subject = meta?.subject || contactLabel(remoteJid);
        groupNameCache.set(remoteJid, subject);
        return subject;
      } catch (_) { /* groupe inaccessible : on retombe sur le numéro */ }
    }
    groupNameCache.set(remoteJid, contactLabel(remoteJid));
    return contactLabel(remoteJid);
  }
  return (pushName && String(pushName).trim()) || contactLabel(sender || remoteJid);
}

let sock = null;
let currentStatus = 'disconnected'; // disconnected | connecting | qr | connected
let startPending = false;          // un socket est en cours de création
let reconnectTimer = null;         // un seul timer de reconnexion à la fois
let restartRequested = false;      // redémarrage demandé par le backend
let lastQr = null;                 // dernier QR généré (re-envoyé si besoin)
let lastNotifyErrorAt = 0;         // anti-spam : 1 log d'erreur backend / 30 s max

/**
 * Planifie une reconnexion. L'ancien timer (s'il existe) est annulé :
 * il ne peut donc jamais y avoir deux reconnexions simultanées.
 */
function scheduleReconnect(delayMs) {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    startPending = false;
    startBot();
  }, delayMs);
}

/* -------------------------------------------------------------------------- */
/*  Communication avec le backend Flask                                       */
/* -------------------------------------------------------------------------- */

/**
 * Envoie l'état de connexion WhatsApp au backend (panneau d'administration).
 * Les erreurs ne sont journalisées qu'au maximum une fois toutes les 30 s :
 * le backend peut démarrer plus tard que le bot (course au lancement).
 */
async function notifyBackend(status, extra = {}) {
  try {
    await axios.post(`${FLASK_URL}/api/whatsapp/status`, { status, ...extra }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 8000,
    });
    lastNotifyErrorAt = 0; // succès : on réarme la journalisation d'erreur
  } catch (err) {
    const now = Date.now();
    if (now - lastNotifyErrorAt > 30000) {
      lastNotifyErrorAt = now;
      console.error(`[backend] Impossible d'envoyer le statut "${status}" : ${err.message}`);
    }
  }
}

/**
 * Pulsation périodique : re-envoie l'état toutes les 10 s. Ainsi, même si le
 * backend a démarré APRÈS le bot (ou a été redémarré entre-temps), le panneau
 * repasse automatiquement à jour : statut connecté, numéro, ou QR en cache.
 */
setInterval(() => {
  if (currentStatus === 'connected' && sock) {
    notifyBackend('connected', { number: sock.user?.id || null });
  } else if (currentStatus === 'qr' && lastQr) {
    notifyBackend('qr', { qr: lastQr });
  }
}, 10000);

/* -------------------------------------------------------------------------- */
/*  Connexion WhatsApp (Baileys)                                              */
/* -------------------------------------------------------------------------- */

/**
 * Démarre (ou redémarre) la connexion WhatsApp.
 * La garde startPending garantit qu'un seul socket est actif à la fois.
 */
async function startBot() {
  if (startPending) return;  // un démarrage est déjà en cours
  if (sock) return;          // un socket est déjà actif — JAMAIS deux
  startPending = true;
  console.log('🤖 Démarrage du bot WhatsApp…');
  try {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();
    console.log(`📦 Version Baileys : ${version.join('.')}`);

    sock = makeWASocket({
      version,
      logger,
      printQRInTerminal: false,
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      // Traite TOUS les types d'historique (INITIAL_BOOTSTRAP, RECENT, FULL,
      // ON_DEMAND). Par défaut Baileys ignore le FULL history : le téléphone le
      // renvoie pourtant au démarrage → on le collecte pour .clear (sans ça, la
      // purge ne voyait que la session courante, soit ~8 messages).
      shouldSyncHistoryMessage: () => true,
    });

    // Sauvegarde automatique des identifiants de session
    sock.ev.on('creds.update', saveCreds);

    // Événements de connexion : QR code, ouverture, fermeture
    sock.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        currentStatus = 'qr';
        debugLog('[connexion] QR code généré');
        console.log('\n📱 Scannez le QR code ci-dessous pour connecter WhatsApp :\n');
        try {
          qrcodeTerminal.generate(qr, { small: true });
        } catch (err) {
          console.error('Erreur d\'affichage du QR dans le terminal :', err.message);
        }
        // Génère une image PNG et l'envoie au backend pour le panneau.
        // L'image est mise en cache : si le backend redémarre, la pulsation
        // périodique pourra la renvoyer au panneau.
        try {
          const dataUrl = await QRCode.toDataURL(qr, {
            width: 400,
            margin: 2,
            errorCorrectionLevel: 'L',
          });
          lastQr = dataUrl;
          await notifyBackend('qr', { qr: dataUrl });
        } catch (err) {
          console.error('Erreur de génération du QR PNG :', err.message);
        }
      }

      if (connection === 'open') {
        currentStatus = 'connected';
        lastQr = null; // plus besoin du QR
        debugLog(`[connexion] ouverte : ${sock.user?.id || 'inconnu'}`);
        console.log('✅ WhatsApp connecté :', sock.user?.id || 'inconnu');
        await notifyBackend('connected', { number: sock.user?.id || null });
      } else if (connection === 'connecting') {
        currentStatus = 'connecting';
        debugLog('[connexion] en cours…');
        console.log('⏳ Connexion en cours…');
      } else if (connection === 'close') {
        currentStatus = 'disconnected';
        const code = lastDisconnect?.error?.output?.statusCode;
        const loggedOut = code === DisconnectReason.loggedOut;
        debugLog(`[connexion] fermée (code ${code})`);
        console.log(`❌ Connexion fermée (code ${code})`);
        sock = null; // plus aucun socket actif
        // Non bloquant : ne jamais retarder la reconnexion par un appel au backend.
        notifyBackend('disconnected', { reason: code });

        if (restartRequested) {
          // Redémarrage demandé par le panneau : reconnexion immédiate
          restartRequested = false;
          console.log('🔄 Redémarrage : reconnexion dans 1 seconde…');
          scheduleReconnect(1000);
        } else if (loggedOut) {
          console.log('🚪 Session déconnectée : un nouveau QR code sera nécessaire.');
          console.log('   Supprimez le dossier auth_info/ puis relancez pour re-scanner.');
        } else {
          console.log('🔄 Reconnexion automatique dans 3 secondes…');
          scheduleReconnect(3000);
        }
      }
    });

    // Historique de messages reçu au démarrage (ou via on-demand) : on collecte
    // les clés EN CONTINU pour que .clear puisse purger bien plus que la
    // session courante. WhatsApp envoie l'historique au moment de la connexion
    // (pas pendant la commande) : un écouteur temporaire dans .clear ne voyait
    // presque rien → d'où le "8 messages" au lieu de l'historique complet.
    sock.ev.on('messaging-history.set', (data) => {
      const msgs = (data && Array.isArray(data.messages)) ? data.messages : [];
      for (const m of msgs) {
        const k = m && m.key;
        if (!k || !k.remoteJid || !k.id || k.remoteJid === 'status@broadcast') continue;
        if (!historyChatKeys[k.remoteJid]) historyChatKeys[k.remoteJid] = [];
        // Dédup par id : on remplace s'il existe déjà, sinon on ajoute
        const arr = historyChatKeys[k.remoteJid];
        const idx = arr.findIndex((x) => x.id === k.id);
        const entry = { ...k, messageTimestamp: m.messageTimestamp };
        if (idx !== -1) arr[idx] = entry;
        else if (arr.length < CLEAR_MAX) arr.push(entry);
      }
    });

    // Messages entrants
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
      debugLog(`[upsert] type=${type} nb=${messages.length}`);
      // Collecte des clés pour la commande .clear (en mémoire, session courante).
      // Faite pour TOUS les types (y compris les échos de nos propres messages)
      // afin que .clear puisse aussi supprimer les messages du bot.
      for (const msg of messages) {
        const mkey = msg.key || {};
        if (mkey.remoteJid && mkey.id && mkey.remoteJid !== 'status@broadcast') {
          if (!chatKeys[mkey.remoteJid]) chatKeys[mkey.remoteJid] = [];
          if (chatKeys[mkey.remoteJid].length < CLEAR_MAX) chatKeys[mkey.remoteJid].push(mkey);
        }
      }
      if (type !== 'notify') return;
      for (const msg of messages) {
        handleMessage(msg).catch((err) => {
          debugLog(`[ERREUR] traitement du message : ${err.message}`);
          console.error('[message] Erreur :', err.message);
        });
      }
    });

    // Accueil automatique des nouveaux membres (.welcome)
    sock.ev.on('group-participants.update', async (update) => {
      const { id: groupJid, participants, action } = update;
      if (action !== 'add' || !participants || !participants.length) return;
      const welcomeText = botState.welcome[jidLocal(groupJid)];
      if (!welcomeText) return;
      try {
        await sock.sendMessage(groupJid, { text: welcomeText, mentions: participants });
        debugLog(`[welcome] accueil envoyé (${participants.length} membre(s))`);
      } catch (err) {
        debugLog(`[welcome] envoi impossible : ${err.message}`);
      }
    });

    startPending = false; // le socket est prêt
    return sock;
  } catch (err) {
    startPending = false;
    sock = null;
    console.error('💥 Erreur au démarrage :', err.message);
    scheduleReconnect(5000); // nouvelle tentative
  }
}

/* -------------------------------------------------------------------------- */
/*  Traitement des messages                                                   */
/* -------------------------------------------------------------------------- */

/**
 * Extrait le texte d'un message WhatsApp (conversation, extendedText, légendes…).
 */
function extractText(msg) {
  const m = msg.message || {};
  if (m.conversation) return m.conversation;
  if (m.extendedTextMessage?.text) return m.extendedTextMessage.text;
  if (m.imageMessage?.caption) return m.imageMessage.caption;
  if (m.videoMessage?.caption) return m.videoMessage.caption;
  if (m.buttonsResponseMessage?.selectedButtonId) return m.buttonsResponseMessage.selectedButtonId;
  return null;
}

/**
 * Traite un message : l'envoie au backend Flask puis renvoie la réponse.
 */
// Étiquette lisible pour un message sans texte (photo, vidéo, audio…)
const MEDIA_LABELS = {
  imageMessage: '📷 [photo]',
  videoMessage: '🎬 [vidéo]',
  audioMessage: '🎵 [audio]',
  stickerMessage: '🖼 [sticker]',
  documentMessage: '📄 [document]',
  locationMessage: '📍 [localisation]',
  contactMessage: '👤 [contact]',
  reactionMessage: '👍 [réaction]',
};

/**
 * Journalise un message échangé dans le transcript.
 *
 * Le nom du contact est résolu en tâche de fond (groupMetadata peut prendre
 * quelques centaines de ms) : on n'attend JAMAIS cette résolution pour
 * continuer le traitement du message.
 */
async function transcriptMessage(msg, key, body) {
  if (!TRANSCRIPT_ENABLED || !key?.remoteJid || key.remoteJid === 'status@broadcast') return;
  const rawType = Object.keys(msg.message || {})[0] || '?';
  const remoteJid = key.remoteJid;
  const sender = key.participant || remoteJid;
  const stamp = fmtStamp(new Date());

  let content;
  if (body && body.trim()) {
    content = body.trim();
  } else {
    content = MEDIA_LABELS[rawType] || (rawType !== '?' ? `📎 [${rawType}]` : '[message sans texte]');
  }

  if (key.fromMe) {
    transcriptLog(`[${stamp}] 🤖 BrixBot : ${content}`);
    return;
  }
  // Nom résolu de manière asynchrone (ne bloque jamais le flux)
  const who = await resolveSenderName(remoteJid, sender, msg.pushName);
  transcriptLog(`[${stamp}] ${who} : ${content}`);
}

async function handleMessage(msg) {
  const key = msg.key || {};
  const rawType = Object.keys(msg.message || {})[0] || '?';
  const body = extractText(msg);
  debugLog(`[recu] fromMe=${!!key.fromMe} jid=${key.remoteJid || '?'} `
    + `participant=${key.participant || ''} type=${rawType} `
    + `corps="${(body || '').slice(0, 100).replace(/\n/g, ' ')}"`);

  const remoteJid = key.remoteJid;
  // Expéditeur réel : pour un message envoyé depuis le compte lié (fromMe),
  // c'est le numéro du bot lui-même (l'utilisateur) — sinon le participant.
  // Ceci permet aux commandes propriétaire (.blacklist, .stats…) d'être
  // reconnues quand l'utilisateur envoie depuis son propre WhatsApp.
  const sender = key.fromMe
    ? (sock?.user?.id || key.participant || remoteJid)
    : (key.participant || remoteJid);

  // Journal de conversations : enregistre TOUT message échangé (reçu ou
  // envoyé), même ceux que le backend décidera d'ignorer. L'écriture est
  // lancée sans attendre (fire-and-forget) pour ne pas ralentir le bot.
  transcriptMessage(msg, key, body).catch(() => {});

  // Les statuts WhatsApp sont ignorés
  if (key.remoteJid === 'status@broadcast') return;

  const trimmed = (body || '').trim();

  // AFK : l'expéditeur était absent → il est de retour (statut effacé).
  // Placé AVANT le garde-fou fromMe : même le propriétaire voit son AFK
  // s'effacer en écrivant un message normal. La réponse du bot est fromMe
  // sans commande → ignorée par le garde-fou, aucune boucle possible.
  const senderLocal = jidLocal(sender);
  if (botState.afk[senderLocal]) {
    delete botState.afk[senderLocal];
    saveBotStore();
    if (sock) {
      sock.sendMessage(remoteJid, { text: `👋 ${contactLabel(sender)} est de retour !` }, { quoted: msg }).catch(() => {});
    }
  }

  // Messages envoyés depuis le compte lié (fromMe) : on ne traite QUE les
  // commandes (qui commencent par ".") afin que l'utilisateur puisse utiliser
  // le bot depuis son propre WhatsApp. Les réponses du bot (texte normal)
  // restent ignorées → aucune boucle possible.
  if (key.fromMe && !trimmed.startsWith('.')) return;

  const isGroup = remoteJid.endsWith('@g.us');

  // Membre muet (mode admin) : son message est supprimé immédiatement.
  // La suppression d'un message exige que le bot soit administrateur.
  if (isGroup && !key.fromMe && isMuted(remoteJid, key.participant)) {
    debugLog(`[mute] suppression du message de ${contactLabel(key.participant)}`);
    if (sock) {
      sock.sendMessage(remoteJid, {
        delete: { remoteJid, id: key.id, participant: key.participant },
      }).catch((err) => debugLog(`[mute] suppression impossible : ${err.message}`));
    }
    return; // un membre muet ne déclenche rien d'autre
  }

  // Anti-lien : les liens envoyés par les non-admins sont supprimés
  if (isGroup && !key.fromMe && botState.antilink[jidLocal(remoteJid)]
    && trimmed && /(https?:\/\/|www\.|chat\.whatsapp\.com|wa\.me\/)/i.test(trimmed)) {
    if (!(await isGroupAdmin(remoteJid, sender))) {
      debugLog(`[antilink] lien supprimé de ${contactLabel(sender)}`);
      if (sock) {
        sock.sendMessage(remoteJid, {
          delete: { remoteJid, id: key.id, participant: key.participant },
        }).catch((err) => debugLog(`[antilink] suppression impossible : ${err.message}`));
        sock.sendMessage(remoteJid, { text: `🔗 Les liens sont interdits ici, ${contactLabel(sender)}.` }, { quoted: msg }).catch(() => {});
      }
      return;
    }
  }

  // Marque le message comme lu
  if (sock) sock.readMessages([key]).catch(() => {});

  // Signale si on mentionne / écrit à un membre absent (AFK)
  maybeNotifyAfk(msg, remoteJid, sender, isGroup).catch((err) => {
    debugLog(`[afk] erreur de notification : ${err.message}`);
  });

  // --- Note vocale : transcription automatique (Whisper) puis réponse IA ---
  // (vérifiée AVANT le garde-fou "message sans texte" : un vocal n'a pas de
  // corps de message mais doit quand même être traité)
  if (rawType === 'audioMessage' && msg.message.audioMessage?.ptt && !trimmed) {
    debugLog('[vocal] note vocale reçue → transcription…');
    handleVoiceNote(msg, key, remoteJid, sender).catch((err) => {
      debugLog(`[vocal] erreur globale : ${err.message}`);
    });
    return;
  }

  // Messages sans texte (photos, vidéos, documents…) : rien à traiter
  if (!trimmed) return;

  // --- Commandes médias gérées directement par le bot (.sticker / .yt / .extract) ---
  if (/^\.(sticker|yt|audio|extract)\b/i.test(trimmed)) {
    debugLog(`[media] commande locale : ${trimmed}`);
    handleMediaCommand(msg, key, trimmed).catch((err) => {
      debugLog(`[media] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .meta : métadonnées du message cité ---
  if (/^\.meta\b/i.test(trimmed)) {
    debugLog(`[meta] commande : ${trimmed}`);
    handleMetaCommand(msg, remoteJid).catch((err) => {
      debugLog(`[meta] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .json : message cité en JSON brut (mode débogage) ---
  if (/^\.json\b/i.test(trimmed)) {
    debugLog(`[json] commande : ${trimmed}`);
    handleJsonCommand(msg, remoteJid).catch((err) => {
      debugLog(`[json] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .id : identifiants de la conversation ---
  if (/^\.id\b/i.test(trimmed)) {
    debugLog(`[id] commande : ${trimmed}`);
    handleIdCommand(msg, remoteJid, sender).catch((err) => {
      debugLog(`[id] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .tts : synthèse vocale ---
  if (/^\.(?:tts|voice)\b/i.test(trimmed)) {
    debugLog(`[tts] commande : ${trimmed}`);
    handleTtsCommand(msg, remoteJid, trimmed).catch((err) => {
      debugLog(`[tts] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .ocr : lecture du texte d'une image ---
  if (/^\.ocr\b/i.test(trimmed)) {
    debugLog(`[ocr] commande : ${trimmed}`);
    handleOcrCommand(msg, remoteJid).catch((err) => {
      debugLog(`[ocr] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .translate : traduction vers la langue choisie ---
  if (/^\.translate\b/i.test(trimmed)) {
    debugLog(`[translate] commande : ${trimmed}`);
    handleTranslateCommand(msg, remoteJid, trimmed).catch((err) => {
      debugLog(`[translate] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .correct : correction orthographe/grammaire (IA) ---
  if (/^\.correct\b/i.test(trimmed)) {
    debugLog(`[correct] commande : ${trimmed}`);
    handleCorrectCommand(msg, remoteJid, trimmed).catch((err) => {
      debugLog(`[correct] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .resume : résumé d'un message cité par l'IA ---
  // (?=\s|$) plutôt que \b : \b échoue après un accent (é n'est pas un \w).
  if (/^\.(?:resume|resumé|resumer|résumé)(?:\s|$)/i.test(trimmed)) {
    debugLog(`[resume] commande : ${trimmed}`);
    handleResumeCommand(msg, remoteJid, trimmed).catch((err) => {
      debugLog(`[resume] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .pin : recherche d'images (DuckDuckGo Images + Wikimedia) ---
  if (/^\.pin\b/i.test(trimmed)) {
    debugLog(`[pin] commande : ${trimmed}`);
    handlePinCommand(msg, remoteJid, trimmed).catch((err) => {
      debugLog(`[pin] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande secrète : simulation (à découvrir !) ---
  if (/^\.hack\b/i.test(trimmed)) {
    debugLog(`[secrete] commande : ${trimmed}`);
    handleSecretCommand(remoteJid, msg).catch((err) => {
      debugLog(`[secrete] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .transcript : transcrire la note vocale citée ---
  if (/^\.(transcript|transcrire|transcription)\b/i.test(trimmed)) {
    debugLog(`[transcript] commande : ${trimmed}`);
    handleTranscriptCommand(msg, remoteJid).catch((err) => {
      debugLog(`[transcript] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commandes d'administration de groupe (.kick / .mute / .warn / .welcome…) ---
  if (/^\.(kick|mute|unmute|promote|demote|tagall|link|close|open|warn|unwarn|warns|resetwarn|welcome|antilink|revoke)\b/i.test(trimmed)) {
    debugLog(`[admin] commande : ${trimmed}`);
    handleGroupAdminCommand(msg, remoteJid, sender, trimmed, isGroup).catch((err) => {
      debugLog(`[admin] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commandes utilitaires et fun (.roll / .bin / .quote / .ping / .afk) ---
  if (/^\.(roll|bin|quote|ping|afk)\b/i.test(trimmed)) {
    debugLog(`[fun] commande : ${trimmed}`);
    handleFunCommand(msg, remoteJid, sender, trimmed).catch((err) => {
      debugLog(`[fun] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .clear : purge des messages de la conversation ---
  if (/^\.clear\b/i.test(trimmed)) {
    debugLog(`[clear] commande : ${trimmed}`);
    handleClearCommand(msg, remoteJid, sender, isGroup, trimmed).catch((err) => {
      debugLog(`[clear] erreur : ${err.message}`);
    });
    return;
  }

  // --- Commande .clearmem : efface la mémoire IA de l'utilisateur cité ---
  if (/^\.clearmem\b/i.test(trimmed)) {
    debugLog(`[clearmem] commande : ${trimmed}`);
    handleClearMemCommand(msg, remoteJid, sender).catch((err) => {
      debugLog(`[clearmem] erreur : ${err.message}`);
    });
    return;
  }

  try {
    const { data } = await axios.post(`${FLASK_URL}/api/message`, {
      from: sender,
      remoteJid,
      body: body.trim(),
      isGroup,
      messageId: key.id,
      timestamp: msg.messageTimestamp,
    }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 90000,
    });

    if (data?.ignore) {
      debugLog(`[traite] jid=${remoteJid} → ignoré par le backend`);
      return; // le backend ne veut pas répondre
    }
    if (data?.reply) {
      debugLog(`[traite] jid=${remoteJid} → réponse ${data.reply.length} caractères`);
      await sendChunks(remoteJid, data.reply, msg);
      debugLog(`[envoye] jid=${remoteJid} → réponse envoyée`);
    }
  } catch (err) {
    debugLog(`[ERREUR] jid=${remoteJid} : ${err.message}`);
    console.error('Erreur lors du traitement du message :', err.message);
  }
}

/* -------------------------------------------------------------------------- */
/*  Commandes médias (.sticker / .yt) + transcription des vocaux              */
/* -------------------------------------------------------------------------- */

/**
 * Envoie un message d'erreur propre (en citant le message d'origine).
 */
async function replyError(remoteJid, text, quoted) {
  if (!sock) return;
  try {
    await sock.sendMessage(remoteJid, { text }, { quoted });
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ${text}`);
  } catch (err) {
    debugLog(`[media] envoi du message d'erreur impossible : ${err.message}`);
  }
}

/**
 * Point d'entrée des commandes médias : .sticker et .yt / .audio.
 */
async function handleMediaCommand(msg, key, body) {
  const remoteJid = key.remoteJid;
  if (/^\.sticker\b/i.test(body)) {
    return handleStickerCommand(msg, remoteJid);
  }
  if (/^\.extract\b/i.test(body)) {
    return handleExtractCommand(msg, remoteJid);
  }
  return handleYtCommand(msg, remoteJid, body);
}

/**
 * .sticker — transforme une photo en sticker (512×512, webp).
 *
 * La photo peut être le message courant (photo avec la légende ".sticker")
 * ou le message cité (réponse ".sticker" à une photo).
 */
async function handleStickerCommand(msg, remoteJid) {
  const current = msg.message || {};
  const quoted = current.extendedTextMessage?.contextInfo?.quotedMessage || null;

  if (current.videoMessage || quoted?.videoMessage) {
    return replyError(remoteJid,
      '❌ Pour l\'instant, seules les photos deviennent des stickers. (vidéos non supportées)', msg);
  }

  // sharp absent (ex: Termux/Android) : on refuse proprement au lieu de crasher.
  if (!sharp) {
    return replyError(remoteJid,
      '❌ `.sticker` n\'est pas disponible sur cet appareil '
      + '(bibliothèque d\'images native absente).', msg);
  }

  // Photo du message courant (légende .sticker) ou photo citée
  const source = current.imageMessage
    ? msg
    : (quoted?.imageMessage ? { ...msg, message: quoted } : null);

  if (!source) {
    return replyError(remoteJid,
      '🖼 *Commande .sticker*\n'
      + 'Envoyez une photo avec la légende `.sticker`\n'
      + '— ou —\n'
      + 'RÉPONDEZ à une photo avec `.sticker`.', msg);
  }

  let buffer;
  try {
    buffer = await downloadMediaMessage(source, 'buffer', {});
  } catch (err) {
    debugLog(`[sticker] téléchargement impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de télécharger la photo. Réessayez.', msg);
  }

  try {
    const sticker = await sharp(buffer, { animated: false })
      .resize(512, 512, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
      .webp({ quality: 90 })
      .toBuffer();

    await sock.sendMessage(remoteJid, { sticker }, { quoted: msg });
    debugLog(`[sticker] envoyé (${sticker.length} octets)`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🖼 [sticker créé]`);
  } catch (err) {
    debugLog(`[sticker] conversion impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de convertir cette image en sticker.', msg);
  }
}

/**
 * .extract — renvoie le média CITÉ (image, note vocale, vidéo, document…).
 *
 * Répondez à une image ou à une note vocale avec `.extract` : le bot
 * télécharge le média et le renvoie dans la conversation. Une photo envoyée
 * avec la légende `.extract` est également acceptée. La note vocale est
 * renvoyée telle quelle (ptt conservé).
 */
async function handleExtractCommand(msg, remoteJid) {
  const current = msg.message || {};
  const ctx = current.extendedTextMessage?.contextInfo || {};
  const quoted = ctx.quotedMessage || null;

  // Média source : message courant (photo avec légende .extract) ou message cité.
  // Pour le média cité, on reconstruit une clé correcte (stanzaId + participant) :
  // le téléchargement direct marche tant que l'URL est valide, et la clé propre
  // permet le rafraîchissement via updateMediaMessage (médias anciens / groupes).
  let source;
  if (current.imageMessage) {
    source = msg;
  } else if (quoted) {
    source = {
      ...msg,
      key: {
        ...msg.key,
        remoteJid,
        id: ctx.stanzaId || msg.key.id,
        participant: ctx.participant || msg.key.participant,
      },
      message: quoted,
    };
  } else {
    source = null;
  }

  if (!source) {
    return replyError(remoteJid,
      '📥 *Commande .extract*\n'
      + 'RÉPONDEZ à une image, une note vocale ou une vidéo avec `.extract`\n'
      + '— ou —\n'
      + 'Envoyez une photo avec la légende `.extract`.', msg);
  }

  const sm = source.message || {};
  const image = sm.imageMessage;
  const audio = sm.audioMessage;
  const video = sm.videoMessage;
  const doc = sm.documentMessage;
  const sticker = sm.stickerMessage;

  if (!image && !audio && !video && !doc && !sticker) {
    return replyError(remoteJid,
      '❌ `.extract` fonctionne avec une image, une note vocale, une vidéo, '
      + 'un document ou un sticker.\nRépondez à un média avec `.extract`.', msg);
  }

  // Message de statut pendant le téléchargement (utile pour les gros médias)
  if (sock && (audio || video || doc)) {
    sock.sendMessage(remoteJid, { text: '📥 Extraction du média…' }).catch(() => {});
  }

  let buffer;
  try {
    buffer = await downloadMediaMessage(source, 'buffer', {});
  } catch (err) {
    debugLog(`[extract] téléchargement impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de télécharger le média. Réessayez.', msg);
  }

  try {
    if (image) {
      await sock.sendMessage(remoteJid, { image: buffer, caption: '📥 *Média extrait*' },
        { quoted: msg });
    } else if (audio) {
      // Note vocale (ptt) ou audio normal : on conserve le format d'origine
      await sock.sendMessage(remoteJid, {
        audio: buffer,
        mimetype: audio.mimetype || 'audio/ogg; codecs=opus',
        ptt: audio.ptt !== false,
      }, { quoted: msg });
    } else if (video) {
      await sock.sendMessage(remoteJid, {
        video: buffer,
        caption: '📥 *Média extrait*',
        mimetype: video.mimetype,
      }, { quoted: msg });
    } else if (doc) {
      await sock.sendMessage(remoteJid, {
        document: buffer,
        mimetype: doc.mimetype,
        fileName: doc.fileName || 'fichier',
      }, { quoted: msg });
    } else if (sticker) {
      await sock.sendMessage(remoteJid, { sticker: buffer }, { quoted: msg });
    }
    debugLog(`[extract] média renvoyé (${buffer.length} octets)`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 📥 [média extrait]`);
  } catch (err) {
    debugLog(`[extract] envoi impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Envoi du média impossible.', msg);
  }
}

/* -------------------------------------------------------------------------- */
/*  Commande .meta — métadonnées du message cité                              */
/* -------------------------------------------------------------------------- */

/** Formate une taille d'octets en lisible (Ko / Mo, virgule française). */
function humanSize(bytes) {
  const n = Number(bytes);
  if (!n || n <= 0) return '?';
  if (n < 1024) return `${n} o`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1).replace('.', ',')} Ko`;
  return `${(n / (1024 * 1024)).toFixed(2).replace('.', ',')} Mo`;
}

/** Formate une durée en secondes → "m:ss" (ex: 3:05). */
function formatDuration(seconds) {
  const s = Math.round(Number(seconds) || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Tronque proprement une empreinte binaire (Buffer / Uint8Array / chaîne). */
function safeSlice(value, length) {
  if (!value) return '';
  try {
    if (Buffer.isBuffer(value)) return value.toString('base64').slice(0, length);
    if (value instanceof Uint8Array) return Buffer.from(value).toString('base64').slice(0, length);
  } catch (_) { /* on retombe sur la conversion chaîne */ }
  return String(value).slice(0, length);
}

/** Ligne "Transféré" quand le message a été renvoyé (forwardingScore). */
function addForwarding(lines, contextInfo, mediaForwarded) {
  const score = Number(contextInfo?.forwardingScore) || 0;
  const forwarded = contextInfo?.isForwarded || mediaForwarded || score > 0;
  if (forwarded) {
    lines.push(score > 1 ? `• 🔁 Transféré : oui (${score}×)` : '• 🔁 Transféré : oui');
  }
}

/** Lignes communes des médias (format, poids, empreinte). */
function addMediaBase(lines, media) {
  if (media.mimetype) lines.push(`• 📦 Format : ${media.mimetype}`);
  if (media.fileLength) lines.push(`• ⚖️ Poids : ${humanSize(media.fileLength)}`);
  if (media.fileSha256) lines.push(`• 🔑 Empreinte SHA-256 : ${safeSlice(media.fileSha256, 14)}…`);
}

/**
 * Construit la liste des lignes de métadonnées pour un message WhatsApp.
 * Chaque type (texte, image, vidéo, audio, document, sticker, contact,
 * localisation) expose ses propres champs.
 */
function buildMetaLines(sm) {
  const lines = ['🔍 *Métadonnées du message*'];
  const ctx = sm.contextInfo || {};

  if (sm.conversation) {
    lines.push('💬 *Texte*');
    lines.push(`• 🔤 Longueur : ${sm.conversation.length} caractère(s)`);
    addForwarding(lines, ctx);
  } else if (sm.extendedTextMessage) {
    const et = sm.extendedTextMessage;
    lines.push('💬 *Texte*');
    if (et.text) lines.push(`• 🔤 Longueur : ${et.text.length} caractère(s)`);
    if (et.matchedText) lines.push(`• 🔗 Lien détecté : ${et.matchedText}`);
    if (et.title) lines.push(`• 🏷 Titre : ${et.title.slice(0, 60)}`);
    if (et.description) lines.push(`• 📝 Description : ${et.description.slice(0, 80)}`);
    if (et.canonicalUrl) lines.push(`• 🌐 URL : ${et.canonicalUrl}`);
    const mentions = (et.contextInfo?.mentionedJid || []).map(contactLabel).join(', ');
    if (mentions) lines.push(`• 👥 Mentions : ${mentions}`);
    addForwarding(lines, et.contextInfo);
  } else if (sm.imageMessage) {
    const im = sm.imageMessage;
    lines.push('📷 *Image*');
    addMediaBase(lines, im);
    if (im.width && im.height) lines.push(`• 📐 Dimensions : ${im.width} × ${im.height} px`);
    if (im.caption) lines.push(`• 🏷 Légende : ${im.caption.slice(0, 80)}`);
    addForwarding(lines, ctx, im.isForwarded);
  } else if (sm.videoMessage) {
    const vm = sm.videoMessage;
    lines.push('🎬 *Vidéo*');
    addMediaBase(lines, vm);
    if (vm.width && vm.height) lines.push(`• 📐 Dimensions : ${vm.width} × ${vm.height} px`);
    if (vm.seconds) lines.push(`• ⏱ Durée : ${formatDuration(vm.seconds)}`);
    if (vm.caption) lines.push(`• 🏷 Légende : ${vm.caption.slice(0, 80)}`);
    addForwarding(lines, ctx, vm.isForwarded);
  } else if (sm.audioMessage) {
    const am = sm.audioMessage;
    lines.push(am.ptt ? '🎤 *Note vocale*' : '🎵 *Audio*');
    addMediaBase(lines, am);
    if (am.seconds) lines.push(`• ⏱ Durée : ${formatDuration(am.seconds)}`);
    if (am.ptt) lines.push('• 🗣 Format : note vocale (ptt)');
    addForwarding(lines, ctx, am.isForwarded);
  } else if (sm.documentMessage) {
    const dm = sm.documentMessage;
    lines.push('📄 *Document*');
    addMediaBase(lines, dm);
    if (dm.fileName) lines.push(`• 🏷 Nom : ${dm.fileName}`);
    if (dm.pageCount) lines.push(`• 📄 Pages : ${dm.pageCount}`);
    if (dm.title) lines.push(`• 🏷 Titre : ${dm.title.slice(0, 60)}`);
    addForwarding(lines, ctx, dm.isForwarded);
  } else if (sm.stickerMessage) {
    const st = sm.stickerMessage;
    lines.push('🖼 *Sticker*');
    addMediaBase(lines, st);
    if (st.width && st.height) lines.push(`• 📐 Dimensions : ${st.width} × ${st.height} px`);
    if (st.isAnimated) lines.push('• ✨ Animé : oui');
    if (st.isAvatar) lines.push('• 👤 Avatar : oui');
    addForwarding(lines, ctx);
  } else if (sm.contactMessage) {
    const cm = sm.contactMessage;
    lines.push('👤 *Contact*');
    if (cm.displayName) lines.push(`• 🏷 Nom : ${cm.displayName}`);
    if (cm.vcard) lines.push(`• 📇 vCard : ${cm.vcard.replace(/\n/g, ' ').slice(0, 120)}…`);
  } else if (sm.locationMessage) {
    const lm = sm.locationMessage;
    lines.push('📍 *Localisation*');
    if (lm.degreesLatitude != null) lines.push(`• 🧭 Latitude : ${lm.degreesLatitude}`);
    if (lm.degreesLongitude != null) lines.push(`• 🧭 Longitude : ${lm.degreesLongitude}`);
    if (lm.degreesAccuracyInMeters) lines.push(`• 🎯 Précision : ${lm.degreesAccuracyInMeters} m`);
    if (lm.comment) lines.push(`• 📝 Commentaire : ${lm.comment}`);
  } else {
    lines.push('• ℹ️ Type de message non analysable.');
  }
  return lines;
}

/**
 * .meta — affiche les métadonnées du message CITÉ (image, texte, audio…).
 *
 * Répondez à n'importe quel message avec `.meta` : le bot détaille ses
 * métadonnées techniques (format, taille, dimensions, durée, liens…).
 * Une photo envoyée avec la légende `.meta` est également analysée.
 */
async function handleMetaCommand(msg, remoteJid) {
  const current = msg.message || {};
  const ctx = current.extendedTextMessage?.contextInfo || {};
  const quoted = ctx.quotedMessage || null;

  // Message cible : message cité, sinon message courant s'il contient un média
  let target = null;
  let quotedCtx = null;
  if (quoted) {
    target = quoted;
    quotedCtx = ctx;
  } else if (current.imageMessage || current.videoMessage || current.audioMessage
    || current.documentMessage || current.stickerMessage
    || current.contactMessage || current.locationMessage) {
    target = current;
  }

  if (!target) {
    return replyError(remoteJid,
      '🔍 *Commande .meta*\n'
      + 'RÉPONDEZ à n\'importe quel message (image, texte, audio, vidéo…)\n'
      + 'avec `.meta` pour voir ses métadonnées.\n'
      + '— ou —\n'
      + 'Envoyez une photo avec la légende `.meta`.', msg);
  }

  const lines = buildMetaLines(target);
  if (quotedCtx) {
    const sender = quotedCtx.participant || quotedCtx.remoteJid || '';
    if (sender) lines.push(`🧑 *De* : ${contactLabel(sender)}`);
    if (quotedCtx.stanzaId) lines.push(`🆔 *ID du message* : ${String(quotedCtx.stanzaId).slice(0, 10)}…`);
  }
  await sendChunks(remoteJid, lines.join('\n'), msg);
  debugLog(`[meta] métadonnées affichées (${lines.length} lignes)`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🔍 [métadonnées affichées]`);
}

/* -------------------------------------------------------------------------- */
/*  Commandes .json / .id / .tts / .ocr                                       */
/* -------------------------------------------------------------------------- */

/**
 * Sérialise un message en JSON lisible (indenté 2 espaces) en interceptant les
 * types spéciaux de Baileys/protobuf AVANT la sérialisation : BigInt, Long,
 * Buffer, Uint8Array, NaN… (JSON.stringify appelle toJSON() sur les Buffers
 * avant le replacer, d'où ce sérialiseur récursif maison).
 */
function jsonStringify(value) {
  const seen = new WeakSet(); // protection anti-cycles

  function ser(v, depth) {
    const pad = (n) => '  '.repeat(n);
    if (v === null) return 'null';
    const t = typeof v;
    if (t === 'string') return JSON.stringify(v);
    if (t === 'number') return Number.isFinite(v) ? String(v) : `"${v}"`;
    if (t === 'boolean') return String(v);
    if (t === 'bigint') return `"${v}n"`;
    if (t === 'undefined' || t === 'function' || t === 'symbol') return undefined;
    if (t === 'object') {
      if (seen.has(v)) return '"$cycle"';
      if (Buffer.isBuffer(v)) return `"$buffer(${v.length} o): ${v.toString('base64').slice(0, 24)}…"`;
      if (v instanceof Uint8Array) return `"$bytes(${v.length} o): ${Buffer.from(v).toString('base64').slice(0, 24)}…"`;
      // Long protobuf (Baileys) : réduit à un nombre lisible
      if (typeof v.toNumber === 'function' && typeof v.low === 'number' && typeof v.high === 'number') {
        try { return String(v.toNumber()); } catch (_) { /* Long trop grand → chaîne */ }
      }
      if (Array.isArray(v)) {
        if (v.length === 0) return '[]';
        seen.add(v);
        const items = [];
        for (const item of v) {
          const sv = ser(item, depth + 1);
          if (sv !== undefined) items.push(sv);
        }
        seen.delete(v);
        if (items.length === 0) return '[]';
        return `[\n${items.map((i) => `${pad(depth + 1)}${i}`).join(',\n')}\n${pad(depth)}]`;
      }
      const keys = Object.keys(v);
      if (keys.length === 0) return '{}';
      seen.add(v);
      const parts = [];
      for (const k of keys) {
        const sv = ser(v[k], depth + 1);
        if (sv !== undefined) parts.push(`${pad(depth + 1)}${JSON.stringify(k)}: ${sv}`);
      }
      seen.delete(v);
      if (parts.length === 0) return '{}';
      return `{\n${parts.join(',\n')}\n${pad(depth)}}`;
    }
    return undefined;
  }

  return ser(value, 0);
}

/**
 * .json — affiche le message CITÉ en JSON brut (mode débogage).
 * Sans citation, affiche le message courant (le vôtre).
 */
async function handleJsonCommand(msg, remoteJid) {
  const current = msg.message || {};
  const ctx = current.extendedTextMessage?.contextInfo || {};
  const quoted = ctx.quotedMessage || null;

  let target;
  let header;
  if (quoted) {
    target = { message: quoted, contextInfo: ctx };
    header = '📦 *JSON du message cité*';
  } else {
    target = msg;
    header = '📦 *JSON de ce message*';
  }

  let json;
  try {
    json = jsonStringify(target);
  } catch (err) {
    debugLog(`[json] sérialisation impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de sérialiser ce message.', msg);
  }
  if (json.length > 40000) {
    json = `${json.slice(0, 40000)}\n… (tronqué, trop long)`;
  }
  await sendChunks(remoteJid, `${header}\n\n${json}`, msg);
  debugLog(`[json] affiché (${json.length} caractères)`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 📦 [JSON affiché]`);
}

/**
 * .id — affiche les identifiants (jid) de la conversation et de l'expéditeur.
 * Pratique pour remplir la whitelist / liste noire IA sans se tromper.
 */
async function handleIdCommand(msg, remoteJid, sender) {
  const isGroup = remoteJid.endsWith('@g.us');
  const lines = ['🆔 *Identifiants de cette conversation*', ''];
  if (isGroup) {
    lines.push(`👥 *Groupe* : ${remoteJid}`);
    lines.push(`🧑 *Expéditeur* : ${sender || '?'}`);
    lines.push('📁 Type : Groupe');
    // Diagnostic admin : rôle de l'expéditeur et du bot (utile si une
    // commande admin est refusée à tort).
    if (sock) {
      try {
        const meta = await sock.groupMetadata(remoteJid);
        const addressing = meta.addressingMode === 'lid'
          ? 'LID (identifiant numérique)' : 'PN (numéro de téléphone)';
        lines.push(`🔑 Adressage : ${addressing}`);
        lines.push(`👑 Ton rôle ici : ${roleLabel(getParticipantRole(meta, sender))}`);
        lines.push(`🤖 Rôle du bot : ${roleLabel(getParticipantRole(meta, sock.user?.id))}`);
      } catch (_) { /* métadonnées indisponibles */ }
    }
    lines.push('');
    lines.push('💡 Ajoutez l\'ID du groupe dans la whitelist IA (panneau → IA) pour activer l\'IA ici.');
  } else if (remoteJid === 'status@broadcast') {
    lines.push('📢 *Statut* (broadcast)');
  } else {
    lines.push(`💬 *Conversation* : ${remoteJid}`);
    lines.push(`🧑 *Expéditeur* : ${sender || '?'}`);
    lines.push('📁 Type : Message privé');
    lines.push('');
    lines.push('💡 Copiez cet identifiant dans la whitelist IA (panneau → IA).');
  }
  await sendChunks(remoteJid, lines.join('\n'), msg);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🆔 [identifiants affichés]`);
}

/**
 * .tts <texte> — synthèse vocale gratuite (Google TTS), renvoyée en note vocale.
 * Limité à 200 caractères (contrainte du service).
 */
async function handleTtsCommand(msg, remoteJid, body) {
  const text = body.replace(/^\.(?:tts|voice)\s*/i, '').trim();

  if (!text) {
    return replyError(remoteJid,
      '🗣 *Commande .tts*\n'
      + 'Utilisation : `.tts [texte]`\n'
      + 'Exemple : `.tts Bonjour tout le monde !`', msg);
  }
  if (text.length > TTS_MAX_CHARS) {
    return replyError(remoteJid, `❌ Texte trop long (${TTS_MAX_CHARS} caractères max).`, msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: '🗣 Génération de la voix…' }).catch(() => {});

  let audio;
  try {
    const { data } = await axios.get('https://translate.google.com/translate_tts', {
      params: { ie: 'UTF-8', q: text, tl: 'fr', client: 'tw-ob' },
      responseType: 'arraybuffer',
      timeout: 30000,
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
    });
    // Garde-fou : Google renvoie parfois une page HTML d'erreur au lieu d'un MP3
    if (!data || data.length === 0 || data[0] === 0x3c /* '<' */) {
      throw new Error('réponse non audio');
    }
    audio = data;
  } catch (err) {
    debugLog(`[tts] génération impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de générer la voix. Réessayez.', msg);
  }

  try {
    await sock.sendMessage(remoteJid, {
      audio,
      mimetype: 'audio/mpeg',
      ptt: true, // note vocale
      fileName: 'tts.mp3',
    }, { quoted: msg });
    debugLog(`[tts] voix envoyée (${audio.length} octets)`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🗣 [voix] ${text.slice(0, 60)}`);
  } catch (err) {
    debugLog(`[tts] envoi impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Envoi de la voix impossible.', msg);
  }
}

/** Worker OCR partagé (tesseract.js, chargé paresseusement au premier .ocr). */
let ocrWorkerPromise = null;
let ocrQueue = Promise.resolve(); // sérialise les reconnaissances : tesseract.js refuse 2 jobs simultanés
function getOcrWorker() {
  if (!ocrWorkerPromise) {
    ocrWorkerPromise = (async () => {
      const { createWorker } = require('tesseract.js');
      return createWorker('fra', 1, { cachePath: TESS_CACHE });
    })().catch((err) => {
      ocrWorkerPromise = null; // retentera au prochain appel
      throw err;
    });
  }
  return ocrWorkerPromise;
}

/**
 * .ocr — lit le texte d'une image (tesseract.js, français, gratuit, sans clé).
 *
 * Répondez à une photo avec `.ocr`, ou envoyez une photo avec la légende `.ocr`.
 * Le premier appel télécharge les données de langue (puis c'est en cache).
 */
async function handleOcrCommand(msg, remoteJid) {
  const current = msg.message || {};
  const ctx = current.extendedTextMessage?.contextInfo || {};
  const quoted = ctx.quotedMessage || null;

  // Image source : message courant (légende .ocr) ou image citée
  let source = null;
  if (current.imageMessage) {
    source = msg;
  } else if (quoted?.imageMessage) {
    source = {
      ...msg,
      key: {
        ...msg.key,
        remoteJid,
        id: ctx.stanzaId || msg.key.id,
        participant: ctx.participant || msg.key.participant,
      },
      message: quoted,
    };
  }

  if (!source) {
    return replyError(remoteJid,
      '📖 *Commande .ocr*\n'
      + 'RÉPONDEZ à une photo avec `.ocr`\n'
      + '— ou —\n'
      + 'Envoyez une photo avec la légende `.ocr`.\n'
      + 'Le bot lit le texte de l\'image (français).', msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: '🔍 Lecture du texte… (quelques secondes)' }).catch(() => {});

  let buffer;
  try {
    buffer = await downloadMediaMessage(source, 'buffer', {});
  } catch (err) {
    debugLog(`[ocr] téléchargement impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de télécharger l\'image. Réessayez.', msg);
  }

  let worker;
  try {
    worker = await getOcrWorker();
    // Job sérialisé : deux .ocr simultanés ne s'entre-tuent pas (tesseract.js
    // refuse deux reconnaissances en même temps sur le même worker).
    const job = ocrQueue.then(() => worker.recognize(buffer));
    ocrQueue = job.catch(() => {}); // un échec ne doit pas empoisonner la file
    const { data } = await job;
    const text = (data.text || '').trim();
    if (!text) {
      return replyError(remoteJid, '📖 Aucun texte détecté sur cette image.', msg);
    }
    await sendChunks(remoteJid, `📖 *Texte détecté (OCR)*\n\n${text}`, msg);
    debugLog(`[ocr] ${text.length} caractères lus`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 📖 [OCR] ${text.slice(0, 80)}`);
  } catch (err) {
    debugLog(`[ocr] erreur : ${err.message}`);
    ocrWorkerPromise = null; // un worker défaillant est recréé au prochain appel
    return replyError(remoteJid, '❌ Erreur OCR. Vérifiez que la photo est nette et réessayez.', msg);
  }
}

/* -------------------------------------------------------------------------- */
/*  Commande .translate — traduction (Google Translate, gratuit, sans clé)     */
/* -------------------------------------------------------------------------- */

/**
 * Résout un code de langue à partir d'un mot : code ISO ("en", "de"…),
 * nom français ("anglais", "allemand"…) ou nom anglais ("english"…).
 * Renvoie le code ISO, ou null si inconnu.
 */
function parseTranslateLang(word) {
  const w = String(word || '').trim().toLowerCase();
  if (!w) return null;
  if (TRANSLATE_LANGS[w]) return w; // code ISO direct
  // Recherche par nom français, puis anglais, puis code alternatif
  const wNoAccent = w.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  for (const [code, meta] of Object.entries(TRANSLATE_LANGS)) {
    if (meta.name.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '') === wNoAccent) return code;
  }
  return TRANSLATE_EN_ALIASES[w] || null;
}

/** Texte du message cité (conversation / extendedText / légendes). */
function quotedTextOf(quoted) {
  if (!quoted) return '';
  return (quoted.conversation || quoted.extendedTextMessage?.text
    || quoted.imageMessage?.caption || quoted.videoMessage?.caption || '').trim();
}

/**
 * .translate [langue] — traduit un texte vers la langue choisie (gratuit).
 *
 * 3 usages :
 *   .translate en Bonjour        → traduit le texte saisi vers l'anglais
 *   .translate en (réponse)      → traduit le message CITÉ vers l'anglais
 *   .translate Bonjour (sans langue) → traduit vers le français (défaut)
 * La langue source est détectée automatiquement par Google.
 */
async function handleTranslateCommand(msg, remoteJid, body) {
  const rest = body.replace(/^\.translate\s*/i, '').trim();
  const firstMatch = rest.match(/^(\S+)\s*([\s\S]*)$/);
  let langCode = null;
  let text = '';

  if (firstMatch) {
    const code = parseTranslateLang(firstMatch[1]);
    if (code) {
      langCode = code;
      text = firstMatch[2].trim();
    } else {
      text = rest; // pas de langue reconnue → tout le texte, français par défaut
    }
  }
  if (!langCode) langCode = 'fr';

  // Pas de texte saisi → on prend le message cité (réponse à un message)
  let citedBy = '';
  let citedAudio = false;
  if (!text) {
    const ctx = msg.message?.extendedTextMessage?.contextInfo || {};
    const quoted = ctx.quotedMessage || null;
    text = quotedTextOf(quoted);
    if (text) {
      citedBy = contactLabel(ctx.participant || ctx.remoteJid || '');
    } else if (quoted?.audioMessage?.ptt) {
      citedAudio = true; // note vocale citée → on suggère .transcript
    }
  }

  if (!text) {
    if (citedAudio) {
      return replyError(remoteJid,
        '🎤 Une note vocale ne peut pas être traduite directement.\n'
        + 'Utilisez d\'abord `.transcript` (réponse à la note vocale) pour la transcrire, puis `.translate`.', msg);
    }
    const list = Object.entries(TRANSLATE_LANGS)
      .map(([code, meta]) => `${code} ${meta.flag}`).join(' · ');
    return replyError(remoteJid,
      '🌐 *Commande .translate*\n'
      + 'Traduit un texte vers la langue de votre choix (gratuit, sans clé).\n\n'
      + 'Utilisation :\n'
      + '• `.translate en Bonjour` — texte direct\n'
      + '• `.translate es` (réponse à un message) — message cité\n'
      + '• `.translate` (réponse) — message cité vers le français\n\n'
      + `*Langues* : ${list}\n`
      + 'Langue source détectée automatiquement.', msg);
  }
  if (text.length > TRANSLATE_MAX_CHARS) {
    return replyError(remoteJid, `❌ Texte trop long (${TRANSLATE_MAX_CHARS} caractères max).`, msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: '🌐 Traduction…' }).catch(() => {});

  let translated;
  try {
    const { data } = await axios.get('https://translate.googleapis.com/translate_a/single', {
      params: { client: 'gtx', sl: 'auto', tl: langCode, dt: 't', q: text },
      timeout: 30000,
    });
    const segments = (Array.isArray(data) && Array.isArray(data[0])) ? data[0] : [];
    translated = segments.map((seg) => (Array.isArray(seg) ? seg[0] : '')).join('').trim();
  } catch (err) {
    debugLog(`[translate] échec : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de traduire. Réessayez.', msg);
  }
  if (!translated) {
    return replyError(remoteJid, '❌ Aucune traduction renvoyée. Réessayez.', msg);
  }

  const meta = TRANSLATE_LANGS[langCode];
  const targetLabel = meta ? `${meta.name} ${meta.flag}` : langCode;
  const head = `🌐 *Traduction* (→ *${targetLabel}*)`;
  const bodyLines = [head, '', `💬 ${text}`, '', `✨ ${translated}`];
  if (citedBy) bodyLines.push('', `_Message de ${citedBy}_`);
  await sendChunks(remoteJid, bodyLines.join('\n'), msg);
  debugLog(`[translate] ${text.length} caractères → ${langCode} (${translated.length} caractères)`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🌐 [traduction → ${targetLabel}] ${translated.slice(0, 60)}`);
}

/* -------------------------------------------------------------------------- */
/*  Commande .correct — correction orthographe/grammaire via l'IA (GROQ)       */
/* -------------------------------------------------------------------------- */

/**
 * .correct — corrige l'orthographe et la grammaire d'un texte via l'IA.
 *
 * 2 usages :
 *   .correct (réponse à un message) → corrige le message CITÉ
 *   .correct <texte>                → corrige le texte saisi
 * La correction est faite par le backend (clé GROQ) avec un prompt dédié :
 * orthographe/grammaire/ponctuation, sans réécrire le style ni traduire.
 */
async function handleCorrectCommand(msg, remoteJid, body) {
  let text = body.replace(/^\.correct\s*/i, '').trim();
  let citedBy = '';

  // Pas de texte saisi → on prend le message cité (réponse à un message)
  if (!text) {
    const ctx = msg.message?.extendedTextMessage?.contextInfo || {};
    const quoted = ctx.quotedMessage || null;
    text = quotedTextOf(quoted);
    if (text) {
      citedBy = contactLabel(ctx.participant || ctx.remoteJid || '');
    }
  }

  if (!text) {
    return replyError(remoteJid,
      '✏️ *Commande .correct*\n'
      + 'Corrige l\'orthographe et la grammaire d\'un texte (via l\'IA).\n\n'
      + 'Utilisation :\n'
      + '• RÉPONDEZ à un message avec `.correct`\n'
      + '• `.correct voici un texte a coriger`\n\n'
      + 'La langue et le style sont conservés.', msg);
  }
  if (text.length > 4000) {
    return replyError(remoteJid, '❌ Texte trop long (4000 caractères max).', msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: '✏️ Correction en cours…' }).catch(() => {});

  let data;
  try {
    const res = await axios.post(`${FLASK_URL}/api/correct`, { text }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 90000,
    });
    data = res.data || {};
  } catch (err) {
    debugLog(`[correct] appel backend impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de joindre le service de correction. Réessayez.', msg);
  }

  if (!data.ok || !data.corrected) {
    return replyError(remoteJid,
      `❌ ${data.error || 'Correction impossible. Vérifiez la clé GROQ dans le panneau (onglet IA).'}`,
      msg);
  }

  const lines = ['✏️ *Texte corrigé*', ''];
  if (citedBy) lines.push(`_De ${citedBy} :_`, '');
  lines.push(`💬 ${text}`, '', `✅ ${data.corrected}`);
  await sendChunks(remoteJid, lines.join('\n'), msg);
  debugLog(`[correct] ${text.length} caractères corrigés → ${data.corrected.length} caractères`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ✏️ [correction] ${data.corrected.slice(0, 60)}`);
}

/**
 * .resume — résume par l'IA le message CITÉ (ou un texte saisi après la commande).
 *
 * Répondez à un long message avec `.resume` : l'IA produit un résumé fidèle
 * en français (une ligne d'intro + puces). Un texte direct est aussi accepté :
 * `.resume <texte>`.
 */
async function handleResumeCommand(msg, remoteJid, body) {
  let text = body.replace(/^\.(?:resume|resumé|resumer|résumé)(?:\s|$)/i, '').trim();
  let citedBy = '';

  // Pas de texte saisi → on prend le message cité (réponse à un message)
  if (!text) {
    const ctx = msg.message?.extendedTextMessage?.contextInfo || {};
    const quoted = ctx.quotedMessage || null;
    text = quotedTextOf(quoted);
    if (text) {
      citedBy = contactLabel(ctx.participant || ctx.remoteJid || '');
    }
  }

  if (!text) {
    return replyError(remoteJid,
      '📝 *Commande .resume*\n'
      + 'Résume un message avec l\'IA (en français).\n\n'
      + 'Utilisation :\n'
      + '• RÉPONDEZ à un message avec `.resume`\n'
      + '• `.resume <texte>` pour résumer un texte direct.', msg);
  }
  if (text.length > RESUME_MAX_CHARS) {
    return replyError(remoteJid, `❌ Texte trop long (${RESUME_MAX_CHARS} caractères max).`, msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: '📝 Résumé en cours…' }).catch(() => {});

  let data;
  try {
    const res = await axios.post(`${FLASK_URL}/api/resume`, { text }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 90000,
    });
    data = res.data || {};
  } catch (err) {
    debugLog(`[resume] appel backend impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de joindre le service de résumé. Réessayez.', msg);
  }

  if (!data.ok || !data.summary) {
    return replyError(remoteJid,
      `❌ ${data.error || 'Résumé impossible. Vérifiez la clé GROQ dans le panneau (onglet IA).'}`,
      msg);
  }

  const lines = ['📝 *Résumé*', ''];
  if (citedBy) lines.push(`_Message de ${citedBy} :_`, '');
  lines.push(`${data.summary}`, '');
  const secs = Math.round((data.duration_ms || 0) / 1000);
  const footer = `_via ${data.model || 'IA'}` + (secs > 0 ? ` · ${secs} s_` : '_');
  lines.push(footer);
  await sendChunks(remoteJid, lines.join('\n'), msg);
  debugLog(`[resume] ${text.length} caractères résumés → ${data.summary.length} caractères`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 📝 [résumé] ${data.summary.slice(0, 60)}`);
}

/* -------------------------------------------------------------------------- */
/*  Commande .pin — recherche d'images (DuckDuckGo Images + Wikimedia)         */
/* -------------------------------------------------------------------------- */

// Nombre d'images par défaut envoyées par .pin (et maximum autorisé)
const PIN_DEFAULT_COUNT = 5;
const PIN_MAX_COUNT = 10;
// Taille maximale d'une image téléchargée (garde-fou, 15 Mo — largement assez)
const PIN_MAX_BYTES = 15 * 1024 * 1024;
// User-Agent commun pour les services d'images (évite les blocages)
const PIN_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36';

/**
 * Recherche des URLs d'images sur DuckDuckGo Images (gratuit, sans clé API).
 * Le token vqd est récupéré depuis la page de recherche, puis les résultats
 * JSON sont interrogés. Renvoie des URLs https (ou une liste vide).
 *
 * Avec nsfw=true, le filtre SafeSearch est désactivé (paramètre kp=-2) :
 * la recherche peut alors renvoyer du contenu adulte. Par défaut le filtre
 * de sécurité reste ACTIF.
 */
async function searchDdgImages(query, limit, nsfw = false) {
  const pageParams = { q: query, ia: 'web' };
  if (nsfw) pageParams.kp = '-2';
  const page = await axios.get('https://duckduckgo.com/', {
    params: pageParams,
    headers: { 'User-Agent': PIN_UA },
    timeout: 20000,
  });
  const vqd = String(page.data || '').match(/vqd="([^"]+)"/)?.[1];
  if (!vqd) return [];
  const imageParams = { l: 'us-en', o: 'json', q: query, vqd };
  if (nsfw) imageParams.kp = '-2';
  const res = await axios.get('https://duckduckgo.com/i.js', {
    params: imageParams,
    headers: { 'User-Agent': PIN_UA, Referer: 'https://duckduckgo.com/?q=' + encodeURIComponent(query) },
    timeout: 25000,
  });
  const urls = [];
  for (const item of (res.data?.results || [])) {
    if (!item || !item.image) continue;
    const raw = String(item.image);
    // Certains sites ne sont servis qu'en http : on force https pour WhatsApp
    const url = raw.startsWith('http://') ? 'https://' + raw.slice(7) : raw;
    if (url.startsWith('https://') && urls.indexOf(url) === -1) urls.push(url);
    if (urls.length >= limit) break;
  }
  return urls;
}

/**
 * Recherche des images sur Wikimedia Commons (API officielle, sans clé).
 * Renvoie des miniatures 800 px (bien dimensionnées pour WhatsApp).
 */
async function searchWikiImages(query, limit) {
  const res = await axios.get('https://commons.wikimedia.org/w/api.php', {
    params: {
      action: 'query', format: 'json', generator: 'search',
      gsrsearch: query, gsrnamespace: 6, gsrlimit: limit * 2,
      prop: 'imageinfo', iiprop: 'url|size', iiurlwidth: 800,
    },
    headers: { 'User-Agent': 'BrixBot/1.0 (bot WhatsApp)' },
    timeout: 25000,
  });
  const urls = [];
  for (const page of Object.values(res.data?.query?.pages || {})) {
    const info = page.imageinfo && page.imageinfo[0];
    const url = (info && (info.thumburl || info.url)) || '';
    if (url.startsWith('https://') && urls.indexOf(url) === -1) urls.push(url);
    if (urls.length >= limit) break;
  }
  return urls;
}

/**
 * .pin <requête> — envoie des images correspondant à la recherche.
 *
 * Usage :
 *   .pin chat          → 5 images de chats
 *   .pin 3 chat mignon → 3 images (nombre optionnel, 1 à 10)
 * Source : DuckDuckGo Images (varié), secours Wikimedia Commons (fiable).
 * 100 % gratuit, sans clé API.
 */
async function handlePinCommand(msg, remoteJid, body) {
  let rest = body.replace(/^\.pin\s*/i, '').trim();
  let count = PIN_DEFAULT_COUNT;
  let nsfw = false;
  let query = rest;

  // Nombre optionnel au début : ".pin 3 chat" → 3 images de "chat"
  const firstWord = rest.split(' ')[0] || '';
  const parsedCount = parseInt(firstWord, 10);
  if (String(parsedCount) === firstWord && parsedCount > 0) {
    count = Math.min(parsedCount, PIN_MAX_COUNT);
    query = rest.slice(firstWord.length).trim();
  }
  // Mode NSFW : ".pin nsfw chat" ou ".pin 3 nsfw chat" — désactive le filtre
  // SafeSearch (la recherche peut alors renvoyer du contenu adulte).
  // Le mot-clé doit être EXACTEMENT "nsfw" (pas "nsfw-mode" ni "nsfwchat").
  const nsfwWord = query.split(' ')[0] || '';
  if (/^nsfw$/i.test(nsfwWord)) {
    nsfw = true;
    query = query.slice(nsfwWord.length).trim();
  }

  if (!query) {
    return replyError(remoteJid,
      '📌 *Commande .pin*\n'
      + 'Envoie des images correspondant à une recherche (gratuit, sans clé).\n\n'
      + 'Utilisation :\n'
      + '• `.pin chat` — 5 images de chats\n'
      + '• `.pin 3 chat mignon` — 3 images (nombre optionnel, 1 à 10)\n'
      + '• `.pin nsfw chat` — désactive le filtre de sécurité 🔞 (adultes uniquement)\n\n'
      + 'Sources : DuckDuckGo Images · Wikimedia Commons.', msg);
  }

  if (sock) {
    sock.sendMessage(remoteJid, {
      text: nsfw
        ? `🔞 Recherche NSFW pour « ${query} »… (contenu adulte possible)`
        : `📌 Recherche d'images pour « ${query} »…`,
    }).catch(() => {});
  }

  // 1) Recherche des URLs : DuckDuckGo d'abord, Wikimedia en secours.
  // On demande PLUS d'URLs que le nombre voulu : certains liens sont morts ou
  // renvoient du HTML, la boucle d'envoi en ignorera et on doit en avoir assez
  // pour atteindre `count` images effectives.
  const fetchCount = Math.min(count * 3, 30);
  let wikiTried = false; // Wikimedia est tenté au plus une fois (secours ou complément)
  let urls = [];
  try {
    urls = await searchDdgImages(query, fetchCount, nsfw);
  } catch (err) {
    debugLog(`[pin] DuckDuckGo indisponible : ${err.message}`);
  }
  // En mode NSFW, on ne mélange JAMAIS avec Wikimedia : ses images sont toujours
  // "sûres" et n'ont rien à faire dans une recherche adulte.
  if (nsfw) wikiTried = true;
  if (!urls.length && !nsfw) {
    // Secours direct : DuckDuckGo n'a rien renvoyé (mode normal uniquement)
    try {
      urls = await searchWikiImages(query, fetchCount);
      wikiTried = true;
      if (urls.length) debugLog(`[pin] secours Wikimedia utilisé (${urls.length} URL(s))`);
    } catch (err) {
      debugLog(`[pin] Wikimedia indisponible : ${err.message}`);
    }
  }
  if (!urls.length) {
    return replyError(remoteJid, `❌ Aucune image trouvée pour « ${query} ». Réessayez.`, msg);
  }

  // 2) Téléchargement puis envoi, image par image (les liens morts sont ignorés).
  // Chaque URL est tentée en séquentiel ; une URL qui échoue (téléchargement ou
  // envoi) est simplement sautée. Timeout court (15 s) pour ne jamais bloquer
  // la commande sur un lien qui pend.
  let sent = 0;
  const sendOne = async (url) => {
    let buffer;
    try {
      const res = await axios.get(url, {
        responseType: 'arraybuffer',
        timeout: 15000,
        maxContentLength: PIN_MAX_BYTES,
        headers: { 'User-Agent': PIN_UA },
      });
      // Garde-fou : certains liens renvoient une page HTML au lieu d'une image
      const ctype = String(res.headers['content-type'] || '');
      if (!res.data || !res.data.length || ctype.indexOf('image/') !== 0) {
        debugLog(`[pin] lien non-image ignoré : ${url}`);
        return;
      }
      buffer = res.data;
    } catch (err) {
      debugLog(`[pin] téléchargement impossible (${url}) : ${err.message}`);
      return;
    }
    try {
      await sock.sendMessage(remoteJid, {
        image: buffer,
        caption: `📌 *${query}* (${sent + 1}/${count})`,
      }, { quoted: msg });
      sent++;
    } catch (err) {
      debugLog(`[pin] envoi impossible (${url}) : ${err.message}`);
    }
  };

  // Passe 1 : les URLs DuckDuckGo
  for (let i = 0; i < urls.length && sent < count; i++) {
    await sendOne(urls[i]);
  }
  // Passe 2 : complément Wikimedia si DuckDuckGo n'a pas fourni assez d'images
  // (ex: tous ses liens étaient morts) — on ne re-tente jamais Wikimedia deux fois.
  if (sent < count && !wikiTried) {
    wikiTried = true;
    try {
      const wikiUrls = await searchWikiImages(query, fetchCount);
      if (wikiUrls.length) debugLog(`[pin] complément Wikimedia (${wikiUrls.length} URL(s))`);
      for (const url of wikiUrls) {
        if (sent >= count) break;
        await sendOne(url);
      }
    } catch (err) {
      debugLog(`[pin] Wikimedia indisponible : ${err.message}`);
    }
  }

  if (!sent) {
    return replyError(remoteJid, "❌ Aucune image n'a pu être envoyée. Réessayez.", msg);
  }
  debugLog(`[pin] ${sent} image(s) envoyée(s) pour « ${query} »`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 📌 ${sent} image(s) pour « ${query} »`);
}

/* -------------------------------------------------------------------------- */
/*  Administration de groupe (.kick / .mute / .promote / .link…)              */
/* -------------------------------------------------------------------------- */

/** Partie locale d'un jid : "33612345678:38@s.whatsapp.net" → "33612345678". */
function jidLocal(jid) {
  return String(jid || '').split('@')[0].split(':')[0];
}

/**
 * Toutes les identifications locales d'un participant de groupe.
 * WhatsApp migre vers l'adressage LID : `id` peut être un identifiant
 * numérique (@lid) et le vrai numéro est alors dans `phoneNumber` (et
 * inversement, `lid` porte le LID quand `id` est un numéro).
 */
function participantKeys(p) {
  const keys = new Set();
  for (const v of [p && p.id, p && p.lid, p && p.phoneNumber]) {
    const local = jidLocal(v);
    if (local) keys.add(local);
  }
  return keys;
}

/**
 * Rôle d'un jid dans les métadonnées d'un groupe :
 * 'superadmin' | 'admin' | 'member' | null (non membre).
 */
function getParticipantRole(meta, jid) {
  const local = jidLocal(jid);
  const member = (meta.participants || []).find((p) => participantKeys(p).has(local));
  if (!member) return null;
  if (member.admin) return member.admin; // 'admin' | 'superadmin'
  if (member.isSuperAdmin) return 'superadmin';
  if (member.isAdmin) return 'admin';
  return 'member';
}

/** Libellé lisible d'un rôle (pour .id). */
function roleLabel(role) {
  if (role === 'superadmin') return 'Créateur ✅';
  if (role === 'admin') return 'Administrateur ✅';
  if (role === 'member') return 'Membre';
  return 'Non membre';
}

/** Vrai si un participant est muet dans ce groupe (normalisé sans suffixe d'appareil). */
function isMuted(chat, participant) {
  return !!(mutedMap[chat] && mutedMap[chat][jidLocal(participant)]);
}

/**
 * Vrai si l'expéditeur est administrateur du groupe.
 * Le créateur du groupe est TOUJOURS considéré admin (même si le type
 * n'est pas explicite), et la comparaison couvre id / lid / phoneNumber.
 */
async function isGroupAdmin(remoteJid, senderJid) {
  if (!sock) return false;
  try {
    const meta = await sock.groupMetadata(remoteJid);
    const senderLocal = jidLocal(senderJid);
    // Le créateur (id, pn ou username) est toujours administrateur
    for (const v of [meta.owner, meta.ownerPn, meta.ownerUsername]) {
      if (jidLocal(v) === senderLocal) return true;
    }
    const role = getParticipantRole(meta, senderJid);
    return role === 'admin' || role === 'superadmin';
  } catch (_) {
    return false;
  }
}

/** Vrai si le BOT lui-même est administrateur du groupe. */
async function isBotAdmin(remoteJid) {
  if (!sock) return false;
  try {
    const meta = await sock.groupMetadata(remoteJid);
    const role = getParticipantRole(meta, sock.user?.id);
    return role === 'admin' || role === 'superadmin';
  } catch (_) {
    return false;
  }
}

/**
 * Résout le membre visé par une commande admin :
 *   1. la mention (@quelqu'un)   2. le message cité   3. le numéro en argument.
 * Renvoie le jid complet du participant, ou null.
 */
async function resolveTarget(msg, remoteJid, arg) {
  const ctx = msg.message?.extendedTextMessage?.contextInfo || {};
  if (ctx.mentionedJid && ctx.mentionedJid.length) return ctx.mentionedJid[0];
  if (ctx.participant) return ctx.participant;
  const digits = String(arg || '').replace(/\D/g, '');
  if (digits.length >= 8 && sock) {
    const key = (digits.length === 10 && digits.startsWith('0')) ? `33${digits.slice(1)}` : digits;
    try {
      const meta = await sock.groupMetadata(remoteJid);
      const match = (meta.participants || []).find((p) => {
        const keys = [...participantKeys(p)];
        return keys.includes(key) || keys.some((k) => k.length >= 8 && k.endsWith(key));
      });
      if (match) return match.id;
    } catch (_) { /* groupe inaccessible */ }
  }
  return null;
}

/**
 * Point d'entrée des commandes d'administration : réservées aux admins du groupe.
 */
async function handleGroupAdminCommand(msg, remoteJid, sender, body, isGroup) {
  const cmd = body.split(/\s+/)[0].toLowerCase();
  if (!isGroup) {
    return replyError(remoteJid, '⚠️ Cette commande ne fonctionne que dans un groupe.', msg);
  }
  if (!(await isGroupAdmin(remoteJid, sender))) {
    return replyError(remoteJid, '🔒 Cette commande est réservée aux administrateurs du groupe.', msg);
  }
  switch (cmd) {
    case '.kick': return handleKick(msg, remoteJid, body);
    case '.mute': return handleMuteToggle(msg, remoteJid, body, true);
    case '.unmute': return handleMuteToggle(msg, remoteJid, body, false);
    case '.promote': return handlePromoteDemote(msg, remoteJid, body, 'promote');
    case '.demote': return handlePromoteDemote(msg, remoteJid, body, 'demote');
    case '.tagall': return handleTagAll(msg, remoteJid);
    case '.link': return handleGroupLink(msg, remoteJid);
    case '.close': return handleGroupSetting(msg, remoteJid, 'announcement');
    case '.open': return handleGroupSetting(msg, remoteJid, 'not_announcement');
    case '.warn': return handleWarn(msg, remoteJid, body, 'warn');
    case '.unwarn': return handleWarn(msg, remoteJid, body, 'unwarn');
    case '.warns': return handleWarn(msg, remoteJid, body, 'warns');
    case '.resetwarn': return handleWarn(msg, remoteJid, body, 'resetwarn');
    case '.welcome': return handleWelcome(msg, remoteJid, body);
    case '.antilink': return handleAntilink(msg, remoteJid, body);
    case '.revoke': return handleGroupRevoke(msg, remoteJid);
    default: return replyError(remoteJid, 'ℹ️ Commande admin inconnue.', msg);
  }
}

/** .kick — expulse un membre du groupe. */
async function handleKick(msg, remoteJid, body) {
  const arg = body.replace(/^\.kick\s*/i, '').trim();
  const target = await resolveTarget(msg, remoteJid, arg);
  if (!target) {
    return replyError(remoteJid,
      '👢 *Commande .kick*\n'
      + 'Utilisation : `.kick @membre`\n'
      + '— ou —\n'
      + 'Répondez au message du membre avec `.kick`\n'
      + '— ou —\n'
      + '`.kick 0612345678`', msg);
  }
  if (jidLocal(target) === jidLocal(sock?.user?.id)) {
    return replyError(remoteJid, '😅 Je ne peux pas m\'expulser moi-même !', msg);
  }
  try {
    await sock.groupParticipantsUpdate(remoteJid, [target], 'remove');
    const who = contactLabel(target);
    await sock.sendMessage(remoteJid, { text: `👢 *${who}* a été expulsé du groupe.` }, { quoted: msg });
    debugLog(`[admin] kick : ${who}`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 👢 [expulsion] ${who}`);
  } catch (err) {
    debugLog(`[admin] kick impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible d\'expulser. Vérifiez que le bot est administrateur.', msg);
  }
}

/** .mute / .unmute — silence un membre (ses messages sont supprimés). */
async function handleMuteToggle(msg, remoteJid, body, on) {
  const cmdName = on ? 'mute' : 'unmute';
  const arg = body.replace(new RegExp(`^\\.${cmdName}\\s*`, 'i'), '').trim();
  const target = await resolveTarget(msg, remoteJid, arg);
  const list = (mutedMap[remoteJid] = mutedMap[remoteJid] || {});

  // Sans cible : aide + liste des membres muets
  if (!target) {
    const keys = Object.keys(list);
    const lines = [
      on
        ? '🔇 *Commande .mute*\nUtilisation : `.mute @membre` (ou réponse à un message)\nSes messages seront supprimés automatiquement.'
        : '🔊 *Commande .unmute*\nUtilisation : `.unmute @membre` (ou réponse à un message)',
      '',
      keys.length
        ? '🔇 Membres muets ici :\n' + keys.map((k) => `• ${contactLabel(k)}`).join('\n')
        : 'Aucun membre muet dans ce groupe.',
    ];
    return sendChunks(remoteJid, lines.join('\n'), msg);
  }

  if (jidLocal(target) === jidLocal(sock?.user?.id)) {
    return replyError(remoteJid, '😅 Je ne peux pas me muter moi-même !', msg);
  }

  const who = contactLabel(target);
  const keyLocal = jidLocal(target); // clé normalisée (sans suffixe d'appareil)
  if (on) {
    // La suppression des messages exige que le BOT soit administrateur :
    // inutile de confirmer un mute qui ne pourra pas être appliqué.
    if (!(await isBotAdmin(remoteJid))) {
      return replyError(remoteJid,
        '⚠️ Le bot doit être *administrateur* du groupe pour supprimer les messages.\n'
        + `Promotez-le (\`.promote ${who}\` ou via WhatsApp) puis réessayez.`, msg);
    }
    list[keyLocal] = true;
    saveMuteStore();
    try {
      await sock.sendMessage(remoteJid,
        { text: `🔇 *${who}* est muet : ses messages seront supprimés.` }, { quoted: msg });
    } catch (err) { debugLog(`[admin] confirmation mute impossible : ${err.message}`); }
    debugLog(`[admin] mute : ${who}`);
  } else {
    delete list[keyLocal];
    saveMuteStore();
    try {
      await sock.sendMessage(remoteJid,
        { text: `🔊 *${who}* peut reparler librement.` }, { quoted: msg });
    } catch (err) { debugLog(`[admin] confirmation unmute impossible : ${err.message}`); }
    debugLog(`[admin] unmute : ${who}`);
  }
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ${on ? '🔇 [mute]' : '🔊 [unmute]'} ${who}`);
}

/** .promote / .demote — donne ou retire les droits administrateur. */
async function handlePromoteDemote(msg, remoteJid, body, action) {
  const arg = body.replace(new RegExp(`^\\.${action}\\s*`, 'i'), '').trim();
  const target = await resolveTarget(msg, remoteJid, arg);
  if (!target) {
    return replyError(remoteJid,
      `${action === 'promote' ? '👑' : '⬇️'} *Commande .${action}*\n`
      + `Utilisation : \`.${action} @membre\` (ou réponse à un message)`, msg);
  }
  try {
    await sock.groupParticipantsUpdate(remoteJid, [target], action);
    const who = contactLabel(target);
    const text = action === 'promote'
      ? `👑 *${who}* est maintenant administrateur.`
      : `⬇️ *${who}* n'est plus administrateur.`;
    await sock.sendMessage(remoteJid, { text }, { quoted: msg });
    debugLog(`[admin] ${action} : ${who}`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ${action === 'promote' ? '👑 [promote]' : '⬇️ [demote]'} ${who}`);
  } catch (err) {
    debugLog(`[admin] ${action} impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible. Vérifiez que le bot est administrateur.', msg);
  }
}

/** .tagall — mentionne tous les membres du groupe. */
async function handleTagAll(msg, remoteJid) {
  let meta;
  try {
    meta = await sock.groupMetadata(remoteJid);
  } catch (err) {
    debugLog(`[admin] tagall impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de lire le groupe.', msg);
  }
  const botLocal = jidLocal(sock?.user?.id);
  const members = (meta.participants || [])
    .map((p) => p.id)
    .filter((id) => jidLocal(id) !== botLocal);
  if (!members.length) {
    return replyError(remoteJid, '👥 Aucun membre à mentionner.', msg);
  }
  try {
    await sock.sendMessage(remoteJid, {
      text: `👥 *Tous les membres* (${members.length})\n`
        + members.map((id) => `• @${jidLocal(id)}`).join('\n'),
      mentions: members,
    }, { quoted: msg });
  } catch (err) {
    debugLog(`[admin] tagall envoi impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible d\'envoyer la mention à tous.', msg);
  }
  debugLog(`[admin] tagall : ${members.length} membres`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 👥 [tagall ${members.length}]`);
}

/** .link — génère un lien d'invitation pour le groupe. */
async function handleGroupLink(msg, remoteJid) {
  try {
    const code = await sock.groupInviteCode(remoteJid);
    if (!code) throw new Error('aucun code');
    const url = `https://chat.whatsapp.com/${code}`;
    await sock.sendMessage(remoteJid, {
      text: `🔗 *Lien d'invitation au groupe*\n\n${url}\n\n_Si le lien expire, un admin peut le recréer._`,
    }, { quoted: msg });
    debugLog(`[admin] lien généré`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🔗 [lien d'invitation]`);
  } catch (err) {
    debugLog(`[admin] lien impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de générer le lien. Vérifiez que le bot est administrateur.', msg);
  }
}

/** .close / .open — verrouille ou ouvre le groupe (seuls les admins écrivent). */
async function handleGroupSetting(msg, remoteJid, setting) {
  const isAnnounce = setting === 'announcement';
  try {
    await sock.groupSettingUpdate(remoteJid, setting);
    await sock.sendMessage(remoteJid, {
      text: isAnnounce
        ? '🔒 Groupe verrouillé : seuls les administrateurs peuvent écrire.'
        : '🔓 Groupe ouvert : tout le monde peut écrire.',
    }, { quoted: msg });
    debugLog(`[admin] groupe ${isAnnounce ? 'verrouillé' : 'ouvert'}`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ${isAnnounce ? '🔒 [groupe verrouillé]' : '🔓 [groupe ouvert]'}`);
  } catch (err) {
    debugLog(`[admin] réglage impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible. Vérifiez que le bot est administrateur.', msg);
  }
}

/* -------------------------------------------------------------------------- */
/*  Modération (.warn / .welcome / .antilink / .revoke)                       */
/* -------------------------------------------------------------------------- */

// Nombre d'avertissements avant expulsion automatique
const WARN_LIMIT = 3;

/**
 * .warn / .unwarn / .warns / .resetwarn — système d'avertissements par groupe.
 * À 3 avertissements, le membre est expulsé automatiquement (si le bot est admin).
 */
async function handleWarn(msg, remoteJid, body, action) {
  const groupLocal = jidLocal(remoteJid);
  const warns = (botState.warns[groupLocal] = botState.warns[groupLocal] || {});

  if (action === 'warns') {
    const entries = Object.entries(warns).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
    if (!entries.length) return replyError(remoteJid, '⚠️ Aucun avertissement dans ce groupe. 👍', msg);
    const lines = ['⚠️ *Avertissements dans ce groupe*'];
    for (const [k, n] of entries) lines.push(`• ${contactLabel(k)} : ${n}/${WARN_LIMIT}`);
    return sendChunks(remoteJid, lines.join('\n'), msg);
  }

  const arg = body.replace(new RegExp(`^\\.${action}\\s*`, 'i'), '').trim();
  const target = await resolveTarget(msg, remoteJid, arg);
  if (!target) {
    return replyError(remoteJid,
      `⚠️ *Commande .${action}*\nUtilisation : \`.${action} @membre\` (ou réponse à un message)`, msg);
  }
  const targetLocal = jidLocal(target);
  if (targetLocal === jidLocal(sock?.user?.id)) {
    return replyError(remoteJid, '😅 Je ne peux pas avertir le bot lui-même.', msg);
  }
  const who = contactLabel(target);

  if (action === 'unwarn') {
    if ((warns[targetLocal] || 0) === 0) {
      return replyError(remoteJid, `ℹ️ *${who}* n'avait aucun avertissement.`, msg);
    }
    warns[targetLocal] = Math.max(0, (warns[targetLocal] || 0) - 1);
    saveBotStore();
    return replyError(remoteJid, `✅ *${who}* : avertissement retiré (${warns[targetLocal]}/${WARN_LIMIT}).`, msg);
  }
  if (action === 'resetwarn') {
    warns[targetLocal] = 0;
    saveBotStore();
    return replyError(remoteJid, `🧹 Avertissements de *${who}* remis à zéro.`, msg);
  }

  // .warn : on ajoute un avertissement
  warns[targetLocal] = (warns[targetLocal] || 0) + 1;
  const count = warns[targetLocal];
  saveBotStore();

  if (count >= WARN_LIMIT) {
    try {
      await sock.groupParticipantsUpdate(remoteJid, [target], 'remove');
      delete warns[targetLocal]; // on ne remet à zéro qu'APRÈS une expulsion réussie
      saveBotStore();
      await sock.sendMessage(remoteJid,
        { text: `👢 *${who}* expulsé après ${WARN_LIMIT} avertissements.` }, { quoted: msg });
      debugLog(`[warn] kick auto : ${who}`);
      transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 👢 [warn x${WARN_LIMIT}] ${who}`);
    } catch (err) {
      debugLog(`[warn] kick auto impossible : ${err.message}`);
      // Le compte reste à WARN_LIMIT : un prochain .warn retentera le kick.
      saveBotStore();
      return replyError(remoteJid,
        `⚠️ *${who}* a atteint ${WARN_LIMIT} avertissements, mais le bot doit être admin pour l'expulser.`, msg);
    }
    return;
  }

  const remaining = WARN_LIMIT - count;
  try {
    await sock.sendMessage(remoteJid, {
      text: `⚠️ *${who}* : avertissement ${count}/${WARN_LIMIT}.`
        + (remaining === 1 ? ' Encore un et c\'est l\'expulsion !' : ` Encore ${remaining} avant expulsion.`),
    }, { quoted: msg });
  } catch (err) { debugLog(`[warn] confirmation impossible : ${err.message}`); }
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ⚠️ [warn ${count}/3] ${who}`);
}

/** .welcome — message de bienvenue automatique pour ce groupe. */
async function handleWelcome(msg, remoteJid, body) {
  const groupLocal = jidLocal(remoteJid);
  const arg = body.replace(/^\.welcome\s*/i, '').trim();

  if (!arg) {
    const current = botState.welcome[groupLocal];
    const lines = ['👋 *Commande .welcome*', ''];
    lines.push(current ? `📣 Message actuel : "${current}"` : 'Aucun message de bienvenue configuré ici.');
    lines.push('', '• `.welcome <message>` — définir le message', '• `.welcome off` — désactiver l\'accueil');
    return sendChunks(remoteJid, lines.join('\n'), msg);
  }
  if (/^(off|non|stop|0|supprime)$/i.test(arg)) {
    delete botState.welcome[groupLocal];
    saveBotStore();
    return replyError(remoteJid, '🚫 Message de bienvenue désactivé pour ce groupe.', msg);
  }
  if (arg.length > 300) return replyError(remoteJid, '❌ Message trop long (300 caractères max).', msg);
  botState.welcome[groupLocal] = arg;
  saveBotStore();
  return replyError(remoteJid,
    `👋 Message de bienvenue défini :\n"${arg}"\n\n_Il sera envoyé quand quelqu'un rejoint._`, msg);
}

/** .antilink — supprime automatiquement les liens des non-admins. */
async function handleAntilink(msg, remoteJid, body) {
  const groupLocal = jidLocal(remoteJid);
  const arg = body.replace(/^\.antilink\s*/i, '').trim().toLowerCase();
  const active = !!botState.antilink[groupLocal];
  if (['oui', 'on', '1', 'activer'].includes(arg)) {
    botState.antilink[groupLocal] = true;
    saveBotStore();
    return replyError(remoteJid, '🔗 *Anti-lien activé* : les liens des non-admins seront supprimés.', msg);
  }
  if (['non', 'off', '0', 'desactiver'].includes(arg)) {
    delete botState.antilink[groupLocal];
    saveBotStore();
    return replyError(remoteJid, '🔗 *Anti-lien désactivé*.', msg);
  }
  return replyError(remoteJid,
    `🔗 *Anti-lien* : ${active ? 'ACTIF ✅' : 'inactif ❌'}\n\n`
    + '• `.antilink oui` — supprimer les liens des non-admins\n'
    + '• `.antilink non` — désactiver', msg);
}

/** .revoke — révoque le lien d'invitation actuel et en génère un nouveau. */
async function handleGroupRevoke(msg, remoteJid) {
  try {
    const code = await sock.groupRevokeInvite(remoteJid);
    if (!code) throw new Error('aucun code');
    const url = `https://chat.whatsapp.com/${code}`;
    await sock.sendMessage(remoteJid, {
      text: `🔄 Lien d'invitation révoqué.\n🔗 Nouveau lien : ${url}`,
    }, { quoted: msg });
    debugLog(`[admin] lien révoqué`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🔄 [lien révoqué]`);
  } catch (err) {
    debugLog(`[admin] revoke impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de révoquer le lien. Vérifiez que le bot est administrateur.', msg);
  }
}

/* -------------------------------------------------------------------------- */
/*  Fun et utilitaires (.roll / .bin / .quote / .ping / .afk)                 */
/* -------------------------------------------------------------------------- */

/**
 * Analyse une demande de lancer de dés (fonction pure, testable).
 * Retourne { kind: 'd100' } | { kind: 'range', max } | { kind: 'dice', dice, sides }
 * | { kind: 'invalid', message } | { kind: 'help' }.
 */
function parseRoll(arg) {
  const input = (arg || '').trim().toLowerCase();
  if (!input) return { kind: 'd100' };
  if (/^\d+$/.test(input)) {
    const n = parseInt(input, 10);
    if (n < 1) return { kind: 'invalid', message: 'Le nombre doit être ≥ 1.' };
    return { kind: 'range', max: Math.min(n, 1000000) };
  }
  const m = input.match(/^(\d{1,2})d(\d{1,4})$/);
  if (m) {
    return {
      kind: 'dice',
      dice: Math.min(parseInt(m[1], 10) || 1, 100),
      sides: Math.min(parseInt(m[2], 10) || 1, 1000000),
    };
  }
  return { kind: 'help' };
}

/** .roll — lance un dé ou un nombre aléatoire. */
async function handleRoll(msg, remoteJid, body) {
  const arg = body.replace(/^\.roll\s*/i, '').trim();
  const spec = parseRoll(arg);
  const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
  let lines;

  if (spec.kind === 'd100') {
    lines = ['🎲 Un dé à 100 faces…', `🎯 Résultat : *${rand(1, 100)}*`];
  } else if (spec.kind === 'range') {
    lines = [`🎲 Lancement de 1 à ${spec.max}…`, `🎯 Résultat : *${rand(1, spec.max)}*`];
  } else if (spec.kind === 'dice') {
    const rolls = [];
    let total = 0;
    for (let i = 0; i < spec.dice; i++) {
      const r = rand(1, spec.sides);
      rolls.push(r);
      total += r;
    }
    lines = spec.dice === 1
      ? ['🎲 Un dé à ' + spec.sides + ' faces…', `🎯 Résultat : *${total}*`]
      : [`🎲 ${spec.dice} dés à ${spec.sides} faces…`, `🎯 Total : *${total}*`, `📊 Détail : ${rolls.join(' + ')}`];
  } else if (spec.kind === 'invalid') {
    return replyError(remoteJid, `❌ ${spec.message}`, msg);
  } else {
    return replyError(remoteJid,
      '🎲 *Commande .roll*\n'
      + '• `.roll` — dé à 100\n'
      + '• `.roll 50` — nombre entre 1 et 50\n'
      + '• `.roll 2d6` — dés (style D&D)', msg);
  }
  await sendChunks(remoteJid, lines.join('\n'), msg);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🎲 [dé lancé]`);
}

/**
 * Convertit texte ↔ binaire (fonction pure, testable).
 * Retourne { kind: 'encode', bin } | { kind: 'decode', text } | { error }.
 */
function binConvert(arg) {
  const input = (arg || '').trim();
  if (!input) return { error: 'help' };
  const isSpaced = /^[01\s]+$/.test(input) && /\s/.test(input);
  const isBits = /^[01]+$/.test(input);
  try {
    if (isBits || isSpaced) {
      let bytes;
      if (isSpaced) {
        bytes = input.split(/\s+/).filter(Boolean).map((b) => {
          if (b.length > 8) throw new Error('morceau de plus de 8 bits');
          return parseInt(b, 2);
        });
      } else {
        if (input.length % 8 !== 0) throw new Error('longueur non multiple de 8');
        bytes = input.match(/.{8}/g).map((b) => parseInt(b, 2));
      }
      if (bytes.length > 2000) throw new Error('trop de bits');
      return { kind: 'decode', text: Buffer.from(bytes).toString('utf8') };
    }
    if (input.length > 200) throw new Error('texte trop long (200 caractères max)');
    const bin = [...Buffer.from(input, 'utf8')]
      .map((b) => b.toString(2).padStart(8, '0')).join(' ');
    return { kind: 'encode', bin };
  } catch (err) {
    return { error: err.message };
  }
}

/** .bin — texte ↔ binaire. */
async function handleBin(msg, remoteJid, body) {
  const arg = body.replace(/^\.bin\s*/i, '').trim();
  const res = binConvert(arg);
  if (res.error) {
    if (res.error === 'help') {
      return replyError(remoteJid,
        '💻 *Commande .bin*\n'
        + '• `.bin texte` — code le texte en binaire\n'
        + '• `.bin 01101000 01101001` — décode en texte', msg);
    }
    return replyError(remoteJid, `❌ Conversion impossible (${res.error}).`, msg);
  }
  if (res.kind === 'decode') {
    return sendChunks(remoteJid, `💻 *Binaire → texte*\n\n${res.text}`, msg);
  }
  return sendChunks(remoteJid, `💻 *Texte → binaire*\n\n${res.bin}`, msg);
}

/** .quote — cite joliment le message auquel on répond. */
async function handleQuote(msg, remoteJid, sender) {
  const ctx = msg.message?.extendedTextMessage?.contextInfo || {};
  const quoted = ctx.quotedMessage || null;
  if (!quoted) {
    return replyError(remoteJid,
      '📌 *Commande .quote*\nRÉPONDEZ à un message avec `.quote` pour le citer en beauté.', msg);
  }
  const text = quoted.conversation || quoted.extendedTextMessage?.text
    || quoted.imageMessage?.caption || quoted.videoMessage?.caption || '';
  if (!text.trim()) {
    return replyError(remoteJid, '❌ Le message cité n\'a pas de texte.', msg);
  }
  const who = contactLabel(ctx.participant || ctx.remoteJid || sender);
  await sendChunks(remoteJid, `📌 *Citation*\n\n« ${text.trim()} »\n— ${who}`, msg);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 📌 [citation] ${text.trim().slice(0, 60)}`);
}

/** .ping — mesure la latence bot ↔ backend. */
async function handlePing(msg, remoteJid) {
  const t0 = Date.now();
  try {
    await axios.get(`${FLASK_URL}/health`, { timeout: 5000 });
    const ms = Date.now() - t0;
    return replyError(remoteJid, `🏓 *Pong !* Latence bot ↔ backend : *${ms} ms*`, msg);
  } catch (err) {
    debugLog(`[ping] backend injoignable : ${err.message}`);
    return replyError(remoteJid, '❌ Backend injoignable.', msg);
  }
}

/** Formate une durée écoulée en "12 min" ou "2h05". */
function elapsedLabel(ms) {
  const min = Math.max(0, Math.floor(ms / 60000));
  if (min < 60) return `${min} min`;
  return `${Math.floor(min / 60)}h${String(min % 60).padStart(2, '0')}`;
}

// Anti-spam des notifications AFK : une seule par conversation/membre/30 min
const AFK_NOTIFY_COOLDOWN = 30 * 60 * 1000;
const afkNotified = {}; // "groupe|membre" → timestamp

/* -------------------------------------------------------------------------- */
/*  .clear — purge les messages de la conversation                            */
/* -------------------------------------------------------------------------- */

// Clés des messages vus par le bot (en mémoire, par conversation) :
// la commande .clear les supprime pour tout le monde. WhatsApp limite la
// suppression aux messages de moins de ~2 jours.
const chatKeys = {}; // { [remoteJid]: [WAMessageKey] } — messages vus depuis le démarrage
// { [remoteJid]: [WAMessageKey] } — clés collectées depuis l'historique envoyé
// par le téléphone à la connexion (messaging-history.set). Rempli EN CONTINU.
const historyChatKeys = {};
const CLEAR_MAX = 1000; // nombre max de messages chargés/supprimés en une commande
// Historique à la demande (History Sync On Demand) : le téléphone renvoie les
// vieux messages par lots, ce qui permet de supprimer bien plus que la session.
const CLEAR_HISTORY_BATCH = 100; // messages demandés à chaque requête
const CLEAR_HISTORY_TIMEOUT = 60000; // temps max consacré au chargement (ms)
const CLEAR_HISTORY_ROUNDS = 20; // nombre max de requêtes de pagination
const CLEAR_HISTORY_GROWTH_WAIT = 4000; // attente max d'un lot (ms) : le téléphone
// répond en général en < 2 s ; 4 s suffisent pour chaque round (y compris le
// dernier, vide) sans faire traîner la commande.
const CLEAR_BATCH = 10; // taille des lots (évite le rate-limit WhatsApp)

/**
 * .clear — supprime TOUS les messages de la conversation (pour tout le monde).
 *   - Groupe  : réservé aux administrateurs.
 *   - Privé   : réservé au propriétaire (compte lié au bot).
 * Deux temps : `.clear` affiche le nombre, `.clear oui` confirme et purge.
 */
/**
 * Charge les clés de l'historique d'un chat via History Sync On Demand.
 *
 * Le bot demande au TÉLÉPHONE connecté (sock.fetchMessageHistory) de renvoyer
 * les messages du chat par lots (du plus récent au plus ancien). Les messages
 * arrivent par l'événement "messaging-history.set" (Baileys télécharge la
 * notification d'historique reçue). La boucle s'arrête quand plus rien
 * n'arrive, quand maxKeys est atteint, ou après timeoutMs.
 *
 * Renvoie la liste des clés (WAMessageKey), dédupliquées par id.
 */
async function loadChatHistoryKeys(remoteJid, startKey, maxKeys, timeoutMs) {
  if (!sock || typeof sock.fetchMessageHistory !== 'function') return [];
  const keyMap = new Map(); // id -> clé

  const onHistory = (data) => {
    const messages = (data && Array.isArray(data.messages)) ? data.messages : [];
    for (const m of messages) {
      const k = m && m.key;
      if (k && k.remoteJid === remoteJid && k.id) {
        // On garde le timestamp AVEC la clé : il sert de curseur de pagination
        // (m.key ne le porte pas — c'était un bug : pagination bloquée au round 1).
        keyMap.set(k.id, { ...k, messageTimestamp: m.messageTimestamp });
      }
    }
  };
  sock.ev.on('messaging-history.set', onHistory);
  try {
    const deadline = Date.now() + timeoutMs;
    let cursorKey = startKey;
    let cursorTs = startKey ? startKey.messageTimestamp : null;
    for (let round = 0; round < CLEAR_HISTORY_ROUNDS && Date.now() < deadline; round++) {
      const before = keyMap.size;
      try {
        await sock.fetchMessageHistory(CLEAR_HISTORY_BATCH, cursorKey, cursorTs);
      } catch (err) {
        debugLog(`[clear] historique à la demande refusé : ${err.message}`);
        break;
      }
      // Le téléphone doit répondre : on attend que de nouvelles clés arrivent
      const grew = await waitForHistoryGrowth(() => keyMap.size, before, CLEAR_HISTORY_GROWTH_WAIT);
      if (!grew || keyMap.size >= maxKeys) break;
      // Curseur suivant : le message le PLUS ANCIEN reçu jusqu'ici
      let oldest = null;
      let oldestTs = Number.MAX_SAFE_INTEGER;
      for (const k of keyMap.values()) {
        const ts = toMessageTs(k.messageTimestamp);
        if (ts !== null && ts < oldestTs) {
          oldestTs = ts;
          oldest = k;
        }
      }
      if (!oldest) break;
      cursorKey = oldest;
      cursorTs = oldest.messageTimestamp;
    }
  } finally {
    sock.ev.off('messaging-history.set', onHistory);
  }
  return [...keyMap.values()];
}

/** Attend que sizeFn() dépasse before (arrivée de nouveaux messages d'historique). */
async function waitForHistoryGrowth(sizeFn, before, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (sizeFn() > before) return true;
    await sleep(300);
  }
  return sizeFn() > before;
}

/** Timestamp lisible d'un message (number, Long ou bigint) → number, ou null. */
function toMessageTs(value) {
  if (value == null) return null;
  if (typeof value === 'number') return value;
  if (typeof value === 'bigint') return Number(value);
  if (value && typeof value.toNumber === 'function') {
    try { return value.toNumber(); } catch (_) { /* Long trop grand */ }
  }
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

async function handleClearCommand(msg, remoteJid, sender, isGroup, body) {
  // Autorisations
  if (isGroup) {
    if (!(await isGroupAdmin(remoteJid, sender))) {
      return replyError(remoteJid, '🔒 Cette commande est réservée aux administrateurs du groupe.', msg);
    }
  } else if (!msg.key.fromMe) {
    return replyError(remoteJid, "🔒 Cette commande n'est utilisable que par le propriétaire en message privé.", msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: "🧹 Chargement de l'historique…" }).catch(() => {});

  // 1) Historique à la demande : les vieux messages du chat (paginé)
  const historyKeys = await loadChatHistoryKeys(remoteJid, msg.key, CLEAR_MAX, CLEAR_HISTORY_TIMEOUT);

  // 2) Fusion des 3 sources (dédup par id) :
  //    - historyChatKeys : l'historique collecté en continu à la connexion
  //    - chatKeys         : les messages vus depuis le démarrage du bot
  //    - historyKeys      : ce que l'on-demand a renvoyé (souvent vide)
  const allKeys = new Map();
  for (const k of (historyChatKeys[remoteJid] || [])) {
    if (k && k.id) allKeys.set(k.id, k);
  }
  for (const k of (chatKeys[remoteJid] || [])) {
    if (k && k.id) allKeys.set(k.id, k);
  }
  for (const k of historyKeys) {
    if (k && k.id) allKeys.set(k.id, k);
  }
  const keys = [...allKeys.values()].slice(0, CLEAR_MAX);

  if (!keys.length) {
    return replyError(remoteJid,
      "🧹 Aucun message à purger.\n"
      + "_L'historique n'a pas encore été reçu (le téléphone doit être connecté et synchronisé) ou la conversation est vide._", msg);
  }

  const count = keys.length;
  if (!/^\s*(oui|yes|confirmer|y)\s*$/i.test(body.replace(/^\.clear\s*/i, ''))) {
    return replyError(remoteJid,
      `🧹 *Confirmation requise*\n\n`
      + `${count} message(s) seront supprimés *pour tout le monde*,\n`
      + `et la conversation sera vidée *entièrement* de ton côté.\n`
      + 'Tapez .clear oui pour confirmer.', msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: `🧹 Purge de ${count} message(s)…` }).catch(() => {});

  // 3) Suppression "pour tout le monde", par lots espacés (anti-rate-limit)
  let deleted = 0;
  try {
    for (let i = 0; i < keys.length; i += CLEAR_BATCH) {
      const chunk = keys.slice(i, i + CLEAR_BATCH);
      await Promise.all(chunk.map((k) =>
        sock.sendMessage(remoteJid, { delete: k })
          .then(() => { deleted += 1; })
          .catch(() => { /* déjà supprimé, trop vieux, ou droits insuffisants */ })
      ));
      if (i + CLEAR_BATCH < keys.length) await sleep(400); // espacement anti-rate-limit
    }
  } catch (err) {
    debugLog(`[clear] interruption de la purge : ${err.message}`);
  }

  // 4) Vide ENTIÈREMENT la conversation côté compte (même les vieux messages)
  let clearedLocally = false;
  if (sock && typeof sock.chatModify === 'function') {
    try {
      await sock.chatModify({ clear: true, lastMessages: [] }, remoteJid);
      clearedLocally = true;
    } catch (err) {
      debugLog(`[clear] chatModify échoué : ${err.message}`);
    }
  }

  // On retire de la liste les messages traités (des deux collectes)
  const processed = new Set(keys.map((k) => k.id));
  chatKeys[remoteJid] = (chatKeys[remoteJid] || []).filter((k) => !processed.has(k.id));
  historyChatKeys[remoteJid] = (historyChatKeys[remoteJid] || []).filter((k) => !processed.has(k.id));

  const lines = [`✅ ${deleted}/${count} messages supprimés pour tout le monde.`];
  if (clearedLocally) lines.push('🗑 La conversation a été vidée entièrement de ton côté.');
  lines.push('⚠️ WhatsApp limite la suppression pour tous aux messages de moins de ~2 jours.');
  await sock.sendMessage(remoteJid, { text: lines.join('\n') }, { quoted: msg });
  debugLog(`[clear] ${deleted}/${count} supprimés dans ${remoteJid} (vidage local: ${clearedLocally})`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🧹 [purge ${deleted}/${count}]`);
}

/**
 * .clearmem — efface la mémoire IA de l'utilisateur auquel on RÉPOND.
 *
 * La mémoire IA (contexte de conversation) est stockée côté backend, par
 * expéditeur. En répondant à un message de quelqu'un avec `.clearmem`, on
 * supprime TOUT le contexte mémorisé pour cette personne : l'IA repart de
 * zéro avec elle. Réservé au propriétaire (message depuis le compte lié).
 */
async function handleClearMemCommand(msg, remoteJid, sender) {
  // Autorisation : seul le propriétaire (compte lié) peut effacer une mémoire
  if (!msg.key.fromMe) {
    return replyError(remoteJid, '🔒 Cette commande est réservée au propriétaire du bot.', msg);
  }

  // Cible : l'expéditeur du message CITÉ (participant en groupe, sinon jid)
  const ctx = msg.message?.extendedTextMessage?.contextInfo || {};
  const quoted = ctx.quotedMessage || null;
  const target = ctx.participant || ctx.remoteJid || '';

  if (!quoted || !target) {
    return replyError(remoteJid,
      '🧠 *Commande .clearmem*\n'
      + 'Efface la mémoire IA (contexte) de l\'utilisateur auquel tu réponds.\n\n'
      + 'RÉPONDEZ à un message de la personne concernée avec `.clearmem`\n'
      + 'pour que l\'IA oublie toute sa conversation précédente.', msg);
  }

  if (sock) sock.sendMessage(remoteJid, { text: '🧠 Effacement de la mémoire…' }).catch(() => {});

  let data;
  try {
    const res = await axios.post(`${FLASK_URL}/api/ai/memory/clear`, { sender: target }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 30000,
    });
    data = res.data || {};
  } catch (err) {
    debugLog(`[clearmem] appel backend impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de joindre le service de mémoire. Réessayez.', msg);
  }

  if (!data.ok) {
    debugLog(`[clearmem] refusé : ${data.error || 'inconnu'}`);
    return replyError(remoteJid, `❌ ${data.error || 'Effacement impossible.'}`, msg);
  }

  const who = contactLabel(target);
  const deleted = Number(data.deleted) || 0;
  const lines = [
    `🧠 *Mémoire effacée*`,
    '',
    `${deleted} entrée(s) supprimée(s) pour ${who}.`,
    'L\'IA repart de zéro avec cette personne. 🤖✨',
  ];
  await sendChunks(remoteJid, lines.join('\n'), msg);
  debugLog(`[clearmem] ${deleted} entrées effacées pour ${target}`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🧠 [mémoire effacée] ${who} (${deleted})`);
}

/**
 * Signale à l'expéditeur qu'il écrit à / mentionne un membre absent (AFK).
 * Une seule notification par conversation et par membre (cooldown 30 min).
 */
async function maybeNotifyAfk(msg, remoteJid, sender, isGroup) {
  if (!sock) return;
  const key = msg.key || {};
  if (key.fromMe) return;
  const entries = [];
  if (isGroup) {
    const mentioned = msg.message?.extendedTextMessage?.contextInfo?.mentionedJid || [];
    for (const m of mentioned) {
      const local = jidLocal(m);
      if (local && botState.afk[local]) entries.push(local);
    }
  } else {
    const local = jidLocal(remoteJid);
    if (local && botState.afk[local]) entries.push(local);
  }
  const senderLocal = jidLocal(sender);
  for (const local of entries) {
    if (local === senderLocal) continue;
    const notifyKey = `${jidLocal(remoteJid)}|${local}`;
    const last = afkNotified[notifyKey] || 0;
    if (Date.now() - last < AFK_NOTIFY_COOLDOWN) continue;
    afkNotified[notifyKey] = Date.now();
    const info = botState.afk[local] || {};
    const lines = [`💤 *${contactLabel(local)}* est absent depuis *${elapsedLabel(Date.now() - (info.since || Date.now()))}*.`];
    if (info.reason) lines.push(`📝 Raison : ${info.reason}`);
    lines.push('_Il/elle répondra dès son retour._');
    await sock.sendMessage(remoteJid, { text: lines.join('\n') }, { quoted: msg }).catch(() => {});
  }
}

/** .afk [raison] / .afk off — statut absent. */
async function handleAfk(msg, remoteJid, sender, body) {
  const local = jidLocal(sender);
  const arg = body.replace(/^\.afk\s*/i, '').trim();
  if (/^(off|non|stop|retour|0)$/i.test(arg)) {
    if (!botState.afk[local]) return replyError(remoteJid, 'ℹ️ Tu n\'étais pas marqué absent.', msg);
    delete botState.afk[local];
    saveBotStore();
    return replyError(remoteJid, `👋 Bienvenue de retour, ${contactLabel(sender)} !`, msg);
  }
  botState.afk[local] = { reason: arg, since: Date.now() };
  saveBotStore();
  const extra = arg ? `\n📝 Raison : ${arg}` : '';
  return replyError(remoteJid,
    `💤 ${contactLabel(sender)} est marqué absent.${extra}\n_Quand on t'écrit ou te mentionne, ils le sauront._`, msg);
}

/** Point d'entrée des commandes fun (aucune restriction). */
async function handleFunCommand(msg, remoteJid, sender, body) {
  const cmd = body.split(/\s+/)[0].toLowerCase();
  switch (cmd) {
    case '.roll': return handleRoll(msg, remoteJid, body);
    case '.bin': return handleBin(msg, remoteJid, body);
    case '.quote': return handleQuote(msg, remoteJid, sender);
    case '.ping': return handlePing(msg, remoteJid);
    case '.afk': return handleAfk(msg, remoteJid, sender, body);
    default: return replyError(remoteJid, 'ℹ️ Commande inconnue.', msg);
  }
}

/**
 * .yt <url>    — télécharge la VIDÉO YouTube et l'envoie dans le chat.
 * .audio <url> — télécharge uniquement l'AUDIO de la vidéo.
 *
 * Utilise yt-dlp (binaire externe, toujours à jour face à YouTube ; ytdl-core
 * ne sait plus décrypter les flux actuels). Sans ffmpeg, la vidéo est prise
 * dans un format progressif mp4 (360p/720p) qui contient déjà l'audio : aucune
 * fusion nécessaire. Le fichier temporaire est nettoyé après l'envoi.
 */
async function handleYtCommand(msg, remoteJid, body) {
  const isVideo = /^\.yt\b/i.test(body);
  const url = body.replace(/^\.(?:yt|audio)\b/i, '').trim();

  if (!url || !/^https?:\/\//i.test(url)) {
    return replyError(remoteJid,
      (isVideo
        ? '🎬 *Commande .yt*\nUtilisation : `.yt <lien YouTube>`\nExemple : `.yt https://youtu.be/xxxx`\n\n💡 Pour l\'audio seul, utilisez `.audio <lien>`.'
        : '🎵 *Commande .audio*\nUtilisation : `.audio <lien YouTube>`\nExemple : `.audio https://youtu.be/xxxx`\n\n💡 Pour la vidéo, utilisez `.yt <lien>`.'), msg);
  }

  // Vérification rapide : le lien doit ressembler à une vidéo YouTube
  if (!/(youtube\.com|youtu\.be)/i.test(url)) {
    return replyError(remoteJid, '❌ Lien YouTube invalide. Collez l\'URL complète d\'une vidéo.', msg);
  }

  const cmd = resolveYtDlp();
  if (!cmd) {
    return replyError(remoteJid, '❌ yt-dlp n\'est pas installé. Installez-le (pip install yt-dlp) puis relancez.', msg);
  }

  // 1) Lecture des infos (titre + durée) via yt-dlp --dump-json
  let info = null;
  try {
    const out = await runYtDlp(['--dump-json', '--no-playlist', '--no-warnings', url]);
    info = JSON.parse(out.split('\n')[0]);
  } catch (err) {
    debugLog(`[yt] infos indisponibles : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de lire cette vidéo (privée, supprimée ou indisponible ?).', msg);
  }

  const duration = Number(info?.duration) || 0;
  const maxSeconds = isVideo ? YT_VIDEO_MAX_SECONDS : YT_AUDIO_MAX_SECONDS;
  if (duration > maxSeconds) {
    return replyError(remoteJid,
      `⏱️ ${isVideo ? 'Vidéo' : 'Audio'} trop long${isVideo ? 'ue' : ''} (${Math.round(duration / 60)} min). Maximum : ${Math.round(maxSeconds / 60)} min.`, msg);
  }

  const title = String(info?.title || (isVideo ? 'video' : 'audio')).slice(0, 80);
  // Nom de fichier sûr (sans caractères invalides) et préfixe pour retrouver
  // le fichier réel après le téléchargement (yt-dlp choisit l'extension).
  const safeTitle = title.replace(/[\\/:*?"<>|\r\n]+/g, '_').trim() || 'fichier';
  const filePrefix = `yt-${Date.now()}`;

  // Message de statut pendant le téléchargement
  if (sock) {
    sock.sendMessage(remoteJid, {
      text: isVideo
        ? `🎬 Téléchargement de la vidéo… (${Math.round(duration / 60)} min, ça peut prendre un moment)`
        : '🎵 Téléchargement de l\'audio… (quelques secondes)',
    }).catch(() => {});
  }

  // 2) Téléchargement réel
  // Vidéo : format PROGRESSIF mp4 (vidéo+audio déjà combinés, pas de ffmpeg).
  //   On préfère 720p (22) puis 360p (18), sinon le meilleur mp4 AVEC audio
  //   ([acodec!=none][vcodec!=none]) — sans ça, yt-dlp peut choisir un flux
  //   vidéo SANS audio (ex: 137/299) → vidéo muette envoyée sur WhatsApp.
  // Audio : m4a si possible (meilleure compatibilité WhatsApp), sinon webm.
  const formatSel = isVideo
    ? '22/18/best[ext=mp4][acodec!=none][vcodec!=none]'
    : 'bestaudio[ext=m4a]/bestaudio';
  try {
    await runYtDlp([
      '--no-playlist', '--no-warnings',
      '-f', formatSel,
      '-o', path.join(MEDIA_DIR, filePrefix + '.%(ext)s'),
      '--no-part',
      url,
    ]);
  } catch (err) {
    debugLog(`[yt] téléchargement impossible : ${err.message}`);
    // Nettoyage des fichiers partiels éventuels (timeout / interruption)
    cleanupYtPrefix(filePrefix);
    return replyError(remoteJid,
      `❌ Échec du téléchargement ${isVideo ? 'vidéo' : 'audio'}. Réessayez plus tard.`, msg);
  }

  // Retrouve le fichier réellement créé (extension choisie par yt-dlp)
  let filePath = null;
  let ext = 'mp4';
  try {
    const candidates = fs.readdirSync(MEDIA_DIR)
      .filter((f) => f.startsWith(filePrefix))
      .map((f) => path.join(MEDIA_DIR, f));
    filePath = candidates[0] || null;
    if (filePath) ext = path.extname(filePath).slice(1).toLowerCase();
  } catch (_) { /* dossier illisible */ }

  if (!filePath) {
    return replyError(remoteJid, `❌ ${isVideo ? 'Vidéo' : 'Audio'} introuvable après le téléchargement.`, msg);
  }

  // Garde-fou : fichier trop gros pour WhatsApp
  let size = 0;
  try { size = fs.statSync(filePath).size; } catch (_) { /* fichier introuvable */ }
  if (size > YT_MAX_BYTES) {
    debugLog(`[yt] fichier trop gros : ${Math.round(size / 1048576)} Mo`);
    setTimeout(() => fs.unlink(filePath, () => {}), 5000);
    return replyError(remoteJid,
      `❌ Fichier trop gros pour WhatsApp (${Math.round(size / 1048576)} Mo > 60 Mo). Choisissez une vidéo plus courte.`, msg);
  }

  const mimetype = isVideo
    ? 'video/mp4'
    : (ext === 'm4a' || ext === 'mp4') ? 'audio/mp4'
      : (ext === 'webm') ? 'audio/webm'
        : 'audio/mpeg';

  try {
    if (isVideo) {
      await sock.sendMessage(remoteJid, {
        video: { url: filePath },
        mimetype,
        caption: `🎬 ${title}`,
        fileName: `${safeTitle}.mp4`,
      }, { quoted: msg });
    } else {
      await sock.sendMessage(remoteJid, {
        audio: { url: filePath },
        mimetype,
        fileName: `${safeTitle}.${ext}`,
        ptt: false, // audio normal (pas une note vocale)
      }, { quoted: msg });
    }
    debugLog(`[yt] ${isVideo ? 'vidéo' : 'audio'} envoyé : ${title} (${Math.round(size / 1048576)} Mo, .${ext})`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ${isVideo ? '🎬 [vidéo envoyée]' : '🎵 [audio envoyé]'} ${title}`);
  } catch (err) {
    debugLog(`[yt] envoi impossible : ${err.message}`);
    return replyError(remoteJid, `❌ Envoi de la ${isVideo ? 'vidéo' : 'l\'audio'} impossible.`, msg);
  } finally {
    // Nettoyage différé du fichier temporaire
    setTimeout(() => fs.unlink(filePath, () => {}), 60000);
  }
}

/** Exécute yt-dlp et résout avec stdout (rejette en cas d'erreur ou de timeout). */
function runYtDlp(args) {
  return new Promise((resolve, reject) => {
    const cmd = resolveYtDlp();
    const child = execFile(cmd[0], [...cmd.slice(1), ...args], {
      timeout: YT_DOWNLOAD_TIMEOUT_MS,
      maxBuffer: 10 * 1024 * 1024,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    }, (err, stdout, stderr) => {
      if (err) {
        reject(new Error((stderr || err.message || '').split('\n').filter(Boolean).slice(-2).join(' | ')));
      } else {
        resolve(stdout);
      }
    });
  });
}

/**
 * Note vocale reçue → transcription Whisper (via le backend) → réponse IA.
 *
 * Si la transcription est désactivée côté backend, on ne fait rien : la note
 * vocale est déjà tracée dans le transcript comme "🎵 [audio]".
 */
async function handleVoiceNote(msg, key, remoteJid, sender) {
  let buffer;
  try {
    buffer = await downloadMediaMessage(msg, 'buffer', {});
  } catch (err) {
    debugLog(`[vocal] téléchargement impossible : ${err.message}`);
    return;
  }

  const mime = msg.message.audioMessage?.mimetype || 'audio/ogg; codecs=opus';
  const audioB64 = buffer.toString('base64');

  let data;
  try {
    const { data: res } = await axios.post(`${FLASK_URL}/api/ai/transcribe`, {
      audio: audioB64,
      mime,
    }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 120000,
    });
    data = res;
  } catch (err) {
    debugLog(`[vocal] erreur backend (transcription) : ${err.message}`);
    return;
  }

  if (!data?.transcribed || !data.text) {
    debugLog(`[vocal] non transcrit : ${data?.reason || data?.error || 'refusé'}`);
    return;
  }

  const text = data.text.trim();
  debugLog(`[vocal] transcrit (${data.duration_ms || 0} ms) : ${text.slice(0, 120)}`);

  // Journal de conversations : la transcription avec le nom du contact
  try {
    const who = await resolveSenderName(remoteJid, sender, msg.pushName);
    transcriptLog(`[${fmtStamp(new Date())}] ${who} : 🎤 ${text}`);
  } catch (_) { /* non bloquant */ }

  // Le texte transcrit est traité comme un message vocal : l'IA répond
  try {
    const { data: ai } = await axios.post(`${FLASK_URL}/api/message`, {
      from: sender,
      remoteJid,
      body: text,
      isGroup: remoteJid.endsWith('@g.us'),
      voice: true,
      messageId: key.id,
      timestamp: msg.messageTimestamp,
    }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 90000,
    });

    if (ai?.reply) {
      await sendChunks(remoteJid, ai.reply, msg);
      debugLog('[vocal] réponse IA envoyée');
    } else {
      debugLog('[vocal] backend a ignoré la transcription');
    }
  } catch (err) {
    debugLog(`[vocal] erreur traitement : ${err.message}`);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Commande secrète — simulation de brèche en plusieurs étapes (100 % fictif).
 * La progression est envoyée message par message avec de petites pauses.
 */
async function handleSecretCommand(remoteJid, quoted) {
  const ip = `192.168.${Math.floor(Math.random() * 255)}.${Math.floor(Math.random() * 255)}`;
  const targets = [
    'banque-centrale.gov', 'serveur-nasa.internal',
    'base-secrete.mil', 'data-centre-suisse.ch',
  ];
  const target = targets[Math.floor(Math.random() * targets.length)];

  const steps = [
    '🖥️ INITIALISATION DU PROTOCOLE…',
    `🎯 Cible : ${target} (${ip})`,
    '[■□□□□□□□□□] Connexion au serveur distant…',
    '[■■■□□□□□□□] Contournement du pare-feu…',
    '[■■■■■□□□□□] Décryptage SSL (AES-256)…',
    '[■■■■■■■□□□] Injection de la charge utile…',
    '[■■■■■■■■■□] Extraction des données…',
    '[■■■■■■■■■■] 100 % — ACCÈS ACCORDÉ 🔓',
  ];

  for (const step of steps) {
    if (!sock) return;
    await sock.sendMessage(remoteJid, { text: step }, { quoted });
    await sleep(600 + Math.floor(Math.random() * 700));
  }

  const volume = Math.floor(Math.random() * 9000) + 1000;
  const volumeFr = volume.toLocaleString('fr-FR');
  if (sock) {
    await sock.sendMessage(remoteJid, {
      text: `📦 ${volumeFr} octets exfiltrés\n`
        + '🔑 Mot de passe : ********\n\n'
        + "😄 Respire… c'était juste une simulation !\n"
        + 'Utilise-la pour impressionner tes groupes 👀',
    });
  }
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🖥️ [simulation terminée] ${target}`);
}

/**
 * .transcript — transcrit la note vocale CITÉE (réponse ".transcript" à un vocal).
 *
 * Fonctionne à la demande, sans dépendre du réglage "transcrire les vocaux"
 * du panneau : seul une clé GROQ est nécessaire.
 */
async function handleTranscriptCommand(msg, remoteJid) {
  const quoted = msg.message?.extendedTextMessage?.contextInfo?.quotedMessage || null;
  const quotedAudio = quoted?.audioMessage;

  if (!quotedAudio || !quotedAudio.ptt) {
    return replyError(remoteJid,
      '🎤 *Commande .transcript*\n'
      + 'RÉPONDEZ à une note vocale avec `.transcript`\n'
      + 'pour obtenir sa transcription en texte, suivie d\'un résumé IA.', msg);
  }

  // Message de statut pendant la transcription
  if (sock) {
    sock.sendMessage(remoteJid, { text: '🎤 Transcription en cours…' }).catch(() => {});
  }

  let buffer;
  try {
    buffer = await downloadMediaMessage({ ...msg, message: quoted }, 'buffer', {});
  } catch (err) {
    debugLog(`[transcript] téléchargement impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de télécharger la note vocale. Réessayez.', msg);
  }

  const mime = quotedAudio.mimetype || 'audio/ogg; codecs=opus';

  let data;
  try {
    const { data: res } = await axios.post(`${FLASK_URL}/api/ai/transcribe`, {
      audio: buffer.toString('base64'),
      mime,
      manual: true, // transcription demandée explicitement : pas besoin du toggle
    }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 120000,
    });
    data = res;
  } catch (err) {
    debugLog(`[transcript] erreur backend : ${err.message}`);
    return replyError(remoteJid, '❌ Erreur lors de la transcription. Réessayez.', msg);
  }

  if (!data?.transcribed || !data.text) {
    debugLog(`[transcript] refusé : ${data?.error || 'inconnu'}`);
    return replyError(remoteJid, `❌ ${data?.error || 'Transcription indisponible.'}`, msg);
  }

  const text = data.text.trim();
  debugLog(`[transcript] transcrit (${data.duration_ms || 0} ms) : ${text.slice(0, 120)}`);

  // 1) La transcription d'abord (message séparé)
  await sendChunks(remoteJid, `🎤 *Transcription*\n\n${text}`, msg);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🎤 [transcription demandée] ${text}`);

  // 2) Puis le résumé IA du contenu (message séparé). Une panne du résumé ne
  //    doit jamais effacer la transcription déjà envoyée : on journalise et on
  //    prévient discrètement.
  if (sock) sock.sendMessage(remoteJid, { text: '📝 Résumé en cours…' }).catch(() => {});

  let rdata;
  try {
    const res = await axios.post(`${FLASK_URL}/api/resume`, { text }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 90000,
    });
    rdata = res.data || {};
  } catch (err) {
    debugLog(`[transcript] résumé : appel backend impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Transcription envoyée, mais résumé indisponible (backend injoignable).', msg);
  }

  if (!rdata.ok || !rdata.summary) {
    debugLog(`[transcript] résumé refusé : ${rdata.error || 'inconnu'}`);
    return replyError(remoteJid, `❌ Transcription envoyée, mais résumé impossible : ${rdata.error || 'erreur inconnue'}.`, msg);
  }

  const lines = ['📝 *Résumé de la note vocale*', '', rdata.summary.trim(), ''];
  const secs = Math.round((rdata.duration_ms || 0) / 1000);
  lines.push(`_via ${rdata.model || 'IA'}` + (secs > 0 ? ` · ${secs} s_` : '_'));
  await sendChunks(remoteJid, lines.join('\n'), msg);
  debugLog(`[transcript] résumé envoyé (${rdata.summary.length} caractères)`);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 📝 [résumé du vocal] ${rdata.summary.slice(0, 60)}`);
}

/**
 * Envoie un texte éventuellement long en découpant en messages < 4000 caractères
 * (limite de WhatsApp) tout en conservant la citation du message d'origine.
 */
async function sendChunks(remoteJid, text, quoted) {
  if (!sock) return; // plus de socket actif : on abandonne proprement
  const MAX_LENGTH = 3900;
  const parts = [];
  let current = '';

  for (const line of text.split('\n')) {
    const candidate = current ? `${current}\n${line}` : line;
    if (candidate.length > MAX_LENGTH && current) {
      parts.push(current);
      current = line;
    } else {
      current = candidate;
    }
  }
  if (current) parts.push(current);

  for (const part of parts) {
    await sock.sendMessage(remoteJid, { text: part }, { quoted });
    if (TRANSCRIPT_ENABLED) {
      transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : ${part}`);
    }
  }
}

/* -------------------------------------------------------------------------- */
/*  Serveur interne Express (health + redémarrage)                            */
/* -------------------------------------------------------------------------- */

const server = express();
server.use(express.json());

// Garde-fou : les endpoints internes exigent la clé partagée
function checkBotKey(req, res, next) {
  if (req.headers['x-bot-key'] !== BOT_API_KEY) {
    return res.status(401).json({ error: 'Non autorisé' });
  }
  next();
}

server.get('/health', (_req, res) => {
  res.json({ ok: true, status: currentStatus, number: sock?.user?.id || null });
});

// Téléchargement du journal de conversations (transcript.txt)
server.get('/transcript', checkBotKey, (_req, res) => {
  try {
    if (!fs.existsSync(TRANSCRIPT_FILE)) {
      return res.status(404).json({ error: 'Le transcript est vide ou désactivé.' });
    }
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="transcript.txt"');
    res.sendFile(TRANSCRIPT_FILE);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

server.post('/internal/restart', checkBotKey, (_req, res) => {
  console.log('🔄 Redémarrage demandé par le backend…');
  res.json({ ok: true });

  // Le gestionnaire 'close' détecte restartRequested et relance proprement
  // via scheduleReconnect (au plus une reconnexion à la fois).
  restartRequested = true;
  try {
    if (sock) sock.end(undefined); // ferme proprement la connexion
  } catch (err) {
    console.error('Erreur lors de la fermeture du socket :', err.message);
  }

  // Filet de sécurité : si aucun 'close' n'arrive dans 4 s (socket bloqué),
  // on force la fermeture puis on relance. Si 'close' est déjà arrivé,
  // restartRequested est false → aucune double reconnexion possible.
  setTimeout(() => {
    if (!restartRequested) return;
    restartRequested = false;
    try { if (sock) sock.end(undefined); } catch (err) { /* déjà fermé */ }
    sock = null;
    currentStatus = 'disconnected'; // état cohérent avant la reconnexion
    startPending = false;
    scheduleReconnect(500);
  }, 4000);
});

// Si le port est déjà occupé, une autre instance du bot tourne déjà :
// on s'arrête immédiatement avec un message clair (deux instances en même
// temps = erreurs "Key used already" / "Bad MAC").
server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`❌ Le port ${BOT_PORT} est déjà utilisé.`);
    console.error('   Une autre instance du bot est-elle déjà ouverte ?');
    console.error('   Fermez-la (arreter-bot.bat) puis relancez ce bot.');
  } else {
    console.error('💥 Erreur du serveur interne :', err.message);
  }
  process.exit(1);
});

// Démarrage du serveur interne + du bot
server.listen(BOT_PORT, () => {
  console.log(`🟢 Serveur interne démarré sur le port ${BOT_PORT}`);
  startBot();
});
