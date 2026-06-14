#!/usr/bin/env python3
"""
Weekend Dispatch — Crown Heights
Sends every Saturday at 9:30am covering the full weekend.
Sections: MTA alerts · Weather + Mabel walk windows · Farmers market · BPL events · Brooklyn culture news

Required environment variables:
  ANTHROPIC_API_KEY
  DISPATCH_EMAIL        (Gmail sender address)
  DISPATCH_APP_PASSWORD (Gmail App Password)
  DISPATCH_TO           (recipient address)

Changelog (June 2026):
  - FIX: hour-overflow crash in get_walk_windows when evening best hour is 8pm
    (best_hour + 4 = 24 → invalid datetime hour). Now uses timedelta, which
    correctly rolls into the next UTC day.
  - NEW: market_go_nogo now considers *sustained average* temperature across
    the outing window, not just the peak — a 2-hour round trip with a black
    mini labradoodle at 85°F+ average warrants caution even if no single
    hour hits 92°F. Sustained 90°F+ average is a no-go.
  - NEW: fetch_surface_transit_alerts() watches the B65/B43 buses, the JFK
    AirTrain (Jamaica + Howard Beach), and the Nostrand Av LIRR station.
    These are surfaced in the MTA section ONLY when a service is cut, rerouted,
    or suspended — otherwise they are omitted entirely and only train info shows.
  - CHANGE: envelope email redrawn as a single realistic SVG (folded-back flap,
    lit paper, pressed-wax seal, addressed "For Zachary Tharpe"); overall
    envelope height reduced.
"""

import os
import math
import smtplib
import datetime
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
import anthropic

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

LAT = 40.6678
LON = -73.9442
TIMEZONE = "America/New_York"

# Crown Heights streets for walk routing
CORRIDORS = {
    "Bergen St / Dean St (E-W)":  {"azimuth": 100, "canopy": 0.52},
    "Kingston Ave (N-S)":         {"azimuth": 10,  "canopy": 0.43},
    "Albany Ave (N-S)":           {"azimuth": 10,  "canopy": 0.42},
}

# MTA stations to watch
MTA_STATIONS = [
    "Kingston-Throop Avenues",   # C
    "Nostrand Avenue",           # A/C
    "Utica Avenue",              # A/C and 3/4
    "Kingston Avenue",           # 3
    "Crown Heights-Utica Avenue",# 3/4
]

# BPL Central Branch
BPL_EVENTS_URL = "https://www.bklynlibrary.org/events"
BPL_BRANCH_FILTER = "Central Library"  # 560 New York Ave at Maple St

# Farmers market
GRAND_ARMY_MARKET_URL = "https://www.grownyc.org/greenmarket/brooklyn-grand-army-plaza"

# GrowNYC Open Data (Brooklyn markets)
GROWNYC_API = "https://data.cityofnewyork.us/resource/b7kx-qikm.json"

INTEREST_PROFILE = """
Literary/Countercultural: Patti Smith, Sylvia Plath, Joan Didion, Renata Adler, Virginia Woolf,
Truman Capote, Bret Easton Ellis, Chuck Palahniuk, Hunter S. Thompson, Irvine Welsh, Thomas Pynchon.
Heavy on disaffected literary fiction with a New York lens.

NYC/Urban: Jeremiah Moss (Vanishing New York — 5 stars), Herbert Asbury (Gangs of New York),
Samuel Delany, Ian Frazier — drawn to neighborhood change, urban decay, NYC history, gentrification,
landmarks preservation, architecture.

Sci-fi/Speculative: Frank Herbert's Dune (currently reading, 5 stars), Andy Weir's Project Hail Mary
(5 stars), Red Rising, Cloud Atlas — hard sci-fi and epic worldbuilding.

Environmental/Urban Policy: Naomi Klein, Ashley Dawson (Extinction), Worldwatch Institute —
climate, cities, sustainability, civic infrastructure.

Journalism/Narrative Nonfiction: John Carreyrou (Bad Blood — 5 stars), William Finnegan
(Barbarian Days — 5 stars, surf/ocean culture), David Grann, T.J. English — longform investigative.

Food/Life/Memoir: Stanley Tucci (5 stars), Ina Garten — food culture, good living, Italy.

Professional interests: AI governance, technology policy, civic tech, public sector innovation,
Brooklyn/Crown Heights local history and community issues.
"""


# ─────────────────────────────────────────────
# SOLAR + PAVEMENT PHYSICS (from morning dispatch)
# ─────────────────────────────────────────────

def solar_elevation(dt_utc):
    doy = dt_utc.timetuple().tm_yday
    hour_utc = dt_utc.hour + dt_utc.minute / 60
    b = math.radians((360 / 365) * (doy - 81))
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    lstm = -75
    lon_correction = 4 * (LON - lstm)
    solar_time = hour_utc * 60 + lon_correction + eot - 240
    hour_angle = math.radians((solar_time / 60 - 12) * 15)
    decl = math.radians(23.45 * math.sin(math.radians((360 / 365) * (doy - 81))))
    lat_r = math.radians(LAT)
    elev = math.degrees(math.asin(
        math.sin(lat_r) * math.sin(decl) +
        math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle)
    ))
    return max(elev, 0)


def pavement_temp(air_c, solar_elev_deg, canopy=0.45):
    albedo = 0.30
    stefan = 5.67e-8
    solar_irr = max(0, 1000 * math.sin(math.radians(solar_elev_deg)))
    shaded_irr = solar_irr * (1 - canopy)
    absorbed = shaded_irr * (1 - albedo)
    air_k = air_c + 273.15
    net_rad = absorbed - stefan * air_k ** 4 * 0.1
    h = 16
    delta_t = net_rad / h
    return air_c + max(0, delta_t)


def c_to_f(c):
    return c * 9 / 5 + 32


def classify_walk(temp_f, pavement_f, rain_pct, is_winter):
    if temp_f >= 95 or pavement_f >= 125:
        return "🚫 NO WALK", "extreme heat"
    if temp_f <= 20:
        return "🚫 NO WALK", "dangerous cold"
    if rain_pct > 25:
        return "🚫 NO WALK", f"rain {rain_pct}%"
    if is_winter:
        if temp_f <= 32:
            return "⚠️  WIND CHILL", "very cold — booties recommended"
        if temp_f <= 45:
            return "⚠️  CAUTION", "cold — keep it brisk"
        return "✓  GOOD", f"{temp_f:.0f}°F"
    if temp_f >= 90 or pavement_f >= 115:
        return "🔴 HEAT ADVISORY", f"pavement {pavement_f:.0f}°F — go early or skip"
    if temp_f >= 82:
        return "⚠️  CAUTION", f"warm ({temp_f:.0f}°F) — keep it short"
    return "✓  GOOD", f"{temp_f:.0f}°F"


# ─────────────────────────────────────────────
# WEATHER
# ─────────────────────────────────────────────

