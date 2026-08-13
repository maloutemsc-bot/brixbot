"""
pornpics_service.py — Recherche d'images pornpics.com (commande CACHÉE .xxx).

.xxx est le pendant « vrai » (non-anime) de .nsfw : même fonctionnement, mais
les images viennent de pornpics.com (photos réelles) au lieu de rule34.xxx
(anime). Comme rule34_service, le service n'utilise QUE `requests` (déjà
installé partout, aucune dépendance lourde — fonctionne aussi sur
Termux/Android, où les wrappers C comme reliq/pinscrape/pydantic
s'installent mal).

Flux utilisé (UNE SEULE requête à pornpics.com — rapide, pas de rate-limit) :
  1. URL de recherche : https://www.pornpics.com/search/srch.php?q=<query>
     (les mots de la requête sont séparés par des +)
  2. Regex sur la page → les URLs du CDN cdni.pornpics.com (40 par page :
     miniatures 300 et 460). Chaque URL contient un préfixe de taille :
     https://cdni.pornpics.com/<taille>/<a>/<b>/<galerie>/<fichier>.jpg
  3. On remplace le préfixe par la plus grande taille servie (1280) → image
     pleine résolution, ~170 Ko, parfaite pour WhatsApp. Un HEAD léger vérifie
     que l'URL répond bien en image/* et n'est pas trop lourde.

Pas de protection anti-hotlink sur le CDN cdni (testé : téléchargement direct
en 200), contrairement à realbooru.com (hotlink.php) qui a été écarté.

Le service ne lève JAMAIS d'exception : il renvoie [] en cas de problème, et
le bot affiche alors une erreur propre. Un timeout global (30 s) est imposé
via un thread : une recherche qui pend ne doit jamais bloquer le backend.
"""

import logging
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

import requests

SEARCH_TIMEOUT = 30  # secondes max pour une recherche complète

# User-Agent Chrome : requis pour que pornpics.com réponde (et le CDN)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_BASE_HEADERS = {"User-Agent": _USER_AGENT}

_log = logging.getLogger("pornpics_service")


def available():
    """True : la recherche pornpics ne dépend que de `requests`."""
    return True


def _build_search_url(query):
    """
    Transforme la requête libre en URL de recherche pornpics.com.

    Les mots sont séparés par des + : « big ass » → q=big+ass.
    """
    words = [w for w in str(query or "").strip().split() if w]
    if not words:
        return None
    q = "+".join(urllib.parse.quote(w, safe="") for w in words)
    return f"https://www.pornpics.com/search/srch.php?q={q}"


# URL d'une image sur le CDN de pornpics (miniature ou moyen) :
#   https://cdni.pornpics.com/<taille>/<a>/<b>/<galerie>/<fichier>.jpg
# PAS de groupe capturant : findall doit renvoyer l'URL complète.
_CDN_RE = re.compile(
    r"https://cdni\.pornpics\.com/\d+/[^\"\s]+\.(?:jpg|jpeg|png)[^\"\s]*",
    re.IGNORECASE,
)

# Tailles servies par le CDN, par ordre de préférence. 1280 est la plus grande
# disponible (testé : 300/460/1280 OK, 640/1920 → 404). ~170 Ko, parfait pour
# WhatsApp (limite 15 Mo du bot, 5 Mo côté réseau mobile).
_PREFERRED_SIZES = ("1280", "460", "300")

# Taille max d'une image envoyée à WhatsApp (garde-fou réseau mobile).
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Marqueurs de pages de blocage / challenge (filtre opérateur, Cloudflare…) :
# si la page de recherche ne contient aucune image CDN mais ressemble à un
# blocage, on signale le site comme injoignable au lieu d'un faux « aucun
# résultat ».
_BLOCK_MARKERS = ("cf-error", "captcha", "access denied", "attention required",
                  "please verify", "challenge", "blocked", "forbidden")


def _looks_blocked(html):
    """True si la page ressemble à un blocage réseau (filtre, challenge…)."""
    lower = (html or "").lower()
    return any(marker in lower for marker in _BLOCK_MARKERS)


