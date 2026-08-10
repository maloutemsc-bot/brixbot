"""
whatsapp_handler.py — Traitement des messages WhatsApp entrants.

Priorité des messages :
  0. Message vocal transcrit (voice=True) → droit à l'IA (jamais de commande)
  1. .help / .menu → aide générale (tout le monde)
  2. .blacklist / .unblacklist / .me / .stats → commandes propriétaire
  3. .search / .tel → recherche BrixHub (nom ou numéro)
  3b. .meteo / .traduis / .devise → outils gratuits (sans clé)
  3c. .crypto / .8ball / .blague / .horoscope → commandes surprises
  3d. .ask [question] → question à l'IA (tout le monde, clé GROQ requise)
  4. .ia (propriétaire) → gestion whitelist + liste noire
  5. IA automatique (si activée ET conversation autorisée) → GROQ
     (la liste noire est PRIORITAIRE : un chat banni est toujours muet)
  6. Réponse automatique par défaut
  7. Sinon → le message est ignoré
"""

import os
import re
import time

import ai_service
import brixhub_service
import tools_service
from database import (AIConfig, AILog, AIMemory, BotConfig, CommandLog,
                      DEFAULT_ASK_PROMPT, DEFAULT_SYSTEM_PROMPT, db, utc_now_iso)

# Message d'aide affiché pour une commande .search incomplète
USAGE_SEARCH = (
    "📚 *Commande .search*\n"
    "Utilisation : `.search nom [prénom] [ville]`\n"
    "Exemples :\n"
    "• `.search Dupont`\n"
    "• `.search Dupont Jean`\n"
    "• `.search Dupont Jean Paris`"
)

# Message d'aide affiché pour une commande .tel incomplète
USAGE_TEL = (
    "📞 *Commande .tel*\n"
    "Utilisation : `.tel [numéro]`\n"
    "Exemples :\n"
    "• `.tel 0612345678`\n"
    "• `.tel 06 12 34 56 78`"
)

# Message d'aide affiché pour une commande .meteo incomplète
USAGE_METEO = (
    "🌦️ *Commande .meteo*\n"
    "Utilisation : `.meteo [ville]`\n"
    "Exemple : `.meteo Paris`\n"
    "Météo en temps réel, gratuite et sans clé API."
)

# Message d'aide affiché pour une commande .traduis incomplète
USAGE_TRAD = (
    "🌐 *Commande .traduis*\n"
    "Utilisation : `.traduis [texte]`\n"
    "Exemple : `.traduis Hello, how are you?`\n"
    "Traduit automatiquement le texte en français."
)

# Message d'aide affiché pour une commande .devise incomplète
USAGE_DEVISE = (
    "💱 *Commande .devise*\n"
    "Utilisation : `.devise [montant] [devise] [devise]`\n"
    "Exemple : `.devise 100 EUR USD`\n"
    "Devises supportées : EUR, USD, GBP, JPY, CHF, CAD, MAD…"
)

# Message d'aide affiché pour une commande .ask sans question
USAGE_ASK = (
    "❓ *Commande .ask*\n"
    "Pose une question à l'IA, qui répond directement.\n\n"
    "Utilisation : `.ask [question]`\n"
    "Exemple : `.ask c'est quoi la capitale du Japon ?`\n\n"
    "Accessible à tout le monde."
)

# Message par défaut (réponse automatique sans IA)
DEFAULT_RESPONSE = (
    "👋 Bonjour ! Je suis un assistant de recherche.\n"
    "Utilisez la commande `.search nom [prénom] [ville]` pour trouver des informations.\n\n"
    "Exemple : `.search Dupont Jean Paris`"
)

# Aide affichée quand `.ia` est utilisé alors qu'aucun propriétaire n'est défini
OWNER_HINT = (
    "🔒 Les commandes `.ia` sont réservées au propriétaire du bot.\n"
    "Ajoutez votre numéro dans `backend/.env` (ex: `OWNER_NUMBER=33612345678`)\n"
    "puis relancez le backend (arreter-bot.bat puis demarrer-bot.bat)."
)

# Message d'aide général (commande .help)
HELP_TEXT = (
    "🤖 *BrixBot — Commandes*\n"
    "• `.search nom [prénom] [ville]` — chercher une personne\n"
    "• `.tel numéro` — chercher un numéro de téléphone\n"
    "• `.meteo ville` — météo en temps réel\n"
    "• `.traduis texte` — traduire en français\n"
    "• `.devise 100 EUR USD` — conversion de devises\n"
    "• `.me` — mon utilisation et mon état IA\n"
    "🤫 Psst… il existe des commandes secrètes. Sois curieux !\n"
    "• `.stats` — statistiques du bot (propriétaire)\n"
    "• `.blacklist` / `.unblacklist` — silence de l'IA ici (propriétaire)\n"
    "• `.ia` — gestion de l'IA dans cette conversation (propriétaire)\n"
    "🖼 Envoyez une photo avec la légende `.sticker` pour la transformer en sticker.\n"
    "🎬 `.yt lien` — télécharge la VIDÉO YouTube (.audio lien → audio seul).\n"
    "🎤 `.transcript` (réponse à un vocal) — transcrit la note vocale.\n"
    "📥 `.extract` (réponse à une image/vocal) — renvoie le média dans le chat.\n"
    "🔍 `.meta` (réponse à un message) — affiche ses métadonnées techniques.\n"
    "📦 `.json` (réponse à un message) — affiche le message en JSON brut.\n"
    "📝 `.resume` (réponse à un message) — résume le message avec l'IA.\n"
    "❓ `.ask question` — pose une question à l'IA, tout le monde peut l'utiliser.\n"
    "🆔 `.id` — affiche les identifiants de la conversation.\n"
    "🗣 `.tts texte` — transforme un texte en note vocale.\n"
    "    🌐 `.translate en texte` — traduire vers la langue de ton choix (ou réponse à un message)\n"
    "    ✏️ `.correct` (réponse à un message ou `.correct texte`) — corrige l'orthographe et la grammaire (IA)\n"
    "📖 `.ocr` (réponse à une photo) — lit le texte de l'image.\n"
    "    📌 .pin chat — envoie des images (gratuit) · .pin nsfw chat pour désactiver le filtre 🔞\n"
    "👑 *Groupe (admins)* : `.kick` · `.mute` · `.unmute` · `.promote` · `.demote` · `.tagall` · `.link` · `.close` · `.open` · `.revoke`\n"
    "⚠️ *Modération (admins)* : `.warn` · `.unwarn` · `.warns` · `.resetwarn` · `.welcome` · `.antilink`\n"
    "🎲 `.roll` (`.roll 2d6`) · 💻 `.bin texte` · 📌 `.quote` (réponse) · 🏓 `.ping` · 💤 `.afk [raison]`\n"
    "🧹 `.clear` — purge les messages récents de la conversation (admin en groupe, proprio en privé)\n"
    "🧠 `.clearmem` (réponse à un message) — efface la mémoire IA de l'utilisateur (propriétaire)\n"
    "🎤 Les notes vocales sont transcrites automatiquement (réglage dans le panneau).\n"
    "💬 Envoie un message normal pour discuter avec l'IA."
)


