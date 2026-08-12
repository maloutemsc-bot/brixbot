"""
rule34_service.py — Recherche d'images rule34.xxx (commande CACHÉE .nsfw).

Historique : l'utilisateur voulait une commande .nsfw « qui marche comme .pin »
mais avec du vrai contenu adulte. On a choisi le scraper officiel
TUVIMEN/rule34-scraper (https://github.com/TUVIMEN/rule34-scraper), GPLv3,
vendored dans backend/vendor/rule34xxx.py (module `rule34xxx`, classe
`rule34xxx`). Ses dépendances (reliq, treerequests) sont 100 % Python — aucun
binaire natif, donc elles s'installent aussi sur Termux/Android.

Flux utilisé (le plus léger possible, sans commentaires) :
  1. URL de liste : https://rule34.xxx/index.php?page=post&s=list&tags=<tags>
     (les mots de la requête deviennent des tags séparés par des + ; l'utilisateur
     met des _ dans un tag multi-mots, ex: .nsfw cat_girl blue_eyes)
  2. get_page() → URLs des posts (une page de 42 posts suffit)
  3. get_post(post_url, comments=False) → dict {image, original, rating, …}
     → on garde `original` (CDN wimg.rule34.xxx) en priorité : les échantillons
       (rule34.xxx/samples/) renvoient 403 hors navigateur (hotlink protection),
       alors que le CDN se laisse télécharger directement par le bot.

Le service ne lève JAMAIS d'exception : il renvoie [] en cas de problème, et
le bot affiche alors une erreur propre. Un timeout global (30 s) est imposé
via un thread : une recherche qui pend ne doit jamais bloquer le backend.
"""

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from pathlib import Path

# Le module vendored (rule34xxx.py) vit dans backend/vendor/
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

SEARCH_TIMEOUT = 30  # secondes max pour une recherche complète

_log = logging.getLogger("rule34_service")


def available():
    """True si le scraper est importable (deps reliq/treerequests présentes)."""
    try:
        import rule34xxx  # noqa: F401
        return True
    except Exception:
        return False


def _build_list_url(query):
    """
    Transforme la requête libre en URL de liste rule34.xxx.

    Chaque mot devient un tag, les tags sont séparés par des + :
    « cat_girl blue_eyes » → tags=cat_girl+blue_eyes (l'utilisateur met déjà
    des _ dans un tag multi-mots, les espaces séparent les tags).
    """
    # Les mots de la requête deviennent des tags rule34 (séparés par des +).
    # L'utilisateur met déjà des _ dans un tag multi-mots (ex: cat_girl) :
    # les espaces séparent les tags (cat_girl blue_eyes → cat_girl+blue_eyes).
    words = [w for w in str(query or "").strip().split() if w]
    if not words:
        return None
    import urllib.parse
    tags = "+".join(urllib.parse.quote(w, safe="") for w in words)
    return f"https://rule34.xxx/index.php?page=post&s=list&tags={tags}"


def _clean_image_url(url):
    """Nettoie une URL image renvoyée par le scraper (// parasites, ?xxx)."""
    url = str(url or "").strip()
    if not url.startswith("http"):
        return ""
    # Le scraper renvoie parfois https://rule34.xxx//samples/... (double slash)
    url = url.replace("https://rule34.xxx//", "https://rule34.xxx/")
    url = url.replace("https://wimg.rule34.xxx//", "https://wimg.rule34.xxx/")
    # Coupe le paramètre ?id (inutile pour le téléchargement)
    url = url.split("?")[0]
    return url


def _do_search(query, count):
    """Recherche réelle (lancée dans un thread pour le timeout global)."""
    try:
        import rule34xxx
    except Exception as exc:
        _log.warning("Scraper rule34 indisponible : %s", exc)
        return []

    list_url = _build_list_url(query)
    if not list_url:
        return []

    try:
        r34 = rule34xxx.rule34xxx()
        page = r34.get_page(list_url, 1)
        posts = page.get("posts") or []
    except Exception as exc:
        _log.warning("Liste rule34 échouée pour « %s » : %s", query, exc)
        return []
    if not posts:
        _log.info("Aucun post rule34 pour « %s »", query)
        return []

    # On récupère les détails des posts (un peu plus que demandé : certains
    # posts n'ont pas d'image sur le CDN wimg). 1 requête par post, sans
    # commentaires, avec une petite pause pour respecter le site. On s'arrête
    # dès qu'on a assez d'URLs pour ne pas faire trainer la commande (sur un
    # réseau mobile lent, chaque requête compte : le timeout global est 30 s).
    out = []
    target = count + 2  # marge pour les posts sans CDN wimg
    needed = min(count + 5, 30)
    for post_url in posts[:needed]:
        if len(out) >= target:
            break
        try:
            data, code = r34.get_post(post_url, comments=False)
        except Exception as exc:
            _log.warning("Post rule34 échoué (%s) : %s", post_url, exc)
            continue
        # Page en erreur (403/500…) : on saute le post plutôt que de garder
        # un dict partiel potentiellement vide.
        if not isinstance(data, dict) or code >= 300:
            continue
        # NE GARDER QUE le CDN wimg.rule34.xxx : c'est le seul hôte qui se
        # laisse télécharger directement par le bot. Les autres
        # (rule34.xxx/samples/, rule34.xxx/images/) renvoient 403 hors
        # navigateur (hotlink protection).
        url = _clean_image_url(data.get("original") or data.get("image") or "")
        if url.startswith("https://wimg.rule34.xxx/") and url not in out:
            out.append(url)
        time.sleep(0.1)  # politesse : évite un blocage par rate-limit

    return out


def search(query, count=10):
    """
    Recherche d'images rule34.xxx (commande cachée .nsfw).

    Renvoie une liste d'URLs https (limité à `count`), ou [] en cas de
    problème. Ne lève JAMAIS d'exception : le bot peut appeler ce service sans
    try/except.
    """
    if not query or not str(query).strip():
        return []
    count = max(1, min(int(count or 10), 30))

    # Executor géré MANUELLEMENT (pas de `with`) : en cas de timeout on
    # abandonne le thread avec shutdown(wait=False) au lieu de le bloquer.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_do_search, str(query).strip(), max(count, 5))
        urls = future.result(timeout=SEARCH_TIMEOUT)
        return (urls or [])[:count]
    except FutTimeout:
        _log.warning("Recherche rule34 en timeout (%ss) pour « %s »", SEARCH_TIMEOUT, query)
        return []
    except Exception as exc:  # garde-fou final : jamais d'exception remontée
        _log.warning("Recherche rule34 impossible pour « %s » : %s", query, exc)
        return []
    finally:
        pool.shutdown(wait=False)
