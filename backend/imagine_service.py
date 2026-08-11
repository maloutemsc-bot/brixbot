"""
imagine_service.py — Génération d'images par IA (Pollinations).

Pollinations est un service gratuit de génération d'images par IA, SANS clé
API : https://image.pollinations.ai/prompt/{description}?width=&height=

Utilisé par la commande .imagine du bot. Le backend télécharge l'image et la
renvoie au bot (les appels externes restent centralisés dans le backend).
"""

import requests
import urllib.parse

# Endpoint de génération (prompt dans le chemin, paramètres en query)
BASE_URL = "https://image.pollinations.ai/prompt/{prompt}"

# Temps maximal de génération (les modèles peuvent être lents à chauffer)
TIMEOUT_S = 120
# Taille maximale de l'image téléchargée (garde-fou)
MAX_BYTES = 15 * 1024 * 1024


def generate(prompt, width=512, height=512):
    """
    Génère une image à partir d'une description.

    Retourne les octets JPEG (Buffer) ou None en cas d'échec.
    """
    url = BASE_URL.format(prompt=urllib.parse.quote(prompt))
    params = {
        "width": width,
        "height": height,
        "nologo": "true",
        "model": "flux",  # modèle par défaut : bon équilibre qualité/vitesse
    }
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT_S, stream=True)
        response.raise_for_status()
        chunks = []
        total = 0
        for chunk in response.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                return None
        data = b"".join(chunks)
        return data if data else None
    except requests.exceptions.RequestException:
        return None