# --------------------------------------------------------------------------- #
#  Utilitaires
# --------------------------------------------------------------------------- #
def _digits(value):
    """Extrait uniquement les chiffres d'une chaîne (pour comparer les numéros)."""
    return re.sub(r"\D", "", value or "")


def _phone_key(digits):
    """
    Normalise un numéro français local (06... / 07...) au format international
    (336... / 337...) pour permettre des comparaisons cohérentes.
    """
    digits = digits or ""
    if len(digits) == 10 and digits.startswith("0"):
        return "33" + digits[1:]
    return digits


def _jid_local(value):
    """
    Partie locale d'un identifiant WhatsApp, sans le suffixe d'appareil.
    Ex : "33612345678:15@s.whatsapp.net" -> "33612345678".
    """
    if not value:
        return ""
    return (str(value).split("@")[0].split(":")[0] or "").lower()


def _is_owner(sender):
    """
    Vrai si l'expéditeur est le propriétaire du bot (variable OWNER_NUMBER).

    OWNER_NUMBER accepte le format local (0612345678) ou international (33612345678).
    """
    owner = os.environ.get("OWNER_NUMBER", "").strip()
    if not owner:
        return False
    owner_key = _phone_key(_digits(owner))
    return bool(owner_key) and owner_key == _phone_key(_digits(sender))


def _whitelist_list(ai_cfg):
    """Convertit le champ texte ai_whitelist en liste d'entrées nettoyées."""
    if not ai_cfg or not ai_cfg.ai_whitelist:
        return []
    return [
        line.strip()
        for line in str(ai_cfg.ai_whitelist).splitlines()
        if line.strip()
    ]


def _blacklist_list(ai_cfg):
    """Convertit le champ texte ai_blacklist en liste d'entrées nettoyées."""
    if not ai_cfg or not ai_cfg.ai_blacklist:
        return []
    return [
        line.strip()
        for line in str(ai_cfg.ai_blacklist).splitlines()
        if line.strip()
    ]


def _entry_matches(entry, sender_local, chat_local, sender_key, chat_key):
    """
    Vrai si une entrée de la liste (numéro ou identifiant) correspond
    à l'expéditeur ou à la conversation. La normalisation téléphone est
    appliquée dans les deux branches pour rester cohérent.
    """
    if "@" in entry:  # identifiant complet (ex: 336...@s.whatsapp.net ou groupe@g.us)
        entry_local = _jid_local(entry)
        if entry_local and (entry_local == sender_local or entry_local == chat_local):
            return True
        entry_key = _phone_key(_digits(entry_local))
        return bool(entry_key) and (entry_key == sender_key or entry_key == chat_key)
    # simple numéro (ex: 0612345678)
    entry_key = _phone_key(_digits(entry))
    return bool(entry_key) and (entry_key == sender_key or entry_key == chat_key)


def _chat_blacklisted(ai_cfg, sender, remote_jid, is_group=False):
    """
    Vrai si la conversation est sur la liste noire (l'IA doit se taire ici).
    Même logique de normalisation que la whitelist.
    """
    entries = _blacklist_list(ai_cfg)
    if not entries:
        return False

    sender_local = _jid_local(sender)
    chat_local = _jid_local(remote_jid)
    sender_key = _phone_key(_digits(sender_local))
    chat_key = _phone_key(_digits(chat_local))

    if is_group:
        return any(
            _entry_matches(entry, "", chat_local, "", chat_key)
            for entry in entries
        )
    return any(
        _entry_matches(entry, sender_local, sender_local, sender_key, sender_key)
        for entry in entries
    )


def _chat_allowed(whitelist_text, sender, remote_jid, is_group=False):
    """
    Détermine si l'IA peut répondre dans cette conversation.

    - Whitelist vide → l'IA répond à tout le monde (comportement par défaut).
    - En message privé : on teste l'expéditeur.
    - Dans un groupe : seul le groupe est pris en compte (un contact listé
      dans la whitelist n'active pas l'IA dans tous les groupes).
    """
    entries = [
        line.strip()
        for line in (whitelist_text or "").splitlines()
        if line.strip()
    ]
    if not entries:
        return True

    sender_local = _jid_local(sender)
    chat_local = _jid_local(remote_jid)
    sender_key = _phone_key(_digits(sender_local))
    chat_key = _phone_key(_digits(chat_local))

    if is_group:
        # Dans un groupe : seule l'identité du groupe est prise en compte
        return any(
            _entry_matches(entry, "", chat_local, "", chat_key)
            for entry in entries
        )

    # En message privé : l'expéditeur et la conversation sont identiques
    return any(
        _entry_matches(entry, sender_local, sender_local, sender_key, sender_key)
        for entry in entries
    )


