"""
ai_service.py — Service d'appel à l'API GROQ (compatible OpenAI).

Endpoint : POST https://api.groq.com/openai/v1/chat/completions
Auth     : header "Authorization: Bearer gsk_..."
"""

import os
import time

import requests

from database import AIConfig, DEFAULT_SYSTEM_PROMPT

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# Modèle de transcription vocale (Whisper, proposé gratuitement par GROQ)
TRANSCRIBE_MODEL = "whisper-large-v3"
TIMEOUT = 60

# Modèles proposés dans le panneau (identifiants officiels GROQ).
# Les modèles "Mixtral" et "Gemma" sont conservés pour compatibilité mais
# peuvent être dépréciés — en cas de 404, choisissez un modèle actif.
MODELS = [
    {"value": "llama-3.3-70b-versatile", "label": "Llama 3 70B (recommandé)"},
    {"value": "llama-3.1-8b-instant", "label": "Llama 3 8B (rapide)"},
    {"value": "openai/gpt-oss-120b", "label": "OpenAI GPT-OSS 120B"},
    {"value": "openai/gpt-oss-20b", "label": "OpenAI GPT-OSS 20B"},
    {"value": "mixtral-8x7b-32768", "label": "Mixtral 8x7B (déprécié ?)"},
    {"value": "gemma2-9b-it", "label": "Gemma 2 9B (déprécié ?)"},
]

# Prompt système dédié à la commande .correct : corriger SANS réécrire le style,
# en gardant la langue et le ton d'origine. La réponse ne contient QUE le texte
# corrigé (aucun commentaire), pour un rendu propre dans le chat WhatsApp.
CORRECT_SYSTEM_PROMPT = (
    "Tu es un correcteur d'orthographe et de grammaire méticuleux. "
    "Corrige le texte fourni : orthographe, grammaire, conjugaison, ponctuation "
    "et accords. Conserve TOUJOURS la langue et le style d'origine (ne traduis "
    "jamais). Ne réécris pas le texte : corrige-le uniquement, en préservant le "
    "sens et le ton. Réponds avec le texte corrigé SEUL, sans guillemets, sans "
    "commentaire ni explication. Si le texte est déjà correct, renvoie-le tel quel."
)


