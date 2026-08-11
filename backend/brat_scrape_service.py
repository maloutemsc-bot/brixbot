"""
brat_scrape_service.py — Scraping du générateur brat officiel (bratify).

Charge la VRAIE page https://bratify.vercel.app/ dans un navigateur headless
(Playwright + navigateur système : Chrome, Edge ou Chromium), sélectionne le
preset "custom color" (fond blanc / texte noir), tape le texte demandé dans
le contenteditable et clique sur le bouton "download" du site. Le PNG produit
par le site (avec son post-traitement authentique : flou + double
redimensionnement pixelisé nearestNeighbor) est récupéré puis converti en
WebP pour WhatsApp.

Fiabilité :
  - Si playwright ou un navigateur est absent → available() = False
  - render() ne lève JAMAIS : retourne None en cas d'échec (le backend retombe
    alors sur la génération locale brat_service.py).
  - Timeout global : un .brat ne doit jamais bloquer le bot.
"""

import asyncio
import io
import os
import threading

# Playwright est optionnel : si absent, le service est simplement indisponible
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

# Chemins connus des navigateurs système (Windows, Linux, Termux/Android)
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/data/data/com.termux/files/usr/bin/chromium",
    "/data/data/com.termux/files/usr/bin/chromium-browser",
]
_EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
]

# Verrou global : Playwright crée une boucle d'événements par appel, on
# sérialise pour éviter tout conflit entre requêtes simultanées.
_lock = threading.Lock()


# Temps max pour un rendu complet (chargement + export du site). La page doit
# répondre dans ce délai, sinon on abandonne (le bot timeout à 60 s au-delà).
RENDER_TIMEOUT_S = 45


def _find_browser():
    """Retourne le premier navigateur existant (chemin) ou None."""
    for p in _CHROME_PATHS + _EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def available():
    """Vrai si playwright ET un navigateur sont présents."""
    return PLAYWRIGHT_OK and _find_browser() is not None


async def _render_async(text, browser_path):
    """Scrape bratify et renvoie le PNG brut (bytes) du site."""
    async with async_playwright() as p:
        # executable_path : on utilise le navigateur système, aucun
        # téléchargement Chromium requis (playwright install jamais nécessaire)
        browser = await p.chromium.launch(
            executable_path=browser_path,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            page = await browser.new_page(
                viewport={"width": 800, "height": 1000},
                accept_downloads=True,
            )
            await page.goto(
                "https://bratify.vercel.app/",
                timeout=20000,
                wait_until="domcontentloaded",
            )
            # Attend que les contrôles (select preset, bouton download) existent
            await page.wait_for_selector("#preset", timeout=10000)
            # Preset "custom color" → fond blanc + texte noir (le style demandé)
            await page.select_option("#preset", "custom")
            await page.fill("#background", "#ffffff")
            await page.fill("#foreground", "#000000")
            # Texte demandé dans le contenteditable
            editable = page.locator('div[contenteditable="true"]')
            await editable.fill(text)
            await page.wait_for_timeout(400)  # laisse la police web se charger
            # Clic sur "download" : le site exporte le PNG authentique
            btn = page.get_by_role("button", name="download", exact=True)
            await btn.scroll_into_view_if_needed()
            async with page.expect_download(timeout=40000) as dl_info:
                await btn.click(force=True)
            dl = await dl_info.value
            path = await dl.path()
            with open(path, "rb") as fh:
                data = fh.read()
            # Vérifie que c'est bien une image PNG
            if not data[:8] == b"\x89PNG\r\n\x1a\n":
                return None
            return data
        finally:
            await browser.close()


def render(text):
    """
    Scrape bratify et renvoie un sticker WebP 512×512 (bytes), ou None.
    Ne lève jamais.
    """
    if not PLAYWRIGHT_OK or not text:
        return None
    browser_path = _find_browser()
    if not browser_path:
        return None

    data = None
    with _lock:
        try:
            # Boucle fraîche à chaque appel (Flask est synchrone)
            loop = asyncio.new_event_loop()
            try:
                data = loop.run_until_complete(
                    asyncio.wait_for(
                        _render_async(text, browser_path),
                        timeout=RENDER_TIMEOUT_S,
                    )
                )
            finally:
                loop.close()
        except Exception:
            return None

    if not data:
        return None

    # Conversion PNG (384×384, exactement le rendu du site) → WebP
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        out = io.BytesIO()
        img.convert("RGB").save(out, "WEBP", quality=92)
        return out.getvalue()
    except Exception:
        return None
