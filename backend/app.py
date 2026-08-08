"""
app.py — Application Flask principale.

Rôles :
  - Sert le panneau d'administration (HTML/CSS/JS)
  - Expose l'API REST utilisée par le bot Node.js (Baileys)
  - Centralise les appels BrixHub et GROQ (les clés API ne quittent jamais le backend)

Sécurité :
  - Si ADMIN_PASSWORD est défini, le panneau est protégé par un mot de passe
    (session Flask + cookie signé). Les endpoints internes du bot exigent
    l'en-tête "X-Bot-Key".
  - Rate limiting sur l'API.

Démarrage :
  cd backend
  python app.py              # développement
  gunicorn app:app ...       # production
"""

import base64
import binascii
import datetime
import os
import sys
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import ai_service
import brixhub_service
import whatsapp_handler
from database import AIConfig, AILog, BotConfig, CommandLog, db, init_db, utc_now_iso

# --------------------------------------------------------------------------- #
#  Configuration de l'application
# --------------------------------------------------------------------------- #
load_dotenv()  # charge backend/.env si présent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(INSTANCE_DIR, "bot.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = os.environ.get("SECRET_KEY", "clé-secrète-par-défaut-à-changer")

CORS(app, supports_credentials=True)  # autorise les accès croisés (développement)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per minute"])

init_db(app)

# Clé partagée entre le bot Node.js et le backend (headers "X-Bot-Key")
BOT_API_KEY = os.environ.get("BOT_API_KEY", "changez-moi-bot")
# URL interne du bot (utilisée pour le redémarrage depuis le panneau)
BOT_INTERNAL_URL = os.environ.get("BOT_INTERNAL_URL", "http://localhost:3000").rstrip("/")
# Mot de passe du panneau d'administration (vide = pas d'authentification)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
AUTH_ENABLED = bool(ADMIN_PASSWORD)

# État WhatsApp reçu du bot (stocké en mémoire)
WHATSAPP_STATE = {
    "status": "unknown",        # unknown | connecting | qr | connected | disconnected
    "number": None,
    "qr": None,                 # data URL PNG du QR code
    "updated_at": None,
}


# --------------------------------------------------------------------------- #
#  Middlewares / garde-fous
# --------------------------------------------------------------------------- #
def require_bot_key(fn):
    """Protège les endpoints internes appelés par le bot Node.js."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Bot-Key") != BOT_API_KEY:
            return jsonify({"error": "Non autorisé"}), 401
        return fn(*args, **kwargs)

    return wrapper


def require_admin(fn):
    """Protège les endpoints du panneau si un mot de passe est configuré."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if AUTH_ENABLED and not session.get("admin"):
            return jsonify({"error": "Authentification requise"}), 401
        return fn(*args, **kwargs)

    return wrapper


