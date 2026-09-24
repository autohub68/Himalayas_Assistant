"""Pull location / country from Himalayas search cards and talent profile text."""
from __future__ import annotations

import re
import unicodedata

# Common ISO 3166-1 alpha-2 → English name. Unknown codes fall back to the code itself.
ISO_COUNTRIES = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan", "AL": "Albania", "AM": "Armenia",
    "AR": "Argentina", "AT": "Austria", "AU": "Australia", "AZ": "Azerbaijan", "BA": "Bosnia and Herzegovina",
    "BD": "Bangladesh", "BE": "Belgium", "BG": "Bulgaria", "BH": "Bahrain", "BO": "Bolivia", "BR": "Brazil",
    "BY": "Belarus", "BZ": "Belize", "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CN": "China",
    "CO": "Colombia", "CR": "Costa Rica", "CU": "Cuba", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany",
    "DK": "Denmark", "DO": "Dominican Republic", "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia",
    "EG": "Egypt", "ES": "Spain", "ET": "Ethiopia", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "GE": "Georgia", "GH": "Ghana", "GR": "Greece", "GT": "Guatemala", "HK": "Hong Kong", "HN": "Honduras",
    "HR": "Croatia", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IN": "India",
    "IQ": "Iraq", "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JM": "Jamaica", "JO": "Jordan", "JP": "Japan",
    "KE": "Kenya", "KG": "Kyrgyzstan", "KH": "Cambodia", "KR": "South Korea", "KW": "Kuwait", "KZ": "Kazakhstan",
    "LA": "Laos", "LB": "Lebanon", "LK": "Sri Lanka", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia",
    "MA": "Morocco", "MD": "Moldova", "ME": "Montenegro", "MK": "North Macedonia", "MM": "Myanmar",
    "MN": "Mongolia", "MT": "Malta", "MX": "Mexico", "MY": "Malaysia", "NG": "Nigeria", "NI": "Nicaragua",
    "NL": "Netherlands", "NO": "Norway", "NP": "Nepal", "NZ": "New Zealand", "OM": "Oman", "PA": "Panama",
    "PE": "Peru", "PH": "Philippines", "PK": "Pakistan", "PL": "Poland", "PR": "Puerto Rico", "PT": "Portugal",
    "PY": "Paraguay", "QA": "Qatar", "RO": "Romania", "RS": "Serbia", "RU": "Russia", "SA": "Saudi Arabia",
    "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "SV": "El Salvador", "TH": "Thailand",
    "TN": "Tunisia", "TR": "Turkey", "TW": "Taiwan", "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda",
    "US": "United States", "UY": "Uruguay", "UZ": "Uzbekistan", "VE": "Venezuela", "VN": "Vietnam",
    "XK": "Kosovo", "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}

FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
PIN_RE = re.compile(r"(?:📍|🗺)\s*(.+)", re.UNICODE)
GLOBE_RE = re.compile(r"[🌍🌎🌏]\s*(.+)", re.UNICODE)
LABEL_RE = re.compile(r"(?im)^(?:\*\*)?(?:location|country|based in|remote from)\**\s*[:\-–]\s*(.+)$")
BASED_RE = re.compile(r"(?i)\b(?:based in|remote from|located in|living in)\s+([^.\n]{2,80})")
SECTION_RE = re.compile(r"(?im)^##\s*Location\s*$")
# Do not contact members whose country / location is Korea (South or North).
KOREA_RE = re.compile(
    r"(?i)(?:\b(?:south|north)\s+)?\bkorea(?:n)?\b|\brepublic\s+of\s+korea\b|\bdprk\b(?!\w)|\brok\b(?!\w)|🇰🇷|🇰🇵"
)
KOREA_CODE_RE = re.compile(r"(?i)^\s*(?:KR|KP|KOR|PRK)\s*$")
KOREA_SKIP_REASON = "Skipped: country is Korea (do not contact)"


