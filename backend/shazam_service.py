"""
shazam_service.py — Reconnaissance musicale via shazamio (API Shazam).

shazamio est une bibliothèque Python qui inverse l'API mobile de Shazam :
elle calcule l'empreinte audio localement (module natif Rust "shazamio-core")
puis interroge l'API officielle. 100 % gratuit, aucune clé API.

Contrainte : le décodage des formats compressés (ogg/opus des vocaux
WhatsApp) exige ffmpeg. On contourne ça côté bot : le bot Node.js convertit
la note vocale en WAV avec ffmpeg-static (binaire embarqué, aucun ffmpeg
système requis) et nous envoie le WAV. shazamio-core accepte le WAV brut
sans ffmpeg.
"""

import asyncio

try:
    from shazamio import Shazam
    SHAZAMIO_OK = True
except ImportError:
    # shazamio absent (installation incomplète) : la commande affichera un
    # message clair au lieu de crasher. install.sh l'installe.
    SHAZAMIO_OK = False

# Taille maximale d'un audio à analyser (20 Mo : ~3 minutes de WAV 16 kHz mono)
MAX_AUDIO_BYTES = 20 * 1024 * 1024


def available():
    """Vrai si shazamio est installé (la reconnaissance est possible)."""
    return SHAZAMIO_OK


def recognize(audio_bytes):
    """
    Identifie la chanson contenue dans les octets audio (WAV/PCM).

    Retourne un dict :
      - succès : {"ok": True, "title", "artist", "album", "cover_url", "link"}
      - échec  : {"ok": False, "error": "..."}

    Ne lève JAMAIS d'exception : le bot ne doit pas crasher.
    """
    if not SHAZAMIO_OK:
        return {"ok": False, "error": "shazamio n'est pas installé sur le backend."}
    if not audio_bytes:
        return {"ok": False, "error": "Aucun audio reçu."}
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return {"ok": False, "error": "Audio trop volumineux."}

    loop = asyncio.new_event_loop()
    try:
        async def _recognize():
            shazam = Shazam()
            return await shazam.recognize(audio_bytes)

        result = loop.run_until_complete(_recognize())
    except Exception as exc:  # réseau, API, décodage…
        return {"ok": False, "error": f"Reconnaissance impossible : {exc.__class__.__name__}"}
    finally:
        loop.close()

    track = (result or {}).get("track") or {}
    if not track:
        return {"ok": False,
                "error": "Aucune chanson identifiée. Essayez un extrait plus long ou plus net."}

    # Album (extrait de la section "Song" du résultat Shazam)
    album = None
    sections = track.get("sections") or []
    if sections:
        for item in sections[0].get("metadata") or []:
            if str(item.get("title", "")).lower() in ("album", "album/artist"):
                album = item.get("text")
                break

    # Lien : action "uri" de Shazam (Apple Music) ou lien de partage
    link = None
    hub = track.get("hub") or {}
    actions = hub.get("actions") or []
    for action in actions:
        uri = action.get("uri")
        if uri and not str(uri).lower().startswith("applemusicplay"):
            link = uri
            break

    images = track.get("images") or {}
    cover_url = images.get("coverart") or images.get("background") or None

    return {
        "ok": True,
        "title": track.get("title") or "Inconnu",
        "artist": track.get("subtitle") or "Inconnu",
        "album": album,
        "cover_url": cover_url,
        "link": link,
    }