@app.after_request
def security_headers(response):
    """Ajoute des en-têtes de sécurité de base."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


from flask_limiter.errors import RateLimitExceeded  # noqa: E402


@app.errorhandler(RateLimitExceeded)
def rate_limit_handler(_error):
    """Réponse JSON propre quand le rate limiting est dépassé."""
    return jsonify({"error": "Trop de requêtes. Veuillez patienter quelques secondes."}), 429


# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return redirect("/admin")


@app.route("/admin")
def admin():
    # Si un mot de passe est configuré et que la session n'est pas valide,
    # on renvoie vers la page de connexion.
    if AUTH_ENABLED and not session.get("admin"):
        return redirect("/login")
    return render_template("admin.html", auth_enabled=AUTH_ENABLED)


@app.route("/login")
def login_page():
    return render_template("login.html", auth_enabled=AUTH_ENABLED)


@app.route("/health")
def health():
    """Point de contrôle de santé (utilisé par Render pour les health checks)."""
    return jsonify({"ok": True, "whatsapp": WHATSAPP_STATE.get("status")})


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    """Connexion au panneau (si un mot de passe est configuré)."""
    if not AUTH_ENABLED:
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    if str(data.get("password", "")) == ADMIN_PASSWORD:
        session["admin"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Mot de passe incorrect"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  Tableau de bord
# --------------------------------------------------------------------------- #
def _compute_stats():
    total = CommandLog.query.count()
    success = CommandLog.query.filter_by(status="success").count()
    errors = CommandLog.query.filter_by(status="error").count()
    avg_time = db.session.query(db.func.avg(CommandLog.response_time)).scalar() or 0.0
    return {
        "total": total,
        "success": success,
        "errors": errors,
        "avg_response_time": round(float(avg_time), 3),
        "ai_count": CommandLog.query.filter_by(is_ai=True).count(),
    }


@app.route("/api/dashboard")
@require_admin
@limiter.limit("60 per minute")
def api_dashboard():
    return jsonify({
        "whatsapp": WHATSAPP_STATE,
        "stats": _compute_stats(),
        "quota": brixhub_service.get_remaining_quota(),
        "command_enabled": bool(BotConfig.get().command_enabled),
        "ai_enabled": bool(AIConfig.get().enabled),
    })


# --------------------------------------------------------------------------- #
#  Configuration .search
# --------------------------------------------------------------------------- #
@app.route("/api/config", methods=["GET"])
@require_admin
@limiter.limit("30 per minute")
def get_config():
    return jsonify(BotConfig.get().to_dict())


@app.route("/api/config", methods=["POST"])
@require_admin
@limiter.limit("30 per minute")
def save_config():
    data = request.get_json(silent=True) or {}
    cfg = BotConfig.get()

    if "command_enabled" in data:
        cfg.command_enabled = bool(data["command_enabled"])
    if "api_key" in data:
        cfg.api_key = str(data["api_key"]).strip()
    if "max_results" in data:
        try:
            cfg.max_results = max(1, min(100, int(data["max_results"])))
        except (TypeError, ValueError):
            pass  # valeur invalide : on conserve l'ancienne
    if "flexible_search" in data:
        cfg.flexible_search = bool(data["flexible_search"])
    if "auto_response" in data:
        cfg.auto_response = bool(data["auto_response"])

    db.session.commit()
    return jsonify({"ok": True, "config": cfg.to_dict()})


# --------------------------------------------------------------------------- #
#  Configuration IA (GROQ)
# --------------------------------------------------------------------------- #
@app.route("/api/ai/config", methods=["GET"])
@require_admin
@limiter.limit("30 per minute")
def get_ai_config():
    return jsonify(AIConfig.get().to_dict())


@app.route("/api/ai/config", methods=["POST"])
@require_admin
@limiter.limit("30 per minute")
def save_ai_config():
    data = request.get_json(silent=True) or {}
    cfg = AIConfig.get()

    if "enabled" in data:
        cfg.enabled = bool(data["enabled"])
    if "api_key" in data:
        cfg.api_key = str(data["api_key"]).strip()
    if "model" in data and str(data["model"]).strip():
        cfg.model = str(data["model"]).strip()
    if "system_prompt" in data:
        cfg.system_prompt = str(data["system_prompt"]).strip()
    if "temperature" in data:
        try:
            cfg.temperature = max(0.0, min(1.0, float(data["temperature"])))
        except (TypeError, ValueError):
            pass
    if "max_tokens" in data:
        try:
            cfg.max_tokens = max(1, min(8192, int(data["max_tokens"])))
        except (TypeError, ValueError):
            pass
    if "memory_enabled" in data:
        cfg.memory_enabled = bool(data["memory_enabled"])
    if "memory_exchanges" in data:
        try:
            cfg.memory_exchanges = max(1, min(20, int(data["memory_exchanges"])))
        except (TypeError, ValueError):
            pass
    if "ai_whitelist" in data:
        # Nettoie : une entrée par ligne, sans lignes vides
        cfg.ai_whitelist = "\n".join(
            line.strip() for line in str(data["ai_whitelist"]).splitlines() if line.strip()
        )
    if "ai_blacklist" in data:
        # Nettoie : une entrée par ligne, sans lignes vides
        cfg.ai_blacklist = "\n".join(
            line.strip() for line in str(data["ai_blacklist"]).splitlines() if line.strip()
        )
    if "transcribe_voice" in data:
        cfg.transcribe_voice = bool(data["transcribe_voice"])

    db.session.commit()
    return jsonify({"ok": True, "config": cfg.to_dict()})


@app.route("/api/ai/models")
@require_admin
@limiter.limit("30 per minute")
def get_ai_models():
    return jsonify(ai_service.MODELS)


@app.route("/api/ai/test", methods=["POST"])
@require_admin
@limiter.limit("10 per minute")
def test_ai():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    if not message:
        return jsonify({"error": "Le message de test est vide."}), 400
    try:
        return jsonify({"ok": True, **ai_service.test(message)})
    except ai_service.AIError as exc:
        return jsonify({"ok": False, "error": exc.message}), 400


@app.route("/api/ai/logs")
@require_admin
@limiter.limit("60 per minute")
def get_ai_logs():
    limit = min(int(request.args.get("limit", 20)), 100)
    logs = AILog.query.order_by(AILog.id.desc()).limit(limit).all()
    return jsonify([log.to_dict() for log in logs])


@app.route("/api/ai/transcribe", methods=["POST"])
@require_bot_key
@limiter.limit("60 per minute")
def transcribe_audio():
    """
    Transcrit une note vocale via Whisper (GROQ). Appelé par le bot Node.js
    quand il reçoit un vocal. Ne transcrit que si l'IA ET la transcription
    des vocaux sont activées dans l'onglet IA.
    """
    data = request.get_json(silent=True) or {}
    audio_b64 = str(data.get("audio", "") or "")
    if not audio_b64:
        return jsonify({"ok": False, "transcribed": False, "error": "Aucun audio reçu."}), 400

    # Garde-fou : limite de taille (base64 ≈ 4/3 du binaire → 34 Mo ≈ 25 Mo)
    if len(audio_b64) > 34 * 1024 * 1024:
        return jsonify({"ok": False, "transcribed": False,
                        "error": "Note vocale trop volumineuse."}), 400

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except (binascii.Error, ValueError):
        return jsonify({"ok": False, "transcribed": False, "error": "Audio invalide."}), 400
    if not audio_bytes:
        return jsonify({"ok": False, "transcribed": False, "error": "Audio vide."}), 400
    if len(audio_bytes) > 25 * 1024 * 1024:
        return jsonify({"ok": False, "transcribed": False,
                        "error": "Note vocale trop volumineuse."}), 400

    ai_cfg = AIConfig.get()
    if not ai_cfg.enabled:
        return jsonify({"ok": True, "transcribed": False, "reason": "ai_disabled"})
    if not ai_cfg.transcribe_voice:
        return jsonify({"ok": True, "transcribed": False, "reason": "voice_disabled"})

    mime = str(data.get("mime", "audio/ogg; codecs=opus")) or "audio/ogg"
    try:
        text, duration_ms = ai_service.transcribe(audio_bytes, mime=mime)
        return jsonify({
            "ok": True, "transcribed": True,
            "text": text, "duration_ms": duration_ms,
        })
    except ai_service.AIError as exc:
        # HTTP 200 : le bot journalise l'erreur sans crasher
        return jsonify({"ok": False, "transcribed": False, "error": exc.message})


# --------------------------------------------------------------------------- #
#  WhatsApp (état + redémarrage)
# --------------------------------------------------------------------------- #
@app.route("/api/whatsapp/status", methods=["GET"])
@require_admin
def get_whatsapp_status():
    """État WhatsApp affiché dans le panneau.

    Si l'état stocké est encore "unknown" (backend redémarré, ou démarré
    après le bot), on interroge directement le bot via /health pour afficher
    un état à jour immédiatement. La pulsation du bot synchronise ensuite
    la mémoire du backend.
    """
    state = dict(WHATSAPP_STATE)
    # La sonde est active si l'état est inconnu OU si personne ne s'est
    # manifesté depuis plus de 30 s (ex. bot coupé sans avoir signalé
    # "disconnected").
    updated = state.get("updated_at")
    stale = True
    if updated:
        try:
            stale = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.datetime.fromisoformat(updated)
            ) > datetime.timedelta(seconds=30)
        except (ValueError, TypeError):
            stale = True
    if state.get("status") == "unknown" or stale:
        try:
            response = requests.get(f"{BOT_INTERNAL_URL}/health", timeout=2)
            if response.status_code == 200:
                data = response.json() or {}
                live = str(data.get("status", "unknown"))
                if live in ("connected", "qr", "connecting", "disconnected"):
                    state["status"] = live
                    state["number"] = data.get("number") or state.get("number")
                    state["updated_at"] = utc_now_iso()
                    WHATSAPP_STATE.update(state)  # cohérence avec le dashboard
        except requests.exceptions.RequestException:
            pass  # bot injoignable : on garde l'état stocké
    return jsonify(state)


@app.route("/api/whatsapp/status", methods=["POST"])
@require_bot_key
def update_whatsapp_status():
    """Endpoint interne : le bot Node.js envoie son statut (et son QR code)."""
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "disconnected"))
    WHATSAPP_STATE.update({
        "status": status,
        "number": data.get("number") or None,
        "qr": data.get("qr") if status == "qr" else None,
        "updated_at": utc_now_iso(),
    })
    return jsonify({"ok": True})


@app.route("/api/transcript", methods=["GET"])
@require_admin
@limiter.limit("20 per minute")
def get_transcript():
    """
    Télécharge le journal des conversations (transcript.txt) écrit par le bot.
    Le fichier vit dans whatsapp-bot/ ; on le lit directement sur disque.
    """
    transcript_path = os.path.join(BASE_DIR, "..", "whatsapp-bot", "transcript.txt")
    try:
        if not os.path.exists(transcript_path):
            return jsonify({"ok": False, "error": "Le transcript est vide ou désactivé."}), 404
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return content, 200, {"Content-Type": "text/plain; charset=utf-8",
                              "Content-Disposition": 'attachment; filename="transcript.txt"'}
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Lecture impossible : {exc}"}), 500


@app.route("/api/whatsapp/restart", methods=["POST"])
@require_admin
@limiter.limit("10 per minute")
def restart_whatsapp():
    """Demande au bot Node.js de redémarrer sa connexion WhatsApp."""
    try:
        response = requests.post(
            f"{BOT_INTERNAL_URL}/internal/restart",
            headers={"X-Bot-Key": BOT_API_KEY},
            timeout=10,
        )
        if response.status_code == 200:
            return jsonify({"ok": True, "message": "Redémarrage demandé au bot…"})
        return jsonify({
            "ok": False,
            "message": f"Le bot a répondu avec le statut HTTP {response.status_code}.",
        }), 502
    except requests.exceptions.RequestException as exc:
        return jsonify({
            "ok": False,
            "message": f"Bot injoignable ({exc.__class__.__name__}). Vérifiez BOT_INTERNAL_URL.",
        }), 502


# --------------------------------------------------------------------------- #
#  Messages entrants (depuis le bot)
# --------------------------------------------------------------------------- #
@app.route("/api/message", methods=["POST"])
@require_bot_key
@limiter.limit("600 per minute")
def incoming_message():
    """Reçoit un message WhatsApp du bot et renvoie la réponse à envoyer."""
    data = request.get_json(silent=True) or {}
    try:
        result = whatsapp_handler.handle_message(
            body=str(data.get("body", "")),
            sender=str(data.get("from", "")),
            remote_jid=str(data.get("remoteJid", "")),
            is_group=bool(data.get("isGroup", False)),
            voice=bool(data.get("voice", False)),
        )
        return jsonify(result)
    except Exception as exc:  # garde-fou : le bot ne doit jamais crasher
        return jsonify({"reply": "❌ Une erreur interne est survenue. Réessayez plus tard."})


# --------------------------------------------------------------------------- #
#  Statistiques pour les graphiques du tableau de bord
# --------------------------------------------------------------------------- #
@app.route("/api/stats/chart")
@require_admin
@limiter.limit("30 per minute")
def stats_chart():
    """
    Activité par jour (messages, réponses IA, vocaux, succès, erreurs)
    sur les N derniers jours (défaut : 7) + totaux par statut pour le camembert.
    """
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except (TypeError, ValueError):
        days = 7

    today = datetime.datetime.now(datetime.timezone.utc).date()
    per_day = {}
    labels = []
    for offset in range(days - 1, -1, -1):
        key = (today - datetime.timedelta(days=offset)).isoformat()
        labels.append(key)
        per_day[key] = {"total": 0, "ai": 0, "vocal": 0, "success": 0, "error": 0}

    # On ne charge que les colonnes utiles (jamais les corps de messages)
    rows = CommandLog.query.with_entities(
        CommandLog.timestamp, CommandLog.status, CommandLog.is_ai, CommandLog.command
    ).all()
    for timestamp, status, is_ai, command in rows:
        key = (timestamp or "")[:10]
        bucket = per_day.get(key)
        if bucket is None:
            continue
        bucket["total"] += 1
        bucket["ai"] += 1 if is_ai else 0
        bucket["vocal"] += 1 if command == "VOCAL" else 0
        bucket["success"] += 1 if status == "success" else 0
        bucket["error"] += 1 if status == "error" else 0

    return jsonify({
        "days": days,
        "labels": labels,
        "series": {
            key: [per_day[day][key] for day in labels]
            for key in ("total", "ai", "vocal", "success", "error")
        },
        "totals": {
            "success": CommandLog.query.filter_by(status="success").count(),
            "error": CommandLog.query.filter_by(status="error").count(),
            "ignored": CommandLog.query.filter_by(status="ignored").count(),
        },
    })


# --------------------------------------------------------------------------- #
#  Test API BrixHub (depuis le panneau)
# --------------------------------------------------------------------------- #
@app.route("/api/test-search", methods=["POST"])
@require_admin
@limiter.limit("10 per minute")
def test_search():
    """Recherche BrixHub de test (utilisée par l'onglet "Test API")."""
    data = request.get_json(silent=True) or {}
    nom = str(data.get("nom_famille", "")).strip()
    if not nom:
        return jsonify({"error": "Le champ 'nom' est requis."}), 400

    prenom = str(data.get("prenom", "")).strip() or None
    ville = str(data.get("ville", "")).strip() or None
    flexible = data.get("flexible")
    per_page = data.get("per_page")

    try:
        result = brixhub_service.search(
            nom, prenom, ville, flexible=flexible, max_results=per_page
        )
        label = whatsapp_handler.build_label(nom, prenom, ville)
        formatted = whatsapp_handler.format_results(result, label, hint=f".search {nom}")
        return jsonify({"ok": True, "result": result, "formatted": formatted})
    except brixhub_service.BrixHubError as exc:
        return jsonify({"ok": False, "error": exc.message}), 400


@app.route("/api/brixhub/me")
@require_admin
@limiter.limit("10 per minute")
def brixhub_me():
    """Statistiques d'utilisation du compte BrixHub (GET /me)."""
    try:
        return jsonify({"ok": True, "data": brixhub_service.get_me()})
    except brixhub_service.BrixHubError as exc:
        return jsonify({"ok": False, "error": exc.message}), 400


# --------------------------------------------------------------------------- #
#  Logs
# --------------------------------------------------------------------------- #
@app.route("/api/logs")
@require_admin
@limiter.limit("60 per minute")
def get_logs():
    """Historique paginé des commandes, avec filtres (statut / type)."""
    status = request.args.get("status")
    is_ai = request.args.get("is_ai")
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(max(int(request.args.get("per_page", 20)), 1), 100)
    except ValueError:
        page, per_page = 1, 20

    query = CommandLog.query
    if status in ("success", "error", "ignored"):
        query = query.filter_by(status=status)
    if is_ai in ("true", "false"):
        query = query.filter_by(is_ai=(is_ai == "true"))

    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page)
    logs = query.order_by(CommandLog.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "logs": [log.to_dict() for log in logs],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })


