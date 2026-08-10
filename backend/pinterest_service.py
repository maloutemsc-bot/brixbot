"""
pinterest_service.py — Recherche d'images Pinterest (PRIORITÉ de la commande .pin).

Historique : on utilisait la bibliothèque `pinscrape`, mais son installation
échoue sur Termux/Android (pydantic-core est un binaire Rust sans wheel pour
Android). Or pinscrape ne fait que appeler l'API Pinterest
`BaseSearchResource/get/` avec `requests` puis parser le JSON de réponse.

Ce module reproduit exactement ce mécanisme AVEC `requests` SEUL (déjà installé
partout, aucune dépendance lourde) : Pinterest fonctionne donc partout, sans
pinscrape, sans pydantic, sans opencv, sans numpy.

L'API publique utilisée est la même que celle de pinscrape (BaseSearchResource) :
  - warm-up sur la page de recherche (indispensable : initialise la session)
  - POST/GET du resource endpoint avec le payload d'options
  - extraction de `resource_response.data.results[].images.orig.url`

Le service ne lève JAMAIS d'exception : il renvoie [] en cas de problème, et le
bot retombe alors sur DuckDuckGo / Wikimedia.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from urllib.parse import quote, quote_plus

import requests

BASE_URL = "https://in.pinterest.com"
SEARCH_TIMEOUT = 25  # secondes max pour une recherche Pinterest

# En-têtes identiques à ceux de pinscrape : nécessaires pour que Pinterest
# réponde (User-Agent Chrome + en-têtes Sec-Ch-Ua / X-Pinterest-*).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
)
BASE_HEADERS = {
    "Host": BASE_URL.replace("https://", ""),
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Ch-Ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
    "Sec-Ch-Ua-Model": '""',
    "Sec-Ch-Ua-Mobile": "?0",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*, q=0.01",
    "X-Pinterest-Source-Url": "",
    "X-Pinterest-Appstate": "active",
    "Accept-Language": "en-US,en;q=0.9",
    "Screen-Dpr": "1",
    "X-Pinterest-Pws-Handler": "www/search/[scope].js",
    "User-Agent": _USER_AGENT,
    "Sec-Ch-Ua-Platform-Version": '""',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Referer": f"{BASE_URL}/",
    "Priority": "u=1, i",
}

_log = logging.getLogger("pinterest_service")


def available():
    """True : la recherche Pinterest directe ne dépend que de `requests`."""
    return True


def _navigate(data, *keys, default=None):
    """Navigue dans un dict/JSON sans jamais lever (retourne `default` sinon)."""
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _do_search(query, count):
    """Appel réel à l'API Pinterest (lancé dans un thread pour le timeout)."""
    session = requests.Session()
    source_url = f"/search/pins/?q={quote(query)}&rs=typed"

    # 1) Warm-up (critique) : sans cette requête, Pinterest renvoie une erreur.
    try:
        session.get(f"{BASE_URL}{source_url}", headers=BASE_HEADERS, timeout=15)
    except requests.exceptions.RequestException as exc:
        _log.warning("Warm-up Pinterest échoué : %s", exc)
        return []

    # 2) Payload d'options (même structure que pinscrape / l'API web Pinterest).
    payload = {
        "options": {
            "applied_unified_filters": None,
            "appliedProductFilters": "---",
            "article": None,
            "auto_correction_disabled": False,
            "corpus": None,
            "customized_rerank_type": None,
            "domains": None,
            "filters": None,
            "journey_depth": None,
            "page_size": f"{count}",
            "price_max": None,
            "price_min": None,
            "query_pin_sigs": None,
            "query": quote(query),
            "redux_normalize_feed": True,
            "request_params": None,
            "rs": "typed",
            "scope": "pins",
            "selected_one_bar_modules": None,
            "source_id": None,
            "source_module_id": None,
            "seoDrawerEnabled": False,
            "source_url": quote_plus(source_url),
            "top_pin_id": None,
            "top_pin_ids": None,
        },
        "context": {},
    }

    encoded = quote_plus(json.dumps(payload).replace(" ", ""))
    encoded = (
        encoded.replace("%2520", "%20")
        .replace("%252F", "%2F")
        .replace("%253F", "%3F")
        .replace("%252520", "%2520")
        .replace("%253D", "%3D")
        .replace("%2526", "%26")
    )
    url = (
        f"{BASE_URL}/resource/BaseSearchResource/get/"
        f"?source_url={quote_plus(source_url)}&data={encoded}&_={int(time.time() * 1000)}"
    )
    headers = BASE_HEADERS.copy()
    headers["X-Pinterest-Source-Url"] = source_url

    try:
        response = session.get(url, headers=headers, timeout=20)
    except requests.exceptions.RequestException as exc:
        _log.warning("Requête Pinterest échouée : %s", exc)
        return []
    if response.status_code != 200:
        _log.warning("Pinterest a répondu HTTP %s", response.status_code)
        return []

    try:
        data = response.json()
    except ValueError:
        return []

    # 3) Extraction : resource_response.data.results[].images.orig.url
    results = _navigate(data, "resource_response", "data", "results", default=[]) or []
    out = []
    for item in results:
        url = _navigate(item, "images", "orig", "url", default="")
        url = str(url or "").strip()
        if url.startswith("https://") and url not in out:
            out.append(url)
    return out


def search(query, count=10):
    """
    Recherche des URLs d'images Pinterest (API directe, sans dépendance lourde).

    Renvoie une liste d'URLs https (limitée à `count`), ou [] en cas de
    problème. Ne lève JAMAIS d'exception : le bot peut appeler ce service sans
    try/except, et retombera sur DuckDuckGo / Wikimedia si Pinterest échoue.
    """
    if not query or not str(query).strip():
        return []
    count = max(1, min(int(count or 10), 30))
    # Executor géré MANUELLEMENT (pas de `with`) : en cas de timeout on abandonne
    # le thread avec shutdown(wait=False) au lieu de le bloquer. Un Pinterest
    # lent ne doit JAMAIS bloquer le backend.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_do_search, str(query).strip(), max(count, 5))
        urls = future.result(timeout=SEARCH_TIMEOUT)
        return (urls or [])[:count]
    except FutTimeout:
        _log.warning("Recherche Pinterest en timeout (%ss) pour « %s »", SEARCH_TIMEOUT, query)
        return []
    except Exception as exc:  # garde-fou final : jamais d'exception remontée
        _log.warning("Recherche Pinterest impossible pour « %s » : %s", query, exc)
        return []
    finally:
        pool.shutdown(wait=False)
