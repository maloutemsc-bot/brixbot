"""
tools_service.py — Outils pratiques 100 % gratuits, sans clé API.

APIs utilisées (aucune inscription requise) :
  - Open-Meteo   : https://open-meteo.com   → géocodage + prévisions météo
  - Google (gtx) : endpoint public de traduction (détection automatique)
  - Frankfurter  : https://frankfurter.app  → taux de change de la BCE

Toutes les erreurs sont levées sous forme de ToolsError avec un message
français directement affichable dans WhatsApp.
"""

import re

import requests

TIMEOUT = 12

# Description des codes météo de l'API Open-Meteo (codes WMO).
WEATHER_CODES = {
    0: "☀️ Ciel dégagé",
    1: "🌤️ Plutôt dégagé",
    2: "⛅ Partiellement nuageux",
    3: "☁️ Couvert",
    45: "🌫️ Brouillard",
    48: "🌫️ Brouillard givrant",
    51: "🌦️ Bruine légère",
    53: "🌦️ Bruine",
    55: "🌧️ Bruine dense",
    56: "🌧️ Bruine verglaçante",
    57: "🌧️ Bruine verglaçante dense",
    61: "🌧️ Pluie faible",
    63: "🌧️ Pluie modérée",
    65: "🌧️ Pluie forte",
    66: "🌧️ Pluie verglaçante",
    67: "🌧️ Pluie verglaçante forte",
    71: "🌨️ Neige faible",
    73: "🌨️ Neige modérée",
    75: "❄️ Neige forte",
    77: "❄️ Grains de neige",
    80: "🌦️ Averses faibles",
    81: "🌧️ Averses modérées",
    82: "⛈️ Averses violentes",
    85: "🌨️ Averses de neige",
    86: "❄️ Averses de neige fortes",
    95: "⛈️ Orage",
    96: "⛈️ Orage avec grêle",
    99: "⛈️ Orage avec grêle violente",
}


class ToolsError(Exception):
    """Erreur métier liée à un outil (message affichable à l'utilisateur)."""

    def __init__(self, message, is_config=False):
        super().__init__(message)
        self.message = message
        self.is_config = is_config


def _weather_label(code):
    """Traduit un code météo WMO en libellé français avec émoji."""
    try:
        return WEATHER_CODES.get(int(code), "🌡️ Temps variable")
    except (TypeError, ValueError):
        return "🌡️ Temps variable"


# --------------------------------------------------------------------------- #
#  Météo (Open-Meteo — sans clé)
# --------------------------------------------------------------------------- #
def weather(city):
    """
    Renvoie la météo actuelle d'une ville, avec les min/max du jour et de demain.

    Lève ToolsError si la ville est introuvable ou si l'API est injoignable.
    """
    city = (city or "").strip()
    if not city:
        raise ToolsError("Indiquez une ville. Exemple : `.meteo Paris`.")

    # 1) Géocodage : transforme le nom de ville en coordonnées
    try:
        response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "fr", "format": "json"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        geo = (response.json() or {}).get("results") or []
    except requests.exceptions.RequestException as exc:
        raise ToolsError("Impossible de joindre le service météo (%s)." % exc.__class__.__name__)

    if not geo:
        raise ToolsError(
            f"😕 Ville « {city} » introuvable. Vérifiez l'orthographe (ex: `Paris`, `Lyon`)."
        )

    place = geo[0]
    name = place.get("name", city)
    if place.get("admin1"):
        name += f", {place['admin1']}"

    # 2) Prévisions actuelles + min/max du jour et du lendemain
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min",
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json() or {}
    except requests.exceptions.RequestException as exc:
        raise ToolsError("Impossible de joindre le service météo (%s)." % exc.__class__.__name__)

    current = data.get("current") or {}
    daily = data.get("daily") or {}

    temp = current.get("temperature_2m")
    hum = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    code = current.get("weather_code")

    max_today = (daily.get("temperature_2m_max") or [None])[0]
    min_today = (daily.get("temperature_2m_min") or [None])[0]
    max_tomorrow = (daily.get("temperature_2m_max") or [None, None])[1]
    min_tomorrow = (daily.get("temperature_2m_min") or [None, None])[1]

    lines = [f"🌦️ *Météo — {name}*"]
    if temp is not None:
        lines.append(f"{_weather_label(code)} · *{temp:.0f}°C*")
    extras = []
    if min_today is not None and max_today is not None:
        extras.append(f"min {min_today:.0f}°C / max {max_today:.0f}°C")
    if hum is not None:
        extras.append(f"💧 {hum:.0f} %")
    if wind is not None:
        extras.append(f"🌬️ {wind:.0f} km/h")
    if extras:
        lines.append(" · ".join(extras))
    if min_tomorrow is not None and max_tomorrow is not None:
        lines.append(f"📅 Demain : {min_tomorrow:.0f}°C → {max_tomorrow:.0f}°C")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Traduction (endpoint public Google translate — sans clé)
# --------------------------------------------------------------------------- #
def translate(text, target="fr"):
    """
    Traduit un texte vers la langue cible (français par défaut).

    La langue source est détectée automatiquement. Lève ToolsError en cas
    de problème.
    """
    text = (text or "").strip()
    if not text:
        raise ToolsError("Indiquez le texte à traduire. Exemple : `.traduis Hello world`.")
    if len(text) > 4000:
        raise ToolsError("Texte trop long (5000 caractères maximum).")

    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        raise ToolsError("Impossible de joindre le service de traduction (%s)."
                         % exc.__class__.__name__)

    try:
        translated = "".join(segment[0] for segment in (data or [])[0] if segment and segment[0])
    except (TypeError, IndexError, KeyError):
        raise ToolsError("Réponse de traduction illisible. Réessayez.")

    if not translated.strip():
        raise ToolsError("Aucune traduction trouvée pour ce texte.")
    return translated.strip()


# --------------------------------------------------------------------------- #
#  Conversion de devises (Frankfurter — taux BCE, sans clé)
# --------------------------------------------------------------------------- #
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")


def currency(amount, source, target):
    """
    Convertit un montant entre deux devises (codes ISO 4217, ex: EUR, USD).

    Lève ToolsError si le montant ou un code devise est invalide.
    """
    try:
        amount = float(str(amount).replace(",", "."))
    except (TypeError, ValueError):
        raise ToolsError(f"Montant invalide : « {amount} ». Exemple : `.devise 100 EUR USD`.")

    source = (source or "").upper().strip()
    target = (target or "").upper().strip()
    if not _CURRENCY_RE.match(source) or not _CURRENCY_RE.match(target):
        raise ToolsError(
            "Codes de devise invalides. Utilisez 3 lettres (ex: EUR, USD, GBP, MAD)."
        )
    if source == target:
        return round(amount, 2)

    try:
        response = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": source, "to": target, "amount": amount},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json() or {}
    except requests.exceptions.RequestException as exc:
        raise ToolsError("Impossible de joindre le service de change (%s)."
                         % exc.__class__.__name__)

    if response.status_code == 404 or not data:
        raise ToolsError(f"Devise « {source} » ou « {target} » inconnue. "
                         f"Exemples : EUR, USD, GBP, JPY, CHF, MAD.")

    rate = (data.get("rates") or {}).get(target)
    if rate is None:
        raise ToolsError(f"Conversion {source} → {target} indisponible.")
    return round(float(rate), 2)