def _upgrade_size(url):
    """
    Remplace le préfixe de taille d'une URL CDN par la plus grande servie.
    Renvoie l'URL 1280 (ou 460/300 si 1280 ne répond pas).
    """
    for size in _PREFERRED_SIZES:
        candidate = re.sub(r"/(\d+)/", f"/{size}/", url, count=1)
        try:
            response = requests.head(candidate, headers=_BASE_HEADERS, timeout=8)
        except requests.exceptions.RequestException:
            continue
        if response.status_code != 200:
            continue
        ctype = response.headers.get("content-type") or ""
        try:
            content_length = int(response.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if ctype.startswith("image/") and (
            content_length == 0 or content_length <= MAX_IMAGE_BYTES
        ):
            return candidate
    return ""


def _do_search(query, count):
    """
    Recherche réelle (lancée dans un thread pour le timeout global).
    Renvoie (urls, reachable) — reachable=False si pornpics.com n'a pas répondu.
    """
    search_url = _build_search_url(query)
    if not search_url:
        return [], False

    # 1) Page de recherche → URLs CDN (une seule requête au site)
    try:
        response = requests.get(search_url, headers=_BASE_HEADERS, timeout=20)
    except requests.exceptions.RequestException as exc:
        _log.warning("Recherche pornpics échouée pour « %s » : %s", query, exc)
        return [], False  # site injoignable (réseau bloque les sites adultes ?)
    if response.status_code >= 300:
        _log.warning("Recherche pornpics en erreur HTTP %s pour « %s »",
                     response.status_code, query)
        return [], response.status_code < 500  # 403/404 = le site répond mais refuse

    urls = _CDN_RE.findall(response.text)
    if not urls:
        # Aucune image : soit vraiment aucun résultat, soit une page de blocage
        # (filtre opérateur/parental, Cloudflare…). Dans ce dernier cas on
        # signale le site comme injoignable pour un message clair.
        if _looks_blocked(response.text):
            _log.warning("Page pornpics bloquée pour « %s » (filtre/challenge ?)", query)
            return [], False
        _log.info("Aucun résultat pornpics pour « %s »", query)
        return [], True

    # 2) Mise à niveau vers la pleine résolution (1280) + déduplication.
    #    On s'arrête dès qu'on a assez d'URLs.
    out = []
    seen = set()
    target = count + 2  # marge pour les liens morts / non-images
    needed = min(count * 3, 40)  # la page fournit ~40 URLs (300 + 460)
    for url in urls[:needed]:
        if len(out) >= target:
            break
        if url in seen:
            continue
        seen.add(url)
        full = _upgrade_size(url)
        if full and full not in out:
            out.append(full)
        time.sleep(0.03)  # légère pause : reste très rapide

    return out, True


def search(query, count=10):
    """
    Recherche d'images pornpics.com (commande cachée .xxx).

    Renvoie un dict {"urls": [...], "reachable": bool}. `urls` est une liste
    d'URLs https du CDN cdni.pornpics.com (pleine résolution 1280, limitée à
    `count`), ou []. `reachable` indique si pornpics.com a répondu (faux =
    site injoignable depuis ce réseau : le bot pourra l'afficher au lieu d'un
    simple « aucun résultat »).
    """
    result = {"urls": [], "reachable": False}
    if not query or not str(query).strip():
        return result
    count = max(1, min(int(count or 10), 30))

    # Executor géré MANUELLEMENT (pas de `with`) : en cas de timeout on
    # abandonne le thread avec shutdown(wait=False) au lieu de le bloquer.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_do_search, str(query).strip(), max(count, 5))
        urls, reachable = future.result(timeout=SEARCH_TIMEOUT)
        result["urls"] = (urls or [])[:count]
        result["reachable"] = bool(reachable)
        return result
    except FutTimeout:
        _log.warning("Recherche pornpics en timeout (%ss) pour « %s »", SEARCH_TIMEOUT, query)
        return result
    except Exception as exc:  # garde-fou final : jamais d'exception remontée
        _log.warning("Recherche pornpics impossible pour « %s » : %s", query, exc)
        return result
    finally:
        pool.shutdown(wait=False)
