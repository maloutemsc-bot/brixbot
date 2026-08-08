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

const express = require('express');
const axios = require('axios');
const pino = require('pino');
const qrcodeTerminal = require('qrcode-terminal');
const QRCode = require('qrcode');

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
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
    && /decrypt message|MessageCounterError|Bad MAC|session_cipher/i.test(a));
}
const _origError = console.error;
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

const FLASK_URL = (process.env.FLASK_INTERNAL_URL || 'http://localhost:5000').replace(/\/+$/, '');
const BOT_PORT = parseInt(process.env.BOT_PORT || '3000', 10);
const BOT_API_KEY = process.env.BOT_API_KEY || 'changez-moi-bot';
const AUTH_DIR = process.env.AUTH_DIR || 'auth_info';
const LOG_LEVEL = process.env.LOG_LEVEL || 'silent';

const logger = pino({ level: LOG_LEVEL });

let sock = null;
let currentStatus = 'disconnected'; // disconnected | connecting | qr | connected
let startPending = false;          // un socket est en cours de création
let reconnectTimer = null;         // un seul timer de reconnexion à la fois
let restartRequested = false;      // redémarrage demandé par le backend

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
 */
async function notifyBackend(status, extra = {}) {
  try {
    await axios.post(`${FLASK_URL}/api/whatsapp/status`, { status, ...extra }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 8000,
    });
  } catch (err) {
    console.error(`[backend] Impossible d'envoyer le statut "${status}" : ${err.message}`);
  }
}

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
        console.log('\n📱 Scannez le QR code ci-dessous pour connecter WhatsApp :\n');
        try {
          qrcodeTerminal.generate(qr, { small: true });
        } catch (err) {
          console.error('Erreur d\'affichage du QR dans le terminal :', err.message);
        }
        // Génère une image PNG et l'envoie au backend pour le panneau
        try {
          const dataUrl = await QRCode.toDataURL(qr, {
            width: 400,
            margin: 2,
            errorCorrectionLevel: 'L',
          });
          await notifyBackend('qr', { qr: dataUrl });
        } catch (err) {
          console.error('Erreur de génération du QR PNG :', err.message);
        }
      }

      if (connection === 'open') {
        currentStatus = 'connected';
        console.log('✅ WhatsApp connecté :', sock.user?.id || 'inconnu');
        await notifyBackend('connected', { number: sock.user?.id || null });
      } else if (connection === 'connecting') {
        currentStatus = 'connecting';
        console.log('⏳ Connexion en cours…');
      } else if (connection === 'close') {
        currentStatus = 'disconnected';
        const code = lastDisconnect?.error?.output?.statusCode;
        const loggedOut = code === DisconnectReason.loggedOut;
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
      if (type !== 'notify') return;
      for (const msg of messages) {
        handleMessage(msg).catch((err) => console.error('[message] Erreur :', err.message));
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
async function handleMessage(msg) {
  // Ignore nos propres messages et les statuts
  if (msg.key?.fromMe) return;
  if (msg.key?.remoteJid === 'status@broadcast') return;

  const body = extractText(msg);
  if (!body || !body.trim()) return;

  const remoteJid = msg.key.remoteJid;
  const sender = msg.key.participant || remoteJid;
  const isGroup = remoteJid.endsWith('@g.us');

  // Marque le message comme lu
  if (sock) sock.readMessages([msg.key]).catch(() => {});

  try {
    const { data } = await axios.post(`${FLASK_URL}/api/message`, {
      from: sender,
      remoteJid,
      body: body.trim(),
      isGroup,
      messageId: msg.key.id,
      timestamp: msg.messageTimestamp,
    }, {
      headers: { 'X-Bot-Key': BOT_API_KEY, 'Content-Type': 'application/json' },
      timeout: 90000,
    });

    if (data?.ignore) return;                      // le backend ne veut pas répondre
    if (data?.reply) await sendChunks(remoteJid, data.reply, msg);
  } catch (err) {
    console.error('Erreur lors du traitement du message :', err.message);
  }
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
