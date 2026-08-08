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
#  Chat
# --------------------------------------------------------------------------- #
def chat(user_message):
    """
    Envoie un message à GROQ.

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
    system_prompt = (cfg.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT

    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
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


def test(user_message):
    """Test rapide de l'IA (sans enregistrement de log en base)."""
    reply, model, tokens, duration_ms = chat(user_message)
    return {
        "reply": reply,
        "model": model,
        "tokens_used": tokens,
        "duration_ms": duration_ms,
    }
