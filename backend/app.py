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
import io
import json
import os
import re
import sys
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import ai_service
import brat_scrape_service
import brat_service
import brixhub_service
import imagine_service
import pinterest_service
import pornpics_service
import reverse_service
import rule34_service
import shazam_service
import whatsapp_handler
from database import AIConfig, AILog, AIMemory, BotConfig, CommandLog, GalleryItem, db, init_db, utc_now_iso

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


def _push_config_to_bot(cfg):
    """Pousse la configuration légère au bot Node.js, INSTANTANÉMENT.

    Appelée après chaque sauvegarde de configuration dans le panneau : le bot
    met à jour sa config en mémoire sans attendre son polling (30 s).
    Fire-and-forget : si le bot est injoignable (éteint, en redémarrage…), le
    polling reprendra la main au prochain cycle.
    """
    try:
        requests.post(
            f"{BOT_INTERNAL_URL}/internal/config",
            json={
                "newsletter_react_enabled": bool(cfg.newsletter_react_enabled),
                "newsletter_react_emoji": cfg.newsletter_react_emoji or "👍",
            },
            headers={"X-Bot-Key": BOT_API_KEY},
            timeout=5,
        )
    except Exception:
        pass  # bot injoignable : le polling 30 s prendra le relais


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


# --------------------------------------------------------------------------- #
#  Site bratify local (self-host) — copié par fetch_bratify_site.py
# --------------------------------------------------------------------------- #
# Le générateur brat est servi EN LOCAL sous /bratify/ : le scraping Playwright
# charge http://localhost/bratify/ au lieu du site distant → rendu 100 %
# identique, aucune dépendance réseau, fonctionne même si bratify est down.
BRATIFY_SITE_DIR = os.path.join(BASE_DIR, "bratify_site")


@app.route("/bratify/")
def bratify_home():
    # On lit le fichier depuis le disque (pas send_from_directory : son objet
    # Response est en mode "passthrough", impossible à modifier).
    index_path = os.path.join(BRATIFY_SITE_DIR, "index.html")
    if not os.path.exists(index_path):
        return jsonify({"error": "Site bratify non copié. Lancer fetch_bratify_site.py."}), 404
    with open(index_path, "rb") as fh:
        html_bytes = fh.read()
    # Le HTML copié référence certains assets en chemin ABSOLU (/_app/...) qui
    # 404 sur notre serveur (le site est servi sous /bratify/). On les réécrit.
    html_bytes = html_bytes.replace(b'href="/_app/', b'href="/bratify/_app/')
    return Response(html_bytes, mimetype="text/html")


@app.route("/bratify/<path:filename>")
def bratify_files(filename):
    return send_from_directory(BRATIFY_SITE_DIR, filename)


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
    if "newsletter_react_enabled" in data:
        cfg.newsletter_react_enabled = bool(data["newsletter_react_enabled"])
    if "newsletter_react_emoji" in data:
        emoji = str(data["newsletter_react_emoji"]).strip()
        cfg.newsletter_react_emoji = (emoji or "👍")[:16]

    db.session.commit()

    # Pousse la config au bot instantanément (pas d'attente du polling 30 s).
    _push_config_to_bot(cfg)

    return jsonify({"ok": True, "config": cfg.to_dict()})


@app.route("/api/bot/config", methods=["GET"])
@require_bot_key
@limiter.limit("60 per minute")
def bot_config_for_bot():
    """Configuration légère que le bot Node.js poll régulièrement.

    Le bot n'a pas besoin de TOUTE la configuration (les clés API restent
    dans le backend) : uniquement ce dont il a besoin côté Node pour ses
    réactions autonomes — ici, la réaction émoji aux chaînes @newsletter.
    """
    cfg = BotConfig.get()
    return jsonify({
        "newsletter_react_enabled": bool(cfg.newsletter_react_enabled),
        "newsletter_react_emoji": cfg.newsletter_react_emoji or "👍",
    })


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
    if "ask_prompt" in data:
        cfg.ask_prompt = str(data["ask_prompt"]).strip()

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
    # Transcription "à la demande" (commande .transcript) : on ignore les
    # réglages enabled / transcribe_voice, seule la clé GROQ est requise.
    manual = bool(data.get("manual", False))
    if not manual:
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


