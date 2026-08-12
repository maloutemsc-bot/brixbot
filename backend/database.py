"""
database.py — Modèles SQLAlchemy et initialisation de la base SQLite.

Tables :
  - bot_config   : configuration générale du bot (.search / BrixHub)
  - ai_config    : configuration de l'IA (GROQ + mémoire + whitelist)
  - command_logs : historique des commandes traitées
  - ai_logs      : historique des conversations IA
  - ai_memory    : mémoire de conversation (contexte par utilisateur)
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

# Prompt par défaut de la commande .ask (question directe à l'IA).
# L'IA répond dans la langue de la question (l'utilisateur peut le modifier
# dans le panneau, onglet IA).
DEFAULT_ASK_PROMPT = (
    "Tu es un assistant WhatsApp intelligent et amical. "
    "Règle importante pour la commande .ask : réponds TOUJOURS dans la langue "
    "de la question posée par l'utilisateur (anglais si la question est en "
    "anglais, espagnol si elle est en espagnol, etc.). Ne réponds en français "
    "que si la question est en français. Sois naturel, concis et utile, "
    "et utilise les émojis avec parcimonie."
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
    """Configuration de l'IA (clé API GROQ, modèle, prompt, mémoire, whitelist…)."""

    __tablename__ = "ai_config"

    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    api_key = db.Column(db.String(255), default="", nullable=False)
    model = db.Column(db.String(100), default="llama-3.3-70b-versatile", nullable=False)
    system_prompt = db.Column(db.Text, default=DEFAULT_SYSTEM_PROMPT, nullable=False)
    temperature = db.Column(db.Float, default=0.7, nullable=False)
    max_tokens = db.Column(db.Integer, default=1024, nullable=False)
    # Mémoire de conversation : garde le contexte par utilisateur
    memory_enabled = db.Column(db.Boolean, default=True, nullable=False)
    memory_exchanges = db.Column(db.Integer, default=5, nullable=False)
    # Whitelist des conversations autorisées (une entrée par ligne).
    # Vide = l'IA répond à tout le monde.
    ai_whitelist = db.Column(db.Text, default="", nullable=False)
    # Blacklist des conversations bannies (une entrée par ligne).
    # Prioritaire : une conversation listée ici ne reçoit JAMAIS de réponse IA.
    ai_blacklist = db.Column(db.Text, default="", nullable=False)
    # Transcription des notes vocales : les vocaux sont transcrits (Whisper
    # via GROQ) puis l'IA répond au texte transcrit.
    transcribe_voice = db.Column(db.Boolean, default=False, nullable=False)
    # Prompt dédié à la commande .ask (modifiable dans le panneau, onglet IA).
    # Vide = l'ancien comportement (prompt système + règle de langue).
    ask_prompt = db.Column(db.Text, default="", nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "enabled": bool(self.enabled),
            "api_key": self.api_key or "",
            "model": self.model,
            "system_prompt": (self.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "memory_enabled": bool(self.memory_enabled),
            "memory_exchanges": self.memory_exchanges,
            "ai_whitelist": self.ai_whitelist or "",
            "ai_blacklist": self.ai_blacklist or "",
            "transcribe_voice": bool(self.transcribe_voice),
            "ask_prompt": (self.ask_prompt or "").strip() or DEFAULT_ASK_PROMPT,
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
    """Trace de chaque commande traitée (.search, .tel ou réponse IA)."""

    __tablename__ = "command_logs"

    id = db.Column(db.Integer, primary_key=True)
    command = db.Column(db.String(50), nullable=False)      # ".search" | ".tel" | "IA"
    # NB : l'attribut Python s'appelle query_text pour ne pas masquer la
    # propriété SQLAlchemy "query" des modèles ; la colonne SQL reste "query".
    query_text = db.Column("query", db.String(500), default="")  # requête brute reçue
    results_count = db.Column(db.Integer, default=0)        # nombre de résultats
    status = db.Column(db.String(20), default="success")    # "success" | "error"
    error = db.Column(db.Text, default="")                  # message d'erreur éventuel
    response_time = db.Column(db.Float, default=0.0)        # temps de réponse (secondes)
    timestamp = db.Column(db.String(40), default=utc_now_iso)
    is_ai = db.Column(db.Boolean, default=False)            # True si généré par l'IA
    chat = db.Column(db.String(100), default="")            # conversation (jid)
    sender = db.Column(db.String(100), default="")          # expéditeur (jid)

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
            "chat": self.chat or "",
            "sender": self.sender or "",
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
#  GalleryItem — médias "vu unique" capturés silencieusement
# --------------------------------------------------------------------------- #
class GalleryItem(db.Model):
    """
    Média capturé : image/vidéo "vu unique" OU média extrait par .extract.

    Pour un vu unique, le bot télécharge à la réception (avant que WhatsApp ne
    le supprime) et enregistre ici, sans rien répondre à l'expéditeur. Pour
    .extract, le bot archive le média qu'il vient de renvoyer. Le fichier vit
    dans backend/gallery/ (gitignoré), la table garde les métadonnées.
    """

    __tablename__ = "gallery_items"

    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(100), default="", nullable=False)   # expéditeur (jid)
    chat = db.Column(db.String(100), default="", nullable=False)     # conversation (jid)
    media_type = db.Column(db.String(10), default="image", nullable=False)  # "image" | "video" | "audio" | "document"
    mime = db.Column(db.String(100), default="image/jpeg", nullable=False)
    filename = db.Column(db.String(200), default="", nullable=False)  # nom sécurisé sur disque
    caption = db.Column(db.String(500), default="", nullable=False)
    size = db.Column(db.Integer, default=0, nullable=False)
    timestamp = db.Column(db.String(40), default=utc_now_iso)
    message_id = db.Column(db.String(80), default="", nullable=False)  # id WhatsApp du message d'origine (dédoublonnage)

    def to_dict(self):
        return {
            "id": self.id,
            "sender": self.sender or "",
            "chat": self.chat or "",
            "media_type": self.media_type,
            "mime": self.mime,
            "filename": self.filename,
            "caption": self.caption or "",
            "message_id": self.message_id or "",
            "size": self.size,
            "timestamp": self.timestamp,
            "url": f"/api/gallery/{self.id}/file",
        }


# --------------------------------------------------------------------------- #
#  AIMemory — mémoire de conversation (contexte par utilisateur)
# --------------------------------------------------------------------------- #
class AIMemory(db.Model):
    """Message mémorisé pour garder le contexte d'une conversation IA."""

    __tablename__ = "ai_memory"

    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(100), default="", nullable=False)
    role = db.Column(db.String(10), default="user", nullable=False)  # "user" | "assistant"
    content = db.Column(db.Text, default="", nullable=False)
    timestamp = db.Column(db.String(40), default=utc_now_iso)