def _parse_query(query):
    """Découpe une requête .search en (nom, prénom, ville)."""
    parts = query.split()
    nom = parts[0] if parts else ""
    prenom = parts[1] if len(parts) > 1 else ""
    ville = " ".join(parts[2:]) if len(parts) > 2 else ""
    return nom, prenom, ville


def _parse_phone(query):
    """Nettoie un numéro de téléphone (garde uniquement chiffres et +)."""
    return re.sub(r"[^\d+]", "", query or "")


def build_label(nom, prenom, ville):
    """Libellé humain d'une recherche (ex: "Dupont Jean (Paris)")."""
    label = " ".join(part for part in (nom, prenom) if part)
    if ville:
        label += f" ({ville})"
    return label or "recherche"


# --------------------------------------------------------------------------- #
#  Point d'entrée principal
# --------------------------------------------------------------------------- #
def handle_message(body, sender="", remote_jid="", is_group=False, voice=False):
    """
    Traite un message WhatsApp reçu via l'API /api/message.

    Chaque message reçu est journalisé dans command_logs, MÊME ceux qui sont
    ignorés, avec le motif exact : le panneau montre ainsi tout le trafic.

    Renvoie un dict :
      {"reply": "..."}  → le bot doit répondre ce texte
      {"ignore": True}  → le bot ne répond rien
    """
    body = (body or "").strip()
    start = time.perf_counter()
    elapsed = 0.0

    # 0) Message vocal transcrit (note vocale → texte via Whisper).
    #    On ignore TOUTES les commandes : le texte transcrit va directement
    #    à l'IA, en respectant la liste noire et la whitelist.
    if voice:
        # Garde-fou : transcription vide → rien à envoyer à l'IA
        if not body:
            _log_command("VOCAL", "", 0, "ignored", "Transcription vide", 0.0,
                         is_ai=True, chat=remote_jid, sender=sender)
            return {"ignore": True}
        ai_cfg = AIConfig.get()
        if not ai_cfg.enabled:
            _log_command("VOCAL", body[:500], 0, "ignored", "IA désactivée", 0.0,
                         is_ai=True, chat=remote_jid, sender=sender)
            return {"ignore": True}
        if _chat_blacklisted(ai_cfg, sender, remote_jid, is_group):
            _log_command("VOCAL", body[:500], 0, "ignored",
                         "Conversation sur liste noire (blacklist IA)", 0.0,
                         is_ai=True, chat=remote_jid, sender=sender)
            return {"ignore": True}
        if _chat_allowed(ai_cfg.ai_whitelist, sender, remote_jid, is_group):
            return _handle_ai(
                f"[Note vocale transcrite] {body}", sender, remote_jid, is_group,
                log_cmd="VOCAL",
            )
        _log_command("VOCAL", body[:500], 0, "ignored",
                     "Conversation non autorisée (whitelist IA)", 0.0,
                     is_ai=True, chat=remote_jid, sender=sender)
        return {"ignore": True}

    if not body:
        _log_command("MSG", "", 0, "ignored", "Message vide (sans texte)", 0.0,
                     chat=remote_jid, sender=sender)
        return {"ignore": True}

    # 1) Aide générale (accessible à tout le monde)
    if re.match(r"^\.(?:help|aide|menu|start|commands|commandes)(?:\s|$)", body, re.IGNORECASE):
        _log_command(".help", body[:500], 0, "success", "Aide affichée", 0.0,
                     chat=remote_jid, sender=sender)
        return {"reply": HELP_TEXT}

    # 2) Commandes propriétaire (.blacklist/.stats) + .me (ouvert à tous)
    if re.match(r"^\.(?:blacklist|unblacklist|stats)(?:\s|$)", body, re.IGNORECASE):
        return _handle_owner_command(body, sender, remote_jid, is_group, start)
    if re.match(r"^\.me(?:\s|$)", body, re.IGNORECASE):
        result = _handle_me(sender, remote_jid, is_group)
        _log_command(".me", body[:500], 0, "success", "", 0.0,
                     chat=remote_jid, sender=sender)
        return result

    # 3) Commandes de recherche BrixHub
    if re.match(r"^\.search(?:\s|$)", body, re.IGNORECASE):
        return _handle_search(body, remote_jid, sender, is_group, start)
    if re.match(r"^\.tel(?:\s|$)", body, re.IGNORECASE):
        return _handle_tel(body, remote_jid, sender, is_group, start)

    # 3b) Commandes pratiques (gratuites, sans clé API)
    if re.match(r"^\.meteo(?:\s|$)", body, re.IGNORECASE):
        return _handle_meteo(body, remote_jid, sender, is_group, start)
    if re.match(r"^\.(?:traduis|traduire|translate)(?:\s|$)", body, re.IGNORECASE):
        return _handle_translate(body, remote_jid, sender, is_group, start)
    if re.match(r"^\.(?:devise|convert)(?:\s|$)", body, re.IGNORECASE):
        return _handle_devise(body, remote_jid, sender, is_group, start)

    # 3c) Commandes surprises (cachées — à découvrir)
    if re.match(r"^\.crypto(?:\s|$)", body, re.IGNORECASE):
        return _handle_crypto(body, remote_jid, sender, is_group, start)
    if re.match(r"^\.(?:8ball|8balle|boule)(?:\s|$)", body, re.IGNORECASE):
        return _handle_8ball(body, remote_jid, sender, is_group, start)
    if re.match(r"^\.blague(?:\s|$)", body, re.IGNORECASE):
        return _handle_blague(body, remote_jid, sender, is_group, start)
    if re.match(r"^\.(?:horoscope|astro)(?:\s|$)", body, re.IGNORECASE):
        return _handle_horoscope(body, remote_jid, sender, is_group, start)

    # 3d) Commande .ask : poser une question à l'IA (accessible à TOUT le monde)
    #     Fonctionne comme .resume / .correct : commande explicite, seule la
    #     clé GROQ est requise (l'interrupteur "IA auto" n'a pas besoin d'être
    #     allumé). Réutilise le moteur IA complet (mémoire + logs).
    if re.match(r"^\.ask(?:\s|$)", body, re.IGNORECASE):
        return _handle_ask(body, remote_jid, sender, is_group, start)

    # 4) Commande de contrôle .ia (réservée au propriétaire)
    if re.match(r"^\.ia(?:\s|$)", body, re.IGNORECASE):
        if not os.environ.get("OWNER_NUMBER", "").strip():
            _log_command(".ia", body[:500], 0, "success",
                         "Aide affichée (OWNER_NUMBER non configuré)", 0.0,
                         chat=remote_jid, sender=sender)
            return {"reply": OWNER_HINT}
        if not _is_owner(sender):
            _log_command(".ia", body[:500], 0, "ignored", "Non propriétaire", 0.0,
                         chat=remote_jid, sender=sender)
            return {"ignore": True}
        result = _handle_ia(body, sender, remote_jid)
        _log_command(".ia", body[:500], 0, "success", "", 0.0,
                     chat=remote_jid, sender=sender)
        return result

    # 5) IA automatique (si activée et conversation autorisée)
    ai_cfg = AIConfig.get()
    if ai_cfg.enabled:
        # Blacklist PRIORITAIRE : cette conversation est bannie → on se tait
        if _chat_blacklisted(ai_cfg, sender, remote_jid, is_group):
            _log_command("IA", body[:500], 0, "ignored",
                         "Conversation sur liste noire (blacklist IA)", elapsed,
                         is_ai=True, chat=remote_jid, sender=sender)
            return {"ignore": True}
        if _chat_allowed(ai_cfg.ai_whitelist, sender, remote_jid, is_group):
            return _handle_ai(body, sender, remote_jid, is_group)

    # 6) Réponse par défaut (message ignoré par l'IA mais réponse auto active)
    bot_cfg = BotConfig.get()
    elapsed = time.perf_counter() - start
    if bot_cfg.auto_response:
        _log_command("AUTO", body[:500], 0, "success",
                     "Réponse automatique par défaut", elapsed,
                     chat=remote_jid, sender=sender)
        return {"reply": DEFAULT_RESPONSE}

    # 7) Ignorer — on journalise TOUT message non traité avec le motif exact.
    if ai_cfg.enabled:
        _log_command("IA", body[:500], 0, "ignored",
                     "Conversation non autorisée (whitelist IA)", time.perf_counter() - start,
                     is_ai=True, chat=remote_jid, sender=sender)
    else:
        _log_command("MSG", body[:500], 0, "ignored",
                     "IA désactivée et réponse auto désactivée", elapsed,
                     chat=remote_jid, sender=sender)
    return {"ignore": True}