@app.route("/api/logs/clear", methods=["POST"])
@require_admin
@limiter.limit("10 per minute")
def clear_logs():
    CommandLog.query.delete()
    AILog.query.delete()
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  Diagnostic complet (panneau → copier/coller à l'assistant)
# --------------------------------------------------------------------------- #
@app.route("/api/debug/dump", methods=["GET"])
@require_admin
@limiter.limit("10 per minute")
def debug_dump():
    """
    Export de diagnostic : config (clés masquées), état des services, derniers
    logs, et le journal du bot. L'utilisateur colle ce JSON à son assistant
    pour résoudre un problème sans avoir à décrire les symptômes.
    """
    def mask(value):
        value = str(value or "")
        if not value:
            return ""
        if len(value) <= 8:
            return "***"
        return value[:4] + "…" + f" ({len(value)} caractères)"

    # Dernières lignes du journal du bot (si le fichier existe)
    bot_log_lines = []
    bot_log_path = os.path.join(BASE_DIR, "..", "whatsapp-bot", "bot-messages.log")
    try:
        if os.path.exists(bot_log_path):
            with open(bot_log_path, "r", encoding="utf-8", errors="replace") as fh:
                bot_log_lines = fh.readlines()[-40:]
    except OSError:
        pass

    # Santé du bot (via son endpoint interne)
    bot_health = None
    try:
        response = requests.get(f"{BOT_INTERNAL_URL}/health", timeout=3)
        if response.status_code == 200:
            bot_health = response.json()
    except requests.exceptions.RequestException:
        bot_health = {"error": "bot injoignable"}

    recent = CommandLog.query.order_by(CommandLog.id.desc()).limit(30).all()
    ai_recent = AILog.query.order_by(AILog.id.desc()).limit(10).all()

    bot_cfg = BotConfig.get()
    ai_cfg = AIConfig.get()

    return jsonify({
        "generated_at": utc_now_iso(),
        "services": {
            "backend": {"ok": True, "whatsapp": WHATSAPP_STATE.get("status")},
            "bot": bot_health,
        },
        "environnement": {
            "OWNER_NUMBER": "défini" if os.environ.get("OWNER_NUMBER", "").strip() else "vide/absent",
            "GROQ_API_KEY": "défini" if os.environ.get("GROQ_API_KEY", "").strip() else "vide/absent",
            "BRIX_API_KEY": "défini" if os.environ.get("BRIX_API_KEY", "").strip() else "vide/absent",
            "ADMIN_PASSWORD": "défini" if os.environ.get("ADMIN_PASSWORD", "").strip() else "vide",
            "BOT_API_KEY": "défini" if os.environ.get("BOT_API_KEY", "").strip() else "défaut",
            "SECRET_KEY": "défini" if os.environ.get("SECRET_KEY", "").strip() else "défaut",
        },
        "bot_config": {**bot_cfg.to_dict(), "api_key": mask(bot_cfg.api_key)},
        "ai_config": {**ai_cfg.to_dict(), "api_key": mask(ai_cfg.api_key)},
        "liste_noire_conversations": ai_cfg.ai_blacklist or "(vide)",
        "stats": _compute_stats(),
        "derniers_logs": [log.to_dict() for log in recent],
        "logs_ia": [log.to_dict() for log in ai_recent],
        "journal_bot": "".join(bot_log_lines),
    })


# --------------------------------------------------------------------------- #
#  Démarrage
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
