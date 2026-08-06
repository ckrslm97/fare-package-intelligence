"""IATA airport metadata: city code, country (ISO-2) code/name, and region.

Single source of truth for enriching origin/destination. City code is the IATA
metropolitan code (LON for LHR/LGW, NYC for JFK, ROM for FCO, YTO for YYZ, IST
for SAW); for single-airport cities it equals the airport code.
"""

from __future__ import annotations

# code -> (city_code, city_name, country_code, country_name, region)
_TABLE: dict[str, tuple[str, str, str, str, str]] = {
    "ADJ": ("AMM", "Amman", "JO", "Jordan", "Middle East"),
    "AGP": ("AGP", "Malaga", "ES", "Spain", "Europe"),
    "AMM": ("AMM", "Amman", "JO", "Jordan", "Middle East"),
    "AMS": ("AMS", "Amsterdam", "NL", "Netherlands", "Europe"),
    "AQJ": ("AQJ", "Aqaba", "JO", "Jordan", "Middle East"),
    "ATH": ("ATH", "Athens", "GR", "Greece", "Europe"),
    "BBU": ("BUH", "Bucharest", "RO", "Romania", "Europe"),
    "BCN": ("BCN", "Barcelona", "ES", "Spain", "Europe"),
    "BER": ("BER", "Berlin", "DE", "Germany", "Europe"),
    "BEY": ("BEY", "Beirut", "LB", "Lebanon", "Middle East"),
    "BHX": ("BHX", "Birmingham", "GB", "UK", "Europe"),
    "BKK": ("BKK", "Bangkok", "TH", "Thailand", "Asia"),
    "BLQ": ("BLQ", "Bologna", "IT", "Italy", "Europe"),
    "BOM": ("BOM", "Mumbai", "IN", "India", "Asia"),
    "BOS": ("BOS", "Boston", "US", "USA", "N. America"),
    "BRU": ("BRU", "Brussels", "BE", "Belgium", "Europe"),
    "CAN": ("CAN", "Guangzhou", "CN", "China", "Asia"),
    "CDG": ("PAR", "Paris", "FR", "France", "Europe"),
    "CGH": ("SAO", "Sao Paulo", "BR", "Brazil", "S. America"),
    "CLJ": ("CLJ", "Cluj-Napoca", "RO", "Romania", "Europe"),
    "CMB": ("CMB", "Colombo", "LK", "Sri Lanka", "Asia"),
    "CND": ("CND", "Constanta", "RO", "Romania", "Europe"),
    "CRL": ("BRU", "Brussels", "BE", "Belgium", "Europe"),
    "DAC": ("DAC", "Dhaka", "BD", "Bangladesh", "Asia"),
    "DEL": ("DEL", "Delhi", "IN", "India", "Asia"),
    "DFW": ("DFW", "Dallas/Fort Worth", "US", "USA", "N. America"),
    "DME": ("MOW", "Moscow", "RU", "Russia", "Europe"),
    "DMK": ("BKK", "Bangkok", "TH", "Thailand", "Asia"),
    "DPS": ("DPS", "Denpasar (Bali)", "ID", "Indonesia", "Asia"),
    "DTW": ("DTT", "Detroit", "US", "USA", "N. America"),
    "DUS": ("DUS", "Dusseldorf", "DE", "Germany", "Europe"),
    "ECN": ("ECN", "North Nicosia", "CY", "Cyprus", "Europe"),   # Ercan; traffic routes via Türkiye
    "EDI": ("EDI", "Edinburgh", "GB", "UK", "Europe"),
    "EVN": ("EVN", "Yerevan", "AM", "Armenia", "Asia"),
    "FCO": ("ROM", "Rome", "IT", "Italy", "Europe"),
    "FRA": ("FRA", "Frankfurt", "DE", "Germany", "Europe"),
    "GMP": ("SEL", "Seoul", "KR", "South Korea", "Asia"),
    "GRU": ("SAO", "Sao Paulo", "BR", "Brazil", "S. America"),
    "HAM": ("HAM", "Hamburg", "DE", "Germany", "Europe"),
    "HAN": ("HAN", "Hanoi", "VN", "Vietnam", "Asia"),
    "HHN": ("HHN", "Frankfurt-Hahn", "DE", "Germany", "Europe"),
    "HKG": ("HKG", "Hong Kong", "HK", "Hong Kong", "Asia"),
    "HKT": ("HKT", "Phuket", "TH", "Thailand", "Asia"),
    "HND": ("TYO", "Tokyo", "JP", "Japan", "Asia"),
    "IAD": ("WAS", "Washington", "US", "USA", "N. America"),
    "IAH": ("HOU", "Houston", "US", "USA", "N. America"),
    "ICN": ("SEL", "Seoul", "KR", "South Korea", "Asia"),
    "ISB": ("ISB", "Islamabad", "PK", "Pakistan", "Asia"),
    "JED": ("JED", "Jeddah", "SA", "Saudi Arabia", "Middle East"),
    "JFK": ("NYC", "New York", "US", "USA", "N. America"),
    "EWR": ("NYC", "New York", "US", "USA", "N. America"),
    "KHI": ("KHI", "Karachi", "PK", "Pakistan", "Asia"),
    "KIX": ("OSA", "Osaka", "JP", "Japan", "Asia"),
    "KRK": ("KRK", "Krakow", "PL", "Poland", "Europe"),
    "KUL": ("KUL", "Kuala Lumpur", "MY", "Malaysia", "Asia"),
    "LAX": ("LAX", "Los Angeles", "US", "USA", "N. America"),
    "LED": ("LED", "St Petersburg", "RU", "Russia", "Europe"),
    "LGW": ("LON", "London", "GB", "UK", "Europe"),
    "LHR": ("LON", "London", "GB", "UK", "Europe"),
    "LHE": ("LHE", "Lahore", "PK", "Pakistan", "Asia"),
    "LTN": ("LON", "London", "GB", "UK", "Europe"),
    "MAD": ("MAD", "Madrid", "ES", "Spain", "Europe"),
    "MAN": ("MAN", "Manchester", "GB", "UK", "Europe"),
    "MED": ("MED", "Medina", "SA", "Saudi Arabia", "Middle East"),
    "MEL": ("MEL", "Melbourne", "AU", "Australia", "Oceania"),
    "MLE": ("MLE", "Male", "MV", "Maldives", "Asia"),
    "MNL": ("MNL", "Manila", "PH", "Philippines", "Asia"),
    "MUC": ("MUC", "Munich", "DE", "Germany", "Europe"),
    "MXP": ("MIL", "Milan", "IT", "Italy", "Europe"),
    "NCE": ("NCE", "Nice", "FR", "France", "Europe"),
    "NQZ": ("NQZ", "Astana", "KZ", "Kazakhstan", "Asia"),
    "NRT": ("TYO", "Tokyo", "JP", "Japan", "Asia"),
    "ORD": ("CHI", "Chicago", "US", "USA", "N. America"),
    "OTP": ("BUH", "Bucharest", "RO", "Romania", "Europe"),
    "PEK": ("BJS", "Beijing", "CN", "China", "Asia"),
    "PKX": ("BJS", "Beijing", "CN", "China", "Asia"),
    "PRG": ("PRG", "Prague", "CZ", "Czechia", "Europe"),
    "PVG": ("SHA", "Shanghai", "CN", "China", "Asia"),
    "RUH": ("RUH", "Riyadh", "SA", "Saudi Arabia", "Middle East"),
    "SFO": ("SFO", "San Francisco", "US", "USA", "N. America"),
    "SGN": ("SGN", "Ho Chi Minh City", "VN", "Vietnam", "Asia"),
    "SIN": ("SIN", "Singapore", "SG", "Singapore", "Asia"),
    "SJJ": ("SJJ", "Sarajevo", "BA", "Bosnia and Herzegovina", "Europe"),
    "SKG": ("SKG", "Thessaloniki", "GR", "Greece", "Europe"),
    "SOF": ("SOF", "Sofia", "BG", "Bulgaria", "Europe"),
    "SVO": ("MOW", "Moscow", "RU", "Russia", "Europe"),
    "SVQ": ("SVQ", "Seville", "ES", "Spain", "Europe"),
    "SYD": ("SYD", "Sydney", "AU", "Australia", "Oceania"),
    "TBS": ("TBS", "Tbilisi", "GE", "Georgia", "Asia"),
    "TIF": ("TIF", "Taif", "SA", "Saudi Arabia", "Middle East"),
    "TPE": ("TPE", "Taipei", "TW", "Taiwan", "Asia"),
    "VAR": ("VAR", "Varna", "BG", "Bulgaria", "Europe"),
    "VCE": ("VCE", "Venice", "IT", "Italy", "Europe"),
    "VKO": ("MOW", "Moscow", "RU", "Russia", "Europe"),
    "WAW": ("WAW", "Warsaw", "PL", "Poland", "Europe"),
    "YNB": ("YNB", "Yanbu", "SA", "Saudi Arabia", "Middle East"),
    "YTZ": ("YTO", "Toronto", "CA", "Canada", "N. America"),
    "YUL": ("YMQ", "Montreal", "CA", "Canada", "N. America"),
    "YVR": ("YVR", "Vancouver", "CA", "Canada", "N. America"),
    "YYZ": ("YTO", "Toronto", "CA", "Canada", "N. America"),
    "ZYR": ("BRU", "Brussels", "BE", "Belgium", "Europe"),  # Brussels Midi rail
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