@app.route("/api/correct", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def correct_text():
    """
    Corrige l'orthographe et la grammaire d'un texte via GROQ.

    Appelé par le bot Node.js pour la commande .correct (message cité ou
    texte direct). Le prompt système dédié est défini dans ai_service ; la
    clé GROQ ne quitte jamais le backend.
    """
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Aucun texte à corriger."}), 400
    if len(text) > 4000:
        return jsonify({"ok": False, "error": "Texte trop long (4000 caractères max)."}), 400

    try:
        corrected, model, tokens, duration_ms = ai_service.correct(text)
        return jsonify({
            "ok": True,
            "corrected": corrected,
            "model": model,
            "tokens_used": tokens,
            "duration_ms": duration_ms,
        })
    except ai_service.AIError as exc:
        return jsonify({"ok": False, "error": exc.message}), 400


@app.route("/api/ai/memory/clear", methods=["POST"])
@require_bot_key
@limiter.limit("60 per minute")
def clear_ai_memory():
    """
    Efface la mémoire IA d'un utilisateur (commande .clearmem).

    Le bot Node.js envoie l'identifiant (jid) de l'expéditeur dont il faut
    oublier le contexte : toutes les entrées AIMemory de ce sender sont
    supprimées en base. L'IA repartira de zéro avec cet utilisateur.
    """
    data = request.get_json(silent=True) or {}
    sender = str(data.get("sender", "") or "").strip()
    if not sender:
        return jsonify({"ok": False, "error": "Aucun expéditeur fourni."}), 400
    if len(sender) > 100:
        return jsonify({"ok": False, "error": "Expéditeur invalide."}), 400

    deleted = AIMemory.query.filter_by(sender=sender).delete()
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/resume", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def resume_text():
    """
    Résume un texte via GROQ (commande .resume).

    Appelé par le bot Node.js : le texte cité est résumé par l'IA avec un
    prompt système dédié (défini dans ai_service). La clé GROQ ne quitte
    jamais le backend.
    """
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Aucun texte à résumer."}), 400
    if len(text) > ai_service.RESUME_MAX_CHARS:
        return jsonify({"ok": False,
                        "error": "Texte trop long (%d caractères max)." % ai_service.RESUME_MAX_CHARS}), 400

    try:
        summary, model, tokens, duration_ms = ai_service.resume(text)
        return jsonify({
            "ok": True,
            "summary": summary,
            "model": model,
            "tokens_used": tokens,
            "duration_ms": duration_ms,
        })
    except ai_service.AIError as exc:
        return jsonify({"ok": False, "error": exc.message}), 400


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


# --------------------------------------------------------------------------- #
#  Chats — reconstruction des conversations depuis le journal structuré (JSONL)
# --------------------------------------------------------------------------- #

TRANSCRIPT_JSONL = os.path.join(BASE_DIR, "..", "whatsapp-bot", "transcript.jsonl")
TRANSCRIPT_TXT = os.path.join(BASE_DIR, "..", "whatsapp-bot", "transcript.txt")

_TXT_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.+?) : (.*)$")

# Cache simple (clé = mtime+taille des deux journaux) : le panneau interroge
# ces routes toutes les 15 s, on évite de reparser 5 Mo à chaque fois.
_chat_cache = {"key": None, "entries": None, "approx": None}


