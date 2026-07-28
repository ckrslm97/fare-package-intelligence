"""IATA airport metadata: city code, country (ISO-2) code/name, and region.

Single source of truth for enriching origin/destination. City code is the IATA
metropolitan code (LON for LHR/LGW, NYC for JFK, ROM for FCO, YTO for YYZ, IST
for SAW); for single-airport cities it equals the airport code.
"""

from __future__ import annotations

# code -> (city_code, city_name, country_code, country_name, region)
_TABLE: dict[str, tuple[str, str, str, str, str]] = {
    "AMS": ("AMS", "Amsterdam", "NL", "Netherlands", "Europe"),
    "ATH": ("ATH", "Athens", "GR", "Greece", "Europe"),
    "BKK": ("BKK", "Bangkok", "TH", "Thailand", "Asia"),
    "DEL": ("DEL", "Delhi", "IN", "India", "Asia"),
    "FCO": ("ROM", "Rome", "IT", "Italy", "Europe"),
    "HAN": ("HAN", "Hanoi", "VN", "Vietnam", "Asia"),
    "HKG": ("HKG", "Hong Kong", "HK", "Hong Kong", "Asia"),
    "ISB": ("ISB", "Islamabad", "PK", "Pakistan", "Asia"),
    "JFK": ("NYC", "New York", "US", "USA", "N. America"),
    "EWR": ("NYC", "New York", "US", "USA", "N. America"),
    "KUL": ("KUL", "Kuala Lumpur", "MY", "Malaysia", "Asia"),
    "LGW": ("LON", "London", "GB", "UK", "Europe"),
    "LHR": ("LON", "London", "GB", "UK", "Europe"),
    "LHE": ("LHE", "Lahore", "PK", "Pakistan", "Asia"),
    "PRG": ("PRG", "Prague", "CZ", "Czechia", "Europe"),
    "SFO": ("SFO", "San Francisco", "US", "USA", "N. America"),
    "SIN": ("SIN", "Singapore", "SG", "Singapore", "Asia"),
    "SJJ": ("SJJ", "Sarajevo", "BA", "Bosnia and Herzegovina", "Europe"),
    "TPE": ("TPE", "Taipei", "TW", "Taiwan", "Asia"),
    "WAW": ("WAW", "Warsaw", "PL", "Poland", "Europe"),
    "YVR": ("YVR", "Vancouver", "CA", "Canada", "N. America"),
    "YYZ": ("YTO", "Toronto", "CA", "Canada", "N. America"),
    # Turkey (for the Local/Beyond rule and future ONDs)
    "IST": ("IST", "Istanbul", "TR", "Türkiye", "Turkey"),
    "SAW": ("IST", "Istanbul", "TR", "Türkiye", "Turkey"),
    "ESB": ("ANK", "Ankara", "TR", "Türkiye", "Turkey"),
    "ADB": ("IZM", "Izmir", "TR", "Türkiye", "Turkey"),
    "AYT": ("AYT", "Antalya", "TR", "Türkiye", "Turkey"),
}

_FIELDS = ("city_code", "city_name", "country_code", "country_name", "region")


def meta(code: str) -> dict[str, str]:
    """Return airport metadata; unknown codes get sensible 'Other' defaults."""
    code = (code or "").strip().upper()
    row = _TABLE.get(code)
    if row is None:
        return {"city_code": code, "city_name": "", "country_code": "",
                "country_name": "Other", "region": "Other"}
    return dict(zip(_FIELDS, row))
