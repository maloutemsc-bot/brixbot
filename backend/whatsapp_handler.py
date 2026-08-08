"""
whatsapp_handler.py — Traitement des messages WhatsApp entrants.

Priorité des messages :
  1. La commande ".search ..." → recherche BrixHub
  2. Sinon, si l'IA est activée → réponse automatique GROQ
  3. Sinon, si la réponse automatique est activée → message d'aide par défaut
  4. Sinon → le message est ignoré
"""

import re
import time

import ai_service
import brixhub_service
from database import AIConfig, AILog, BotConfig, CommandLog, db, utc_now_iso

# Message d'aide affiché pour une commande .search incomplète
USAGE_TEXT = (
    "📚 *Commande .search*\n"
    "Utilisation : `.search nom [prénom] [ville]`\n"
    "Exemples :\n"
    "• `.search Dupont`\n"
    "• `.search Dupont Jean`\n"
    "• `.search Dupont Jean Paris`"
)

# Message par défaut (réponse automatique sans IA)
DEFAULT_RESPONSE = (
    "👋 Bonjour ! Je suis un assistant de recherche.\n"
    "Utilisez la commande `.search nom [prénom] [ville]` pour trouver des informations.\n\n"
    "Exemple : `.search Dupont Jean Paris`"
)


# --------------------------------------------------------------------------- #
#  Point d'entrée principal
# --------------------------------------------------------------------------- #
def handle_message(body, sender="", remote_jid="", is_group=False):
    """
    Traite un message WhatsApp reçu via l'API /api/message.

    Renvoie un dict :
      {"reply": "..."}  → le bot doit répondre ce texte
      {"ignore": True}  → le bot ne répond rien
    """
    body = (body or "").strip()
    if not body:
        return {"ignore": True}

    # 1) Commande .search — le préfixe doit être suivi d'un espace (ou rien)
    #    pour ne pas confondre ".searching" ou ".searchX" avec une commande.
    if re.match(r"^\.search(?:\s|$)", body, re.IGNORECASE):
        return _handle_search(body)

    # 2) IA automatique
    ai_cfg = AIConfig.get()
    if ai_cfg.enabled:
        return _handle_ai(body, sender, remote_jid, is_group)

    # 3) Réponse par défaut
    bot_cfg = BotConfig.get()
    if bot_cfg.auto_response:
        return {"reply": DEFAULT_RESPONSE}

    # 4) Ignorer
    return {"ignore": True}


# --------------------------------------------------------------------------- #
#  Commande .search
# --------------------------------------------------------------------------- #
def _parse_query(query):
    """Découpe la requête en (nom, prénom, ville)."""
    parts = query.split()
    nom = parts[0] if parts else ""
    prenom = parts[1] if len(parts) > 1 else ""
    ville = " ".join(parts[2:]) if len(parts) > 2 else ""
    return nom, prenom, ville


def _handle_search(body):
    """Exécute une recherche BrixHub et renvoie le message de réponse."""
    cfg = BotConfig.get()
    query = re.sub(r"^\.search\s*", "", body, flags=re.IGNORECASE).strip()
    start = time.perf_counter()

    if not cfg.command_enabled:
        return {"reply": "❌ La commande `.search` est actuellement désactivée par l'administrateur."}

    nom, prenom, ville = _parse_query(query)
    if not nom:
        return {"reply": USAGE_TEXT}

    try:
        result = brixhub_service.search(nom, prenom, ville)
        elapsed = time.perf_counter() - start
        reply = format_results(result, nom, prenom, ville, elapsed)
        _log_command(
            ".search", query, len(result["results"]), "success", "",
            elapsed, is_ai=False,
        )
        return {"reply": reply}
    except brixhub_service.BrixHubError as exc:
        elapsed = time.perf_counter() - start
        _log_command(".search", query, 0, "error", exc.message, elapsed, is_ai=False)
        prefix = "⚠️ " if exc.is_config else "❌ "
        return {"reply": f"{prefix}{exc.message}"}


def format_results(result, nom, prenom, ville, elapsed=0.0):
    """
    Formate les résultats BrixHub en message WhatsApp lisible.

    Utilisé à la fois par le flux WhatsApp et par l'onglet "Test API" du panneau.
    """
    results = result["results"]
    meta = result.get("meta") or {}
    took_ms = meta.get("took_ms") or int(elapsed * 1000)

    label = " ".join(p for p in (nom, prenom) if p)
    if ville:
        label += " (%s)" % ville

    lines = [f"🔍 *Recherche : {label}*"]

    if not results:
        lines.append("")
        lines.append("😕 *Aucun résultat* trouvé pour cette recherche.")
        lines.append("💡 *Astuce :* essayez avec moins de critères ou une orthographe différente.")
        lines.append(f"   Exemple : `.search {nom}`")
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
#  IA automatique
# --------------------------------------------------------------------------- #
def _handle_ai(body, sender, remote_jid, is_group):
    """Répond automatiquement avec GROQ et journalise la conversation."""
    start = time.perf_counter()
    try:
        reply, model, tokens, duration_ms = ai_service.chat(body)
        elapsed = (time.perf_counter() - start) / 1000.0
        _log_command("IA", body[:500], 1, "success", "", elapsed, is_ai=True)
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
        return {"reply": reply}
    except ai_service.AIError as exc:
        elapsed = (time.perf_counter() - start) / 1000.0
        _log_command("IA", body[:500], 0, "error", exc.message, elapsed, is_ai=True)
        prefix = "⚠️ " if exc.is_config else "❌ "
        return {"reply": f"{prefix}{exc.message}"}


# --------------------------------------------------------------------------- #
#  Journalisation
# --------------------------------------------------------------------------- #
def _log_command(command, query, results_count, status, error, response_time, is_ai=False):
    """Enregistre une ligne dans la table command_logs."""
    db.session.add(CommandLog(
        command=command,
        query_text=query[:500],
        results_count=results_count,
        status=status,
        error=error[:500],
        response_time=round(response_time or 0.0, 3),
        is_ai=is_ai,
    ))
    db.session.commit()
