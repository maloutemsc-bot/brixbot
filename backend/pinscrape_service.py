"""
pinscrape_service.py — Recherche d'images Pinterest via la bibliothèque pinscrape.

Utilisée en PRIORITÉ par la commande .pin du bot WhatsApp : Pinterest donne des
résultats plus variés et plus \"pinterestiens\" que DuckDuckGo / Wikimedia.

pinscrape est OPTIONNEL :
  - s'il n'est pas installé (pip install pinscrape), le service renvoie une
    liste vide et le bot retombe automatiquement sur DuckDuckGo / Wikimedia ;
  - ses dépendances lourdes (opencv-python, numpy) ne sont utilisées que par
    download() — jamais par search(). On les remplace par des stubs minimaux
    quand elles manquent, pour rester léger sur Termux/Android.

Installation recommandée (légère, sans opencv) :
    pip install --no-deps pinscrape beautifulsoup4 pydotmap pydantic requests
"""

import logging
import sys
import types
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

# --------------------------------------------------------------------------- #
#  Stubs cv2 / numpy (uniquement si absents) — suffisent pour search()
# --------------------------------------------------------------------------- #
def _install_light_stubs():
    """Shime cv2 et numpy par des modules minimaux quand ils manquent.

    pinscrape les importe en dur en haut de ses modules, mais ne les utilise
    que dans download() (jamais dans search()) : des stubs vides suffisent
    pour que l'import et search() fonctionnent sans opencv (~350 Mo).
    """
    # numpy : aucune utilisation au niveau module → stub vide
    if "numpy" not in sys.modules:
        try:
            import numpy  # noqa: F401
        except ImportError:
            stub = types.ModuleType("numpy")
            sys.modules["numpy"] = stub

    # cv2 : l'annotation `cv2.Mat` de utils.image_hash est évaluée à l'import,
    # et download() utilise imdecode/imwrite/resize → on fournit des no-op.
    if "cv2" not in sys.modules:
        try:
            import cv2  # noqa: F401
        except ImportError:
            stub = types.ModuleType("cv2")
            stub.Mat = type("Mat", (), {})
            stub.IMREAD_COLOR = 1
            stub.imdecode = lambda *a, **k: None
            stub.imwrite = lambda *a, **k: None
            stub.resize = lambda *a, **k: None
            sys.modules["cv2"] = stub


# NB : ces stubs restent dans sys.modules pour TOUTE la vie du processus Flask.
# C'est volontaire et sans danger : le backend n'importe jamais numpy/cv2
# ailleurs, et si pinscrape devient inutilisable, .pin retombe sur les autres
# sources. (Si un jour le backend avait besoin du VRAI numpy/cv2, il faudrait
# retirer ces stubs et installer les paquets complets.)
_install_light_stubs()

# --------------------------------------------------------------------------- #
#  Import de pinscrape (optionnel)
# --------------------------------------------------------------------------- #
_PINTEREST = None  # classe Pinterest (lazy) ou None si indisponible

try:
    from pinscrape import Pinterest as _PINTEREST_IMPORT
    _PINTEREST = _PINTEREST_IMPORT
    logging.getLogger("pinscrape_service").info(
        "pinscrape disponible : .pin utilisera Pinterest en priorité."
    )
except Exception as exc:  # ImportError ou échec interne : on garde le repli
    _PINTEREST = None
    logging.getLogger("pinscrape_service").warning(
        "pinscrape indisponible (%s) : .pin utilisera DuckDuckGo/Wikimedia.", exc
    )

SEARCH_TIMEOUT = 25  # secondes max pour une recherche Pinterest


def available():
    """True si pinscrape est importable (recherche Pinterest possible)."""
    return _PINTEREST is not None


def _do_search(query, count):
    """Appel réel (lancé dans un thread pour appliquer un timeout)."""
    p = _PINTEREST(sleep_time=0)
    urls = p.search(query, page_size=count)
    # pinscrape renvoie des objets pydantic HttpUrl (pas des str) → conversion.
    out = []
    for u in (urls or []):
        try:
            s = str(u).strip()
        except Exception:
            continue
        if s.startswith("https://") and s not in out:
            out.append(s)
    return out


def search(query, count=10):
    """
    Recherche des URLs d'images Pinterest via pinscrape.

    Renvoie une liste d'URLs https (limitée à `count`), ou [] si pinscrape
    est indisponible / a échoué / n'a rien trouvé. Ne lève jamais d'exception :
    le bot peut appeler ce service sans try/except.
    """
    if not available():
        return []
    if not query or not str(query).strip():
        return []
    count = max(1, min(int(count or 10), 30))
    # Executor géré MANUELLEMENT (pas de `with`) : en cas de timeout on abandonne
    # le thread avec shutdown(wait=False) au lieu de le bloquer. C'est crucial
    # car les requêtes internes de pinscrape n'ont PAS de timeout : sans ça, un
    # Pinterest lent bloquerait le backend indéfiniment.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_do_search, str(query).strip(), max(count, 5))
        urls = future.result(timeout=SEARCH_TIMEOUT)
        return (urls or [])[:count]
    except FutTimeout:
        logging.getLogger("pinscrape_service").warning(
            "Recherche Pinterest en timeout (%ss) pour « %s »", SEARCH_TIMEOUT, query)
        return []
    except Exception as exc:
        logging.getLogger("pinscrape_service").warning(
            "Recherche Pinterest impossible pour « %s » : %s", query, exc)
        return []
    finally:
        # N'attend JAMAIS le thread : s'il est bloqué sur Pinterest, il est
        # abandonné (collecté au prochain GC) — le backend reste réactif.
        pool.shutdown(wait=False)