def _load_jsonl_entries():
    """Lit le journal structuré (transcript.jsonl) : une entrée par ligne."""
    entries = []
    if not os.path.exists(TRANSCRIPT_JSONL):
        return entries
    try:
        with open(TRANSCRIPT_JSONL, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue  # ligne corrompue : ignorée, on continue
                if not e.get("jid"):
                    continue
                entries.append({
                    "ts": str(e.get("ts") or ""),
                    "jid": str(e["jid"]),
                    "sender": str(e.get("sender") or ""),
                    "name": str(e.get("name") or ""),
                    "chat_name": str(e.get("chat_name") or e.get("name") or ""),
                    "content": str(e.get("content") or ""),
                    "fromMe": bool(e.get("fromMe")),
                    "type": str(e.get("type") or ""),
                })
    except OSError:
        pass
    return entries


def _chat_entries_from_txt():
    """
    Repli (approximatif) sur transcript.txt : lit le journal texte et regroupe
    par nom. Sans identifiants, deux contacts au même nom seraient fusionnés et
    les réponses du bot sont rattachées à la dernière conversation vue — d'où
    le drapeau "approx" affiché dans le panneau.
    """
    entries = []
    if not os.path.exists(TRANSCRIPT_TXT):
        return entries
    last_non_bot_key = None
    conv_of_name = {}
    try:
        with open(TRANSCRIPT_TXT, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _TXT_LINE_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                stamp, name, content = m.group(1), m.group(2).strip(), m.group(3)
                is_bot = name.startswith("🤖") or name.strip() == "BrixBot"
                if is_bot:
                    key = last_non_bot_key
                    sender = ""
                else:
                    key = conv_of_name.get(name)
                    if key is None:
                        key = f"txt:{name}"
                        conv_of_name[name] = key
                    last_non_bot_key = key
                    sender = key
                if key is None:
                    continue
                # Le transcript.txt est en heure locale : on convertit en UTC pour
                # rester cohérent avec le JSONL (les deux journaux se fusionnent).
                try:
                    naive = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M")
                    ts = naive.astimezone().astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    ts = f"{stamp.replace(' ', 'T')}:00"
                entries.append({
                    "ts": ts,
                    "jid": key,
                    "sender": sender,
                    "name": "🤖 BrixBot" if is_bot else name,
                    "chat_name": "" if is_bot else name,
                    "content": content,
                    "fromMe": is_bot,
                    "type": "txt",
                })
    except OSError:
        pass
    return entries


def _chat_entries():
    """
    Charge tous les messages échangés (liste de dicts). Priorité au journal
    structuré JSONL ; l'ancien journal texte (transcript.txt) est FUSIONNÉ
    pour que l'historique déjà collecté reste visible : les conversations texte
    dont le nom existe déjà dans le JSONL sont rattachées à la vraie conversation
    (jid réel), les autres gardent un jid "txt:Nom".

    Retourne (entries, approx) où approx=True signale qu'une partie des données
    provient de l'ancien format (reconstruction approximative).
    """
    def _key():
        parts = []
        for path in (TRANSCRIPT_JSONL, TRANSCRIPT_TXT):
            try:
                st = os.stat(path)
                parts.append(f"{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append("x")
        return "|".join(parts)

    key = _key()
    if _chat_cache["key"] == key:
        return _chat_cache["entries"], _chat_cache["approx"]

    jsonl_entries = _load_jsonl_entries()
    txt_entries = _chat_entries_from_txt()

    if not jsonl_entries and not txt_entries:
        _chat_cache.update(key=key, entries=[], approx=False)
        return [], False

    if not jsonl_entries:
        _chat_cache.update(key=key, entries=txt_entries, approx=True)
        return txt_entries, True

    # Fusion : nom de conversation (chat_name JSONL) → jid réel
    name_to_jid = {}
    for e in jsonl_entries:
        if e["chat_name"] and e["chat_name"] not in name_to_jid:
            name_to_jid[e["chat_name"]] = e["jid"]

    merged = list(jsonl_entries)
    txt_used = 0
    for e in txt_entries:
        if e["jid"].startswith("txt:"):
            target = name_to_jid.get(e["jid"][4:])
            if target:
                e["jid"] = target
        merged.append(e)
        txt_used += 1

    _chat_cache.update(key=key, entries=merged, approx=txt_used > 0)
    return merged, txt_used > 0


@app.route("/api/chats")
@require_admin
@limiter.limit("30 per minute")
def chats_list():
    """
    Liste les conversations reconstruites depuis le journal du bot, triées par
    dernière activité. Paramètre optionnel ?q= pour filtrer par nom ou jid.
    """
    entries, approx = _chat_entries()
    chats = {}
    for e in entries:
        c = chats.get(e["jid"])
        if c is None:
            c = {
                "jid": e["jid"],
                "name": e["chat_name"] or e["name"] or "Inconnu",
                "count": 0,
                "last_ts": "",
                "last_text": "",
                "last_from_me": False,
            }
            chats[e["jid"]] = c
        c["count"] += 1
        if e["ts"] >= c["last_ts"]:
            c["last_ts"] = e["ts"]
            c["last_text"] = e["content"]
            c["last_from_me"] = e["fromMe"]

    items = sorted(chats.values(), key=lambda c: c["last_ts"], reverse=True)
    q = (request.args.get("q") or "").strip().lower()
    if q:
        items = [c for c in items if q in c["name"].lower() or q in c["jid"].lower()]
    return jsonify({
        "chats": items[:200],
        "total": len(items),
        "approx": approx,
        "source": "txt" if approx else "jsonl",
    })


@app.route("/api/chats/messages")
@require_admin
@limiter.limit("60 per minute")
def chats_messages():
    """
    Messages d'une conversation, du plus ancien au plus récent. Pagination par
    l'arrière via ?offset=<nombre déjà chargé> : le fil de discussion ne perd
    jamais de messages, même quand plusieurs partagent le même timestamp.
    """
    jid = (request.args.get("jid") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "Paramètre jid requis."}), 400
    try:
        limit = min(int(request.args.get("limit") or 60), 200)
    except ValueError:
        limit = 60
    limit = max(1, limit)
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        offset = 0

    entries, approx = _chat_entries()
    mine = sorted(
        (e for e in entries if e["jid"] == jid),
        key=lambda e: (e["ts"], e["sender"]),
    )
    total = len(mine)
    end = max(0, total - offset)
    start = max(0, end - limit)
    page = mine[start:end]
    return jsonify({
        "messages": page,
        "has_more": start > 0,
        "total": total,
        "approx": approx,
    })


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
#  Stickers — conversion d'image en WebP 512×512 (fallback du bot quand sharp
#  est indisponible, ex: Termux/Android). Pillow encode le WebP nativement.
# --------------------------------------------------------------------------- #
@app.route("/api/sticker", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def make_sticker():
    """
    Convertit l'image reçue (body brut) en sticker WebP 512×512.

    Utilisé par le bot Node.js quand la bibliothèque native sharp n'est pas
    installable (Termux/Android) : le bot envoie les octets de la photo ici,
    Pillow redimensionne (fit contain, fond transparent) et encode en WebP.
    La réponse est l'image WebP brute (Content-Type: image/webp).
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        # install.sh installe Pillow de façon non-fatale : il peut manquer
        return jsonify({"ok": False, "error": "Pillow non installé sur le backend (python-pillow requis pour .sticker)."}), 503

    raw = request.get_data()
    if not raw:
        return jsonify({"ok": False, "error": "Image manquante."}), 400
    if len(raw) > 15 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Image trop lourde (> 15 Mo)."}), 400

    try:
        # Ouverture + correction automatique de l'orientation EXIF (photos)
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        img = img.convert("RGBA")
        # Fit contain 512×512 sur fond transparent (comme sharp)
        img.thumbnail((512, 512), Image.LANCZOS)
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        canvas.paste(
            img,
            ((512 - img.width) // 2, (512 - img.height) // 2),
            img,
        )
        out = io.BytesIO()
        canvas.save(out, "WEBP", quality=90)
        return Response(out.getvalue(), mimetype="image/webp")
    except Exception as exc:  # image corrompue / format inconnu
        return jsonify({"ok": False, "error": f"Conversion impossible : {exc}"}), 422


# --------------------------------------------------------------------------- #
#  Sticker → image — commande .image (l'inverse de .sticker). Pillow décode le
#  WebP et ré-encode en PNG (la transparence des stickers est conservée).
# --------------------------------------------------------------------------- #
@app.route("/api/sticker-to-image", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def sticker_to_image():
    """
    Convertit le sticker WebP reçu (body brut) en image PNG.

    Utilisé par le bot Node.js pour la commande .image quand sharp n'est pas
    installable (Termux/Android) : le bot envoie les octets du sticker ici,
    Pillow décode le WebP (première frame pour un sticker animé) et ré-encode
    en PNG — la transparence est conservée. Réponse : PNG brut.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return jsonify({"ok": False, "error": "Pillow non installé sur le backend (python-pillow requis pour .image)."}), 503

    raw = request.get_data()
    if not raw:
        return jsonify({"ok": False, "error": "Sticker manquant."}), 400
    if len(raw) > 15 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Sticker trop lourd (> 15 Mo)."}), 400

    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        # PNG conserve la transparence des stickers (fond transparent)
        img = img.convert("RGBA")
        out = io.BytesIO()
        img.save(out, "PNG")
        return Response(out.getvalue(), mimetype="image/png")
    except Exception as exc:  # format inconnu / sticker corrompu
        return jsonify({"ok": False, "error": f"Conversion impossible : {exc}"}), 422


# --------------------------------------------------------------------------- #
#  Stickers brat — esthétique Charli XCX : fond blanc, texte minuscules noir,
#  police condensée + grain. Généré par Pillow (backend/brat_service.py).
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  Images IA — commande .imagine (Pollinations, gratuit sans clé)
# --------------------------------------------------------------------------- #
@app.route("/api/imagine", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def imagine_image():
    """
    Génère une image par IA (Pollinations) à partir d'une description.

    Corps JSON : {"prompt": "un chat ninja dans l'espace"}. Utilisé par la
    commande .imagine du bot Node.js. La réponse est l'image brute
    (Content-Type: image/jpeg).
    """
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "Description manquante."}), 400
    if len(prompt) > 500:
        return jsonify({"ok": False, "error": "Description trop longue (500 caractères max)."}), 400

    image = imagine_service.generate(prompt)
    if not image:
        return jsonify({"ok": False, "error": "Génération impossible. Réessayez plus tard."}), 502
    return Response(image, mimetype="image/jpeg")


# --------------------------------------------------------------------------- #
#  Reconnaissance musicale — commande .shazam (shazamio, gratuit sans clé)
# --------------------------------------------------------------------------- #
@app.route("/api/shazam", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def shazam_recognize():
    """
    Identifie la chanson contenue dans l'audio reçu (WAV).

    Corps JSON : {"audio": "<base64 WAV>"}. Utilisé par la commande .shazam
    du bot Node.js (qui convertit le vocal ogg/opus en WAV avec ffmpeg-static).
    """
    data = request.get_json(silent=True) or {}
    audio_b64 = str(data.get("audio", "") or "")
    if not audio_b64:
        return jsonify({"ok": False, "error": "Aucun audio reçu."}), 400
    # Garde-fou : base64 ≈ 4/3 du binaire → 28 Mo ≈ 21 Mo d'audio
    if len(audio_b64) > 28 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Audio trop volumineux."}), 400

    if not shazam_service.available():
        return jsonify({"ok": False,
                        "error": "shazamio n'est pas installé sur le backend (requirements.txt)."}), 503

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except (binascii.Error, ValueError):
        return jsonify({"ok": False, "error": "Audio invalide."}), 400
    if not audio_bytes:
        return jsonify({"ok": False, "error": "Audio vide."}), 400

    return jsonify(shazam_service.recognize(audio_bytes))


@app.route("/api/brat", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def make_brat():
    """
    Transforme un texte en sticker brat (WebP 512×512, fond blanc).

    Corps JSON : {"text": "votre texte"}. Utilisé par la commande .brat du
    bot Node.js. La réponse est l'image WebP brute (Content-Type: image/webp).
    """
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Texte manquant."}), 400
    if len(text) > 600:
        return jsonify({"ok": False, "error": "Texte trop long (max 600 caractères)."}), 400

    # 1) Scraping du VRAI générateur (bratify.vercel.app) : rendu authentique.
    #    Si le navigateur est absent ou le site indisponible → génération locale.
    webp = brat_scrape_service.render(text) if brat_scrape_service.available() else None
    if not webp:
        webp = brat_service.render(text)
    if not webp:
        return jsonify({"ok": False, "error": "Rendu brat impossible."}), 422
    return Response(webp, mimetype="image/webp")


# --------------------------------------------------------------------------- #
#  Galerie média — vus uniques capturés silencieusement + médias .extract
# --------------------------------------------------------------------------- #
# Les fichiers vivent dans backend/gallery/ (gitignoré). Deux sources : les
# images/vidéos "vu unique" (le bot télécharge à la réception, enregistre, ne
# répond rien) et les médias extraits par la commande .extract du bot.
GALLERY_DIR = os.path.join(BASE_DIR, "gallery")
os.makedirs(GALLERY_DIR, exist_ok=True)

# Extension déduite du type MIME (jamais prise du client : sécurité)
_GALLERY_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/heic": ".heic",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/3gpp": ".3gp",
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/wav": ".wav", "audio/webm": ".webm", "audio/amr": ".amr",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "application/zip": ".zip",
    "application/vnd.android.package-archive": ".apk",
    "application/octet-stream": ".bin",
}


@app.route("/api/gallery/save", methods=["POST"])
@require_bot_key
@limiter.limit("120 per minute")
def gallery_save():
    """
    Enregistre un média "vu unique" capturé par le bot (endpoint interne).

    Corps JSON : {media: base64, media_type, mime, sender, chat, caption}.
    Le fichier est écrit dans backend/gallery/ avec un nom sécurisé, la ligne
    est ajoutée en base, et on ne répond RIEN à l'expéditeur WhatsApp.
    """
    data = request.get_json(silent=True) or {}
    media_b64 = str(data.get("media", "") or "")
    if not media_b64:
        return jsonify({"ok": False, "error": "Média manquant."}), 400
    # Garde-fou : base64 ≈ 4/3 du binaire → 34 Mo ≈ 25 Mo de média
    if len(media_b64) > 34 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Média trop volumineux."}), 400

    try:
        media_bytes = base64.b64decode(media_b64)
    except (binascii.Error, ValueError):
        return jsonify({"ok": False, "error": "Média invalide."}), 400
    if not media_bytes:
        return jsonify({"ok": False, "error": "Média vide."}), 400

    media_type = str(data.get("media_type", "image") or "image")
    if media_type not in ("image", "video", "audio", "document"):
        media_type = "image"
    # MIME nettoyé de ses paramètres (ex: "audio/ogg; codecs=opus" → "audio/ogg")
    mime = str(data.get("mime", "") or "").lower().split(";")[0].strip()
    if mime not in _GALLERY_EXT:
        # Document inconnu → .bin (jamais .pdf : on ne fausse pas le type réel)
        mime = {"video": "video/mp4", "audio": "audio/ogg",
                "document": "application/octet-stream"}.get(media_type, "image/jpeg")
    ext = _GALLERY_EXT[mime]

    # Dédoublonnage : si ce message WhatsApp a déjà été archivé (capture à la
    # réception, réponse du propriétaire ou .extract), on ne l'enregistre pas
    # deux fois. L'id WhatsApp du message d'origine est stable quel que soit le
    # moyen de capture (la réponse cite le même id que le message reçu).
    message_id = str(data.get("message_id", "") or "").strip()
    if message_id:
        existing = GalleryItem.query.filter_by(message_id=message_id).first()
        if existing:
            return jsonify({"ok": True, "id": existing.id, "duplicate": True})

    # Nom de fichier sécurisé : jamais construit depuis une entrée client
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}-{os.urandom(4).hex()}{ext}"
    try:
        with open(os.path.join(GALLERY_DIR, filename), "wb") as fh:
            fh.write(media_bytes)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Écriture impossible : {exc}"}), 500

    item = GalleryItem(
        sender=str(data.get("sender", "") or "")[:100],
        chat=str(data.get("chat", "") or "")[:100],
        media_type=media_type,
        mime=mime,
        filename=filename,
        caption=str(data.get("caption", "") or "")[:500],
        size=len(media_bytes),
        message_id=message_id,
    )
    db.session.add(item)
    db.session.commit()

    # Garde-fou : la galerie ne doit jamais remplir le disque du téléphone.
    # Au-delà de GALLERY_MAX, les médias les plus anciens sont supprimés
    # (fichier + ligne).
    GALLERY_MAX = 500
    overflow = GalleryItem.query.order_by(GalleryItem.id.desc()).offset(GALLERY_MAX).all()
    for old in overflow:
        try:
            os.remove(os.path.join(GALLERY_DIR, old.filename))
        except OSError:
            pass
        db.session.delete(old)
    if overflow:
        db.session.commit()
        print(f"[galerie] nettoyage auto : {len(overflow)} média(s) ancien(s) supprimé(s)")

    return jsonify({"ok": True, "id": item.id})


@app.route("/api/gallery")
@require_admin
@limiter.limit("60 per minute")
def gallery_list():
    """Liste paginée des médias capturés (onglet Galerie du panneau)."""
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(max(int(request.args.get("per_page", 24)), 1), 100)
    except ValueError:
        page, per_page = 1, 24

    query = GalleryItem.query
    media_type = request.args.get("type")
    if media_type in ("image", "video", "audio", "document"):
        query = query.filter_by(media_type=media_type)
    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page)
    items = query.order_by(GalleryItem.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        "items": [item.to_dict() for item in items],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })


