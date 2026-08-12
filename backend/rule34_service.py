"""
rule34_service.py — Recherche d'images rule34.xxx (commande CACHÉE .nsfw).

Historique : l'utilisateur voulait une commande .nsfw « qui marche comme .pin »
mais avec du vrai contenu adulte. On a d'abord utilisé le scraper officiel
TUVIMEN/rule34-scraper (https://github.com/TUVIMEN/rule34-scraper, GPLv3)
vendored dans backend/vendor/. MAIS sa dépendance `reliq` est un wrapper C
compilé (libreliq.so) : impossible à installer sur Termux/Android sans chaîne
de compilation (même problème que pinscrape/pydantic). Le scraper ne fait
finalement que du parsing HTML simple → on reproduit EXACTEMENT sa logique
avec `requests` SEUL (déjà installé partout, aucune dépendance lourde),
comme pinterest_service. rule34.xxx fonctionne donc partout, sans reliq,
sans treerequests, sans binaire natif.

Flux utilisé (UNE SEULE requête à rule34.xxx — pas de rate-limit) :
  1. URL de liste : https://rule34.xxx/index.php?page=post&s=list&tags=<tags>
     (les mots de la requête deviennent des tags séparés par des + ; l'utilisateur
     met des _ dans un tag multi-mots, ex: .nsfw cat_girl blue_eyes)
  2. Regex sur la page de liste → les miniatures (dossier + hash). La page en
     contient ~42, largement assez.
  3. Pour chaque miniature, on DEVINE l'URL originale pleine résolution sur le
     CDN wimg.rule34.xxx (même hash que la miniature, extension à tester) via
     une requête HEAD légère. Le CDN wimg n'a PAS de rate-limit et se laisse
     télécharger directement par le bot (les rule34.xxx/samples/ renvoient 403
     hors navigateur, hotlink protection).

Pourquoi pas de requête par post ? rule34.xxx rate-limite agressivement les
requêtes séquentielles (HTTP 429) : visiter 10 pages de posts dépassait le
timeout sur réseau lent. La page de liste suffit.

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

# User-Agent Chrome : requis pour que rule34.xxx réponde (et pour accéder au CDN)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_BASE_HEADERS = {"User-Agent": _USER_AGENT}

_log = logging.getLogger("rule34_service")


def available():
    """True : la recherche rule34 directe ne dépend que de `requests`."""
    return True


def _build_list_url(query):
    """
    Transforme la requête libre en URL de liste rule34.xxx.

    Chaque mot devient un tag, les tags sont séparés par des + :
    « cat_girl blue_eyes » → tags=cat_girl+blue_eyes (l'utilisateur met déjà
    des _ dans un tag multi-mots, les espaces séparent les tags).
    """
    words = [w for w in str(query or "").strip().split() if w]
    if not words:
        return None
    tags = "+".join(urllib.parse.quote(w, safe="") for w in words)
    return f"https://rule34.xxx/index.php?page=post&s=list&tags={tags}"


# Miniature d'un post dans la page de liste :
#   https://rule34.xxx/thumbnails/<dossier>/thumbnail_<hash>.jpg?<id>
#   https://wimg.rule34.xxx/thumbnails/<dossier>/thumbnail_<hash>.jpg?<id>
_THUMB_RE = re.compile(
    r"https?://(?:wimg\.)?rule34\.xxx/thumbnails/(\d+)/thumbnail_([0-9a-f]+)\.\w+"
)

# Extensions possibles de l'original sur le CDN wimg (le hash est le même que
# celui de la miniature, seule l'extension change). Ordre par fréquence.
_ORIGINAL_EXTS = ("jpeg", "png", "jpg", "gif", "webp")

# Taille max d'une image envoyée à WhatsApp. Les originaux rule34 peuvent
# dépasser 10 Mo : sur un réseau mobile, le bot les sauterait (timeout 15 s) et
# l'utilisateur recevrait moins d'images. On préfère les ignorer dès la
# recherche. (WhatsApp limite aussi les médias lourds.)
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Marqueurs de pages de blocage / challenge (filtre opérateur, Cloudflare…) :
# si la page de liste ne contient aucune miniature mais ressemble à un blocage,
# on signale le site comme injoignable au lieu d'un faux « aucun résultat ».
_BLOCK_MARKERS = ("cf-error", "captcha", "access denied", "attention required",
                  "please verify", "challenge", "blocked", "forbidden")


def _looks_blocked(html):
    """True si la page ressemble à un blocage réseau (filtre, challenge…)."""
    lower = (html or "").lower()
    return any(marker in lower for marker in _BLOCK_MARKERS)


def _guess_original(folder, image_hash):
    """
    Devine l'URL originale pleine résolution sur le CDN wimg (même hash que la
    miniature, extension à tester). HEAD léger : on ne télécharge rien. Renvoie
    l'URL wimg (sans paramètre ?id) ou "" si rien ne correspond.
    """
    for ext in _ORIGINAL_EXTS:
        candidate = f"https://wimg.rule34.xxx/images/{folder}/{image_hash}.{ext}"
        try:
            response = requests.head(candidate, headers=_BASE_HEADERS, timeout=8)
        except requests.exceptions.RequestException:
            continue
        if response.status_code != 200:
            continue
        ctype = response.headers.get("content-type") or ""
        # On saute les images trop lourdes pour WhatsApp (voir MAX_IMAGE_BYTES)
        try:
            size = int(response.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            size = 0
        if ctype.startswith("image/") and (size == 0 or size <= MAX_IMAGE_BYTES):
            return candidate
    return ""


def _do_search(query, count):
    """
    Recherche réelle (lancée dans un thread pour le timeout global).
    Renvoie (urls, reachable) — reachable=False si rule34.xxx n'a pas répondu.
    """
    list_url = _build_list_url(query)
    if not list_url:
        return [], False

    # 1) Page de liste → miniatures (une seule requête au site, pas de 429)
    try:
        response = requests.get(list_url, headers=_BASE_HEADERS, timeout=20)
    except requests.exceptions.RequestException as exc:
        _log.warning("Liste rule34 échouée pour « %s » : %s", query, exc)
        return [], False  # site injoignable (réseau bloque les sites adultes ?)
    if response.status_code >= 300:
        _log.warning("Liste rule34 en erreur HTTP %s pour « %s »", response.status_code, query)
        return [], response.status_code < 500  # 403/404 = le site répond mais refuse

    thumbs = _THUMB_RE.findall(response.text)
    if not thumbs:
        # Aucune miniature : soit vraiment aucun résultat, soit une page de
        # blocage (filtre opérateur/parental, Cloudflare…). Dans ce dernier cas
        # on signale le site comme injoignable pour un message clair.
        if _looks_blocked(response.text):
            _log.warning("Page rule34 bloquée pour « %s » (filtre/challenge ?)", query)
            return [], False
        _log.info("Aucun post rule34 pour « %s »", query)
        return [], True
    # Déduplique (une miniature peut apparaître deux fois : domaine + CDN)
    seen = set()
    unique = []
    for folder, image_hash in thumbs:
        key = (folder, image_hash)
        if key not in seen:
            seen.add(key)
            unique.append(key)

    # 2) Devinette des originaux pleine résolution sur le CDN wimg (HEAD, sans
    #    rate-limit). On s'arrête dès qu'on a assez d'URLs.
    out = []
    target = count + 2  # marge pour les images sans original devinable
    needed = min(count + 10, 30)
    for folder, image_hash in unique[:needed]:
        if len(out) >= target:
            break
        url = _guess_original(folder, image_hash)
        if url and url not in out:
            out.append(url)
        time.sleep(0.05)  # légère pause : reste très rapide

    return out, True


def search(query, count=10):
    """
    Recherche d'images rule34.xxx (commande cachée .nsfw).

    Renvoie un dict {"urls": [...], "reachable": bool}. `urls` est une liste
    d'URLs https du CDN wimg (limitée à `count`), ou []. `reachable` indique
    si rule34.xxx a répondu (faux = site injoignable depuis ce réseau : le bot
    pourra l'afficher au lieu d'un simple « aucun résultat »).
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
        _log.warning("Recherche rule34 en timeout (%ss) pour « %s »", SEARCH_TIMEOUT, query)
        return result
    except Exception as exc:  # garde-fou final : jamais d'exception remontée
        _log.warning("Recherche rule34 impossible pour « %s » : %s", query, exc)
        return result
    finally:
        pool.shutdown(wait=False)
