"""
whatsapp_handler.py — Traitement des messages WhatsApp entrants.

Priorité des messages :
  1. Commandes .search / .tel → recherche BrixHub (nom ou numéro)
  2. Commande .ia (réservée au propriétaire) → gestion de la whitelist IA
  3. IA automatique (si activée ET conversation autorisée) → GROQ
  4. Réponse automatique par défaut
  5. Sinon → le message est ignoré
"""

import os
import re
import time

import ai_service
import brixhub_service
from database import AIConfig, AILog, AIMemory, BotConfig, CommandLog, db, utc_now_iso

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
def handle_message(body, sender="", remote_jid="", is_group=False):
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

    if not body:
        _log_command("MSG", "", 0, "ignored", "Message vide (sans texte)", 0.0,
                     chat=remote_jid, sender=sender)
        return {"ignore": True}

    # 1) Commandes de recherche BrixHub
    if re.match(r"^\.search(?:\s|$)", body, re.IGNORECASE):
        return _handle_search(body, remote_jid, sender, is_group, start)
    if re.match(r"^\.tel(?:\s|$)", body, re.IGNORECASE):
        return _handle_tel(body, remote_jid, sender, is_group, start)

    # 2) Commande de contrôle .ia (réservée au propriétaire)
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

    # 3) IA automatique (si activée et conversation autorisée)
    ai_cfg = AIConfig.get()
    if ai_cfg.enabled and _chat_allowed(ai_cfg.ai_whitelist, sender, remote_jid, is_group):
        return _handle_ai(body, sender, remote_jid, is_group)

    # 4) Réponse par défaut (message ignoré par l'IA mais réponse auto active)
    bot_cfg = BotConfig.get()
    elapsed = time.perf_counter() - start
    if bot_cfg.auto_response:
        _log_command("AUTO", body[:500], 0, "success",
                     "Réponse automatique par défaut", elapsed,
                     chat=remote_jid, sender=sender)
        return {"reply": DEFAULT_RESPONSE}

    # 5) Ignorer — on journalise TOUT message non traité avec le motif exact.
    if ai_cfg.enabled:
        _log_command("IA", body[:500], 0, "ignored",
                     "Conversation non autorisée (whitelist IA)", elapsed,
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
#  Commande .ia (whitelist, réservée au propriétaire)
# --------------------------------------------------------------------------- #
def _handle_ia(body, sender, remote_jid):
    """Active/désactive l'IA pour la conversation courante, ou affiche l'état."""
    ai_cfg = AIConfig.get()
    entries = _whitelist_list(ai_cfg)
    parts = body.split()
    action = parts[1].lower() if len(parts) > 1 else ""
    chat_key = remote_jid or sender

    def save_whitelist():
        ai_cfg.ai_whitelist = "\n".join(entries)
        db.session.commit()

    if action in ("oui", "on", "add", "ajouter", "activer", "1"):
        if chat_key not in entries:
            entries.append(chat_key)
            save_whitelist()
            return {"reply": "✅ IA activée pour cette conversation."}
        return {"reply": "ℹ️ L'IA est déjà active pour cette conversation."}

    if action in ("non", "off", "remove", "retirer", "desactiver", "0"):
        if chat_key in entries:
            entries.remove(chat_key)
            save_whitelist()
            return {"reply": "❌ IA désactivée pour cette conversation."}
        if not entries:
            return {"reply": "ℹ️ L'IA répond actuellement à tout le monde. "
                             "Ajoutez d'abord une liste dans le panneau, onglet IA, "
                             "pour pouvoir l'exclure ici."}
        return {"reply": "ℹ️ L'IA n'était pas active pour cette conversation."}

    if action in ("liste", "list", "status", "etat"):
        if not entries:
            return {"reply": "📋 Whitelist IA vide : l'IA répond à tout le monde."}
        return {"reply": "📋 Conversations où l'IA est active :\n" +
                         "\n".join(f"• {entry}" for entry in entries)}

    active = chat_key in entries or not entries
    return {"reply": (
        "🤖 *Commandes IA*\n"
        "• `.ia` — état de cette conversation\n"
        "• `.ia oui` — activer l'IA ici\n"
        "• `.ia non` — désactiver l'IA ici\n"
        "• `.ia liste` — voir la liste complète\n\n"
        f"État actuel pour cette conversation : "
        f"{'✅ active' if active else '❌ inactive'}."
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


def _handle_ai(body, sender, remote_jid, is_group):
    """Répond automatiquement avec GROQ, journalise et met à jour la mémoire."""
    start = time.perf_counter()
    ai_cfg = AIConfig.get()

    history = None
    memory_on = bool(ai_cfg.memory_enabled)
    if memory_on and sender:
        history = _load_memory(sender, ai_cfg.memory_exchanges)

    try:
        reply, model, tokens, duration_ms = ai_service.chat(body, history=history)
        elapsed = (time.perf_counter() - start) / 1000.0

        _log_command("IA", body[:500], 1, "success", "", elapsed, is_ai=True,
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
        _log_command("IA", body[:500], 0, "error", exc.message, elapsed, is_ai=True,
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