# --------------------------------------------------------------------------- #
#  Initialisation + migration légère
# --------------------------------------------------------------------------- #
def _ensure_columns(app):
    """
    Ajoute les colonnes manquantes aux tables existantes (migration légère SQLite).

    Permet de mettre à jour une base créée par une ancienne version du bot
    sans perdre les données (logs, configuration).
    """
    inspector = db.inspect(db.engine)

    def add_column(table, column_name, definition):
        existing = [column["name"] for column in inspector.get_columns(table)]
        if column_name not in existing:
            with db.engine.begin() as conn:
                conn.execute(db.text(
                    f"ALTER TABLE {table} ADD COLUMN {column_name} {definition}"
                ))
            print(f"[migration] Colonne {table}.{column_name} ajoutée")

    # Nouveautés de la version avec mémoire + whitelist
    for table, columns in {
        "ai_config": [
            (            "memory_enabled", "BOOLEAN DEFAULT 1"),
            ("memory_exchanges", "INTEGER DEFAULT 5"),
            ("ai_whitelist", "TEXT DEFAULT ''"),
            ("ai_blacklist", "TEXT DEFAULT ''"),
            ("transcribe_voice", "BOOLEAN DEFAULT 0"),
            ("ask_prompt", "TEXT DEFAULT ''"),
        ],
        "command_logs": [
            ("chat", "VARCHAR(100) DEFAULT ''"),
            ("sender", "VARCHAR(100) DEFAULT ''"),
        ],
        "gallery_items": [
            ("message_id", "VARCHAR(80) DEFAULT ''"),
        ],
    }.items():
        for column_name, definition in columns:
            add_column(table, column_name, definition)


def init_db(app):
    """Initialise la base de données et garantit l'existence des lignes de config."""
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _ensure_columns(app)
        BotConfig.get()
        AIConfig.get()