def is_korea_country(*parts: str) -> bool:
    """True when the country / location text points to Korea (KR or KP)."""
    text = " ".join(str(part) for part in parts if part)
    if not text.strip():
        return False
    if "🇰🇷" in text or "🇰🇵" in text:
        return True
    code = flag_code(text)
    if code in {"KR", "KP"}:
        return True
    if KOREA_CODE_RE.match(text.strip()):
        return True
    return bool(KOREA_RE.search(text))


def contact_blocked_reason(candidate: dict) -> str | None:
    """Why this member must not get a first message, or None when contact is allowed."""
    country = (candidate.get("country") or "").strip()
    summary = candidate.get("summary") or ""
    # Prefer the stored country. If empty, fall back to location cues in the profile text only.
    if country and is_korea_country(country):
        return KOREA_SKIP_REASON
    if not country:
        location = extract_country(summary)
        if location and is_korea_country(location):
            return KOREA_SKIP_REASON
        # Location lines / flags in the profile even when extract_country is thin.
        if is_korea_country(*(line for line in summary.splitlines() if any(mark in line for mark in ("📍", "🌍", "🌎", "🌏", "🇰🇷", "🇰🇵", "Location", "based in", "Based in", "Remote from")))):
            return KOREA_SKIP_REASON
    return None


def flag_code(text: str) -> str | None:
    match = FLAG_RE.search(text or "")
    if not match:
        return None
    return "".join(chr(ord(ch) - 0x1F1E6 + ord("A")) for ch in match.group(0))


def flag_country(text: str) -> str:
    code = flag_code(text)
    if not code:
        return ""
    return ISO_COUNTRIES.get(code, code)


def _clean_place(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "")
    text = FLAG_RE.sub("", text)
    text = re.sub(r"[📍🗺🌍🌎🌏⛰️🏔️🏔️]+", " ", text)
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"(?i)^(remote(?:ly)?(?:\s+from)?|based(?:\s+in)?)\s*", "", text)
    text = re.sub(r"^[\s·•|,;:\-–]+", "", text)
    text = re.split(r"(?i)\s+(?:where|looking|seeking|studying|open to|available)\b|,?\s+I[\u2019'`]m\b", text, maxsplit=1)[0]
    text = re.sub(r"\s+", " ", text).strip(" \t-–|,.;:·•")
    return text[:120]


def _prefer(place: str, country: str) -> str:
    place, country = place.strip(), country.strip()
    if country and place:
        if country.lower() in place.lower():
            return place
        return f"{place}, {country}"
    return country or place


def extract_country(*texts: str) -> str:
    """Best-effort country / location label from one or more Himalayas text blobs."""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return ""

    candidates: list[str] = []

    for line in blob.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in (PIN_RE, GLOBE_RE):
            match = pattern.search(stripped)
            if match:
                place = _clean_place(match.group(1))
                country = flag_country(stripped)
                value = _prefer(place, country)
                if value:
                    candidates.append(value)
        label = LABEL_RE.match(stripped)
        if label:
            place = _clean_place(label.group(1))
            country = flag_country(stripped)
            value = _prefer(place, country)
            if value:
                candidates.append(value)

    lines = blob.splitlines()
    for index, line in enumerate(lines):
        if SECTION_RE.match(line.strip()) and index + 1 < len(lines):
            place = _clean_place(lines[index + 1])
            country = flag_country(lines[index + 1])
            value = _prefer(place, country)
            if value:
                candidates.append(value)

    if not candidates:
        for match in BASED_RE.finditer(blob):
            place = _clean_place(match.group(1))
            # Prefer a nearby flag on the same sentence.
            window = blob[match.start(): match.end() + 40]
            country = flag_country(window)
            value = _prefer(place, country)
            if value and len(value) >= 3:
                candidates.append(value)
                break

    if not candidates:
        country = flag_country(blob)
        if country:
            return country

    # Prefer a pin/globe/label hit over prose "based in".
    return candidates[0] if candidates else ""