def fetch_weekend_weather():
    """Pull hourly weather for Saturday + Sunday from Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "temperature_2m,precipitation_probability,weathercode",
        "daily": "temperature_2m_max,temperature_2m_min,weathercode,precipitation_probability_max",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": TIMEZONE,
        "forecast_days": 7,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def parse_weekend_days(weather_data):
    """Return data for Saturday and Sunday (next occurrence)."""
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    if days_until_sat == 0:
        days_until_sat = 0  # today is Saturday
    saturday = today + datetime.timedelta(days=days_until_sat)
    sunday = saturday + datetime.timedelta(days=1)

    daily_times = weather_data["daily"]["time"]
    daily_max = weather_data["daily"]["temperature_2m_max"]
    daily_min = weather_data["daily"]["temperature_2m_min"]
    daily_code = weather_data["daily"]["weathercode"]
    daily_rain = weather_data["daily"]["precipitation_probability_max"]

    hourly_times = weather_data["hourly"]["time"]
    hourly_temps = weather_data["hourly"]["temperature_2m"]
    hourly_rain = weather_data["hourly"]["precipitation_probability"]

    result = {}
    for target_date, label in [(saturday, "saturday"), (sunday, "sunday")]:
        date_str = target_date.isoformat()
        if date_str in daily_times:
            idx = daily_times.index(date_str)
            hourly_for_day = {}
            for h_idx, h_time in enumerate(hourly_times):
                if h_time.startswith(date_str):
                    hour = int(h_time[11:13])
                    hourly_for_day[hour] = {
                        "temp_f": hourly_temps[h_idx],
                        "rain_pct": hourly_rain[h_idx],
                    }
            result[label] = {
                "date": target_date,
                "high_f": daily_max[idx],
                "low_f": daily_min[idx],
                "weather_code": daily_code[idx],
                "rain_pct_max": daily_rain[idx],
                "hourly": hourly_for_day,
            }
    return result


WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


def get_walk_windows(day_data, day_label):
    """
    Generate walk windows based on realistic weekend departure assumptions.

    Primary window: 10am–noon. Zach is not getting up at 6am unless forced.
    Rain fallback: if 10am–noon has >25% rain chance, flag that and find the
    best alternate hour in the 8am–9am or 1pm–3pm range instead.
    Evening window: always assessed (5pm–8pm) as a second walk option.

    Returns 2 windows: [primary/fallback, evening].
    """
    date = day_data["date"]
    month = date.month
    is_winter = month in [11, 12, 1, 2, 3]
    hourly = day_data["hourly"]

    def best_in_range(hours):
        """Return (best_hour, temp_f, rain_pct, score) for a range."""
        best_hour = None
        best_score = -9999
        for h in hours:
            if h not in hourly:
                continue
            temp_f = hourly[h]["temp_f"]
            rain_pct = hourly[h]["rain_pct"]
            if rain_pct > 25:
                score = -100
            elif is_winter:
                score = temp_f - abs(temp_f - 55) * 0.5
            else:
                score = -(temp_f - 72) ** 2 - rain_pct * 0.5
            if score > best_score:
                best_score = score
                best_hour = h
        if best_hour is None:
            return None
        return (best_hour, hourly[best_hour]["temp_f"],
                hourly[best_hour]["rain_pct"], best_score)

    def make_window(zone_name, hour, temp_f, rain_pct, forced_early=False):
        dt_utc = (datetime.datetime(date.year, date.month, date.day)
                  + datetime.timedelta(hours=hour + 4))
        elev = solar_elevation(dt_utc)
        pave_c = pavement_temp((temp_f - 32) * 5 / 9, elev)
        pave_f = c_to_f(pave_c)
        status, note = classify_walk(temp_f, pave_f, rain_pct, is_winter)
        label_hour = f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"
        if forced_early:
            note = f"rain in your usual window — earlier start: {note}"
        return {
            "zone": zone_name,
            "hour_label": label_hour,
            "status": status,
            "note": note,
            "forced_early": forced_early,
        }

    windows = []

    # ── PRIMARY WINDOW ──────────────────────────────────────────────────────
    # Preferred: 10am–noon. Check if rain forces an earlier or later start.
    preferred = best_in_range(range(10, 13))
    rain_in_preferred = all(
        hourly.get(h, {}).get("rain_pct", 0) > 25 for h in range(10, 13)
        if h in hourly
    )

    if preferred and not rain_in_preferred:
        h, temp_f, rain_pct, _ = preferred
        windows.append(make_window("Morning", h, temp_f, rain_pct))
    else:
        # Rain in the preferred window — find the best nearby alternative
        early = best_in_range(range(8, 10))     # 8–9am fallback
        late  = best_in_range(range(13, 16))    # 1–3pm fallback
        best_alt = None
        if early and late:
            best_alt = early if early[3] >= late[3] else late
        elif early:
            best_alt = early
        elif late:
            best_alt = late

        if best_alt:
            h, temp_f, rain_pct, _ = best_alt
            forced = h < 10
            windows.append(make_window("Morning", h, temp_f, rain_pct,
                                       forced_early=forced))
        elif preferred:
            # Everything is bad — just report the preferred window as-is
            h, temp_f, rain_pct, _ = preferred
            windows.append(make_window("Morning", h, temp_f, rain_pct))

    # ── EVENING WINDOW ───────────────────────────────────────────────────────
    evening = best_in_range(range(17, 21))
    if evening:
        h, temp_f, rain_pct, _ = evening
        windows.append(make_window("Evening", h, temp_f, rain_pct))

    return windows


# ─────────────────────────────────────────────
# MTA SERVICE ALERTS
# ─────────────────────────────────────────────

def fetch_mta_alerts():
    """
    Pull MTA PLANNED service change alerts only — reroutes, skip-stop patterns,
    suspended service, express/local changes. Filters out real-time delays,
    signal problems, and general advisories.
    Focuses on A, C, and 3 lines and their Crown Heights stations.
    """
    TARGET_LINES = ["a train", "c train", "3 train", "4 train",
                    "a/c", "a and c", "c and a", "3/4", "3 and 4"]
    TARGET_STATIONS = [
        "kingston-throop", "kingston throop", "nostrand", "utica",
        "kingston ave", "crown heights", "throop", "bedford-nostrand",
        "hoyt-schermerhorn", "jay street", "fulton street",
    ]
    # Words that indicate PLANNED changes (keep these)
    PLANNED_KEYWORDS = [
        "planned", "scheduled", "reroute", "rerouted", "skip", "skipping",
        "will not stop", "won't stop", "bypass", "bypassing", "suspended",
        "shuttle", "replacement bus", "running via", "diverted", "diversion",
        "alternate", "alternating", "extended", "shortened", "weekend service",
        "no service", "suspended between", "running express",
    ]
    # Words that indicate real-time incidents (filter these OUT)
    REALTIME_KEYWORDS = [
        "delay", "delayed", "signal problem", "signal issue", "sick customer",
        "police activity", "investigation", "smoke", "fire department",
        "medical emergency", "offloading", "switch problem",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    found = []

    for url in ["https://new.mta.info/alerts", "https://www.mta.info/alerts"]:
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.find_all(string=True):
                text = el.strip()
                if len(text) < 20:
                    continue
                low = text.lower()

                # Must mention our lines or stations
                line_match = any(kw in low for kw in TARGET_LINES)
                station_match = any(kw in low for kw in TARGET_STATIONS)
                if not (line_match or station_match):
                    continue

                # Must have a planned-change keyword
                is_planned = any(kw in low for kw in PLANNED_KEYWORDS)
                if not is_planned:
                    continue

                # Must NOT be a real-time incident
                is_realtime = any(kw in low for kw in REALTIME_KEYWORDS)
                if is_realtime:
                    continue

                if text not in found:
                    found.append(text[:350])

            if found:
                break
        except Exception:
            continue

    return found[:6]


def fetch_mta_rss_alerts():
    """
    Secondary MTA source: planned service change RSS/text scrape.
    Same filtering logic — planned changes only, A/C/3 focused.
    """
    PLANNED_KEYWORDS = [
        "planned", "scheduled", "reroute", "skip", "will not stop",
        "bypass", "suspended", "shuttle", "replacement", "running via",
        "diverted", "alternate", "weekend service", "no service",
    ]
    REALTIME_KEYWORDS = [
        "delay", "delayed", "signal problem", "sick customer",
        "police activity", "investigation", "smoke", "medical",
    ]
    TARGET = [
        "kingston", "nostrand", "utica", "crown heights",
        "throop", "a train", "c train", "3 train", "a/c", "3/4",
    ]
    try:
        r = requests.get(
            "https://new.mta.info/alerts",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        soup = BeautifulSoup(r.text, "html.parser")
        texts = []
        for el in soup.find_all(string=True):
            stripped = el.strip()
            if len(stripped) < 30:
                continue
            low = stripped.lower()
            if not any(kw in low for kw in TARGET):
                continue
            if not any(kw in low for kw in PLANNED_KEYWORDS):
                continue
            if any(kw in low for kw in REALTIME_KEYWORDS):
                continue
            texts.append(stripped[:250])
        return list(dict.fromkeys(texts))[:5]
    except Exception:
        return []


def fetch_surface_transit_alerts():
    """
    Conditional alerts for the B65 / B43 buses, the JFK AirTrain (Jamaica and
    Howard Beach), and the Nostrand Avenue LIRR station.

    These services are ONLY surfaced when they are disrupted — a planned
    reroute, suspension, or service cut. If nothing matching is found, this
    returns an empty list and the services are not mentioned in the dispatch
    at all (per the "only if cut or altered" rule). Normal subway info is
    handled separately by fetch_mta_alerts / fetch_mta_rss_alerts.

    Returns a list of {"service": <label>, "text": <alert text>} dicts.
    """
    # Each watched service: a label plus the substrings that identify it.
    WATCHED = [
        ("B65 bus",            ["b65"]),
        ("B43 bus",            ["b43"]),
        ("JFK AirTrain (Jamaica)",     ["airtrain", "air train"]),
        ("JFK AirTrain (Howard Beach)", ["airtrain", "air train"]),
        ("Nostrand Av LIRR",   ["nostrand av", "nostrand avenue"]),
    ]

    # Words that indicate the service is actually CUT or ALTERED (keep these).
    DISRUPTION_KEYWORDS = [
        "reroute", "rerouted", "rerouting", "detour", "diverted", "diversion",
        "suspended", "suspension", "no service", "not running", "will not run",
        "cancelled", "canceled", "service change", "service cut", "cut",
        "skipping", "will not stop", "won't stop", "bypass", "bypassing",
        "replacement bus", "shuttle", "running via", "out of service",
        "closed", "closure", "no trains", "no buses", "not stopping",
    ]
    # Real-time noise we explicitly ignore even if it mentions a service.
    REALTIME_KEYWORDS = [
        "delay", "delayed", "running late", "signal problem", "sick customer",
        "police activity", "investigation", "smoke", "medical", "minor delays",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    found = []
    seen = set()

    for url in ["https://new.mta.info/alerts", "https://www.mta.info/alerts"]:
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.find_all(string=True):
                text = el.strip()
                if len(text) < 20:
                    continue
                low = text.lower()

                # Must reference one of the watched services.
                matched_service = None
                for label, needles in WATCHED:
                    if any(n in low for n in needles):
                        # For AirTrain, require the right station context so a
                        # generic AirTrain mention doesn't fire both entries.
                        if "airtrain" in label.lower() or "air train" in low:
                            if "jamaica" in label.lower() and "jamaica" not in low:
                                continue
                            if "howard beach" in label.lower() and "howard beach" not in low:
                                continue
                        matched_service = label
                        break
                if not matched_service:
                    continue

                # Must describe an actual cut/alteration, not a real-time delay.
                if not any(kw in low for kw in DISRUPTION_KEYWORDS):
                    continue
                if any(kw in low for kw in REALTIME_KEYWORDS):
                    continue

                key = (matched_service, text[:120])
                if key in seen:
                    continue
                seen.add(key)
                found.append({"service": matched_service, "text": text[:300]})

            if found:
                break
        except Exception:
            continue

    return found[:6]


# ─────────────────────────────────────────────
# FARMERS MARKET
# ─────────────────────────────────────────────

def fetch_market_data():
    """Pull Brooklyn greenmarket data from GrowNYC open data."""
    try:
        params = {
            "$where": "lower(borough) = 'brooklyn'",
            "$limit": 50,
        }
        r = requests.get(GROWNYC_API, params=params, timeout=10)
        r.raise_for_status()
        markets = r.json()
        # Filter to Saturday-operating markets
        saturday_markets = [
            m for m in markets
            if "saturday" in str(m.get("daysoperation", "")).lower()
            or "sat" in str(m.get("daysoperation", "")).lower()
        ]
        return saturday_markets
    except Exception as e:
        return [{"marketname": f"[Market data unavailable: {e}]"}]


def market_go_nogo(saturday_data, market_list):
    """
    Go/no-go logic for Grand Army Plaza Greenmarket.
    Rules:
    - Rain > 15% during 11am–3:30pm outing window → no-go
    - Sustained heat: average ≥ 90°F across the outing window → no-go
      (2 hours of round-trip walking with a black-coated dog in 90°F+ is unsafe)
    - Peak ≥ 92°F at any point, OR average ≥ 85°F → caution
      (a single hot hour can be timed around; a hot *average* can't)
    - Temp < 32°F average → no-go (not a summer concern but included)
    Walk time: 1 hour each way with Mabel. Outing window: 11am–3:30pm.
    """
    hourly = saturday_data.get("hourly", {})
    outing_hours = [11, 12, 13, 14, 15]  # 11am–3pm

    max_rain = max((hourly.get(h, {}).get("rain_pct", 0) for h in outing_hours), default=0)
    temps_outing = [hourly.get(h, {}).get("temp_f", 72) for h in outing_hours if h in hourly]
    peak_temp = max(temps_outing) if temps_outing else 72
    avg_temp = sum(temps_outing) / len(temps_outing) if temps_outing else 72

    if max_rain > 15:
        verdict = "NO-GO"
        reason = f"Rain chance hits {max_rain:.0f}% during your outing window. Market's not worth the gamble with Mabel."
    elif avg_temp >= 90:
        verdict = "NO-GO"
        reason = (f"Sustained {avg_temp:.0f}°F average across your whole outing window — "
                  f"two hours of walking in that heat is unsafe for Mabel. Go solo by subway, or skip.")
    elif peak_temp >= 92 or avg_temp >= 85:
        verdict = "CAUTION"
        if avg_temp >= 85:
            reason = (f"Averages {avg_temp:.0f}°F across the outing (peak {peak_temp:.0f}°F) — "
                      f"that's sustained heat for a black-coated dog. Bring water, stick to shaded "
                      f"blocks, and seriously consider going before 11am or leaving Mabel home.")
        else:
            reason = f"Peaks at {peak_temp:.0f}°F — doable but bring water, go early (before 11am if you can)."
    elif avg_temp < 32:
        verdict = "NO-GO"
        reason = "Too cold for the walk with Mabel."
    else:
        verdict = "GO"
        reason = f"Conditions look solid — {avg_temp:.0f}°F average, rain {max_rain:.0f}% during your window."

    # Find Grand Army Plaza specifically in the market list
    gap = next((m for m in market_list if "grand army" in str(m.get("marketname", "")).lower()), None)
    market_hours = gap.get("hoursoperations", "8am–3pm") if gap else "8am–3pm"

    return {
        "verdict": verdict,
        "reason": reason,
        "market_hours": market_hours,
        "max_rain_outing": max_rain,
        "peak_temp": peak_temp,
        "avg_temp": avg_temp,
    }


# ─────────────────────────────────────────────
# BPL EVENTS
# ─────────────────────────────────────────────

def fetch_bpl_events():
    """
    Fetch BPL events via multiple strategies:
    1. BPL JSON/API endpoint (most reliable)
    2. Calendar page scrape with realistic browser headers
    3. Google cache fallback
    Returns list of event dicts with 'text' and 'is_central' fields.
    """
    today = datetime.date.today()
    end_date = today + datetime.timedelta(days=14)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bklynlibrary.org/",
    }

    events = []

    # Strategy 1: BPL's internal event JSON feed (Drupal-based)
    try:
        json_url = (
            f"https://www.bklynlibrary.org/events?format=json"
            f"&date_filter[min]={today.isoformat()}"
            f"&date_filter[max]={end_date.isoformat()}"
        )
        r = requests.get(json_url, headers=headers, timeout=15)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
            for item in (data.get("nodes") or data.get("events") or [])[:40]:
                title = item.get("title", item.get("node_title", ""))
                date = item.get("field_event_date", item.get("date", ""))
                time = item.get("field_event_time", item.get("time", ""))
                location = item.get("field_location", item.get("branch", ""))
                desc = item.get("body", item.get("field_description", ""))
                path = item.get("path", item.get("url", item.get("nid", "")))
                url = (f"https://www.bklynlibrary.org{path}"
                       if path and path.startswith("/") else
                       f"https://www.bklynlibrary.org/events/{path}" if path else "")
                text = f"{title} | {date} {time} | {location} | {desc}"
                is_central = "central" in str(location).lower() or "grand army" in str(location).lower()
                events.append({"text": text[:400], "url": url, "is_central": is_central})
    except Exception:
        pass

    # Strategy 2: HTML scrape of calendar page — capture anchor hrefs
    if not events:
        for cal_url in [
            "https://www.bklynlibrary.org/calendar",
            "https://www.bklynlibrary.org/events",
        ]:
            try:
                r = requests.get(cal_url, headers=headers, timeout=15)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")

                for el in soup.find_all(["article", "div", "li", "section"]):
                    classes = " ".join(el.get("class", []))
                    if not any(kw in classes.lower() for kw in
                               ["event", "program", "calendar", "views-row"]):
                        continue
                    text = el.get_text(" ", strip=True)
                    if len(text) < 30 or len(text) > 1000:
                        continue
                    # Extract first internal link found within this element
                    anchor = el.find("a", href=lambda h: h and "/event" in h)
                    if not anchor:
                        anchor = el.find("a", href=lambda h: h and h.startswith("/"))
                    href = anchor["href"] if anchor else ""
                    event_url = (f"https://www.bklynlibrary.org{href}"
                                 if href.startswith("/") else href)
                    is_central = (
                        "central" in text.lower() or
                        "grand army" in text.lower() or
                        "560 new york" in text.lower()
                    )
                    events.append({"text": text[:400], "url": event_url, "is_central": is_central})

                if events:
                    break
            except Exception:
                continue

    # Strategy 3: Eventbrite public search for BPL Central events
    if not events:
        try:
            r = requests.get(
                "https://www.eventbrite.com/d/ny--brooklyn/brooklyn-public-library/",
                headers=headers,
                timeout=15
            )
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                for el in soup.find_all(["article", "div"],
                                         class_=lambda c: c and "event" in str(c).lower()):
                    text = el.get_text(" ", strip=True)
                    anchor = el.find("a", href=True)
                    event_url = anchor["href"] if anchor else ""
                    if 30 < len(text) < 600:
                        events.append({"text": text[:400], "url": event_url, "is_central": True})
        except Exception:
            pass

    if not events:
        events = [{
            "text": "[BPL event data unavailable — check bklynlibrary.org/events directly]",
            "url": "https://www.bklynlibrary.org/events",
            "is_central": True
        }]

    return events[:30]


# ─────────────────────────────────────────────
# BROOKLYN CULTURE NEWS (RSS)
# ─────────────────────────────────────────────

# Slot 6: Bushwick Daily vs Bedford + Bowery — selected at runtime
SLOT_6_FEEDS = {
    "Bushwick Daily":   "https://bushwickdaily.com/feed/",
    "Bedford + Bowery": "https://bedfordandbowery.com/feed/",
}

# Feed priority tiers:
# Tier 1 — Brooklyn-focused
# Tier 2 — NYC-focused
# Tier 3 — National/international failsafe
NEWS_FEEDS = {
    # Tier 1: Brooklyn
    "Brooklyn Magazine": "https://brooklynmagazine.org/feed/",
    "The City":          "https://thecity.nyc/feed",
    "Streetsblog NYC":   "https://nyc.streetsblog.org/feed/",
    # Tier 2: NYC
    "Gothamist":         "https://gothamist.com/feed",
    "Hyperallergic":     "https://hyperallergic.com/feed/",
    "The Drift":         "https://thedriftmag.com/feed",
    # Tier 3: National failsafe
    "NPR":               "https://feeds.npr.org/1001/rss.xml",
}

# Keywords that strongly suggest Manhattan focus
MANHATTAN_KEYWORDS = [
    "manhattan", "midtown", "upper east side", "upper west side",
    "lower east side", "les ", "soho", "tribeca", "chelsea",
    "hell's kitchen", "harlem", "times square", "wall street",
    "financial district", "battery park", "east village", "west village",
    "greenwich village", "flatiron", "gramercy", "murray hill",
]

# Keywords that confirm Brooklyn focus
BROOKLYN_KEYWORDS = [
    "brooklyn", "bushwick", "williamsburg", "bedford", "crown heights",
    "bed-stuy", "bedford-stuyvesant", "flatbush", "park slope",
    "carroll gardens", "cobble hill", "red hook", "gowanus",
    "greenpoint", "sunset park", "bay ridge", "canarsie", "east new york",
    "brownsville", "prospect", "atlantic ave", "fulton", "nostrand",
    "kingston", "utica", "grand army",
]


def _parse_rss(url, source_name, max_items=8):
    """Fetch and parse an RSS or Atom feed. Follows redirects. Returns list of item dicts."""
    try:
        import feedparser as _fp
        feed = _fp.parse(url)
        # feedparser returns status 200/301/302 etc
        status = getattr(feed, 'status', 200)
        if status in (403, 404, 410):
            return []
        entries = feed.entries[:max_items]
        items = []
        for entry in entries:
            title_text = entry.get("title", "")
            link_text  = entry.get("link", "")
            # Try summary then content for description
            desc_raw = entry.get("summary", "") or ""
            if not desc_raw and entry.get("content"):
                desc_raw = entry["content"][0].get("value", "")
            desc_text = BeautifulSoup(desc_raw, "html.parser").get_text(strip=True)[:300]
            date_text = entry.get("published", "")

            combined = f"{title_text} {desc_text}".lower()
            is_brooklyn = any(kw in combined for kw in BROOKLYN_KEYWORDS)
            is_manhattan = any(kw in combined for kw in MANHATTAN_KEYWORDS) and not is_brooklyn

            items.append({
                "source":      source_name,
                "title":       title_text,
                "url":         link_text,
                "desc":        desc_text,
                "date":        date_text,
                "is_brooklyn": is_brooklyn,
                "is_manhattan": is_manhattan,
            })
        return items
    except Exception:
        return []


def _score_brooklyn_edginess(item):
    """
    Score an item for Brooklyn-edgy relevance when both slot-6 feeds
    are equally Brooklyn (or equally not). Higher = more relevant.
    """
    text = f"{item['title']} {item['desc']}".lower()
    edge_keywords = [
        "art", "music", "show", "gallery", "mural", "graffiti", "skate",
        "bike", "diy", "punk", "underground", "bar", "venue", "festival",
        "nightlife", "community", "protest", "neighborhood", "local",
        "housing", "gentrification", "landmark", "historic", "street",
    ]
    return sum(1 for kw in edge_keywords if kw in text)


def fetch_brooklyn_news():
    """
    Fetch items from all six feeds. For slot 6 (Bushwick Daily vs
    Bedford + Bowery), apply the selection logic:
      - Brooklyn-focused beats Manhattan-focused
      - If tied, pick the edgier/more-relevant one
      - If neither is Brooklyn-focused, pick the edgier one (surprise him)
    Returns a dict: {source_name: [items]} for the five confirmed slots,
    plus the winning slot-6 source.
    """
    results = {}

    # Fetch all tiered feeds
    for name, url in NEWS_FEEDS.items():
        items = _parse_rss(url, name)
        results[name] = items

    # Fetch both slot-6 candidates
    slot6_candidates = {}
    for name, url in SLOT_6_FEEDS.items():
        items = _parse_rss(url, name)
        slot6_candidates[name] = items

    # Selection logic for slot 6
    def brooklyn_score(items):
        return sum(1 for i in items if i["is_brooklyn"])

    def manhattan_score(items):
        return sum(1 for i in items if i["is_manhattan"])

    bd_items = slot6_candidates["Bushwick Daily"]
    bb_items = slot6_candidates["Bedford + Bowery"]

    bd_brooklyn = brooklyn_score(bd_items)
    bb_brooklyn = brooklyn_score(bb_items)
    bd_manhattan = manhattan_score(bd_items)
    bb_manhattan = manhattan_score(bb_items)

    # One is Brooklyn-focused, the other is not
    if bd_brooklyn > 0 and bb_manhattan > bb_brooklyn:
        winner = "Bushwick Daily"
    elif bb_brooklyn > 0 and bd_manhattan > bd_brooklyn:
        winner = "Bedford + Bowery"
    else:
        # Both Brooklyn or neither — pick edgier
        bd_edge = sum(_score_brooklyn_edginess(i) for i in bd_items[:5])
        bb_edge = sum(_score_brooklyn_edginess(i) for i in bb_items[:5])
        winner = "Bushwick Daily" if bd_edge >= bb_edge else "Bedford + Bowery"

    results[winner] = slot6_candidates[winner]
    results["_slot6_winner"] = winner

    return results


# ─────────────────────────────────────────────
# CLAUDE NARRATIVE LAYER
# ─────────────────────────────────────────────

def generate_narrative(
    saturday_data, sunday_data,
    sat_windows, sun_windows,
    mta_alerts, mta_rss,
    surface_alerts,
    market_result, market_list,
    bpl_events,
    brooklyn_news,
    client
):
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    saturday = today + datetime.timedelta(days=days_until_sat)
    sunday = saturday + datetime.timedelta(days=1)

    sat_str = saturday.strftime("%B %d")
    sun_str = sunday.strftime("%B %d")

    # Combine MTA alert sources
    all_mta = list(dict.fromkeys(mta_alerts + mta_rss))

    # Format surface-transit alerts (bus / AirTrain / LIRR). These are only
    # present in the data when a service is actually cut or altered.
    if surface_alerts:
        surface_lines = "\n".join(
            f"  - {a['service']}: {a['text']}" for a in surface_alerts
        )
    else:
        surface_lines = ("NONE — no disruptions found for the B65, B43, JFK AirTrain "
                         "(Jamaica/Howard Beach), or Nostrand Av LIRR. DO NOT MENTION "
                         "these services at all.")

    # Format walk windows — include forced_early flag for Claude
    def fmt_windows(windows):
        lines = []
        for w in windows:
            line = f"  {w['zone']} ({w['hour_label']}): {w['status']} — {w['note']}"
            if w.get("forced_early"):
                line += " [RAIN FORCES EARLIER START]"
            lines.append(line)
        return "\n".join(lines)

    # Format market list
    market_names = [m.get("marketname", "Unknown") for m in market_list[:5]]

    # Format BPL events — include URLs for Claude to weave in
    def fmt_bpl(events):
        lines = []
        for e in events[:20]:
            url_str = f" [URL: {e['url']}]" if e.get("url") else ""
            lines.append(f"{e['text']}{url_str}")
        return "\n---\n".join(lines)

    bpl_text = fmt_bpl(bpl_events)

    # Guard market temps against non-numeric fallback
    _avg = market_result.get("avg_temp")
    _peak = market_result.get("peak_temp")
    market_avg_str = f"{_avg:.0f}°F" if isinstance(_avg, (int, float)) else "?"
    market_peak_str = f"{_peak:.0f}°F" if isinstance(_peak, (int, float)) else "?"

    # Format news feeds for prompt
    slot6_winner = brooklyn_news.get("_slot6_winner", "Bushwick Daily")

    def fmt_feed(name, items):
        if not items:
            return f"{name}: [no items fetched]"
        lines = [f"{name}:"]
        for item in items[:5]:
            brooklyn_tag = " [BROOKLYN]" if item["is_brooklyn"] else (" [MANHATTAN]" if item["is_manhattan"] else "")
            lines.append(f"  • {item['title']}{brooklyn_tag}")
            if item["desc"]:
                lines.append(f"    {item['desc'][:150]}")
            if item["url"]:
                lines.append(f"    URL: {item['url']}")
        return "\n".join(lines)

    news_sections = []
    # Priority order: Brooklyn → NYC → National failsafe → Slot 6
    feed_order = [
        # Tier 1: Brooklyn-focused
        "Brooklyn Magazine", "The City", "Streetsblog NYC", slot6_winner,
        # Tier 2: NYC-focused
        "Gothamist", "Hyperallergic", "The Drift",
        # Tier 3: National failsafe
        "NPR",
    ]
    for name in feed_order:
        if name in brooklyn_news and brooklyn_news[name]:
            news_sections.append(fmt_feed(name, brooklyn_news[name]))

    news_text = "\n\n".join(news_sections)

    prompt = f"""You are writing the Crown Heights Weekend Dispatch — a single, tightly-written Saturday morning email for Zach, a Crown Heights resident with a black mini labradoodle named Mabel (~20 lbs).
He lives near Bergen/Dean and Kingston Ave. Grand Army Plaza Greenmarket is a 1-hour walk with Mabel.

Write in plain, direct prose. No markdown headers. No bullet lists. Five sections, each with a clear label line then 2–4 sentences of narrative. Be specific and useful, not generic.

=== WEATHER DATA ===
SATURDAY ({sat_str}):
  High: {saturday_data.get('high_f', '?')}°F  Low: {saturday_data.get('low_f', '?')}°F
  Conditions: {WMO_CODES.get(saturday_data.get('weather_code', 0), 'Unknown')}
  Max rain chance: {saturday_data.get('rain_pct_max', '?')}%

SATURDAY MABEL WALK WINDOWS:
{fmt_windows(sat_windows)}

SUNDAY ({sun_str}):
  High: {sunday_data.get('high_f', '?')}°F  Low: {sunday_data.get('low_f', '?')}°F
  Conditions: {WMO_CODES.get(sunday_data.get('weather_code', 0), 'Unknown')}
  Max rain chance: {sunday_data.get('rain_pct_max', '?')}%

SUNDAY MABEL WALK WINDOWS:
{fmt_windows(sun_windows)}

=== MTA SERVICE ALERTS ===
Stations to watch: Kingston-Throop Avenues (C), Nostrand Avenue (A/C),
Utica Avenue (A/C), Kingston Avenue (3), Crown Heights-Utica Avenue (3/4)

Raw alert data found:
{chr(10).join(all_mta) if all_mta else "No specific alerts found for these stations."}

Additional surface-transit services (CONDITIONAL — mention ONLY if disrupted):
B65 bus, B43 bus, JFK AirTrain (Jamaica and Howard Beach), Nostrand Av LIRR.
Disruption data found for these services:
{surface_lines}

=== FARMERS MARKET ===
Grand Army Plaza Greenmarket verdict: {market_result['verdict']}
Reason: {market_result['reason']}
Sustained average temp during outing window: {market_avg_str} (peak {market_peak_str})
Market hours: {market_result['market_hours']}
Other Brooklyn Saturday markets found: {', '.join(market_names)}

=== BPL EVENTS (Brooklyn Public Library — Central Branch, 560 New York Ave) ===
Upcoming events scraped (filter to top 3 most relevant to Zach's interests):

Zach's interest profile:
{INTEREST_PROFILE}

Event timing rules:
- Same-day Saturday events: must start at 1pm or later
- Weekday events: must start at 5:30pm or later
- Sunday events: any time
- Following Saturday: any time before 1pm is fine

Raw events data (URLs included — use them as hyperlinks in your output):
{bpl_text if bpl_text else "No events data available."}

=== BROOKLYN CULTURE NEWS ===
Items from six feeds. Each item tagged [BROOKLYN] or [MANHATTAN] where detectable.
Slot 6 this week: {slot6_winner}

Zach's interest profile for filtering (same as above — literary, NYC history, urbanism,
sci-fi, investigative journalism, bikes/surf/street culture, food, left politics):
{INTEREST_PROFILE}

{news_text}

=== INSTRUCTIONS ===

Write the dispatch in five labeled sections:

MTA THIS WEEKEND
PLANNED SERVICE CHANGES ONLY. Ignore real-time delays, signal problems, and incidents — those are not in the data. If the alerts data contains planned reroutes, skip-stop patterns, suspended service, or route changes for the A, C, or 3 lines, translate them into plain English: which line, which stations affected, which direction, and what to do instead. If no planned changes are found for the subway, say exactly: "No planned service changes for the A, C, or 3 this weekend."

Then, regarding the B65 bus, B43 bus, JFK AirTrain (Jamaica/Howard Beach), and Nostrand Av LIRR: these are CONDITIONAL. Mention a service ONLY if the "Disruption data found" block above contains an actual reroute, suspension, or service cut for it. If that block says NONE, or a given service is not listed there, do NOT mention that service at all — say nothing about the buses, AirTrain, or LIRR. When a disruption IS present, add a brief plain-English line for it (which service, what changed, what to do instead) after the subway info.

WEATHER + MABEL
2–3 sentences covering both days. Zach's default is leaving home between 10am and noon on weekends — he is not getting up at 6am unless forced. Work from that assumption. If the walk window data shows [RAIN FORCES EARLIER START], acknowledge that plainly and tell him rain is arriving mid-morning so he'll need to move if he wants to get Mabel out dry. If the 10am–noon window is just hot (not rainy), tell him what to expect at that hour and how to manage it with Mabel. Always include the evening window as a second option. Be specific about Saturday vs Sunday if they differ.

GRAND ARMY PLAZA GREENMARKET
1–2 sentences. Lead with the verdict. If no-go, suggest one concrete indoor Brooklyn fallback.

BROOKLYN PUBLIC LIBRARY
Up to 3 events matching his interests, applying timing rules. For each: event name, date, time, one sentence on why it's relevant, and a hyperlink formatted as HTML: <a href="URL">event name</a>. If no URL is available, omit the link. If nothing matches, say so honestly.

AROUND BROOKLYN
Surface 4–6 items across the feeds, grouped by source. Priority order:
- Tier 1 (Brooklyn-focused): Brooklyn Magazine, The City, Streetsblog NYC, {slot6_winner}
- Tier 2 (NYC-focused): Gothamist, Hyperallergic, The Drift
- Tier 3 (National failsafe): NPR — only use if nothing relevant in Tiers 1 and 2

Prioritize Tier 1 sources. Only move to Tier 2 if Tier 1 has fewer than 2 relevant items. Only use NPR if both Tier 1 and Tier 2 are sparse.

CRITICAL FORMATTING RULE: Group all items from the same source together. Use this exact format:

[SOURCE NAME]
<a href="URL">Headline</a> — One sentence on why it's worth his attention.
<a href="URL">Headline</a> — One sentence on why it's worth his attention.

[SOURCE NAME]
<a href="URL">Headline</a> — One sentence on why it's worth his attention.

Write in the voice of The New Yorker's Goings On — terse, specific, a little editorial. Keep the [SOURCE NAME] tags exactly as shown so the email template can parse and style them. Only include sources that actually have relevant items.

Write it as one cohesive email. Don't start with "Hello" or "Hi Zach." Just start with the first section label."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ─────────────────────────────────────────────
# EMAIL ASSEMBLY
# ─────────────────────────────────────────────

def build_email_html(narrative, saturday_data, sunday_data, saturday, sunday):
    sat_label = saturday.strftime("%A, %B %d")
    sun_label = sunday.strftime("%B %d")
    sat_date_top = saturday.strftime("%B %d")
    sun_date_top = sunday.strftime("%B %d")
    year = saturday.year

    # Parse narrative into labeled sections
    section_order = [
        "MTA THIS WEEKEND",
        "WEATHER + MABEL",
        "GRAND ARMY PLAZA GREENMARKET",
        "BROOKLYN PUBLIC LIBRARY",
        "AROUND BROOKLYN",
    ]
    section_labels_display = [
        ("MTA This Weekend", "i."),
        ("Weather + Mabel", "ii."),
        ("Grand Army Greenmarket", "iii."),
        ("Brooklyn Public Library", "iv."),
        ("Around Brooklyn", "v."),
    ]

    # Split narrative by section headers
    import re
    blocks = re.split(
        r'\n(?=' + '|'.join(re.escape(s) for s in section_order) + r')',
        narrative.strip()
    )

    parsed = {}
    for block in blocks:
        for key in section_order:
            if block.strip().upper().startswith(key):
                content = block.strip()[len(key):].strip().lstrip(':').strip()
                parsed[key] = content
                break

    # Build HTML sections
    html_sections = ""
    for i, key in enumerate(section_order):
        label, roman = section_labels_display[i]
        content = parsed.get(key, "")

        if key == "AROUND BROOKLYN":
            # Parse grouped [SOURCE] blocks into styled cards
            import re as _re
            items_html = ""
            # Split on lines that are just [SOURCE NAME]
            source_blocks = _re.split(r'\n(?=\[[^\]]+\]\s*\n)', content.strip())
            for block in source_blocks:
                block = block.strip()
                if not block:
                    continue
                # Extract source name from first line
                source_match = _re.match(r'^\[([^\]]+)\]\s*\n(.*)', block, _re.DOTALL)
                if source_match:
                    source = source_match.group(1).strip()
                    rest = source_match.group(2).strip()
                    # Each remaining line is a headline + blurb
                    headline_lines = [l.strip() for l in rest.split('\n') if l.strip()]
                    headlines_html = ""
                    for hl in headline_lines:
                        # Split on " — " to separate link from blurb
                        parts = hl.split(' — ', 1)
                        link_part = parts[0].strip()
                        blurb = parts[1].strip() if len(parts) > 1 else ""
                        headlines_html += f"""
              <div class="news-headline-row">
                <div class="news-hed">{link_part}</div>
                {"<div class='news-dek'>" + blurb + "</div>" if blurb else ""}
              </div>"""
                    items_html += f"""
          <div class="news-item">
            <div class="news-source">{source}</div>
            {headlines_html}
          </div>"""
                else:
                    # Fallback: try old single-line [SOURCE] format
                    old_match = _re.match(r'^\[([^\]]+)\]\s*(.*)', block, _re.DOTALL)
                    if old_match:
                        source = old_match.group(1).strip()
                        rest = old_match.group(2).strip()
                        lines = rest.split('\n', 1)
                        headline_html = lines[0].strip()
                        blurb = lines[1].strip() if len(lines) > 1 else ""
                        items_html += f"""
          <div class="news-item">
            <div class="news-source">{source}</div>
            <div class="news-hed">{headline_html}</div>
            {"<div class='news-dek'>" + blurb + "</div>" if blurb else ""}
          </div>"""
                    else:
                        items_html += f"<p>{block}</p>"

            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          <div class="news-feed">{items_html}</div>
        </div>"""
        else:
            paras = [p.strip() for p in content.split("\n\n") if p.strip()]
            paras_html = "".join(f"<p>{p}</p>" for p in paras) if paras else f"<p>{content}</p>"
            html_sections += f"""
        <div class="b4-section">
          <div class="b4-label-row">
            <span class="b4-label">{label}</span>
            <div class="b4-label-rule"></div>
            <span class="b4-label-roman">{roman}</span>
          </div>
          {paras_html}
        </div>"""

    # Pigeon SVG definition (reused via <use>)
    pigeon_defs = """<defs>
      <g id="pp">
        <path d="M40 0 C44 8 46 14 40 18 C46 16 52 20 52 28 C52 36 44 42 32 42 C20 42 8 36 6 28 L0 36 L8 30 C4 28 2 22 6 16 C10 10 18 6 26 8 C28 4 34 -2 40 0Z" fill="#8A6A3A"/>
        <path d="M34 2 L38 -2 L36 4Z" fill="#8A6A3A"/>
        <circle cx="36" cy="4" r="2.5" fill="#F5EDD8" opacity="0.9"/>
        <circle cx="36" cy="4" r="1.2" fill="#8A6A3A"/>
        <path d="M10 26 Q22 22 36 26" stroke="#6A5030" stroke-width="1.8" fill="none" stroke-linecap="round" opacity="0.7"/>
        <path d="M11 30 Q23 26 37 30" stroke="#6A5030" stroke-width="1.2" fill="none" stroke-linecap="round" opacity="0.5"/>
        <line x1="18" y1="42" x2="16" y2="52" stroke="#8A6A3A" stroke-width="2.2" stroke-linecap="round"/>
        <line x1="26" y1="43" x2="24" y2="53" stroke="#8A6A3A" stroke-width="2.2" stroke-linecap="round"/>
        <path d="M12 52 L16 52 L20 52" stroke="#8A6A3A" stroke-width="1.8" stroke-linecap="round"/>
        <path d="M16 52 L16 56" stroke="#8A6A3A" stroke-width="1.8" stroke-linecap="round"/>
        <path d="M20 53 L24 53 L28 53" stroke="#8A6A3A" stroke-width="1.8" stroke-linecap="round"/>
        <path d="M24 53 L24 57" stroke="#8A6A3A" stroke-width="1.8" stroke-linecap="round"/>
      </g>
    </defs>"""

    pigeon_uses = """
    <use href="#pp" transform="translate(30,30) scale(0.55)" opacity="0.08"/>
    <use href="#pp" transform="translate(185,20) scale(0.45)" opacity="0.07"/>
    <use href="#pp" transform="translate(360,35) scale(0.6)" opacity="0.08"/>
    <use href="#pp" transform="translate(510,15) scale(0.5)" opacity="0.07"/>
    <use href="#pp" transform="translate(80,185) scale(0.5)" opacity="0.07"/>
    <use href="#pp" transform="translate(270,195) scale(0.55)" opacity="0.08"/>
    <use href="#pp" transform="translate(450,180) scale(0.45)" opacity="0.07"/>
    <use href="#pp" transform="translate(20,345) scale(0.6)" opacity="0.08"/>
    <use href="#pp" transform="translate(200,355) scale(0.48)" opacity="0.07"/>
    <use href="#pp" transform="translate(390,340) scale(0.55)" opacity="0.08"/>
    <use href="#pp" transform="translate(530,360) scale(0.44)" opacity="0.07"/>
    <use href="#pp" transform="translate(100,510) scale(0.52)" opacity="0.07"/>
    <use href="#pp" transform="translate(310,520) scale(0.58)" opacity="0.08"/>
    <use href="#pp" transform="translate(480,505) scale(0.46)" opacity="0.07"/>
    <use href="#pp" transform="translate(40,665) scale(0.56)" opacity="0.08"/>
    <use href="#pp" transform="translate(230,675) scale(0.5)" opacity="0.07"/>
    <use href="#pp" transform="translate(430,660) scale(0.54)" opacity="0.08"/>
    <use href="#pp" transform="translate(545,680) scale(0.42)" opacity="0.07"/>
    <use href="#pp" transform="translate(120,825) scale(0.53)" opacity="0.07"/>
    <use href="#pp" transform="translate(340,835) scale(0.57)" opacity="0.08"/>
    <use href="#pp" transform="translate(510,820) scale(0.47)" opacity="0.07"/>
    <use href="#pp" transform="translate(25,985) scale(0.58)" opacity="0.08"/>
    <use href="#pp" transform="translate(200,995) scale(0.5)" opacity="0.07"/>
    <use href="#pp" transform="translate(390,980) scale(0.55)" opacity="0.08"/>
    <use href="#pp" transform="translate(540,1000) scale(0.44)" opacity="0.07"/>
    <use href="#pp" transform="translate(85,1140) scale(0.52)" opacity="0.07"/>
    <use href="#pp" transform="translate(290,1150) scale(0.56)" opacity="0.08"/>
    <use href="#pp" transform="translate(470,1135) scale(0.48)" opacity="0.07"/>
    <use href="#pp" transform="translate(35,1295) scale(0.54)" opacity="0.08"/>
    <use href="#pp" transform="translate(215,1305) scale(0.5)" opacity="0.07"/>
    <use href="#pp" transform="translate(415,1290) scale(0.57)" opacity="0.08"/>
    <use href="#pp" transform="translate(545,1310) scale(0.43)" opacity="0.07"/>
    <use href="#pp" transform="translate(100,1430) scale(0.5)" opacity="0.06"/>
    <use href="#pp" transform="translate(310,1435) scale(0.46)" opacity="0.06"/>
    <use href="#pp" transform="translate(490,1425) scale(0.52)" opacity="0.06"/>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400;1,600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
  body {{ margin: 0; padding: 16px; background: #EDE0C0; }}
  .b4-wrap {{
    max-width: 600px;
    margin: 0 auto;
    background: #F5EDD8;
    color: #1A1208;
    font-family: 'Cormorant Garamond', Georgia, serif;
    border: 1px solid #D4B98A;
    position: relative;
    overflow: hidden;
  }}
  .pigeons-global {{
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    width: 100%;
    height: 100%;
  }}
  .b4-header {{
    padding: 24px 32px 0;
    background: #EDE0C0;
    position: relative;
  }}
  .b4-eyebrow {{
    font-family: 'Courier Prime', monospace;
    font-size: 8.5px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #8A6A3A;
    margin-bottom: 8px;
    position: relative;
    z-index: 1;
    display: flex;
    justify-content: space-between;
  }}
  .b4-title-row {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
    position: relative;
    z-index: 1;
  }}
  .b4-title {{
    font-family: 'Cormorant Garamond', Georgia, serif;
    font-weight: 300;
    font-style: italic;
    font-size: 58px;
    line-height: 0.9;
    color: #1A1208;
    margin: 0;
    letter-spacing: -0.01em;
  }}
  .b4-title em {{ color: #9B3A1A; font-style: normal; }}
  .b4-date {{
    font-family: 'Courier Prime', monospace;
    font-size: 9.5px;
    letter-spacing: 0.14em;
    color: #8A6A3A;
    line-height: 1.9;
    text-transform: uppercase;
    text-align: right;
    position: relative;
    z-index: 1;
  }}
  .b4-rule {{ width: 32px; height: 2px; background: #9B3A1A; margin: 10px 0 0; position: relative; z-index: 1; }}
  .b4-header-bottom-rule {{ height: 1px; background: #C4A46A; margin-top: 12px; position: relative; z-index: 1; }}
  .b4-body {{ padding: 0 32px 28px; position: relative; z-index: 1; }}
  .b4-section {{ padding: 20px 0 18px; border-bottom: 1px solid #D4B98A; }}
  .b4-section:last-child {{ border-bottom: none; }}
  .b4-label-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }}
  .b4-label {{ font-family: 'Courier Prime', monospace; font-size: 8px; letter-spacing: 0.26em; text-transform: uppercase; color: #9B3A1A; white-space: nowrap; }}
  .b4-label-rule {{ flex: 1; height: 1px; background: #D4B98A; }}
  .b4-label-roman {{ font-family: 'Cormorant Garamond', Georgia, serif; font-style: italic; font-size: 12px; color: #B89A5A; }}
  .b4-section p {{ font-family: 'Cormorant Garamond', Georgia, serif; font-size: 16.5px; line-height: 1.72; color: #2A1E0A; margin: 0 0 12px; font-weight: 400; }}
  .b4-section p:last-child {{ margin-bottom: 0; }}
  .b4-section a {{ color: #9B3A1A; }}
  .news-feed {{ display: flex; flex-direction: column; gap: 0; }}
  .news-item {{ padding: 12px 0 12px 14px; border-left: 2px solid #9B3A1A; margin-bottom: 14px; }}
  .news-item:last-child {{ margin-bottom: 0; }}
  .news-source {{ font-family: 'Courier Prime', monospace; font-size: 8px; letter-spacing: 0.22em; text-transform: uppercase; color: #B89A5A; margin-bottom: 8px; }}
  .news-headline-row {{ margin-bottom: 8px; }}
  .news-headline-row:last-child {{ margin-bottom: 0; }}
  .news-hed {{ font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 600; font-size: 15px; line-height: 1.3; color: #1A1208; margin-bottom: 5px; }}
  .news-hed a {{ color: #1A1208; text-decoration: none; border-bottom: 1px solid #D4B98A; }}
  .news-dek {{ font-family: 'Cormorant Garamond', Georgia, serif; font-style: italic; font-size: 13.5px; color: #6A5030; line-height: 1.55; }}
  .b4-footer {{
    background: #1A1208;
    padding: 16px 32px;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    position: relative;
    z-index: 1;
  }}
  .b4-footer-left {{ display: flex; flex-direction: column; gap: 3px; }}
  .b4-footer-eyebrow {{ font-family: 'Courier Prime', monospace; font-size: 7.5px; letter-spacing: 0.16em; text-transform: uppercase; color: #9B8A5A; }}
  .b4-signature {{ font-family: 'Cormorant Garamond', Georgia, serif; font-style: italic; font-size: 20px; color: #D4B98A; line-height: 1; margin-top: 2px; }}
  .b4-tagline {{ font-family: 'Courier Prime', monospace; font-size: 7.5px; letter-spacing: 0.13em; text-transform: uppercase; color: #9B8A5A; margin-top: 2px; }}
</style>
</head>
<body>
<div class="b4-wrap">

  <svg class="pigeons-global" viewBox="0 0 600 1500" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg">
    {pigeon_defs}
    {pigeon_uses}
  </svg>

  <div class="b4-header">
    <div class="b4-eyebrow">
      <span>Weekend Dispatch</span>
      <span>Crown Heights &middot; Brooklyn</span>
    </div>
    <div class="b4-title-row">
      <h1 class="b4-title">The<br><em>Week</em>end.</h1>
      <div class="b4-date">
        Saturday<br>{sat_date_top}<br>&mdash;&mdash;<br>Sunday<br>{sun_date_top}<br>{year}
      </div>
    </div>
    <div class="b4-rule"></div>
    <div class="b4-header-bottom-rule"></div>
  </div>

  <div class="b4-body">
    {html_sections}
  </div>

  <div class="b4-footer">
    <div class="b4-footer-left">
      <span class="b4-footer-eyebrow">Crown Heights &middot; Brooklyn, N.Y. &middot; {year}</span>
      <span class="b4-signature">Zachary Tharpe</span>
      <span class="b4-tagline">A personal dispatch &mdash; written by machines, curated by instinct.</span>
    </div>
  </div>

</div>
</body>
</html>"""
    return html


def save_newsletter_html(html, saturday):
    """
    Save newsletter as weekend.html in the current directory (git repo),
    commit and push to GitHub, then return the GitHub Pages URL.
    Falls back to local file:// URL if GITHUB_PAGES_URL not set.
    """
    import pathlib
    import subprocess

    # Save to current working directory as weekend.html
    path = pathlib.Path.cwd() / "weekend.html"
    path.write_text(html, encoding="utf-8")
    print(f"  Newsletter saved: {path}")

    # Git config for GitHub Actions environment
    subprocess.run(["git", "config", "user.email", "actions@github.com"], check=False)
    subprocess.run(["git", "config", "user.name", "GitHub Actions"], check=False)

    # Commit and push
    try:
        subprocess.run(["git", "add", "weekend.html"], check=True)
        subprocess.run(["git", "commit", "-m", "Weekend Dispatch update"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("  Pushed weekend.html to GitHub")
    except subprocess.CalledProcessError as e:
        print(f"  Git push skipped or failed: {e}")

    # Return GitHub Pages URL if set, otherwise local file path
    pages_url = os.environ.get("GITHUB_PAGES_URL")
    if pages_url:
        return pages_url
    return f"file://{path}"


def build_envelope_email(saturday, sunday, newsletter_url):
    """
    Static envelope email — no JavaScript. Renders pre-opened in Gmail:
    flap folded back, seal as decoration, newsletter peek always visible,
    prominent link to the full newsletter.
    """
    sat_label = saturday.strftime("%B %d")
    sun_label = sunday.strftime("%B %d")
    sat_day   = saturday.strftime("%A")
    year      = saturday.year

    html = f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;1,300;1,400&family=Courier+Prime:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {{ color-scheme: light only; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #2A1E0A !important;
    margin: 0 !important;
    padding: 32px 16px !important;
    font-family: 'Cormorant Garamond', Georgia, serif !important;
    -webkit-text-size-adjust: 100%;
  }}
  .scene {{
    max-width: 620px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    align-items: center;
  }}
  .pre-label {{
    font-family: 'Courier Prime', monospace;
    font-size: 9px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #6A5030;
    text-align: center;
    margin-bottom: 22px;
  }}

  /* ── ENVELOPE (realistic, pre-opened, single SVG) ── */
  .env-wrap {{
    width: 100% !important;
    filter: drop-shadow(0 22px 38px rgba(40,28,10,0.42)) drop-shadow(0 4px 10px rgba(40,28,10,0.3));
  }}
  .env-svg {{
    width: 100% !important;
    display: block !important;
  }}

  /* ── NEWSLETTER PEEK (always visible below envelope) ── */
  .newsletter-peek {{
    width: calc(100% - 48px) !important;
    background: #F7F0DC !important;
    border: 1px solid #D4B98A !important;
    border-top: none !important;
    padding: 22px 26px !important;
    margin-top: -4px !important;
    position: relative !important;
    box-shadow: 0 14px 36px rgba(0,0,0,0.32) !important;
  }}
  /* Force light mode — prevent Gmail dark mode inversion */
  [data-ogsc] .newsletter-peek {{ background: #F7F0DC !important; color: #1A1208 !important; }}
  [data-ogsc] body {{ background: #2A1E0A !important; }}
  [data-ogsc] .peek-text {{ color: #2A1E0A !important; }}
  [data-ogsc] .peek-title {{ color: #1A1208 !important; }}
  [data-ogsc] .cta-btn {{ background: #1A1208 !important; color: #F5EDD8 !important; }}
  @media (prefers-color-scheme: dark) {{
    .newsletter-peek {{ background: #F7F0DC !important; color: #1A1208 !important; }}
    body {{ background: #2A1E0A !important; }}
    .peek-text {{ color: #2A1E0A !important; }}
    .peek-title {{ color: #1A1208 !important; }}
    .cta-btn {{ background: #1A1208 !important; color: #F5EDD8 !important; }}
  }}

  .peek-eyebrow {{
    font-family: 'Courier Prime', monospace;
    font-size: 7.5px;
    letter-spacing: 0.28em;
    text-transform: uppercase;
    color: #8A6A3A;
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
  }}

  .peek-title {{
    font-family: 'Cormorant Garamond', Georgia, serif;
    font-weight: 300;
    font-style: italic;
    font-size: 44px;
    line-height: 0.88;
    color: #1A1208;
    margin-bottom: 12px;
  }}
  .peek-title em {{ color: #9B3A1A; font-style: normal; }}

  .peek-rule {{ height: 1px; background: #C4A46A; margin: 10px 0 14px; }}

  .peek-section-label {{
    font-family: 'Courier Prime', monospace;
    font-size: 7.5px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: #9B3A1A;
    margin-bottom: 6px;
  }}

  .peek-text {{
    font-family: 'Cormorant Garamond', Georgia, serif;
    font-size: 15px;
    line-height: 1.7;
    color: #2A1E0A;
    margin-bottom: 16px;
  }}

  /* CTA button */
  .cta-wrap {{
    text-align: center;
    padding-top: 4px;
  }}

  .cta-btn {{
    display: inline-block;
    font-family: 'Courier Prime', monospace;
    font-size: 9px;
    letter-spacing: 0.24em;
    text-transform: uppercase;
    color: #F5EDD8;
    background: #1A1208;
    text-decoration: none;
    padding: 12px 28px;
    border: 1px solid #3A2A10;
  }}

  .bottom-note {{
    font-family: 'Courier Prime', monospace;
    font-size: 8px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: #3A2A10;
    text-align: center;
    margin-top: 22px;
  }}
</style>
</head>
<body>
<div class="scene">
  <div class="pre-label">Your weekend dispatch has arrived</div>

  <!-- Realistic envelope: single SVG. Flap folded back above body;
       side + bottom panels fold to center; pressed wax seal at junction. -->
  <div class="env-wrap">
    <svg class="env-svg" viewBox="0 0 620 400" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="paper" x1="0" y1="0" x2="0.35" y2="1">
          <stop offset="0" stop-color="#F4E9CD"/>
          <stop offset="0.5" stop-color="#ECDFBE"/>
          <stop offset="1" stop-color="#E2D2AC"/>
        </linearGradient>
        <linearGradient id="sideL" x1="0" y1="0" x2="1" y2="0.4">
          <stop offset="0" stop-color="#E6D7B2"/><stop offset="1" stop-color="#D2C098"/>
        </linearGradient>
        <linearGradient id="sideR" x1="1" y1="0" x2="0" y2="0.4">
          <stop offset="0" stop-color="#E6D7B2"/><stop offset="1" stop-color="#D2C098"/>
        </linearGradient>
        <linearGradient id="bottomP" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#D8C7A0"/><stop offset="1" stop-color="#E0D0AA"/>
        </linearGradient>
        <linearGradient id="flap" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#F6ECD2"/><stop offset="1" stop-color="#E4D4AE"/>
        </linearGradient>
        <linearGradient id="flapShadow" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#B49A66" stop-opacity="0.55"/>
          <stop offset="1" stop-color="#B49A66" stop-opacity="0"/>
        </linearGradient>
        <radialGradient id="waxBody" cx="0.36" cy="0.30" r="0.85">
          <stop offset="0" stop-color="#C75732"/>
          <stop offset="0.4" stop-color="#A23F1D"/>
          <stop offset="0.78" stop-color="#822F13"/>
          <stop offset="1" stop-color="#5E200B"/>
        </radialGradient>
        <radialGradient id="waxDish" cx="0.5" cy="0.42" r="0.6">
          <stop offset="0" stop-color="#6A2710" stop-opacity="0.55"/>
          <stop offset="0.6" stop-color="#6A2710" stop-opacity="0.12"/>
          <stop offset="1" stop-color="#6A2710" stop-opacity="0"/>
        </radialGradient>
        <radialGradient id="waxRim" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0.74" stop-color="#000000" stop-opacity="0"/>
          <stop offset="0.93" stop-color="#5A1F0A" stop-opacity="0.5"/>
          <stop offset="1" stop-color="#3E1305" stop-opacity="0.65"/>
        </radialGradient>
        <filter id="sealShadow" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur in="SourceAlpha" stdDeviation="4"/>
          <feOffset dx="0" dy="4" result="o"/>
          <feComponentTransfer><feFuncA type="linear" slope="0.45"/></feComponentTransfer>
          <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>

      <!-- ENVELOPE BODY -->
      <rect x="6" y="74" width="608" height="318" rx="7" fill="url(#paper)" stroke="#C2A668" stroke-width="1.5"/>

      <!-- side + bottom folds meet at center (310,224) -->
      <polygon points="6,76 6,390 310,224" fill="url(#sideL)" opacity="0.92"/>
      <polygon points="614,76 614,390 310,224" fill="url(#sideR)" opacity="0.92"/>
      <polygon points="6,390 614,390 310,224" fill="url(#bottomP)" opacity="0.95"/>
      <line x1="6" y1="76" x2="310" y2="224" stroke="#BBA269" stroke-width="1" opacity="0.5"/>
      <line x1="614" y1="76" x2="310" y2="224" stroke="#BBA269" stroke-width="1" opacity="0.5"/>
      <line x1="6" y1="390" x2="310" y2="224" stroke="#B59C61" stroke-width="1" opacity="0.55"/>
      <line x1="614" y1="390" x2="310" y2="224" stroke="#B59C61" stroke-width="1" opacity="0.55"/>

      <!-- shadow the open flap casts onto the body top -->
      <rect x="7" y="75" width="606" height="66" fill="url(#flapShadow)" opacity="0.8"/>

      <!-- OPEN FLAP (folded back, above the top edge) -->
      <polygon points="6,76 614,76 310,4" fill="url(#flap)" stroke="#C2A668" stroke-width="1.5"/>
      <polygon points="6,76 614,76 310,24" fill="#D8C9A2" opacity="0.4"/>
      <line x1="6" y1="76" x2="310" y2="4" stroke="#CBB376" stroke-width="1" opacity="0.6"/>
      <line x1="614" y1="76" x2="310" y2="4" stroke="#CBB376" stroke-width="1" opacity="0.6"/>

      <!-- RETURN ADDRESS (aligned with stamp band) -->
      <text x="34" y="194" font-family="'Courier Prime', monospace" font-size="11" letter-spacing="1.5" fill="#7A5A28">13 REVERE PL</text>
      <text x="34" y="212" font-family="'Courier Prime', monospace" font-size="11" letter-spacing="1.5" fill="#7A5A28">CROWN HEIGHTS, BROOKLYN</text>
      <text x="34" y="230" font-family="'Courier Prime', monospace" font-size="11" letter-spacing="1.5" fill="#7A5A28">NEW YORK, N.Y. 11213</text>

      <!-- POSTMARK -->
      <g transform="translate(486,184) rotate(-10)" opacity="0.4">
        <circle cx="23" cy="23" r="21" stroke="#6A5030" stroke-width="2" fill="none"/>
        <text x="23" y="16" font-family="monospace" font-size="5" fill="#6A5030" text-anchor="middle" letter-spacing="1">CROWN</text>
        <text x="23" y="23" font-family="monospace" font-size="5" fill="#6A5030" text-anchor="middle" letter-spacing="1">HEIGHTS</text>
        <text x="23" y="30" font-family="monospace" font-size="5" fill="#6A5030" text-anchor="middle" letter-spacing="1">NY</text>
        <text x="23" y="37" font-family="monospace" font-size="5" fill="#6A5030" text-anchor="middle" letter-spacing="1">{sat_label}</text>
      </g>

      <!-- STAMP -->
      <g transform="translate(548,178)">
        <rect x="0" y="0" width="52" height="62" fill="#F7F0DC" stroke="#C4A46A" stroke-width="1"/>
        <g transform="translate(2,6)" opacity="0.8">
          <path d="M40 0 C44 8 46 14 40 18 C46 16 52 20 52 28 C52 36 44 42 32 42 C20 42 8 36 6 28 L0 36 L8 30 C4 28 2 22 6 16 C10 10 18 6 26 8 C28 4 34 -2 40 0Z" fill="#8A6A3A"/>
          <circle cx="36" cy="4" r="2.5" fill="#F7F0DC" opacity="0.9"/>
          <circle cx="36" cy="4" r="1.2" fill="#8A6A3A"/>
          <path d="M10 26 Q22 22 36 26" stroke="#6A5030" stroke-width="1.8" fill="none" stroke-linecap="round" opacity="0.7"/>
        </g>
        <text x="26" y="56" font-family="'Courier Prime', monospace" font-size="6" fill="#8A6A3A" text-anchor="middle" letter-spacing="1.2">BROOKLYN</text>
      </g>

      <!-- ADDRESSEE -->
      <text x="310" y="362" font-family="'Cormorant Garamond', Georgia, serif" font-style="italic" font-size="21" fill="#5C4422" text-anchor="middle" letter-spacing="1.5">For Zachary Tharpe</text>

      <!-- PRESSED WAX SEAL (center over fold junction) -->
      <g filter="url(#sealShadow)">
        <path d="M310 184 C322 183 331 188 337 191 C345 188 351 192 352 199 C361 201 365 208 362 215 C368 221 367 230 360 234 C362 242 356 249 348 248 C345 256 336 259 328 255 C321 261 311 261 305 256 C297 261 287 258 284 250 C275 251 268 244 270 236 C263 232 261 222 267 217 C262 209 267 200 275 199 C276 191 284 186 292 188 C297 183 304 183 310 184 Z" fill="url(#waxBody)" stroke="#5E200B" stroke-width="1" stroke-opacity="0.4"/>
        <circle cx="313" cy="222" r="36" fill="url(#waxRim)"/>
        <circle cx="313" cy="222" r="27" fill="none" stroke="#5A2310" stroke-width="2" opacity="0.45"/>
        <circle cx="313" cy="222" r="27" fill="none" stroke="#E08A60" stroke-width="0.8" opacity="0.3" transform="translate(-1,-1)"/>
        <circle cx="313" cy="222" r="25" fill="url(#waxDish)"/>
        <ellipse cx="298" cy="204" rx="11" ry="6" fill="#E8916B" opacity="0.45" transform="rotate(-28 298 204)"/>
        <text x="314.5" y="231" font-family="'Cormorant Garamond', Georgia, serif" font-style="italic" font-size="22" fill="#4A1A08" opacity="0.6" text-anchor="middle">W.D.</text>
        <text x="313" y="229.5" font-family="'Cormorant Garamond', Georgia, serif" font-style="italic" font-size="22" fill="#E9B89C" text-anchor="middle">W.D.</text>
      </g>
    </svg>
  </div>

  <!-- Newsletter peek — always visible -->
  <div class="newsletter-peek">
    <div class="peek-eyebrow">
      <span>Weekend Dispatch</span>
      <span>Crown Heights &middot; Brooklyn</span>
    </div>
    <div class="peek-title">The<br><em>Week</em>end.</div>
    <div class="peek-rule"></div>
    <div class="peek-section-label">{sat_day}, {sat_label} &mdash; {sun_label} &middot; {year}</div>
    <p class="peek-text">Your full dispatch is ready &mdash; weather &amp; Mabel walk windows, the Greenmarket verdict, BPL events, and what&rsquo;s going on around Brooklyn this weekend.</p>
    <div class="cta-wrap">
      <a class="cta-btn" href="{newsletter_url}">&darr;&nbsp;&nbsp;Open Full Newsletter</a>
    </div>
  </div>

  <div class="bottom-note">Weekend Dispatch &middot; Crown Heights &middot; {year}</div>
</div>
</body>
</html>"""
    return html


def send_email(html_body, saturday):
    sender = os.environ["DISPATCH_EMAIL"]
    password = os.environ["DISPATCH_APP_PASSWORD"]
    recipient = os.environ["DISPATCH_TO"]

    sat_label = saturday.strftime("%B %d")
    subject = f"Weekend Dispatch · {sat_label}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("Initializing Weekend Dispatch...")

    # Check env vars
    required = ["ANTHROPIC_API_KEY", "DISPATCH_EMAIL", "DISPATCH_APP_PASSWORD", "DISPATCH_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"⚠️  Missing env vars: {', '.join(missing)}")
        print("   Set them and re-run. For local testing, you can export them in your shell.")
        return

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Determine weekend dates
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    saturday = today + datetime.timedelta(days=days_until_sat)
    sunday = saturday + datetime.timedelta(days=1)

    print(f"Covering weekend: {saturday.strftime('%b %d')} – {sunday.strftime('%b %d')}")

    # 1. Weather
    print("Fetching weather...")
    weather_data = fetch_weekend_weather()
    weekend = parse_weekend_days(weather_data)
    saturday_data = weekend.get("saturday", {})
    sunday_data = weekend.get("sunday", {})

    # 2. Walk windows
    print("Computing Mabel walk windows...")
    sat_windows = get_walk_windows(saturday_data, "saturday") if saturday_data else []
    sun_windows = get_walk_windows(sunday_data, "sunday") if sunday_data else []

    # 3. MTA
    print("Fetching MTA alerts...")
    mta_alerts = fetch_mta_alerts()
    mta_rss = fetch_mta_rss_alerts()
    surface_alerts = fetch_surface_transit_alerts()
    if surface_alerts:
        print(f"  Surface-transit disruptions: "
              f"{', '.join(sorted({a['service'] for a in surface_alerts}))}")
    else:
        print("  No B65/B43/AirTrain/LIRR disruptions — services omitted")

    # 4. Farmers market
    print("Fetching market data...")
    market_list = fetch_market_data()
    market_result = market_go_nogo(saturday_data, market_list)

    # 5. BPL events
    print("Scraping BPL events...")
    bpl_events = fetch_bpl_events()

    # 6. Brooklyn culture news
    print("Fetching Brooklyn news feeds...")
    brooklyn_news = fetch_brooklyn_news()
    slot6 = brooklyn_news.get("_slot6_winner", "?")
    print(f"  Slot 6 winner: {slot6}")

    # 7. Claude narrative
    print("Generating narrative...")
    narrative = generate_narrative(
        saturday_data, sunday_data,
        sat_windows, sun_windows,
        mta_alerts, mta_rss,
        surface_alerts,
        market_result, market_list,
        bpl_events,
        brooklyn_news,
        client
    )

    # 8. Build newsletter HTML + save locally
    print("Building newsletter...")
    newsletter_html = build_email_html(narrative, saturday_data, sunday_data, saturday, sunday)
    newsletter_url = save_newsletter_html(newsletter_html, saturday)
    print(f"  Newsletter saved: {newsletter_url}")

    # 9. Build envelope email + send
    print("Building envelope email...")
    envelope_html = build_envelope_email(saturday, sunday, newsletter_url)

    print("Sending...")
    send_email(envelope_html, saturday)

    sat_label = saturday.strftime("%B %d")
    print(f"✓ Weekend Dispatch sent for {sat_label} – {sunday.strftime('%B %d')}")
    print()
    print("─── NARRATIVE PREVIEW ───")
    print(narrative)


if __name__ == "__main__":
    main()
