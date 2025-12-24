#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import math
import socket
import ssl
import time
import uuid
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Optional geopy import (script still works without it)
try:
    from geopy.geocoders import Nominatim
except ImportError:
    Nominatim = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SANTA_INFO_URL = (
    "https://santa-api.appspot.com/info"
    "?client=web&language=en&fingerprint=&routeOffset=0&streamOffset=0"
)

DEFAULT_MULTICAST_IP = "239.2.3.1"
DEFAULT_PORT = 6969

# Offline CSV (Natural Earth populated places) in same folder as script
OFFLINE_CSV_PATH = Path(__file__).with_name("ne_50m_populated_places.csv")

# In-memory caches
OFFLINE_INDEX = None  # loaded from CSV
LOOKUP_CACHE = {}     # per-destination cache

# UUIDs for Santa and for persistent destination markers while script runs
SANTA_UUID = "SANTA"
DESTINATION_UUIDS = {}   # raw_id -> UUID
RB_UUID = str(uuid.uuid4())  # single persistent Range & Bearing line UID

# Init geopy geolocator if available
GEOLOCATOR = Nominatim(user_agent="tak_santa_tracker") if Nominatim is not None else None

# US state and Canadian province abbreviations
STATE_PROVINCE_CODES = {
    # United States
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",

    # U.S. territories
    "puerto rico": "PR", "guam": "GU", "american samoa": "AS",
    "u.s. virgin islands": "VI", "northern mariana islands": "MP",

    # Canada
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "nova scotia": "NS", "ontario": "ON", "prince edward island": "PE",
    "quebec": "QC", "saskatchewan": "SK",
    "northwest territories": "NT", "nunavut": "NU", "yukon": "YT",
}


# ---------------------------------------------------------------------------
# Output Senders (UDP multicast, TCP, TLS)
# ---------------------------------------------------------------------------

class SenderBase:
    def open(self): ...
    def close(self): ...
    def send(self, xml_text: str): ...
    def __enter__(self):
        self.open()
        return self
    def __exit__(self, exc_type, exc, tb):
        self.close()


class UdpMulticastSender(SenderBase):
    """
    UDP multicast sender.
    - bind_ip: local IP to bind the socket to (optional)
    - iface_ip: local interface used for multicast (optional; often "0.0.0.0" works)
    """
    def __init__(self, mcast_ip: str, port: int, bind_ip: str | None, iface_ip: str, ttl: int = 1):
        self.mcast_ip = mcast_ip
        self.port = port
        self.bind_ip = bind_ip
        self.iface_ip = iface_ip
        self.ttl = ttl
        self.sock: socket.socket | None = None

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        bind_addr = self.bind_ip if self.bind_ip else "0.0.0.0"
        self.sock.bind((bind_addr, 0))

        # set multicast interface
        try:
            self.sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.iface_ip)
            )
        except OSError:
            # If iface_ip isn't valid on this system, fall back
            self.sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton("0.0.0.0")
            )

        # TTL (1 = local subnet)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send(self, xml_text: str):
        if not self.sock:
            raise RuntimeError("UDP sender not opened")
        self.sock.sendto(xml_text.encode("utf-8"), (self.mcast_ip, self.port))


