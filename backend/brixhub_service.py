"""
brixhub_service.py — Service d'appel à l'API BrixHub.

Base URL  : https://api.brixhub.is/api/v1
Auth      : header "X-API-Key: brix_votre_cle"
Endpoints : POST /search , GET /me
"""

import os

import requests

from database import BotConfig

BASE_URL = "https://api.brixhub.is/api/v1"
TIMEOUT = 30

# Quota restant BrixHub, mis à jour à chaque réponse contenant l'en-tête
# "X-RateLimit-Remaining-Day" (stocké en mémoire pour le tableau de bord).
_remaining_quota = None


class BrixHubError(Exception):
    """Erreur métier liée à l'API BrixHub (message affichable à l'utilisateur)."""

    def __init__(self, message, status_code=None, is_config=False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        # True si le problème vient d'une configuration manquante (clé API...)
        self.is_config = is_config


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def resolve_api_key():
    """Clé API BrixHub : priorité à la config en base, sinon variable d'environnement."""
    cfg = BotConfig.get()
    key = (cfg.api_key or "").strip()
    if not key:
        key = os.environ.get("BRIX_API_KEY", "").strip()
    return key


def _headers():
    return {
        "X-API-Key": resolve_api_key(),
        "Content-Type": "application/json",
        "User-Agent": "whatsapp-brixhub-bot/1.0",
    }


def _update_quota(response):
    """Lit l'en-tête de quota restant et le mémorise."""
    global _remaining_quota
    value = response.headers.get("X-RateLimit-Remaining-Day")
    if value is not None:
        try:
            _remaining_quota = int(value)
        except ValueError:
            pass


def get_remaining_quota():
    """Renvoie le dernier quota restant connu (ou None)."""
    return _remaining_quota


def _map_error(status_code, body=""):
    """Traduit un code HTTP BrixHub en message français compréhensible."""
    if status_code in (401, 403):
        return "Clé API BrixHub invalide ou refusée (HTTP 401/403). Vérifiez votre clé."
    if status_code == 402:
        return "Quota BrixHub épuisé (HTTP 402). Pensez à recharger votre compte."
    if status_code == 404:
        return "Ressource BrixHub introuvable (HTTP 404)."
    if status_code == 429:
        return "Limite de requêtes BrixHub atteinte (HTTP 429). Réessayez plus tard."
    if status_code >= 500:
        return "Le service BrixHub rencontre un problème (HTTP %d). Réessayez plus tard." % status_code
    snippet = (body or "")[:120].strip()
    if snippet:
        return "Erreur BrixHub (HTTP %d) : %s" % (status_code, snippet)
    return "Erreur BrixHub inattendue (HTTP %d)." % status_code


# --------------------------------------------------------------------------- #
#  Recherche
# --------------------------------------------------------------------------- #
def _build_payloads(nom_famille, prenom, ville, telephone, flexible, max_results):
    """
    Construit la liste des requêtes à essayer.

    Si la recherche flexible est activée, on commence par tous les critères
    puis on retire progressivement ville puis prénom pour maximiser les chances
    de trouver un résultat. Une recherche par téléphone seul n'a pas de repli.
    """
    base = {"per_page": max_results, "flexible": True}
    if nom_famille:
        base["nom_famille"] = nom_famille
    if prenom:
        base["prenom"] = prenom
    if ville:
        base["ville"] = ville
    if telephone:
        base["telephone"] = telephone

    if not flexible:
        return [base]

    # Recherche par numéro uniquement : pas de critère à retirer
    if telephone and not nom_famille:
        return [base]

    variants = [base]

    # nom + prénom (sans ville)
    variants.append({key: value for key, value in base.items() if key != "ville"})
    # nom + ville (sans prénom)
    variants.append({key: value for key, value in base.items() if key != "prenom"})
    # nom seul
    variants.append({key: value for key, value in base.items() if key not in ("prenom", "ville")})

    # Déduplication : évite les appels API redondants quand prénom/ville manquent
    seen = set()
    unique = []
    for variant in variants:
        key = tuple(sorted(variant.items()))
        if key not in seen:
            seen.add(key)
            unique.append(variant)
    return unique


def _label(payload):
    """Libellé humain de la requête envoyée (ex: "Dupont Jean (Paris)" ou le numéro)."""
    if payload.get("telephone") and not payload.get("nom_famille"):
        return str(payload["telephone"])
    parts = [payload.get("nom_famille", "")]
    if payload.get("prenom"):
        parts.append(payload["prenom"])
    label = " ".join(parts)
    if payload.get("ville"):
        label += " (%s)" % payload["ville"]
    return label or "recherche"


def search(nom_famille=None, prenom=None, ville=None, telephone=None, flexible=None, max_results=None):
    """
    Recherche une personne sur BrixHub (par nom, prénom, ville et/ou téléphone).

    Renvoie un dict : {results, meta, remaining_quota, used_payload, query_label}
    Lève BrixHubError en cas de problème.
    """
    cfg = BotConfig.get()
    if flexible is None:
        flexible = bool(cfg.flexible_search)
    if max_results is None:
        max_results = cfg.max_results or 10
    max_results = max(1, min(100, int(max_results)))

    api_key = resolve_api_key()
    if not api_key:
        raise BrixHubError(
            "Aucune clé API BrixHub configurée. "
            "Renseignez-la dans l'onglet Configuration du panneau.",
            is_config=True,
        )

    payloads = _build_payloads(nom_famille, prenom, ville, telephone, flexible, max_results)
    last_error = None

    for payload in payloads:
        try:
            response = requests.post(
                f"{BASE_URL}/search", json=payload, headers=_headers(), timeout=TIMEOUT
            )
            _update_quota(response)

            if response.status_code == 200:
                data = response.json() or {}
                results = (data.get("data") or {}).get("results") or []
                if results:
                    return {
                        "results": results,
                        "meta": data.get("meta") or {},
                        "remaining_quota": _remaining_quota,
                        "used_payload": payload,
                        "query_label": _label(payload),
                    }
                # Aucun résultat : on poursuit avec le payload suivant (flexible)
                last_error = None
            else:
                last_error = _map_error(response.status_code, response.text)
                # Une erreur d'auth ou de quota ne sert à rien de réessayer
                if response.status_code in (401, 402, 403, 429):
                    raise BrixHubError(last_error, response.status_code)
                break  # Erreur serveur : inutile d'enchaîner les requêtes
        except requests.exceptions.Timeout:
            last_error = "Le service BrixHub a mis trop de temps à répondre."
        except requests.exceptions.ConnectionError:
            last_error = "Impossible de joindre le service BrixHub (problème réseau)."
        except requests.exceptions.RequestException as exc:
            last_error = "Erreur de communication avec BrixHub : %s" % exc.__class__.__name__

    if last_error:
        raise BrixHubError(last_error)

    # Tous les payloads essayés sans résultat
    return {
        "results": [],
        "meta": {"total": 0},
        "remaining_quota": _remaining_quota,
        "used_payload": payloads[-1],
        "query_label": _label(payloads[-1]),
    }


# --------------------------------------------------------------------------- #
#  Statistiques d'utilisation
# --------------------------------------------------------------------------- #
def get_me():
    """Récupère les statistiques d'utilisation du compte BrixHub (GET /me)."""
    response = requests.get(f"{BASE_URL}/me", headers=_headers(), timeout=TIMEOUT)
    _update_quota(response)
    if response.status_code == 200:
        return response.json()
    raise BrixHubError(_map_error(response.status_code, response.text), response.status_code)
