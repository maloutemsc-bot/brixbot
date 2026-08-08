"""
tools_service.py — Outils pratiques 100 % gratuits, sans clé API.

APIs utilisées (aucune inscription requise) :
  - Open-Meteo   : https://open-meteo.com   → géocodage + prévisions météo
  - Google (gtx) : endpoint public de traduction (détection automatique)
  - Frankfurter  : https://frankfurter.app  → taux de change de la BCE

Toutes les erreurs sont levées sous forme de ToolsError avec un message
français directement affichable dans WhatsApp.
"""

import random
import re
import unicodedata
from datetime import date

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


# --------------------------------------------------------------------------- #
#  Crypto en direct (CoinGecko — gratuit, sans clé)
# --------------------------------------------------------------------------- #
CRYPTO_MAP = {
    "btc": ("bitcoin", "₿ Bitcoin"),
    "eth": ("ethereum", "Ξ Ethereum"),
    "sol": ("solana", "◎ Solana"),
    "doge": ("dogecoin", "🐕 Dogecoin"),
    "xrp": ("ripple", "✕ Ripple"),
    "ada": ("cardano", "🅰 Cardano"),
    "ltc": ("litecoin", "Ł Litecoin"),
    "bnb": ("binancecoin", "🔶 BNB"),
}

# Symboles affichés par défaut
DEFAULT_CRYPTO = ["btc", "eth", "sol", "doge", "xrp"]


def _fmt_price(value):
    """Formate un prix à la française : 98245.123 → 98 245,12."""
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return "?"


def crypto(symbols=None):
    """
    Renvoie les prix en direct des cryptomonnaies demandées (EUR + USD).

    symbols : liste de symboles (btc, eth…) ou None pour les valeurs par défaut.
    Lève ToolsError si un symbole est inconnu ou si l'API est injoignable.
    """
    if symbols is None:
        symbols = list(DEFAULT_CRYPTO)
    symbols = list(dict.fromkeys(symbols))  # dédoublonne en conservant l'ordre
    if not symbols:
        raise ToolsError("Aucune cryptomonnaie demandée.")

    ids = []
    labels = []
    for sym in symbols:
        entry = CRYPTO_MAP.get(str(sym).lower())
        if not entry:
            raise ToolsError(
                f"Symbole « {sym} » inconnu. Disponibles : "
                + ", ".join(sorted(CRYPTO_MAP.keys()))
            )
        ids.append(entry[0])
        labels.append(entry)

    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "eur,usd"},
            timeout=TIMEOUT,
        )
        if response.status_code == 429:
            raise ToolsError("CoinGecko a limité les requêtes. Réessayez dans une minute.")
        response.raise_for_status()
        data = response.json() or {}
    except requests.exceptions.RequestException as exc:
        raise ToolsError("Impossible de joindre CoinGecko (%s)." % exc.__class__.__name__)

    lines = ["📈 *Crypto en direct*"]
    for cid, label in labels:  # chaque entrée = (id CoinGecko, libellé affiché)
        prices = data.get(cid) or {}
        if not prices:
            continue
        lines.append(
            f"{label} : {_fmt_price(prices.get('eur'))} € · {_fmt_price(prices.get('usd'))} $"
        )
    if len(lines) == 1:
        raise ToolsError("Aucun prix reçu de CoinGecko.")
    lines.append("💡 Ajoute un symbole : `.crypto btc`")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Boule magique 🎱
# --------------------------------------------------------------------------- #
EIGHT_BALL_ANSWERS = [
    "Oui, sans aucun doute. ✅",
    "C'est certain. 🔮",
    "Sans l'ombre d'un doute. 🌟",
    "Très probable. ✨",
    "Oui, mais sois patient. ⏳",
    "La réponse est oui. 💫",
    "Peut-être… réessaie plus tard. 🤔",
    "Réponse floue, repose la question. 🌫️",
    "Mieux vaut ne pas te le dire. 🤐",
    "Ne compte pas là-dessus. ❌",
    "Non, vraiment pas. 🙅",
    "Très peu probable. 🌧️",
    "Les sources disent non. 📡",
    "D'après mes calculs… oui ! 🧮",
]