class TcpSender(SenderBase):
    """Plain TCP sender (newline-delimited by default)."""
    def __init__(self, host: str, port: int, bind_ip: str | None, timeout: float = 5.0, newline: bool = True):
        self.host = host
        self.port = port
        self.bind_ip = bind_ip
        self.timeout = timeout
        self.newline = newline
        self.sock: socket.socket | None = None

    def open(self):
        self._connect()

    def _connect(self):
        self.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        if self.bind_ip:
            s.bind((self.bind_ip, 0))
        s.connect((self.host, self.port))
        self.sock = s

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send(self, xml_text: str):
        if not self.sock:
            self._connect()

        payload = xml_text.encode("utf-8")
        if self.newline:
            payload += b"\n"

        try:
            self.sock.sendall(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # reconnect once and retry
            self._connect()
            self.sock.sendall(payload)


class TlsSender(SenderBase):
    """
    TLS sender (CoT over TLS).
    - cafile: CA bundle for server verification (recommended)
    - certfile/keyfile: client cert for mutual TLS (often required by TAK servers)
    - insecure: disable cert verification (not recommended, but useful for testing)
    """
    def __init__(
        self,
        host: str,
        port: int,
        bind_ip: str | None,
        cafile: str | None,
        certfile: str | None,
        keyfile: str | None,
        insecure: bool,
        timeout: float = 5.0,
        newline: bool = True,
    ):
        self.host = host
        self.port = port
        self.bind_ip = bind_ip
        self.cafile = cafile
        self.certfile = certfile
        self.keyfile = keyfile
        self.insecure = insecure
        self.timeout = timeout
        self.newline = newline
        self.sock: ssl.SSLSocket | None = None
        self.ctx: ssl.SSLContext | None = None

    def open(self):
        self._build_context()
        self._connect()

    def _build_context(self):
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context(cafile=self.cafile)

        # Client cert for mTLS
        if self.certfile:
            ctx.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)

        self.ctx = ctx

    def _connect(self):
        self.close()
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        if self.bind_ip:
            raw.bind((self.bind_ip, 0))
        raw.connect((self.host, self.port))

        assert self.ctx is not None
        self.sock = self.ctx.wrap_socket(raw, server_hostname=self.host if not self.insecure else None)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send(self, xml_text: str):
        if not self.sock:
            self._connect()

        payload = xml_text.encode("utf-8")
        if self.newline:
            payload += b"\n"

        try:
            self.sock.sendall(payload)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            self._connect()
            self.sock.sendall(payload)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def normalize_name_key(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    for ch in ("'", ".", ",", "(", ")", "/", "’"):
        s = s.replace(ch, "")
    s = s.replace("-", " ")
    s = "_".join(s.split())
    return s


def format_destination_name(raw: str) -> str:
    if not raw:
        return "Unknown"
    return " ".join(word.capitalize() for word in raw.split("_"))


def abbrev_state_or_province(admin1: str, country_code: str) -> str:
    if not admin1:
        return ""
    key = admin1.strip().lower()
    if country_code in ("US", "CA") and key in STATE_PROVINCE_CODES:
        return STATE_PROVINCE_CODES[key]
    return admin1


def get_uuid_for_destination(raw_id: str) -> str:
    if raw_id not in DESTINATION_UUIDS:
        DESTINATION_UUIDS[raw_id] = str(uuid.uuid4())
    return DESTINATION_UUIDS[raw_id]

def deg2rad(deg: float) -> float:
    return deg * math.pi / 180.0

def rad2deg(rad: float) -> float:
    return rad * 180.0 / math.pi

def compute_range_bearing_inclination(lat1, lon1, hae1, lat2, lon2, hae2):
    R = 6371000.0
    phi1 = deg2rad(lat1)
    phi2 = deg2rad(lat2)
    dphi = deg2rad(lat2 - lat1)
    dlambda = deg2rad(lon2 - lon1)

    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    ground_range = R * c

    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    bearing_rad = math.atan2(y, x)
    bearing_deg = (rad2deg(bearing_rad) + 360.0) % 360.0

    dz = (hae2 - hae1)
    slant_range = math.sqrt(ground_range ** 2 + dz ** 2)
    inclination_rad = 0.0 if ground_range == 0 else math.atan2(dz, ground_range)

    return slant_range, bearing_deg, inclination_rad

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    phi1, phi2 = deg2rad(lat1), deg2rad(lat2)
    dphi = deg2rad(lat2 - lat1)
    dl = deg2rad(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t

@dataclass
class SimState:
    lat: float
    lon: float
    idx: int           # "current" destination index in route list (same meaning as your real idx)

def dest_coords_from_obj(dest: dict):
    """Try to extract lat/lon directly from a destination object."""
    if not dest:
        return None

    # Common: "location": "lat, lon"
    loc = dest.get("location")
    if isinstance(loc, str) and "," in loc:
        try:
            lat_str, lon_str = [s.strip() for s in loc.split(",", 1)]
            return float(lat_str), float(lon_str)
        except Exception:
            pass

    # Common numeric fields
    for lat_k, lon_k in (
        ("lat", "lon"),
        ("latitude", "longitude"),
        ("Lat", "Lon"),
        ("Latitude", "Longitude"),
        ("y", "x"),
    ):
        if lat_k in dest and lon_k in dest:
            try:
                return float(dest[lat_k]), float(dest[lon_k])
            except Exception:
                pass

    return None

def resolve_destination(dest: dict):
    """
    Resolve a destination to a dest_info-like dict:
    {name, lat, lon, admin1, country_code}
    """
    raw_id = dest.get("id") or "landing"

    print(f"[DEST RAW_ID] {raw_id}")  # <-- ADD THIS

    coords = dest_coords_from_obj(dest)
    if coords:
        lat, lon = coords
        # Use your label formatting for display purposes
        return {
            "name": format_destination_name(raw_id),
            "lat": lat,
            "lon": lon,
            "admin1": "",
            "country_code": "",
        }

    # fallback to your existing lookup (geopy/offline CSV)
    return lookup_location(raw_id)

def gc_step(lat1, lon1, lat2, lon2, step_m):
    """
    Move from (lat1,lon1) toward (lat2,lon2) along the great-circle by step_m meters.
    Returns (new_lat, new_lon).
    """
    R = 6371000.0

    φ1, λ1 = deg2rad(lat1), deg2rad(lon1)
    φ2, λ2 = deg2rad(lat2), deg2rad(lon2)

    # Angular distance between points
    a = math.sin((φ2-φ1)/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin((λ2-λ1)/2)**2
    δ = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    if δ == 0:
        return lat1, lon1

    # Clamp step to not overshoot
    δ_step = min(step_m / R, δ)

    # Slerp along the sphere
    A = math.sin(δ - δ_step) / math.sin(δ)
    B = math.sin(δ_step) / math.sin(δ)

    x = A * math.cos(φ1) * math.cos(λ1) + B * math.cos(φ2) * math.cos(λ2)
    y = A * math.cos(φ1) * math.sin(λ1) + B * math.cos(φ2) * math.sin(λ2)
    z = A * math.sin(φ1) + B * math.sin(φ2)

    φ3 = math.atan2(z, math.sqrt(x*x + y*y))
    λ3 = math.atan2(y, x)

    return rad2deg(φ3), (rad2deg(λ3) + 540.0) % 360.0 - 180.0  # normalize to [-180,180]

# ---------------------------------------------------------------------------
# Offline CSV loading (Natural Earth)
# ---------------------------------------------------------------------------

def load_offline_places():
    global OFFLINE_INDEX
    if OFFLINE_INDEX is not None:
        return OFFLINE_INDEX

    OFFLINE_INDEX = {}

    if not OFFLINE_CSV_PATH.exists():
        return OFFLINE_INDEX

    with OFFLINE_CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return OFFLINE_INDEX

        field_map = {fn.lower(): fn for fn in reader.fieldnames}

        def pick(*candidates):
            for c in candidates:
                key = c.lower()
                if key in field_map:
                    return field_map[key]
            return None

        name_col = pick("NAMEASCII", "NAME_EN", "NAME")
        lat_col = pick("LATITUDE", "LAT", "Y")
        lon_col = pick("LONGITUDE", "LON", "X")
        admin1_col = pick("ADM1NAME", "ADMIN1_NAME", "ADMIN1")
        iso2_col = pick("ISO_A2", "ISO2", "COUNTRY", "COUNTRY_CODE")

        if not (name_col and lat_col and lon_col):
            return OFFLINE_INDEX

        for row in reader:
            try:
                name_raw = (row.get(name_col) or "").strip()
                if not name_raw:
                    continue
                lat = float(row[lat_col])
                lon = float(row[lon_col])
            except Exception:
                continue

            admin1 = (row.get(admin1_col) or "").strip() if admin1_col else ""
            iso2 = (row.get(iso2_col) or "").strip().upper() if iso2_col else ""
            key = normalize_name_key(name_raw)
            if not key:
                continue

            OFFLINE_INDEX[key] = {
                "name": name_raw,
                "lat": lat,
                "lon": lon,
                "admin1": admin1,
                "country_code": iso2,
            }

    return OFFLINE_INDEX


# ---------------------------------------------------------------------------
# Geocoding + offline fallback
# ---------------------------------------------------------------------------

def lookup_location(raw_id: str):
    if not raw_id:
        return None

    if raw_id in LOOKUP_CACHE:
        return LOOKUP_CACHE[raw_id]

    # 1) Online geocoding
    if GEOLOCATOR is not None:
        try:
            pretty_name = format_destination_name(raw_id)
            loc = GEOLOCATOR.geocode(pretty_name)
            if loc:
                rev = GEOLOCATOR.reverse(
                    (loc.latitude, loc.longitude),
                    language="en",
                    exactly_one=True,
                )
                addr = rev.raw.get("address", {}) if rev else {}
                country_code = (addr.get("country_code") or "").upper()
                admin1 = addr.get("state", "")

                info = {
                    "name": pretty_name,
                    "lat": loc.latitude,
                    "lon": loc.longitude,
                    "admin1": admin1,
                    "country_code": country_code,
                }
                LOOKUP_CACHE[raw_id] = info
                return info
        except Exception:
            pass

    # 2) Offline CSV lookup
    index = load_offline_places()
    key = raw_id.lower()
    if key in index:
        info = index[key]
        LOOKUP_CACHE[raw_id] = info
        return info

    pretty = format_destination_name(raw_id)
    alt_key = normalize_name_key(pretty)
    if alt_key in index:
        info = index[alt_key]
        LOOKUP_CACHE[raw_id] = info
        return info

    return None


# ---------------------------------------------------------------------------
# Santa API + route / presents
# ---------------------------------------------------------------------------

def get_santa_location():
    resp = requests.get(SANTA_INFO_URL, timeout=5)
    resp.raise_for_status()
    data = resp.json()

    loc = data.get("location")
    if not loc:
        raise RuntimeError(f"No 'location' field in Santa API response: {data!r}")

    lat_str, lon_str = [s.strip() for s in loc.split(",", 1)]
    return float(lat_str), float(lon_str), data


def get_route_destinations(info_json):
    routes = info_json.get("route") or []
    if not routes:
        raise RuntimeError("No 'route' URLs found in info JSON.")

    route_url = routes[0]
    resp = requests.get(route_url, timeout=10)
    resp.raise_for_status()
    route_data = resp.json()

    destinations = route_data.get("destinations") or route_data.get("stops") or []
    if not destinations:
        raise RuntimeError("No 'destinations' or 'stops' found in route JSON.")
    return destinations


def get_presents_status(info_json):
    destinations = get_route_destinations(info_json)
    final_total = destinations[-1].get("presentsDelivered")

    now_ms = info_json.get("now")
    takeoff_ms = info_json.get("takeoff")
    duration_ms = info_json.get("duration") or 1

    if now_ms is None or takeoff_ms is None:
        return final_total, 0, destinations

    frac = (now_ms - takeoff_ms) / duration_ms

    if frac <= 0:
        idx = 0
    elif frac >= 1:
        idx = len(destinations) - 1
    else:
        idx = int(frac * (len(destinations) - 1))

    idx = max(0, min(idx, len(destinations) - 1))
    return destinations[idx].get("presentsDelivered"), idx, destinations

# ---------------------------------------------------------------------------
# Simulated location to test functionality
# ---------------------------------------------------------------------------

def simulate_step(sim: SimState, destinations: list, dt_s: float, speed_mps: float):
    if not destinations:
        return sim, None

    # find the next resolvable destination at/after sim.idx+1
    next_i = sim.idx + 1
    dest_info = None
    while next_i < len(destinations):
        dest_info = resolve_destination(destinations[next_i])
        if dest_info:
            break
        next_i += 1

    if not dest_info:
        return sim, None  # nowhere to go

    tgt_lat = dest_info["lat"]
    tgt_lon = dest_info["lon"]

    dist = haversine_m(sim.lat, sim.lon, tgt_lat, tgt_lon)
    step = max(0.0, speed_mps * dt_s)

    arrive_threshold_m = 20000.0  # 20 km
    if dist <= max(arrive_threshold_m, step):
        sim.lat, sim.lon = tgt_lat, tgt_lon
        sim.idx = min(next_i, len(destinations) - 1)
        return sim, dest_info

    sim.lat, sim.lon = gc_step(sim.lat, sim.lon, tgt_lat, tgt_lon, step)
    return sim, dest_info

# ---------------------------------------------------------------------------
# CoT builders (kept as your working versions)
# ---------------------------------------------------------------------------

def build_santa_cot(lat, lon, presents_delivered, next_display, uid, stale_minutes=5):
    now = datetime.now(timezone.utc)
    time_str = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stale = (now + timedelta(minutes=stale_minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": "a-n-A-C",  # kept as in your working script
        "time": time_str,
        "start": time_str,
        "stale": stale,
        "how": "m-g",
    })

    ET.SubElement(event, "point", {
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "hae": "0",
        "ce": "9999999.0",
        "le": "9999999.0",
    })

    detail = ET.SubElement(event, "detail")

    ET.SubElement(detail, "__group", {
        "role": "Santa Claus",
        "name": "Red",
        "abbr": "S",
    })

    ET.SubElement(detail, "contact", {"callsign": "SANTA"})

    remarks = ET.SubElement(detail, "remarks")
    remarks.text = f"Present Delivered: {presents_delivered:,}\nNext: {next_display}"

    return ET.tostring(event, encoding="utf-8", xml_declaration=True).decode("utf-8")


def build_goto_cot(dest_info, uid, stale_hours=24):
    now = datetime.now(timezone.utc)
    time_str = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stale = (now + timedelta(hours=stale_hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    name = dest_info["name"]
    admin1 = dest_info.get("admin1") or ""
    country_code = dest_info.get("country_code") or ""
    admin1_abr = abbrev_state_or_province(admin1, country_code)

    parts = [name]
    if admin1_abr:
        parts.append(admin1_abr)
    if country_code:
        parts.append(country_code)
    label = ", ".join(parts)

    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": "a-u-G",
        "time": time_str,
        "start": time_str,
        "stale": stale,
        "how": "m-g",
    })

    ET.SubElement(event, "point", {
        "lat": f"{dest_info['lat']:.6f}",
        "lon": f"{dest_info['lon']:.6f}",
        "hae": "0",
        "ce": "9999999.0",
        "le": "9999999.0",
    })

    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": label})
    ET.SubElement(detail, "color", {"argb": "-65536"})
    remarks = ET.SubElement(detail, "remarks")
    remarks.text = f"Santa's next destination: {label}"
    ET.SubElement(detail, "usericon", {
        "iconsetpath": "ad78aafb-83a6-4c07-b2b9-a897a8b6a38f/Markers/wht-circle.png"
    })

    return ET.tostring(event, encoding="utf-8", xml_declaration=True).decode("utf-8")


def build_rb_cot(origin_lat, origin_lon, origin_hae, dest_info, parent_uid, range_uid, uid, stale_minutes=1):
    dest_lat = dest_info["lat"]
    dest_lon = dest_info["lon"]
    dest_hae = 0.0

    r, bearing_deg, incl_rad = compute_range_bearing_inclination(
        origin_lat, origin_lon, origin_hae,
        dest_lat, dest_lon, dest_hae
    )

    now = datetime.now(timezone.utc)
    time_str = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stale = (now + timedelta(minutes=stale_minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": "u-rb-a",
        "time": time_str,
        "start": time_str,
        "stale": stale,
        "how": "m-g",
        "access": "Undefined",
    })

    ET.SubElement(event, "point", {
        "lat": f"{origin_lat:.6f}",
        "lon": f"{origin_lon:.6f}",
        "hae": f"{origin_hae:.1f}",
        "ce": "9999999.0",
        "le": "9999999.0",
    })

    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "range", {"value": f"{r}"})
    ET.SubElement(detail, "bearing", {"value": f"{bearing_deg}"})
    ET.SubElement(detail, "inclination", {"value": f"{incl_rad}"})
    ET.SubElement(detail, "rangeUID", {"value": range_uid})
    ET.SubElement(detail, "rangeUnits", {"value": "1"})
    ET.SubElement(detail, "bearingUnits", {"value": "0"})
    ET.SubElement(detail, "northRef", {"value": "1"})
    ET.SubElement(detail, "color", {"value": "-65536"})
    ET.SubElement(detail, "strokeColor", {"value": "-65536"})
    ET.SubElement(detail, "strokeWeight", {"value": "3.0"})
    ET.SubElement(detail, "strokeStyle", {"value": "solid"})
    ET.SubElement(detail, "link", {
        "uid": parent_uid,
        "type": "a-n-A-C",
        "relation": "p-p",
    })
    ET.SubElement(detail, "contact", {"callsign": f"R&B to {dest_info.get('name', 'Destination')}"})
    ET.SubElement(detail, "remarks")

    return ET.tostring(event, encoding="utf-8", xml_declaration=True).decode("utf-8")

def build_delete_cot(tgt_uid: str, stale_seconds: int = 5) -> str:
    """
    Build a TAK-friendly forced delete CoT:
      - event uid: new UUID each send (unique delete message)
      - link uid: the target object UID to delete (your RB_UUID)
      - includes <__forcedelete/>
      - includes dummy <point .../>
    """
    now = datetime.now(timezone.utc)
    start = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stale = (now + timedelta(seconds=stale_seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    event = ET.Element("event", {
        "version": "2.0",
        "uid": str(uuid.uuid4()),          # delete message UID (NOT the target UID)
        "type": "t-x-d-d",
        "time": start,
        "start": start,
        "stale": stale,
        "how": "m-g",
    })

    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "link", {
        "uid": tgt_uid,                    # target object UID to delete
        "type": "none",
        "relation": "none",
    })
    ET.SubElement(detail, "__forcedelete")

    ET.SubElement(event, "point", {
        "lat": "0.0",
        "lon": "0.0",
        "hae": "0.0",
        "ce": "9999999.0",
        "le": "9999999.0",
    })

    # Match the no-newline, no-extra-whitespace style of your example
    return ET.tostring(event, encoding="utf-8", xml_declaration=True).decode("utf-8")

# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def run_once(sender: SenderBase, args, sim_state: SimState | None):
    # Always fetch route/presents from API (even in simulate mode)
    # so "next destination" logic is real.
    _, _, info = get_santa_location()
    presents_now, idx, destinations = get_presents_status(info)

    if getattr(args, "simulate", False):
        # Initialize sim_state on first run
        if sim_state is None:
            sim_state = SimState(lat=args.sim_start_lat, lon=args.sim_start_lon, idx=0)

        # Drive simulation using your configured interval as dt
        sim_state, _ = simulate_step(sim_state, destinations, dt_s=args.interval, speed_mps=args.sim_speed)

        lat, lon = sim_state.lat, sim_state.lon

        # IMPORTANT: Use sim_state.idx for next destination progression,
        # not the API-derived idx (which will be historical/out of season).
        idx = sim_state.idx
    else:
        # Normal mode uses API location for lat/lon
        lat, lon, _info2 = get_santa_location()

    # --- pick the "next" destination object ---
    if idx < len(destinations) - 1:
        next_dest_obj = destinations[idx + 1]
    else:
        next_dest_obj = {"id": "landing"}

    raw_next_id = next_dest_obj.get("id") or "landing"
    pretty_next_name = format_destination_name(raw_next_id)

    # IMPORTANT: this is the update you added; actually use it here
    dest_info = resolve_destination(next_dest_obj)

    if dest_info:
        admin1 = dest_info.get("admin1") or ""
        country_code = dest_info.get("country_code") or ""
        admin1_display = abbrev_state_or_province(admin1, country_code)

        parts = [dest_info["name"]]
        if admin1_display:
            parts.append(admin1_display)
        if country_code:
            parts.append(country_code)
        next_display = ", ".join(parts)
    else:
        next_display = pretty_next_name

    santa_cot = build_santa_cot(lat, lon, presents_now, next_display, uid=SANTA_UUID)
    sender.send(santa_cot)

    if args.verbose:
        print(f"[Santa] {lat:.6f},{lon:.6f}  presents={presents_now:,}  next={next_display}")

    if dest_info:
        dest_uid = raw_next_id

        goto_cot = build_goto_cot(dest_info, uid=dest_uid)
        sender.send(goto_cot)

        rb_cot = build_rb_cot(
            origin_lat=lat,
            origin_lon=lon,
            origin_hae=0.0,
            dest_info=dest_info,
            parent_uid=SANTA_UUID,
            range_uid=dest_uid,
            uid=RB_UUID,
        )
        sender.send(rb_cot)
        if args.verbose:
            print(f"[GOTO] uid={dest_uid}  {dest_info['lat']:.6f},{dest_info['lon']:.6f}")

    return sim_state

def prompt_runtime_config() -> argparse.Namespace:
    print("No --mode specified. Configure output:\n")
    print("1) UDP Multicast")
    print("2) TCP (unencrypted)")
    print("3) TLS (encrypted)")
    choice = input("Select (1/2/3): ").strip()

    interval = input("Update interval seconds [10]: ").strip()
    interval = float(interval) if interval else 10.0

    ns = argparse.Namespace()
    ns.interval = interval
    ns.once = False
    ns.verbose = True

    # defaults
    ns.mode = None
    ns.bind = None
    ns.iface = "0.0.0.0"
    ns.mcast = DEFAULT_MULTICAST_IP
    ns.port = DEFAULT_PORT
    ns.host = None
    ns.cafile = None
    ns.certfile = None
    ns.keyfile = None
    ns.insecure = False
    # Simulation defaults (must exist because run_once expects them)
    ns.simulate = False
    ns.sim_speed = 250.0
    ns.sim_start_lat = 90.0
    ns.sim_start_lon = 0.0

    if choice == "1":
        ns.mode = "udp-mcast"
        m = input(f"Multicast IP [{DEFAULT_MULTICAST_IP}]: ").strip()
        p = input(f"Port [{DEFAULT_PORT}]: ").strip()
        b = input("Bind IP (optional): ").strip()
        i = input("Multicast interface IP [0.0.0.0]: ").strip()
        if m:
            ns.mcast = m
        if p:
            ns.port = int(p)
        if b:
            ns.bind = b
        if i:
            ns.iface = i

    elif choice == "2":
        ns.mode = "tcp"
        ns.host = input("Host: ").strip()
        ns.port = int(input(f"Port [{DEFAULT_PORT}]: ").strip() or str(DEFAULT_PORT))
        b = input("Bind IP (optional): ").strip()
        if b:
            ns.bind = b

    elif choice == "3":
        ns.mode = "tls"
        ns.host = input("Host: ").strip()
        ns.port = int(input("TLS Port: ").strip())
        ns.cafile = input("CA file path (optional): ").strip() or None
        ns.certfile = input("Client cert path (optional): ").strip() or None
        ns.keyfile = input("Client key path (optional): ").strip() or None
        insecure = input("Disable TLS verification? (y/N): ").strip().lower()
        ns.insecure = (insecure == "y")
        b = input("Bind IP (optional): ").strip()
        if b:
            ns.bind = b
    else:
        raise SystemExit("Invalid selection")

    return ns


def build_sender_from_args(args: argparse.Namespace) -> SenderBase:
    if args.mode == "udp-mcast":
        return UdpMulticastSender(
            mcast_ip=args.mcast,
            port=args.port,
            bind_ip=args.bind,
            iface_ip=args.iface,
            ttl=1,
        )
    if args.mode == "tcp":
        if not args.host:
            raise SystemExit("--host is required for --mode tcp")
        return TcpSender(
            host=args.host,
            port=args.port,
            bind_ip=args.bind,
            timeout=5.0,
            newline=True,
        )
    if args.mode == "tls":
        if not args.host:
            raise SystemExit("--host is required for --mode tls")
        return TlsSender(
            host=args.host,
            port=args.port,
            bind_ip=args.bind,
            cafile=args.cafile,
            certfile=args.certfile,
            keyfile=args.keyfile,
            insecure=args.insecure,
            timeout=5.0,
            newline=True,
        )
    raise SystemExit(f"Unknown mode: {args.mode}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Google Santa Tracker -> CoT broadcaster (UDP/TCP/TLS)")
    p.add_argument("--interval", type=float, default=10.0, help="Update interval in seconds (default: 10)")
    p.add_argument("--once", action="store_true", help="Run one iteration and exit")
    p.add_argument("--quiet", action="store_true", help="Less console output")

    p.add_argument("--mode", choices=["udp-mcast", "tcp", "tls"], help="Output mode")
    p.add_argument("--bind", help="Local bind IP (optional)")

    # UDP multicast options
    p.add_argument("--mcast", default=DEFAULT_MULTICAST_IP, help=f"Multicast IP (default: {DEFAULT_MULTICAST_IP})")
    p.add_argument("--iface", default="0.0.0.0", help="Multicast interface IP (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")

    # TCP/TLS options
    p.add_argument("--host", help="Host for TCP/TLS")
    p.add_argument("--cafile", help="CA file for TLS server verification")
    p.add_argument("--certfile", help="Client certificate file for mTLS")
    p.add_argument("--keyfile", help="Client private key file for mTLS")
    p.add_argument("--insecure", action="store_true", help="Disable TLS verification (testing only)")

    p.add_argument("--simulate", action="store_true", help="Simulate Santa movement from North Pole instead of using API location")
    p.add_argument("--sim-speed", type=float, default=250.0, help="Simulation speed in meters/sec (default 250 m/s)")
    p.add_argument("--sim-start-lat", type=float, default=90.0, help="Simulation start latitude (default North Pole 90.0)")
    p.add_argument("--sim-start-lon", type=float, default=0.0, help="Simulation start longitude (default 0.0)")

    args = p.parse_args()
    args.verbose = (not args.quiet)
    return args

def main():
    args = parse_args()
    if not args.mode:
        args = prompt_runtime_config()

    sender = build_sender_from_args(args)

    if args.verbose:
        print(f"Running every {args.interval:.1f}s via mode={args.mode}. Ctrl+C to stop.")

    sim_state = None
    with sender:
        try:
            # run once or loop
            if args.once:
                sim_state = run_once(sender, args=args, sim_state=sim_state)
                return

            while True:
                sim_state = run_once(sender, args=args, sim_state=sim_state)
                time.sleep(args.interval)

        except KeyboardInterrupt:
            # Delete the RB object on exit
            try:
                sender.send(build_delete_cot(RB_UUID))
            except Exception:
                pass
            if args.verbose:
                print("\nStopped.")

if __name__ == "__main__":
    main()
