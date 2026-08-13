"""
reverse_service.py — Recherche d'images inversée (commande .rev).

Pipeline 100 % gratuit, sans clé API, avec `requests` SEUL (compatible
Termux/Android — la leçon apprise avec reliq/pinscrape) :

  1. L'image est uploadée anonymement sur catbox.moe (hébergement temporaire
     sans compte ni clé) → on obtient une URL publique.
  2. Cette URL est envoyée à Yandex Images (rpt=imageview) : le HTML contient
     les résultats "images similaires" (cbirSimilar) sous forme de JSON
     échappé. Chaque résultat contient :
       - imageUrl : miniature Yandex de l'image TROUVÉE (unique par résultat)
       - title    : titre/source du résultat
       - linkUrl  : lien interne Yandex, avec le paramètre `img_url` = URL de
         l'image trouvée sur le site SOURCE (celle qu'on envoie à l'utilisateur)
     ⚠️ Le paramètre `url=` de linkUrl est l'ID de recherche du CLUSTER
     (identique pour tous les résultats) — c'était le bug de la v1 : le bot
     renvoyait l'image cherchée au lieu des images trouvées !
  3. En parallèle, on renvoie les liens de recherche complets (Yandex,
     Google Lens) pour que l'utilisateur puisse creuser dans un navigateur.

Limites connues (testées) :
  - SauceNAO : l'API refuse les comptes anonymes ("The anonymous account type
    does not permit API usage"), et search.php HTML répond 403.
  - ascii2d.net : derrière Cloudflare (403 avec requests seul).
  - Bing : searchbyimage renvoie 404.
  - L'astuce `-images-thumbs` → `-images` pour obtenir la pleine résolution
    sur le CDN Yandex renvoie 404 ; on utilise donc l'URL source (img_url),
    qui se télécharge sans problème (testé 5/5), avec la miniature Yandex en
    solution de repli côté bot si le site source bloque.
  Le service ne dépend donc que de Yandex + catbox, qui fonctionnent.

Le service ne lève JAMAIS d'exception : il renvoie un dict avec `ok: False`
et un `error` lisible en cas de problème (le bot affiche un message propre).
"""

import html as html_mod
import json
import re
from urllib.parse import unquote

import requests

# --------------------------------------------------------------------------- #
#  Constantes
# --------------------------------------------------------------------------- #

CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
YANDEX_URL = "https://yandex.com/images/search"

# Timeouts généreux (réseau mobile lent, cf. tests : ~2-4 s en desktop).
UPLOAD_TIMEOUT = 40
YANDEX_TIMEOUT = 40
HEAD_TIMEOUT = 15

# Garde-fou : l'image envoyée au backend ne doit pas dépasser ~20 Mo (base64
# ~27 Mo) — même limite que les autres services média.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


# --------------------------------------------------------------------------- #
#  Catbox (upload anonyme)
# --------------------------------------------------------------------------- #

def upload_to_catbox(image_bytes, filename="image.jpg"):
    """
    Upload anonyme sur catbox.moe. Renvoie l'URL publique, ou "" si échec.

    Note : catbox renvoie parfois un HTTP 500 tout en publiant quand même le
    fichier (l'URL est dans le corps de réponse) — on traite ce cas comme un
    succès, puis on vérifie que le fichier est bien téléchargeable.
    """
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        return ""
    try:
        resp = requests.post(
            CATBOX_UPLOAD_URL,
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (filename, image_bytes, "image/jpeg")},
            headers=_UA,
            timeout=UPLOAD_TIMEOUT,
        )
        url = (resp.text or "").strip()
        if not url.startswith("http") or "files.catbox.moe" not in url:
            return ""
        # Vérifie que le fichier est réellement accessible (catbox 500 mais fichier
        # publié : la vérification évite de garder une URL morte).
        try:
            check = requests.get(url, headers=_UA, timeout=HEAD_TIMEOUT)
            if check.status_code != 200:
                return ""
        except requests.RequestException:
            return ""
        return url
    except requests.RequestException:
        return ""


# --------------------------------------------------------------------------- #
#  Yandex — recherche d'images similaires (cbir)
# --------------------------------------------------------------------------- #