# --------------------------------------------------------------------------- #
#  Commande .search
# --------------------------------------------------------------------------- #
def _handle_search(body, remote_jid="", sender="", is_group=False, start=None):
    """Exécute une recherche par nom et renvoie le message de réponse."""
    cfg = BotConfig.get()
    query = re.sub(r"^\.search\s*", "", body, flags=re.IGNORECASE).strip()
    start = start if start is not None else time.perf_counter()

    if not cfg.command_enabled:
        _log_command(".search", query, 0, "success",
                     "Commande désactivée par l'admin",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": "❌ La commande `.search` est actuellement désactivée par l'administrateur."}

    nom, prenom, ville = _parse_query(query)
    if not nom:
        _log_command(".search", query, 0, "success", "Aide affichée",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": USAGE_SEARCH}

    try:
        result = brixhub_service.search(nom, prenom, ville)
        elapsed = time.perf_counter() - start
        label = build_label(nom, prenom, ville)
        reply = format_results(result, label, elapsed, hint=f".search {nom}")
        _log_command(".search", query, len(result["results"]), "success", "",
                     elapsed, chat=remote_jid, sender=sender)
        return {"reply": reply}
    except brixhub_service.BrixHubError as exc:
        elapsed = time.perf_counter() - start
        _log_command(".search", query, 0, "error", exc.message, elapsed,
                     chat=remote_jid, sender=sender)
        prefix = "⚠️ " if exc.is_config else "❌ "
        return {"reply": f"{prefix}{exc.message}"}


# --------------------------------------------------------------------------- #
#  Commande .tel
# --------------------------------------------------------------------------- #
def _handle_tel(body, remote_jid="", sender="", is_group=False, start=None):
    """Exécute une recherche par numéro de téléphone et renvoie la réponse."""
    cfg = BotConfig.get()
    query = re.sub(r"^\.tel\s*", "", body, flags=re.IGNORECASE).strip()
    start = start if start is not None else time.perf_counter()

    if not cfg.command_enabled:
        _log_command(".tel", query, 0, "success",
                     "Commande désactivée par l'admin",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": "❌ La commande `.tel` est actuellement désactivée par l'administrateur."}

    number = _parse_phone(query)
    if not number:
        _log_command(".tel", query, 0, "success", "Aide affichée",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": USAGE_TEL}

    try:
        result = brixhub_service.search(telephone=number)
        elapsed = time.perf_counter() - start
        reply = format_results(result, number, elapsed, hint=f".tel {number}")
        _log_command(".tel", query, len(result["results"]), "success", "",
                     elapsed, chat=remote_jid, sender=sender)
        return {"reply": reply}
    except brixhub_service.BrixHubError as exc:
        elapsed = time.perf_counter() - start
        _log_command(".tel", query, 0, "error", exc.message, elapsed,
                     chat=remote_jid, sender=sender)
        prefix = "⚠️ " if exc.is_config else "❌ "
        return {"reply": f"{prefix}{exc.message}"}


# --------------------------------------------------------------------------- #
#  Commandes pratiques (.meteo / .traduis / .devise)
# --------------------------------------------------------------------------- #
def _handle_meteo(body, remote_jid="", sender="", is_group=False, start=None):
    """Météo en temps réel d'une ville (Open-Meteo, gratuit, sans clé)."""
    start = start if start is not None else time.perf_counter()
    city = re.sub(r"^\.meteo\s*", "", body, flags=re.IGNORECASE).strip()

    if not city:
        _log_command(".meteo", "", 0, "success", "Aide affichée",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": USAGE_METEO}

    try:
        reply = tools_service.weather(city)
        _log_command(".meteo", city[:500], 0, "success", "",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": reply}
    except tools_service.ToolsError as exc:
        _log_command(".meteo", city[:500], 0, "error", exc.message,
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": f"❌ {exc.message}"}


def _handle_translate(body, remote_jid="", sender="", is_group=False, start=None):
    """Traduit un texte en français (détection automatique de la langue)."""
    start = start if start is not None else time.perf_counter()
    text = re.sub(r"^\.(?:traduis|traduire|translate)\s*", "", body,
                  flags=re.IGNORECASE).strip()

    if not text:
        _log_command(".traduis", "", 0, "success", "Aide affichée",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": USAGE_TRAD}

    try:
        translated = tools_service.translate(text, target="fr")
        reply = f"🌐 *Traduction (→ français)*\n💬 {text}\n✨ {translated}"
        _log_command(".traduis", text[:500], 0, "success", "",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": reply}
    except tools_service.ToolsError as exc:
        _log_command(".traduis", text[:500], 0, "error", exc.message,
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": f"❌ {exc.message}"}


def _handle_devise(body, remote_jid="", sender="", is_group=False, start=None):
    """Convertit un montant entre deux devises (taux BCE, gratuit, sans clé)."""
    start = start if start is not None else time.perf_counter()
    cmd = body.split()[0].lower()  # ".devise" ou ".convert" (pour les logs)
    args = re.sub(r"^\.(?:devise|convert)\s*", "", body,
                  flags=re.IGNORECASE).strip().split()

    if len(args) < 3:
        _log_command(cmd, " ".join(args)[:500], 0, "success", "Aide affichée",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": USAGE_DEVISE}

    amount, source, target = args[0], args[1].upper(), args[2].upper()
    try:
        result = tools_service.currency(amount, source, target)
        reply = f"💱 *Conversion*\n{amount} {source} = *{result} {target}*"
        _log_command(cmd, f"{amount} {source} {target}", 0, "success", "",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": reply}
    except tools_service.ToolsError as exc:
        _log_command(cmd, f"{amount} {source} {target}", 0, "error", exc.message,
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": f"❌ {exc.message}"}


# --------------------------------------------------------------------------- #
#  Commandes surprises (crypto / boule magique / blague / horoscope)
# --------------------------------------------------------------------------- #
def _handle_crypto(body, remote_jid="", sender="", is_group=False, start=None):
    """Prix des cryptomonnaies en direct (CoinGecko)."""
    start = start if start is not None else time.perf_counter()
    arg = re.sub(r"^\.crypto\s*", "", body, flags=re.IGNORECASE).strip()
    symbols = [s for s in arg.split() if s][:5] if arg else None
    try:
        reply = tools_service.crypto(symbols)
        _log_command(".crypto", arg[:200] or "défaut", 0, "success", "",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": reply}
    except tools_service.ToolsError as exc:
        _log_command(".crypto", arg[:200], 0, "error", exc.message,
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": f"❌ {exc.message}"}


def _handle_8ball(body, remote_jid="", sender="", is_group=False, start=None):
    """Réponse aléatoire de la boule magique."""
    start = start if start is not None else time.perf_counter()
    question = re.sub(r"^\.(?:8ball|8balle|boule)\s*", "", body,
                      flags=re.IGNORECASE).strip()
    reply = tools_service.magic8ball(question)
    _log_command(".8ball", question[:200] or "-", 0, "success", "",
                 time.perf_counter() - start, chat=remote_jid, sender=sender)
    return {"reply": reply}


def _handle_blague(body, remote_jid="", sender="", is_group=False, start=None):
    """Blague aléatoire."""
    start = start if start is not None else time.perf_counter()
    reply = tools_service.joke()
    _log_command(".blague", "", 0, "success", "",
                 time.perf_counter() - start, chat=remote_jid, sender=sender)
    return {"reply": reply}


def _handle_horoscope(body, remote_jid="", sender="", is_group=False, start=None):
    """Horoscope humoristique du jour."""
    start = start if start is not None else time.perf_counter()
    signe = re.sub(r"^\.(?:horoscope|astro)\s*", "", body,
                   flags=re.IGNORECASE).strip()
    if not signe:
        _log_command(".horoscope", "", 0, "success", "Aide affichée",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": "🔮 *Commande .horoscope*\nUtilisation : `.horoscope [signe]`\n"
                          "Exemple : `.horoscope lion`"}
    try:
        reply = tools_service.horoscope(signe)
        _log_command(".horoscope", signe[:200], 0, "success", "",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": reply}
    except tools_service.ToolsError as exc:
        _log_command(".horoscope", signe[:200], 0, "error", exc.message,
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": f"❌ {exc.message}"}


# --------------------------------------------------------------------------- #
#  Commande .ask (question à l'IA — tout le monde)
# --------------------------------------------------------------------------- #
def _handle_ask(body, remote_jid="", sender="", is_group=False, start=None):
    """
    Pose une question à l'IA et renvoie la réponse (commande .ask).

    Accessible à tout le monde : c'est une commande explicite, comme .resume
    ou .correct — seule la clé GROQ est requise (l'IA automatique n'a pas
    besoin d'être activée). Réutilise _handle_ai pour profiter de la mémoire
    de conversation, des logs IA et de la gestion d'erreurs.
    """
    start = start if start is not None else time.perf_counter()
    question = re.sub(r"^\.ask\s*", "", body, flags=re.IGNORECASE).strip()

    if not question:
        _log_command(".ask", "", 0, "success", "Aide affichée",
                     time.perf_counter() - start, chat=remote_jid, sender=sender)
        return {"reply": USAGE_ASK}
    if len(question) > 4000:
        _log_command(".ask", question[:500], 0, "error", "Question trop longue",
                     time.perf_counter() - start, is_ai=True,
                     chat=remote_jid, sender=sender)
        return {"reply": "❌ Question trop longue (4000 caractères maximum)."}

    # Prompt système : le prompt dédié .ask du panneau (onglet IA) s'il est
    # rempli ; sinon le prompt par défaut de .ask (règle de langue incluse).
    # C'est exactement ce que le panneau affiche (to_dict résout vide → défaut) :
    # aucun écart entre l'affichage et le comportement réel.
    ai_cfg = AIConfig.get()
    ask_prompt = (ai_cfg.ask_prompt or "").strip() or DEFAULT_ASK_PROMPT

    # Réponse complète via le moteur IA (mémoire + journalisation incluses)
    return _handle_ai(question, sender, remote_jid, is_group,
                      log_cmd=".ask", system_prompt=ask_prompt)


# --------------------------------------------------------------------------- #
#  Commandes propriétaire (.blacklist / .unblacklist / .me / .stats)
# --------------------------------------------------------------------------- #
def _handle_owner_command(body, sender, remote_jid, is_group, start=None):
    """
    Point d'entrée des commandes réservées au propriétaire (OWNER_NUMBER).
    Journalise le résultat, et surtout le motif quand c'est ignoré.
    """
    start = start if start is not None else time.perf_counter()
    cmd = body.split()[0].lower()

    if not os.environ.get("OWNER_NUMBER", "").strip():
        _log_command(cmd, body[:500], 0, "success",
                     "Aide affichée (OWNER_NUMBER non configuré)", 0.0,
                     chat=remote_jid, sender=sender)
        return {"reply": OWNER_HINT}
    if not _is_owner(sender):
        _log_command(cmd, body[:500], 0, "ignored", "Non propriétaire", 0.0,
                     chat=remote_jid, sender=sender)
        return {"ignore": True}

    elapsed = time.perf_counter() - start
    if cmd == ".blacklist":
        result = _handle_blacklist(body, sender, remote_jid)
    elif cmd == ".unblacklist":
        result = _handle_unblacklist(body, sender, remote_jid)
    else:  # .stats
        result = _handle_stats()
    _log_command(cmd, body[:500], 0, "success", "", elapsed,
                 chat=remote_jid, sender=sender)
    return result


def _handle_blacklist(body, sender, remote_jid):
    """Gère la liste noire : silence l'IA dans cette conversation."""
    ai_cfg = AIConfig.get()
    blacklist = _blacklist_list(ai_cfg)
    whitelist = _whitelist_list(ai_cfg)
    parts = body.split()
    action = parts[1].lower() if len(parts) > 1 else ""
    chat_key = remote_jid or sender

    def save_lists():
        ai_cfg.ai_blacklist = "\n".join(blacklist)
        ai_cfg.ai_whitelist = "\n".join(whitelist)
        db.session.commit()

    if action in ("oui", "on", "add", "ajouter", "bannir", "ban", "1"):
        if chat_key not in blacklist:
            blacklist.append(chat_key)
            if chat_key in whitelist:
                whitelist.remove(chat_key)  # une conversation bannie ne peut pas rester autorisée
            save_lists()
            return {"reply": "🔇 IA désactivée dans cette conversation (liste noire)."}
        return {"reply": "ℹ️ Cette conversation est déjà sur la liste noire."}

    if action in ("non", "off", "remove", "retirer", "debannir", "unban", "0"):
        if chat_key in blacklist:
            blacklist.remove(chat_key)
            save_lists()
            return {"reply": "🔊 IA réactivée dans cette conversation."}
        return {"reply": "ℹ️ Cette conversation n'était pas sur la liste noire."}

    if action in ("liste", "list", "status", "etat"):
        if not blacklist:
            return {"reply": "📋 Liste noire vide : l'IA peut répondre partout."}
        return {"reply": "🔇 Conversations où l'IA est muette :\n" +
                         "\n".join(f"• {entry}" for entry in blacklist)}

    if action in ("clear", "effacer", "tout"):
        blacklist.clear()
        save_lists()
        return {"reply": "🗑 Liste noire vidée : l'IA peut répondre partout."}

    active = chat_key in blacklist
    return {"reply": (
        "🔇 *Commande .blacklist*\n"
        "• `.blacklist oui` — rendre l'IA muette ici\n"
        "• `.blacklist non` — réactiver l'IA ici\n"
        "• `.blacklist liste` — voir la liste noire\n"
        "• `.blacklist clear` — vider la liste noire\n\n"
        f"Cette conversation est-elle bannie ? {'✅ oui' if active else '❌ non'}."
    )}


def _handle_unblacklist(body, sender, remote_jid):
    """Retire la conversation courante de la liste noire."""
    ai_cfg = AIConfig.get()
    blacklist = _blacklist_list(ai_cfg)
    chat_key = remote_jid or sender
    if chat_key in blacklist:
        blacklist.remove(chat_key)
        ai_cfg.ai_blacklist = "\n".join(blacklist)
        db.session.commit()
        return {"reply": "🔊 IA réactivée dans cette conversation."}
    return {"reply": "ℹ️ Cette conversation n'était pas sur la liste noire."}


def _handle_me(sender, remote_jid, is_group):
    """Résumé d'utilisation de l'expéditeur : messages, IA, mémoire. (Tout le monde.)"""
    ai_cfg = AIConfig.get()
    blacklisted = _chat_blacklisted(ai_cfg, sender, remote_jid, is_group)
    whitelisted = _chat_allowed(ai_cfg.ai_whitelist, sender, remote_jid, is_group)

    # En groupe, les logs portent le jid du groupe dans chat ; en privé,
    # l'expéditeur est le plus fiable. On couvre les deux cas.
    total = CommandLog.query.filter(
        (CommandLog.sender == sender) | (CommandLog.chat == remote_jid)
    ).count()
    ai_total = CommandLog.query.filter(
        CommandLog.is_ai.is_(True),
        (CommandLog.sender == sender) | (CommandLog.chat == remote_jid),
    ).count()
    errors = CommandLog.query.filter(
        CommandLog.status == "error",
        (CommandLog.sender == sender) | (CommandLog.chat == remote_jid),
    ).count()

    # Tokens consommés par CET expéditeur (AILog.sender = jid de l'expéditeur,
    # jamais celui du groupe). En groupe on affiche le total du bot : les
    # conversations de groupe n'ont pas d'expéditeur exploitable ici.
    if is_group:
        tokens = db.session.query(
            db.func.coalesce(db.func.sum(AILog.tokens_used), 0)
        ).scalar() or 0
    else:
        tokens = db.session.query(
            db.func.coalesce(db.func.sum(AILog.tokens_used), 0)
        ).filter(AILog.sender == sender).scalar() or 0

    header = "👤 *Mes stats (groupe)*" if is_group else "👤 *Mes stats*"
    return {"reply": (
        f"{header}\n"
        f"• 📨 Messages traités : {total}\n"
        f"• 🤖 Réponses IA : {ai_total}\n"
        f"• ❌ Erreurs : {errors}\n"
        f"• 🔤 Tokens consommés : {tokens}\n\n"
        f"• État IA ici : {'🔇 muette (liste noire)' if blacklisted else ('✅ active' if whitelisted else '❌ inactive')}\n"
        "• 🔁 Réponses IA sur tous les messages : "
        f"{'✅ oui' if ai_cfg.enabled else '❌ non'}"
    )}


def _handle_stats():
    """Statistiques globales du bot (réservées au propriétaire)."""
    total = CommandLog.query.count()
    success = CommandLog.query.filter_by(status="success").count()
    errors = CommandLog.query.filter_by(status="error").count()
    ignored = CommandLog.query.filter_by(status="ignored").count()
    avg = db.session.query(db.func.avg(CommandLog.response_time)).scalar() or 0.0
    ai_count = CommandLog.query.filter_by(is_ai=True).count()
    tokens = db.session.query(db.func.coalesce(db.func.sum(AILog.tokens_used), 0)).scalar() or 0
    bot_cfg = BotConfig.get()
    search_enabled = "✅" if bot_cfg.command_enabled else "❌"
    key_status = "✅" if bot_cfg.api_key or os.environ.get("BRIX_API_KEY", "").strip() else "⚠️ manquante"

    return {"reply": (
        "📊 *Stats du bot*\n"
        f"• 📨 Messages reçus : {total}\n"
        f"• ✅ Succès : {success} · ❌ Erreurs : {errors}\n"
        f"• 🚫 Ignorés : {ignored} · 🤖 Réponses IA : {ai_count}\n"
        f"• ⚡ Temps moyen : {int(avg * 1000)} ms\n"
        f"• 🔤 Tokens consommés : {tokens}\n\n"
        f"• 🔍 .search : {search_enabled} · Clé BrixHub : {key_status}"
    )}


# --------------------------------------------------------------------------- #
#  Commande .ia (whitelist, réservée au propriétaire)
# --------------------------------------------------------------------------- #
def _handle_ia(body, sender, remote_jid):
    """
    Active/désactive l'IA pour la conversation courante, ou affiche l'état.

    - `.ia oui`  → autorise (whitelist) et retire de la liste noire
    - `.ia non`  → bannit (liste noire) et retire de la whitelist
    La liste noire est prioritaire : un chat banni est toujours muet.
    """
    ai_cfg = AIConfig.get()
    whitelist = _whitelist_list(ai_cfg)
    blacklist = _blacklist_list(ai_cfg)
    parts = body.split()
    action = parts[1].lower() if len(parts) > 1 else ""
    chat_key = remote_jid or sender

    def save_lists():
        ai_cfg.ai_whitelist = "\n".join(whitelist)
        ai_cfg.ai_blacklist = "\n".join(blacklist)
        db.session.commit()

    if action in ("oui", "on", "add", "ajouter", "activer", "1"):
        if chat_key in blacklist:
            blacklist.remove(chat_key)  # réactivation : on retire le bannissement
        if chat_key not in whitelist:
            whitelist.append(chat_key)
            save_lists()
        return {"reply": "✅ IA activée pour cette conversation."}

    if action in ("non", "off", "remove", "retirer", "desactiver", "0"):
        if chat_key not in blacklist:
            blacklist.append(chat_key)  # le bannissement est prioritaire
            if chat_key in whitelist:
                whitelist.remove(chat_key)
            save_lists()
        return {"reply": "🔇 IA désactivée pour cette conversation (liste noire)."}

    if action in ("liste", "list", "status", "etat"):
        lines = ["📋 Conversations autorisées (whitelist) :"]
        lines.append("\n".join(f"• {entry}" for entry in whitelist) if whitelist
                     else "• (vide : l'IA répond à tout le monde)")
        lines.append("")
        lines.append("🔇 Conversations bannies (liste noire) :")
        lines.append("\n".join(f"• {entry}" for entry in blacklist) if blacklist else "• (vide)")
        return {"reply": "\n".join(lines)}

    return {"reply": (
        "🤖 *Commandes IA*\n"
        "• `.ia` — état de cette conversation\n"
        "• `.ia oui` — activer l'IA ici\n"
        "• `.ia non` — bannir l'IA ici (liste noire)\n"
        "• `.ia liste` — voir whitelist et liste noire\n\n"
        f"État actuel pour cette conversation : "
        f"{'🔇 muette (liste noire)' if chat_key in blacklist
          else ('✅ active' if (chat_key in whitelist or not whitelist) else '❌ inactive')}."
    )}


# --------------------------------------------------------------------------- #
#  Formatage des résultats BrixHub
# --------------------------------------------------------------------------- #
def format_results(result, label, elapsed=0.0, hint=None):
    """
    Formate les résultats BrixHub en message WhatsApp lisible.

    Utilisé par le flux WhatsApp (.search / .tel) et par l'onglet "Test API".
    """
    results = result["results"]
    meta = result.get("meta") or {}
    took_ms = meta.get("took_ms") or int(elapsed * 1000)

    lines = [f"🔍 *Recherche : {label}*"]

    if not results:
        lines.append("")
        lines.append("😕 *Aucun résultat* trouvé pour cette recherche.")
        lines.append("💡 *Astuce :* essayez avec moins de critères ou une orthographe différente.")
        if hint:
            lines.append(f"   Exemple : `{hint}`")
        return "\n".join(lines)

    lines.append("")
    for index, item in enumerate(results, start=1):
        lines.append(f"*{index}. {_person_name(item)}* {_stars(item.get('_confidence'))}")
        if item.get("ville"):
            lines.append(f"📍 {item['ville']}")
        if item.get("email"):
            lines.append(f"📧 {item['email']}")
        if item.get("telephone"):
            lines.append(f"📱 {item['telephone']}")
        if index < len(results):
            lines.append("────────────────────")

    total = meta.get("total", len(results))
    lines.append("")
    lines.append(f"📊 {total} résultat(s) · ⚡ {took_ms} ms")
    return "\n".join(lines)


def _person_name(item):
    """Assemble le nom complet d'un résultat (prénom + nom de famille)."""
    prenom = item.get("prenom") or ""
    nom = item.get("nom_famille") or ""
    return (prenom + " " + nom).strip() or "Inconnu"


def _stars(confidence):
    """Convertit un score de confiance (0-100) en étoiles (1 à 5)."""
    try:
        level = max(1, min(5, round(float(confidence or 0) / 20)))
    except (TypeError, ValueError):
        level = 0
    return "⭐" * level if level else ""


# --------------------------------------------------------------------------- #
#  IA automatique (GROQ + mémoire)
# --------------------------------------------------------------------------- #
def _load_memory(sender, exchanges):
    """Charge les derniers échanges mémorisés pour un utilisateur (plus ancien d'abord)."""
    limit = max(1, min(20, int(exchanges or 5))) * 2
    rows = (
        AIMemory.query.filter_by(sender=sender)
        .order_by(AIMemory.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


def _save_memory_pair(sender, user_content, assistant_content):
    """Enregistre un échange complet (message + réponse) en une seule transaction."""
    sender = (sender or "")[:100]
    db.session.add(AIMemory(
        sender=sender, role="user",
        content=(user_content or "")[:6000], timestamp=utc_now_iso(),
    ))
    db.session.add(AIMemory(
        sender=sender, role="assistant",
        content=(assistant_content or "")[:6000], timestamp=utc_now_iso(),
    ))
    db.session.commit()


def _prune_memory(sender, exchanges):
    """Supprime les entrées trop anciennes pour limiter la taille de la mémoire."""
    cap = max(1, min(20, int(exchanges or 5))) * 2 + 2
    old_rows = (
        AIMemory.query.filter_by(sender=sender)
        .order_by(AIMemory.id.desc())
        .offset(cap)
        .all()
    )
    for row in old_rows:
        db.session.delete(row)
    if old_rows:
        db.session.commit()


def _handle_ai(body, sender, remote_jid, is_group, log_cmd="IA", system_prompt=None):
    """Répond automatiquement avec GROQ, journalise et met à jour la mémoire."""
    start = time.perf_counter()
    ai_cfg = AIConfig.get()

    history = None
    memory_on = bool(ai_cfg.memory_enabled)
    if memory_on and sender:
        history = _load_memory(sender, ai_cfg.memory_exchanges)

    try:
        reply, model, tokens, duration_ms = ai_service.chat(
            body, history=history, system_prompt=system_prompt,
        )
        elapsed = (time.perf_counter() - start) / 1000.0

        _log_command(log_cmd, body[:500], 1, "success", "", elapsed, is_ai=True,
                     chat=remote_jid, sender=sender)
        db.session.add(AILog(
            sender=(sender or "inconnu")[:100],
            user_message=body[:6000],
            ai_response=reply[:6000],
            model=model,
            tokens_used=tokens,
            duration_ms=duration_ms,
            timestamp=utc_now_iso(),
        ))
        db.session.commit()

        if memory_on and sender:
            _save_memory_pair(sender, body, reply)
            _prune_memory(sender, ai_cfg.memory_exchanges)

        return {"reply": reply}
    except ai_service.AIError as exc:
        elapsed = (time.perf_counter() - start) / 1000.0
        _log_command(log_cmd, body[:500], 0, "error", exc.message, elapsed, is_ai=True,
                     chat=remote_jid, sender=sender)
        prefix = "⚠️ " if exc.is_config else "❌ "
        return {"reply": f"{prefix}{exc.message}"}


# --------------------------------------------------------------------------- #
#  Journalisation
# --------------------------------------------------------------------------- #
def _log_command(command, query, results_count, status, error, response_time,
                 is_ai=False, chat="", sender=""):
    """Enregistre une ligne dans la table command_logs."""
    db.session.add(CommandLog(
        command=command,
        query_text=query[:500],
        results_count=results_count,
        status=status,
        error=error[:500],
        response_time=round(response_time or 0.0, 3),
        is_ai=is_ai,
        chat=(chat or "")[:100],
        sender=(sender or "")[:100],
    ))
    db.session.commit()
