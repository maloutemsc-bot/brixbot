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
    check("POST", "/api/ai/config", json={
        "temperature": 0.8,
        "max_tokens": 1024,
        "memory_enabled": True,
        "memory_exchanges": 5,
        "ai_whitelist": "0612345678\n123456789@g.us",
    })
    check("GET", "/api/ai/config")
    check("GET", "/api/logs")
    check("POST", "/api/logs/clear")

    # ---- Graphiques du tableau de bord (stats par jour) ----
    check("GET", "/api/stats/chart")
    check("GET", "/api/stats/chart?days=30")

    # ---- Message vocal (voice=True) avec IA désactivée -> ignoré ----
    check("POST", "/api/message",
          json={"body": "transcription d'un vocal", "from": "33600000000",
                "remoteJid": "33600000000@s.whatsapp.net", "voice": True},
          headers=BOT_HEADERS)

    # ---- Transcription vocale : refusée sans clé bot ----
    check("POST", "/api/ai/transcribe", json={"audio": "AAAA"}, expected=(401,))

    # ---- Garde-fou : requête sans clé bot doit être refusée ----
    check("POST", "/api/message", json={"body": "bonjour"}, expected=(401,))

    # ---- Message sans clé BrixHub -> réponse d'erreur propre (200 côté bot) ----
    check("POST", "/api/message", json={"body": ".search Dupont", "from": "33600000000"},
          headers=BOT_HEADERS)

    # ---- Nouvelles commandes : .tel et .ia ----
    check("POST", "/api/message",
          json={"body": ".tel 06 12 34 56 78", "from": "33600000000"},
          headers=BOT_HEADERS)
    check("POST", "/api/message",
          json={"body": ".ia oui", "from": "33600000000",
                "remoteJid": "33600000000@s.whatsapp.net"},
          headers=BOT_HEADERS)

    # ---- Tests unitaires des helpers (whitelist, téléphone, label) ----
    from whatsapp_handler import _chat_allowed, _parse_phone, build_label  # noqa: E402
    assert _parse_phone("06 12 34 56 78") == "0612345678"
    assert _parse_phone("+33 6 12 34 56 78") == "+33612345678"
    assert build_label("Dupont", "Jean", "Paris") == "Dupont Jean (Paris)"
    # Whitelist vide = tout le monde
    assert _chat_allowed("", "33600000000@s.whatsapp.net", "33600000000@s.whatsapp.net") is True
    # DM : l'expéditeur == la conversation. Numéro simple (chiffres) correspond au jid.
    assert _chat_allowed("0612345678", "33612345678:15@s.whatsapp.net",
                         "33612345678@s.whatsapp.net") is True
    # Jid complet (numéro local normalisé vers 336...)
    assert _chat_allowed("0612345678@s.whatsapp.net", "33612345678@s.whatsapp.net",
                         "33612345678@s.whatsapp.net") is True
    # Numéro non autorisé
    assert _chat_allowed("0612345678", "33999999999@s.whatsapp.net",
                         "33999999999@s.whatsapp.net") is False
    # Dans un groupe : seul le groupe compte (un contact listé ne suffit pas)
    assert _chat_allowed("0612345678", "33612345678@s.whatsapp.net",
                         "999@g.us", is_group=True) is False
    assert _chat_allowed("999@g.us", "33612345678@s.whatsapp.net",
                         "999@g.us", is_group=True) is True
    # Un jid de groupe ne s'applique pas aux messages privés
    assert _chat_allowed("999@g.us", "33612345678@s.whatsapp.net",
                         "33612345678@s.whatsapp.net") is False
    print("[OK] Helpers : _parse_phone / build_label / _chat_allowed")

    # ---- Statut WhatsApp (mise à jour + lecture) ----
    check("POST", "/api/whatsapp/status", json={"status": "connected", "number": "33600000000"},
          headers=BOT_HEADERS)
    check("GET", "/api/whatsapp/status")

    # ---- Test search : nom vide -> 400 attendu ----
    check("POST", "/api/test-search", json={"nom_famille": ""}, expected=(400,))

    print("\n✅ Tous les tests du smoke test passent.")


if __name__ == "__main__":
    main()
