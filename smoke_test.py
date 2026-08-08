"""
smoke_test.py — Vérification rapide que le backend Flask démarre et répond.

Usage (depuis la racine du projet) :
    python smoke_test.py                     # si Flask est déjà installé
    .venv/Scripts/python smoke_test.py       # venv Windows

Ce script utilise le client de test de Flask : aucun serveur n'est démarré.
Si la variable d'environnement ADMIN_PASSWORD est définie, le flux
d'authentification du panneau est également testé.
"""

import os
import sys

# Force l'UTF-8 sur la console (évite les erreurs d'encodage des émojis
# et accents sur Windows / cp1252)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ajoute le dossier backend au chemin d'importation
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

from app import app  # noqa: E402  (importe l'application Flask complète)

# Clé partagée du bot et mot de passe du panneau (valeurs par défaut)
BOT_API_KEY = os.environ.get("BOT_API_KEY", "changez-moi-bot")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

client = app.test_client()
BOT_HEADERS = {"X-Bot-Key": BOT_API_KEY}


def check(method, url, expected=(200,), **kwargs):
    """Exécute une requête et vérifie le code HTTP attendu."""
    response = client.open(url, method=method, **kwargs)
    body = response.get_json() if response.is_json else response.data[:80]
    status = "OK" if response.status_code in expected else "ÉCHEC"
    print(f"[{status}] {method} {url} -> {response.status_code}")
    assert response.status_code in expected, f"{method} {url} : {response.status_code} attendu dans {expected}"


def main():
    print("=" * 60)
    print("Smoke test BrixBot — API Flask")
    print("=" * 60)

    # ---- Authentification du panneau (si ADMIN_PASSWORD est défini) ----
    if ADMIN_PASSWORD:
        print(f"\n→ Authentification activée (ADMIN_PASSWORD défini)")
        # Accès refusé sans connexion
        check("GET", "/api/dashboard", expected=(401,))
        check("GET", "/admin", expected=(302,))  # redirige vers /login
        # Mot de passe incorrect
        check("POST", "/api/login", json={"password": "mauvais"}, expected=(401,))
        # Connexion réussie
        check("POST", "/api/login", json={"password": ADMIN_PASSWORD})
        print("")
    else:
        print("\n→ Authentification désactivée (ADMIN_PASSWORD non défini)")

    # ---- Pages ----
    check("GET", "/admin")
    check("GET", "/", expected=(302,))  # redirige vers /admin

    # ---- Endpoints du panneau ----
    check("GET", "/api/dashboard")
    check("GET", "/api/config")
    check("POST", "/api/config", json={"max_results": 15, "command_enabled": True})
    check("GET", "/api/ai/config")
    check("GET", "/api/ai/models")
    check("POST", "/api/ai/config", json={"temperature": 0.8, "max_tokens": 1024})
    check("GET", "/api/logs")
    check("POST", "/api/logs/clear")

    # ---- Garde-fou : requête sans clé bot doit être refusée ----
    check("POST", "/api/message", json={"body": "bonjour"}, expected=(401,))

    # ---- Message sans clé BrixHub -> réponse d'erreur propre (200 côté bot) ----
    check("POST", "/api/message", json={"body": ".search Dupont", "from": "33600000000"},
          headers=BOT_HEADERS)

    # ---- Statut WhatsApp (mise à jour + lecture) ----
    check("POST", "/api/whatsapp/status", json={"status": "connected", "number": "33600000000"},
          headers=BOT_HEADERS)
    check("GET", "/api/whatsapp/status")

    # ---- Test search : nom vide -> 400 attendu ----
    check("POST", "/api/test-search", json={"nom_famille": ""}, expected=(400,))

    print("\n✅ Tous les tests du smoke test passent.")


if __name__ == "__main__":
    main()