def magic8ball(question):
    """Réponse aléatoire de la boule magique, avec la question si fournie."""
    answer = random.choice(EIGHT_BALL_ANSWERS)
    lines = ["🎱 *La boule magique*"]
    if question:
        lines.append(f"❓ {question}")
    lines.append(f"→ {answer}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Blagues 😂
# --------------------------------------------------------------------------- #
JOKES = [
    "Pourquoi les plongeurs plongent-ils toujours en arrière ? Parce que sinon ils tombent dans le bateau ! 🤿",
    "Quel est le comble pour un électricien ? Ne pas être au courant. ⚡",
    "Pourquoi les squelettes ne se battent-ils jamais ? Ils n'en ont pas le courage. 💀",
    "Que dit un informaticien qui tombe dans la piscine ? « Aïe, ça déborde ! » 💻",
    "Pourquoi les mathématiciens n'aiment-ils pas la plage ? Parce qu'ils ont peur des sinus. 🏖️",
    "Qu'est-ce qui est petit, vert et qui monte et descend ? Un petit pois dans un ascenseur. 🟢",
    "Pourquoi les poissons n'aiment-ils pas le Wi-Fi ? Ils ont peur des requins. 🦈",
    "Quel est le sport préféré des oiseaux ? Le bad-minton ! 🦜",
    "Pourquoi le livre de maths est-il triste ? Parce qu'il a trop de problèmes. 📚",
    "Qu'est-ce qu'une grenouille avec une pièce sur la tête ? Une grenouille qui a un sou. 🐸",
    "Pourquoi les fantômes sont-ils de mauvais menteurs ? Parce qu'on les voit à travers. 👻",
    "Que fait une abeille dans une bijouterie ? Elle fait son miel ! 🐝",
]


def joke():
    """Une blague aléatoire (contenu local, aucune API)."""
    return f"😂 {random.choice(JOKES)}"


# --------------------------------------------------------------------------- #
#  Horoscope 🔮
# --------------------------------------------------------------------------- #
SIGNES = {
    "belier": "Bélier", "taureau": "Taureau", "gemeaux": "Gémeaux",
    "cancer": "Cancer", "lion": "Lion", "vierge": "Vierge",
    "balance": "Balance", "scorpion": "Scorpion", "sagittaire": "Sagittaire",
    "capricorne": "Capricorne", "verseau": "Verseau", "poissons": "Poissons",
}

_HORO_AMOUR = [
    "une belle rencontre est possible, garde l'œil ouvert",
    "les cœurs s'ouvrent, ose le premier pas",
    "l'honnêteté fera des merveilles aujourd'hui",
    "un message surprise pourrait te faire sourire",
    "ne force rien : les bonnes choses viennent à leur heure",
]

_HORO_TRAVAIL = [
    "ta créativité impressionne, lance cette idée",
    "un collègue a besoin de toi, sois disponible",
    "évite de repousser ce dossier désagréable… fais-le",
    "une opportunité arrive par e-mail, ne la snooze pas",
    "concentre-toi sur l'essentiel, le reste attendra",
]

_HORO_ARGENT = [
    "une petite rentrée inattendue est possible",
    "résiste aux achats impulsifs, ton compte te remerciera",
    "bon moment pour ranger, pas pour dépenser",
    "un placement prudent paiera plus tard",
    "le café du matin… autorisé. Le reste, on verra",
]

_HORO_CHANCE = [
    "7/10 — une bonne journée, sans plus",
    "9/10 — la chance est de ton côté, tente quelque chose",
    "5/10 — neutre : c'est toi qui crées la chance",
    "8/10 — un fou rire est programmé",
    "6/10 — évite de jouer au loto aujourd'hui",
]


def _normalize(text):
    """Supprime les accents et passe en minuscules (bélier → belier)."""
    return "".join(
        char for char in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(char) != "Mn"
    ).lower().strip()


def horoscope(signe):
    """
    Horoscope humoristique du jour pour un signe du zodiaque.

    Lève ToolsError si le signe est inconnu.
    """
    key = _normalize(signe)
    if key not in SIGNES:
        raise ToolsError(
            "Signe inconnu. Choisis parmi : " + ", ".join(sorted(SIGNES.values()))
        )

    label = SIGNES[key]
    today = date.today().strftime("%d/%m/%Y")
    return (
        f"⭐ *Horoscope {label}* — {today}\n"
        f"💖 Amour : {random.choice(_HORO_AMOUR)}\n"
        f"💼 Travail : {random.choice(_HORO_TRAVAIL)}\n"
        f"💰 Argent : {random.choice(_HORO_ARGENT)}\n"
        f"🍀 Ta chance : {random.choice(_HORO_CHANCE)}\n"
        "_(Lecture humoristique — ne fais surtout pas de folies !)_"
    )
