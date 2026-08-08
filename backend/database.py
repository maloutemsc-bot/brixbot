"""
database.py — Modèles SQLAlchemy et initialisation de la base SQLite.

Tables :
  - bot_config   : configuration générale du bot (.search / BrixHub)
  - ai_config    : configuration de l'IA (GROQ)
  - command_logs : historique des commandes traitées
  - ai_logs      : historique des conversations IA
"""

from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Prompt système par défaut pour l'IA (utilisé si le champ est vide)
DEFAULT_SYSTEM_PROMPT = (
    "Tu es un assistant WhatsApp intelligent et amical qui répond toujours en français. "
    "Sois naturel, concis et utile. Utilise les émojis avec parcimonie. "
    "Si l'utilisateur veut rechercher des informations sur une personne, "
    "oriente-le vers la commande : .search nom [prénom] [ville]."
)


def utc_now_iso():
    """Renvoie l'horodatage UTC actuel au format ISO (texte triable)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
#  BotConfig — configuration de la commande .search
# --------------------------------------------------------------------------- #
class BotConfig(db.Model):
    """Configuration générale du bot (commande .search / API BrixHub)."""

    __tablename__ = "bot_config"

    id = db.Column(db.Integer, primary_key=True)
    command_enabled = db.Column(db.Boolean, default=True, nullable=False)
    api_key = db.Column(db.String(255), default="", nullable=False)
    max_results = db.Column(db.Integer, default=10, nullable=False)
    flexible_search = db.Column(db.Boolean, default=True, nullable=False)
    auto_response = db.Column(db.Boolean, default=True, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "command_enabled": bool(self.command_enabled),
            "api_key": self.api_key or "",
            "max_results": self.max_results,
            "flexible_search": bool(self.flexible_search),
            "auto_response": bool(self.auto_response),
        }

    @staticmethod
    def get():
        """Renvoie la ligne de configuration unique (id = 1), en la créant au besoin."""
        cfg = db.session.get(BotConfig, 1)
        if cfg is None:
            cfg = BotConfig(id=1)
            db.session.add(cfg)
            db.session.commit()
        return cfg


# --------------------------------------------------------------------------- #
#  AIConfig — configuration de l'IA (GROQ)
# --------------------------------------------------------------------------- #
class AIConfig(db.Model):
    """Configuration de l'IA (clé API GROQ, modèle, prompt, température...)."""

    __tablename__ = "ai_config"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    api_key = db.Column(db.String(255), default="", nullable=False)
    model = db.Column(db.String(100), default="llama-3.3-70b-versatile", nullable=False)
    system_prompt = db.Column(db.Text, default=DEFAULT_SYSTEM_PROMPT, nullable=False)
    temperature = db.Column(db.Float, default=0.7, nullable=False)
    max_tokens = db.Column(db.Integer, default=1024, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "enabled": bool(self.enabled),
            "api_key": self.api_key or "",
            "model": self.model,
            "system_prompt": (self.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    @staticmethod
    def get():
        """Renvoie la ligne de configuration unique (id = 1), en la créant au besoin."""
        cfg = db.session.get(AIConfig, 1)
        if cfg is None:
            cfg = AIConfig(id=1)
            db.session.add(cfg)
            db.session.commit()
        return cfg


# --------------------------------------------------------------------------- #
#  CommandLog — historique des commandes
# --------------------------------------------------------------------------- #
class CommandLog(db.Model):
    """Trace de chaque commande traitée (.search ou réponse IA)."""

    __tablename__ = "command_logs"

    id = db.Column(db.Integer, primary_key=True)
    command = db.Column(db.String(50), nullable=False)      # ".search" | "IA"
    # NB : l'attribut Python s'appelle query_text pour ne pas masquer la
    # propriété SQLAlchemy "query" des modèles ; la colonne SQL reste "query".
    query_text = db.Column("query", db.String(500), default="")  # requête brute reçue
    results_count = db.Column(db.Integer, default=0)        # nombre de résultats
    status = db.Column(db.String(20), default="success")    # "success" | "error"
    error = db.Column(db.Text, default="")                  # message d'erreur éventuel
    response_time = db.Column(db.Float, default=0.0)        # temps de réponse (secondes)
    timestamp = db.Column(db.String(40), default=utc_now_iso)
    is_ai = db.Column(db.Boolean, default=False)            # True si généré par l'IA

    def to_dict(self):
        return {
            "id": self.id,
            "command": self.command,
            "query": self.query_text,
            "results_count": self.results_count,
            "status": self.status,
            "error": self.error,
            "response_time": round(self.response_time or 0.0, 3),
            "timestamp": self.timestamp,
            "is_ai": bool(self.is_ai),
        }


# --------------------------------------------------------------------------- #
#  AILog — historique des conversations IA
# --------------------------------------------------------------------------- #
class AILog(db.Model):
    """Conversation IA complète (message utilisateur + réponse + métadonnées)."""

    __tablename__ = "ai_logs"

    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(100), default="inconnu", nullable=False)
    user_message = db.Column(db.Text, default="", nullable=False)
    ai_response = db.Column(db.Text, default="", nullable=False)
    model = db.Column(db.String(100), default="", nullable=False)
    tokens_used = db.Column(db.Integer, default=0, nullable=False)
    duration_ms = db.Column(db.Integer, default=0, nullable=False)
    timestamp = db.Column(db.String(40), default=utc_now_iso)

    def to_dict(self):
        return {
            "id": self.id,
            "sender": self.sender,
            "user_message": self.user_message,
            "ai_response": self.ai_response,
            "model": self.model,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }


# --------------------------------------------------------------------------- #
#  Initialisation
# --------------------------------------------------------------------------- #
def init_db(app):
    """Initialise la base de données et garantit l'existence des lignes de config."""
    db.init_app(app)
    with app.app_context():
        db.create_all()
        BotConfig.get()
        AIConfig.get()