@app.route("/api/gallery/<int:item_id>/file")
@require_admin
@limiter.limit("300 per minute")
def gallery_file(item_id):
    """Sert le fichier du média capturé (aperçu dans le panneau)."""
    item = db.session.get(GalleryItem, item_id)
    if item is None:
        return jsonify({"error": "Média introuvable."}), 404
    return send_from_directory(GALLERY_DIR, item.filename)


@app.route("/api/gallery/<int:item_id>", methods=["DELETE"])
@require_admin
@limiter.limit("30 per minute")
def gallery_delete(item_id):
    """Supprime un média capturé (fichier + entrée en base)."""
    item = db.session.get(GalleryItem, item_id)
    if item is None:
        return jsonify({"ok": False, "error": "Média introuvable."}), 404
    try:
        os.remove(os.path.join(GALLERY_DIR, item.filename))
    except OSError:
        pass  # fichier déjà absent : on supprime quand même la ligne
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  Pinterest — priorité de la commande .pin
# --------------------------------------------------------------------------- #
@app.route("/api/pin/search", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def pin_search():
    """
    Recherche d'images Pinterest (priorité .pin).

    Appelé par le bot Node.js en PREMIER pour la commande .pin. La recherche
    utilise l'API Pinterest directement (requests seul, aucune dépendance
    lourde). Si Pinterest ne renvoie rien, le bot retombe sur ses méthodes
    DuckDuckGo / Wikimedia — jamais d'échec bloquant.
    """
    data = request.get_json(silent=True) or {}
    query = str(data.get("query", "") or "").strip()
    try:
        count = max(1, min(int(data.get("count", 10)), 30))
    except (TypeError, ValueError):
        count = 10
    if not query:
        return jsonify({"ok": False, "error": "Requête vide."}), 400
    if len(query) > 200:
        return jsonify({"ok": False, "error": "Requête trop longue."}), 400

    urls = pinterest_service.search(query, count)
    return jsonify({
        "ok": True,
        "source": "pinterest" if urls else "unavailable",
        "urls": urls,
        "pinterest_available": pinterest_service.available(),
    })


# --------------------------------------------------------------------------- #
#  Rule34 — commande CACHÉE .nsfw (parsing HTML requests seul)
# --------------------------------------------------------------------------- #
@app.route("/api/nsfw/search", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def nsfw_search():
    """
    Recherche d'images rule34.xxx (commande cachée .nsfw).

    Appelé par le bot Node.js pour la commande .nsfw. La recherche est faite
    par rule34_service (parsing HTML direct avec requests SEUL — aucune
    dépendance lourde, fonctionne aussi sur Termux/Android) : la recherche est
    en thread avec un timeout global, et le service ne lève jamais
    d'exception. Si rien n'est trouvé, le bot affiche une erreur propre —
    jamais d'échec bloquant.
    """
    data = request.get_json(silent=True) or {}
    query = str(data.get("query", "") or "").strip()
    # Le bot demande PLUS d'URLs que le nombre d'images voulu (liens morts
    # ignorés) : on accepte jusqu'à 30 comme la route Pinterest.
    try:
        count = max(1, min(int(data.get("count", 5)), 30))
    except (TypeError, ValueError):
        count = 5
    if not query:
        return jsonify({"ok": False, "error": "Requête vide."}), 400
    if len(query) > 200:
        return jsonify({"ok": False, "error": "Requête trop longue."}), 400

    result = rule34_service.search(query, count)
    urls = result.get("urls") or []
    return jsonify({
        "ok": True,
        "source": "rule34" if urls else "unavailable",
        "urls": urls,
        "reachable": bool(result.get("reachable")),
        "rule34_available": rule34_service.available(),
    })


# --------------------------------------------------------------------------- #
#  PornPics — commande CACHÉE .xxx (photos réelles, parsing HTML requests seul)
# --------------------------------------------------------------------------- #
@app.route("/api/xxx/search", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def xxx_search():
    """
    Recherche d'images pornpics.com (commande cachée .xxx — photos réelles,
    le pendant non-anime de .nsfw).

    Appelé par le bot Node.js pour la commande .xxx. La recherche est faite
    par pornpics_service (parsing HTML direct avec requests SEUL — aucune
    dépendance lourde, fonctionne aussi sur Termux/Android) : une seule
    requête à pornpics.com, URLs du CDN cdni mises à la pleine résolution
    (1280), HEAD de vérification, le tout en thread avec un timeout global.
    Le service ne lève jamais d'exception. Si rien n'est trouvé, le bot
    affiche une erreur propre — jamais d'échec bloquant.
    """
    data = request.get_json(silent=True) or {}
    query = str(data.get("query", "") or "").strip()
    try:
        count = max(1, min(int(data.get("count", 5)), 30))
    except (TypeError, ValueError):
        count = 5
    if not query:
        return jsonify({"ok": False, "error": "Requête vide."}), 400
    if len(query) > 200:
        return jsonify({"ok": False, "error": "Requête trop longue."}), 400

    result = pornpics_service.search(query, count)
    urls = result.get("urls") or []
    return jsonify({
        "ok": True,
        "source": "pornpics" if urls else "unavailable",
        "urls": urls,
        "reachable": bool(result.get("reachable")),
        "pornpics_available": pornpics_service.available(),
    })


# --------------------------------------------------------------------------- #
#  Recherche d'images inversée — commande .rev (catbox + Yandex, sans clé)
# --------------------------------------------------------------------------- #
@app.route("/api/rev", methods=["POST"])
@require_bot_key
@limiter.limit("30 per minute")
def reverse_image_search():
    """
    Recherche d'images inversée (commande .rev).

    Corps JSON : {"image": "<base64 jpeg>", "count": 5, "filename": "..."}.
    Appelé par le bot Node.js : l'image est uploadée anonymement sur catbox.moe
    puis cherchée sur Yandex Images (rpt=imageview) — le tout avec `requests`
    SEUL (aucune dépendance lourde, fonctionne aussi sur Termux/Android).
    Le service ne lève jamais d'exception : la réponse est toujours un JSON
    propre avec `ok` et, en cas d'échec, un `error` lisible.
    """
    data = request.get_json(silent=True) or {}
    image_b64 = str(data.get("image", "") or "")
    if not image_b64:
        return jsonify({"ok": False, "error": "Aucune image reçue."}), 400
    # Garde-fou : base64 ≈ 4/3 du binaire → 28 Mo ≈ 20 Mo d'image
    if len(image_b64) > 28 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Image trop volumineuse (20 Mo max)."}), 400

    try:
        image_bytes = base64.b64decode(image_b64)
    except (binascii.Error, ValueError):
        return jsonify({"ok": False, "error": "Image invalide."}), 400
    if not image_bytes:
        return jsonify({"ok": False, "error": "Image vide."}), 400

    filename = str(data.get("filename", "image.jpg") or "image.jpg")[:120]
    count = data.get("count", 5)
    return jsonify(reverse_service.reverse_search(image_bytes, filename=filename, limit=count))


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
