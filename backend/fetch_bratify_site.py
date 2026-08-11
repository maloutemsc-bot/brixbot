"""
fetch_bratify_site.py — Copie le site bratify en LOCAL (self-host).

Télécharge l'intégralité du générateur brat (HTML, CSS, JS, police, favicon)
depuis bratify.vercel.app vers backend/bratify_site/. Le backend sert ensuite
ce site localement : le scraping Playwright charge http://localhost/bratify/
au lieu du site distant → rendu 100 % identique, ZÉRO dépendance réseau,
rapide et fiable même si bratify est indisponible.

Licence : le projet est en Unlicense (domaine public) — libre de copier.
Le site est statique (SvelteKit) : tout le rendu se fait côté client.

Utilisation : python fetch_bratify_site.py
"""

import os
import re
import sys
from urllib.parse import urljoin, urlparse

BASE_URL = "https://bratify.vercel.app/"
SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bratify_site")

# Limite de sécurité : le site est petit (~quelques centaines de Ko)
MAX_FILES = 100
MAX_SIZE = 2 * 1024 * 1024  # 2 Mo par fichier


def _is_local(url):
    """Vrai si l'URL pointe vers le site bratify (pas un lien externe)."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return True
    return parsed.netloc in ("bratify.vercel.app", "bratify.vercel.app.")

def _save(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


def _extract_links(content):
    """Extrait toutes les URLs locales (src/href) du contenu HTML/CSS/JS."""
    links = set()
    # src="..." href="..." import("...") url(...)
    for m in re.finditer(r"""(?:src|href)=["']([^"']+)["']""", content):
        links.add(m.group(1))
    for m in re.finditer(r"""import\s*\(\s*["']([^"']+)["']""", content):
        links.add(m.group(1))
    for m in re.finditer(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", content):
        links.add(m.group(1))
    return links


def main():
    try:
        import requests
    except ImportError:
        print("[WARN] requests absent — impossible de télécharger bratify.")
        return 1

    os.makedirs(SITE_DIR, exist_ok=True)
    seen = set()
    queue = [BASE_URL]
    count = 0

    while queue and count < MAX_FILES:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not _is_local(url):
            continue

        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            if len(r.content) > MAX_SIZE:
                continue
        except Exception:
            continue  # un asset manquant n'est pas bloquant

        # Chemin de destination : bratify_site/<chemin de l'URL>
        parsed = urlparse(url)
        rel = parsed.path.lstrip("/")
        if rel.endswith("/") or not rel:
            rel = "index.html"
        if not rel.endswith((".html", ".css", ".js", ".woff", ".png", ".webp", ".svg", ".ico")):
            continue  # on ne copie que les assets utiles au rendu

        dest = os.path.join(SITE_DIR, rel)
        _save(dest, r.content)
        count += 1

        # Cherche d'autres assets dans les fichiers texte
        ctype = r.headers.get("Content-Type", "")
        if "text" in ctype or rel.endswith((".css", ".js", ".html", ".svg")):
            try:
                text = r.content.decode("utf-8", errors="replace")
            except Exception:
                continue
            for link in _extract_links(text):
                # Ignore les ancres, mails, protocoles
                if link.startswith(("#", "mailto:", "data:", "http://", "https://", "//")):
                    continue
                # résout le chemin relatif depuis l'URL courante
                full = urljoin(url, link)
                if _is_local(full) and full not in seen:
                    queue.append(full)

    if not os.path.exists(os.path.join(SITE_DIR, "index.html")):
        print("[WARN] index.html introuvable — bratify n'a pas pu être copié.")
        return 1

    size = sum(
        os.path.getsize(os.path.join(root, f))
        for root, _dirs, files in os.walk(SITE_DIR)
        for f in files
    )
    print(f"[OK] Site bratify copié en local : {count} fichiers, {size // 1024} Ko")
    return 0


if __name__ == "__main__":
    sys.exit(main())