# Chaque résultat est un objet JSON PLAT (aucune accolade imbriquée) dans le
# tableau "thumbs". On le capture avec \{.*?\} puis on le parse avec
# json.loads — beaucoup plus robuste qu'une méga-regex de regroupement.
_RESULT_OBJ_RE = re.compile(r"\{.*?\}", re.S)


def _extract_results(html_text, limit):
    """
    Extrait les résultats "images similaires" (cbirSimilar.thumbs) du HTML
    Yandex dé-échappé. Renvoie une liste de dicts {title, url, thumb} :
      - url   : image TROUVÉE sur le site source (param img_url de linkUrl)
      - thumb : miniature Yandex de l'image trouvée (repli si source bloquée)
      - title : titre/source du résultat
    """
    start = html_text.find("cbirSimilar")
    if start == -1:
        return []
    zone = html_text[start:start + 500000]
    results = []
    for match in _RESULT_OBJ_RE.finditer(zone):
        try:
            obj = json.loads(match.group(0))
        except (ValueError, TypeError):
            continue
        if "imageUrl" not in obj or "linkUrl" not in obj:
            continue
        thumb = obj.get("imageUrl", "")
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        # URL de l'image trouvée sur le site source (param img_url de linkUrl)
        src = ""
        m = re.search(r"[?&]img_url=([^&]+)", obj.get("linkUrl", ""))
        if m:
            src = unquote(m.group(1))
        if not src:
            continue
        results.append({
            "title": html_mod.unescape(str(obj.get("title", "")).strip()),
            "url": src,
            "thumb": thumb,
        })
        if len(results) >= limit:
            break
    return results


def search_yandex(image_url, limit=5):
    """
    Recherche inversée Yandex pour une URL d'image publique.
    Renvoie (results, reachable) :
      - results : liste de dicts {title, url, thumb} (max `limit`)
      - reachable : False si yandex.com n'a pas répondu (réseau bloqué…)
    Ne lève jamais d'exception.
    """
    try:
        resp = requests.get(
            YANDEX_URL,
            params={"rpt": "imageview", "url": image_url},
            headers=_UA,
            timeout=YANDEX_TIMEOUT,
        )
    except requests.RequestException:
        return [], False
    if resp.status_code != 200:
        return [], True
    text = html_mod.unescape(resp.text)
    return _extract_results(text, limit), True


# --------------------------------------------------------------------------- #
#  Point d'entrée principal
# --------------------------------------------------------------------------- #

def reverse_search(image_bytes, filename="image.jpg", limit=5):
    """
    Recherche d'images inversée complète : upload catbox → Yandex.

    Renvoie un dict :
      {
        "ok": True,
        "catbox_url": "https://files.catbox.moe/...",
        "total": 40,                      # résultats trouvés par Yandex
        "results": [ {"title", "url", "thumb"}, ... ],  # jusqu'à `limit`
        "links": { "yandex": "...", "lens": "..." },    # liens complets
      }
    ou {"ok": False, "error": "..."} en cas de problème. Ne lève jamais.
    """
    try:
        limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        limit = 5

    # 1) Upload
    catbox_url = upload_to_catbox(image_bytes, filename)
    if not catbox_url:
        return {"ok": False, "error": "Impossible d'héberger l'image (catbox.moe). Réessayez plus tard."}

    # 2) Recherche Yandex
    results, reachable = search_yandex(catbox_url, limit)
    if not reachable:
        return {
            "ok": False,
            "error": "Yandex est injoignable depuis ce réseau (peut être bloqué par l'opérateur/Wi-Fi). Essaie avec un VPN.",
        }
    if not results:
        return {
            "ok": True,
            "catbox_url": catbox_url,
            "total": 0,
            "results": [],
            "links": _search_links(catbox_url),
        }

    # 3) Liens de recherche complets (pour creuser dans un navigateur)
    return {
        "ok": True,
        "catbox_url": catbox_url,
        "total": len(results),
        "results": results,
        "links": _search_links(catbox_url),
    }


def _search_links(catbox_url):
    """Liens de recherche inversée vers les moteurs (ouvrables dans un navigateur)."""
    from urllib.parse import quote
    return {
        "yandex": f"{YANDEX_URL}?rpt=imageview&url={quote(catbox_url, safe='')}",
        "lens": f"https://lens.google.com/uploadbyurl?url={quote(catbox_url, safe='')}",
    }


def available():
    """Toujours True : le service ne dépend d'aucune dépendance lourde."""
    return True