class AIError(Exception):
    """Erreur métier liée à l'API GROQ (message affichable à l'utilisateur)."""

    def __init__(self, message, status_code=None, is_config=False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.is_config = is_config


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def resolve_api_key():
    """Clé API GROQ : priorité à la config en base, sinon variable d'environnement."""
    cfg = AIConfig.get()
    key = (cfg.api_key or "").strip()
    if not key:
        key = os.environ.get("GROQ_API_KEY", "").strip()
    return key


# --------------------------------------------------------------------------- #
#  Transcription de notes vocales (Whisper via GROQ)
# --------------------------------------------------------------------------- #
def transcribe(audio_bytes, mime="audio/ogg"):
    """
    Transcrit une note vocale (bytes audio) en texte via Whisper (GROQ).

    Renvoie un tuple : (texte, durée_ms).
    Lève AIError en cas de problème (clé manquante, quota, API…).
    """
    api_key = resolve_api_key()
    if not api_key:
        raise AIError(
            "Aucune clé API GROQ configurée. Renseignez-la dans l'onglet IA du panneau.",
            is_config=True,
        )
    if not audio_bytes:
        raise AIError("Note vocale vide : rien à transcrire.")

    # L'extension doit correspondre au format réel de l'audio reçu.
    filename = "audio.ogg"
    if "mp4" in (mime or "") or "mpeg" in (mime or ""):
        filename = "audio.m4a"
    elif "wav" in (mime or ""):
        filename = "audio.wav"
    elif "mp3" in (mime or ""):
        filename = "audio.mp3"

    start = time.perf_counter()
    try:
        response = requests.post(
            GROQ_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, audio_bytes, mime or "audio/ogg")},
            data={
                "model": TRANSCRIBE_MODEL,
                "language": "fr",  # priorité au français (le bot répond en français)
                "temperature": 0.0,
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise AIError("La transcription a mis trop de temps (timeout).")
    except requests.exceptions.ConnectionError:
        raise AIError("Impossible de joindre l'API GROQ (problème réseau).")
    except requests.exceptions.RequestException as exc:
        raise AIError("Erreur de communication avec GROQ : %s" % exc.__class__.__name__)

    duration_ms = int((time.perf_counter() - start) * 1000)

    if response.status_code == 200:
        data = response.json() or {}
        text = (data.get("text") or "").strip()
        if not text:
            raise AIError("La transcription est vide. Réessayez avec un vocal plus clair.")
        return text, duration_ms

    status = response.status_code
    if status == 401:
        raise AIError("Clé API GROQ invalide (HTTP 401). Vérifiez votre clé dans l'onglet IA.")
    if status in (402, 429):
        raise AIError("Quota ou limite de débit GROQ atteint (HTTP %d)." % status)
    if status == 404:
        raise AIError("Le modèle de transcription est indisponible sur GROQ (HTTP 404).")
    if status == 413:
        raise AIError("Note vocale trop longue à transcrire (HTTP 413).")
    raise AIError("Erreur API GROQ lors de la transcription (HTTP %d)." % status)


# --------------------------------------------------------------------------- #
#  Chat
# --------------------------------------------------------------------------- #
def chat(user_message, history=None, system_prompt=None):
    """
    Envoie un message à GROQ, avec un éventuel historique de conversation.

    history : liste optionnelle de dicts {"role": "user"|"assistant", "content": ...}
              injectés avant le message courant (mémoire de conversation).

    system_prompt : prompt système à utiliser à la place de celui configuré
                    dans le panneau (ex: correction de texte avec .correct).

    Renvoie un tuple : (réponse, modèle, tokens_utilisés, durée_ms).
    Lève AIError en cas de problème.
    """
    cfg = AIConfig.get()
    api_key = resolve_api_key()
    if not api_key:
        raise AIError(
            "Aucune clé API GROQ configurée. Renseignez-la dans l'onglet IA du panneau.",
            is_config=True,
        )

    temperature = max(0.0, min(1.0, float(cfg.temperature if cfg.temperature is not None else 0.7)))
    max_tokens = max(1, min(8192, int(cfg.max_tokens or 1024)))
    # Le prompt système par défaut est celui du panneau ; un prompt dédié
    # (ex: correction) peut le remplacer pour un usage précis.
    system_prompt = (system_prompt or "").strip() \
        or (cfg.system_prompt or "").strip() \
        or DEFAULT_SYSTEM_PROMPT

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history[-20:])  # sécurité : on borne l'historique envoyé
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    start = time.perf_counter()
    try:
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise AIError("L'API GROQ a mis trop de temps à répondre (timeout).")
    except requests.exceptions.ConnectionError:
        raise AIError("Impossible de joindre l'API GROQ (problème réseau).")
    except requests.exceptions.RequestException as exc:
        raise AIError("Erreur de communication avec GROQ : %s" % exc.__class__.__name__)

    duration_ms = int((time.perf_counter() - start) * 1000)

    if response.status_code == 200:
        data = response.json() or {}
        reply = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        usage = data.get("usage") or {}
        return reply.strip(), cfg.model, usage.get("total_tokens", 0), duration_ms

    status = response.status_code
    if status == 401:
        raise AIError("Clé API GROQ invalide (HTTP 401). Vérifiez votre clé dans l'onglet IA.")
    if status in (402, 429):
        raise AIError("Quota ou limite de débit GROQ atteint (HTTP %d)." % status)
    if status == 404:
        raise AIError(
            "Le modèle '%s' est introuvable sur GROQ (HTTP 404). "
            "Choisissez un modèle actif dans l'onglet IA." % cfg.model
        )
    raise AIError("Erreur API GROQ (HTTP %d)." % status)


def correct(text):
    """
    Corrige l'orthographe et la grammaire d'un texte via GROQ.

    Utilise un prompt système dédié (CORRECT_SYSTEM_PROMPT) qui demande une
    correction pure, sans réécriture ni commentaire, en conservant la langue
    et le style d'origine.

    Renvoie un tuple : (texte_corrigé, modèle, tokens_utilisés, durée_ms).
    Lève AIError en cas de problème (clé manquante, quota, API…).
    """
    text = (text or "").strip()
    if not text:
        raise AIError("Aucun texte à corriger.")
    if len(text) > 4000:
        raise AIError("Texte trop long à corriger (4000 caractères maximum).")
    return chat(text, system_prompt=CORRECT_SYSTEM_PROMPT)


def test(user_message):
    """Test rapide de l'IA (sans enregistrement de log en base)."""
    reply, model, tokens, duration_ms = chat(user_message)
    return {
        "reply": reply,
        "model": model,
        "tokens_used": tokens,
        "duration_ms": duration_ms,
    }
