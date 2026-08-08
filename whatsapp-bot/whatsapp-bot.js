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

const sharp = require('sharp');
const ytdl = require('@distube/ytdl-core');

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
// Durée maximale d'une vidéo YouTube téléchargeable (.yt) : 60 minutes
const YT_MAX_SECONDS = 3600;

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

    // Messages entrants
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
      debugLog(`[upsert] type=${type} nb=${messages.length}`);
      if (type !== 'notify') return;
      for (const msg of messages) {
        handleMessage(msg).catch((err) => {
          debugLog(`[ERREUR] traitement du message : ${err.message}`);
          console.error('[message] Erreur :', err.message);
        });
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

  // Messages envoyés depuis le compte lié (fromMe) : on ne traite QUE les
  // commandes (qui commencent par ".") afin que l'utilisateur puisse utiliser
  // le bot depuis son propre WhatsApp. Les réponses du bot (texte normal)
  // restent ignorées → aucune boucle possible.
  if (key.fromMe && !trimmed.startsWith('.')) return;

  const isGroup = remoteJid.endsWith('@g.us');

  // Marque le message comme lu
  if (sock) sock.readMessages([key]).catch(() => {});

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

  // --- Commandes médias gérées directement par le bot (.sticker / .yt) ---
  if (/^\.(sticker|yt|audio)\b/i.test(trimmed)) {
    debugLog(`[media] commande locale : ${trimmed}`);
    handleMediaCommand(msg, key, trimmed).catch((err) => {
      debugLog(`[media] erreur : ${err.message}`);
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
 * .yt <url> — télécharge l'audio d'une vidéo YouTube et l'envoie dans le chat.
 * Le fichier est nettoyé du dossier temporaire après l'envoi.
 */
async function handleYtCommand(msg, remoteJid, body) {
  const url = body.replace(/^\.(?:yt|audio)\b/i, '').trim();

  if (!url) {
    return replyError(remoteJid,
      '🎵 *Commande .yt*\n'
      + 'Utilisation : `.yt <lien YouTube>`\n'
      + 'Exemple : `.yt https://youtu.be/xxxx`', msg);
  }
  if (!ytdl.validateURL(url)) {
    return replyError(remoteJid, '❌ Lien YouTube invalide. Collez l\'URL complète d\'une vidéo.', msg);
  }

  let info;
  try {
    info = await ytdl.getInfo(url);
  } catch (err) {
    debugLog(`[yt] infos indisponibles : ${err.message}`);
    return replyError(remoteJid, '❌ Impossible de lire cette vidéo (privée ou supprimée ?).', msg);
  }

  const duration = Number(info.videoDetails.lengthSeconds) || 0;
  if (duration > YT_MAX_SECONDS) {
    return replyError(remoteJid,
      `⏱️ Vidéo trop longue (${Math.round(duration / 60)} min). Maximum : ${Math.round(YT_MAX_SECONDS / 60)} min.`, msg);
  }

  const title = (info.videoDetails.title || 'audio').slice(0, 80);
  const format = ytdl.chooseFormat(info.formats, { filter: 'audioonly', quality: 'lowestaudio' });
  const container = (format?.container || 'webm').toLowerCase();
  const ext = (container === 'm4a' || container === 'mp4') ? 'm4a'
    : (container === 'mp3' ? 'mp3' : 'webm');
  const mimetype = (container === 'm4a' || container === 'mp4') ? 'audio/mp4'
    : (container === 'mp3' ? 'audio/mpeg' : 'audio/ogg; codecs=opus');
  const filePath = path.join(MEDIA_DIR, `yt-${Date.now()}.${ext}`);

  // Message de statut pendant le téléchargement (peut prendre quelques secondes)
  if (sock) {
    sock.sendMessage(remoteJid, {
      text: '⏳ Téléchargement de l\'audio… (quelques secondes)',
    }).catch(() => {});
  }

  try {
    const stream = ytdl(url, { format });
    await new Promise((resolve, reject) => {
      const write = fs.createWriteStream(filePath);
      // Garde-fou : le téléchargement ne doit jamais rester bloqué
      const timer = setTimeout(() => {
        stream.destroy();
        reject(new Error('timeout de téléchargement (120 s)'));
      }, 120000);
      stream.pipe(write);
      stream.on('error', (err) => { clearTimeout(timer); reject(err); });
      write.on('finish', () => { clearTimeout(timer); resolve(); });
      write.on('error', (err) => { clearTimeout(timer); reject(err); });
    });
  } catch (err) {
    debugLog(`[yt] téléchargement impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Échec du téléchargement audio. Réessayez plus tard.', msg);
  }

  try {
    await sock.sendMessage(remoteJid, {
      audio: { url: filePath },
      mimetype,
      fileName: `${title}.${ext}`,
      ptt: false, // audio normal (pas une note vocale)
    }, { quoted: msg });
    debugLog(`[yt] audio envoyé : ${title}`);
    transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🎵 [audio envoyé] ${title}`);
  } catch (err) {
    debugLog(`[yt] envoi impossible : ${err.message}`);
    return replyError(remoteJid, '❌ Envoi de l\'audio impossible.', msg);
  } finally {
    // Nettoyage différé du fichier temporaire
    setTimeout(() => fs.unlink(filePath, () => {}), 60000);
  }
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
      + 'pour obtenir sa transcription en texte.', msg);
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

  await sendChunks(remoteJid, `🎤 *Transcription*\n\n${text}`, msg);
  transcriptLog(`[${fmtStamp(new Date())}] 🤖 BrixBot : 🎤 [transcription demandée] ${text}`);
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
