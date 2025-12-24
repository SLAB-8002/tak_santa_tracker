#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import math
import os
import requests
import socket
import ssl
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SANTA_INFO_URL = (
    "https://santa-api.appspot.com/info"
    "?client=web&language=en&fingerprint=&routeOffset=0&streamOffset=0"
)

VISITED_PUSH_DONE = False

DEFAULT_MULTICAST_IP = "239.2.3.1"
DEFAULT_PORT = 6969

# UUIDs for Santa and for persistent destination markers while script runs
SANTA_UUID = "SANTA"
DESTINATION_UUIDS = {}   # raw_id -> UUID
RB_UUID = str(uuid.uuid4())  # single persistent Range & Bearing line UID

NORTH_POLE_LAT = 84.6
NORTH_POLE_LON = 168

MAX_ALT_FT = 30000.0
FT_TO_M = 0.3048
MAX_ALT_M = MAX_ALT_FT * FT_TO_M  # 9144.0 m

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
    - cafile: CA bundle for server verification (optional)
    - certfile/keyfile: client cert (PEM) for mutual TLS
    - p12file/p12pass: client cert (PKCS#12 .p12/.pfx) for mutual TLS
    - insecure: disable server cert verification
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
        # NEW:
        p12file: str | None = None,
        p12pass: str | None = None,
    ):
        self.host = host
        self.port = port
        self.bind_ip = bind_ip
        self.cafile = cafile
        self.certfile = certfile
        self.keyfile = keyfile
        self.p12file = p12file
        self.p12pass = p12pass
        self.insecure = insecure
        self.timeout = timeout
        self.newline = newline
        self.sock: ssl.SSLSocket | None = None
        self.ctx: ssl.SSLContext | None = None

        # NEW: hold temp files for p12 conversion + delete on close
        self._p12_temp_files: list[str] = []

    def open(self):
        self._build_context()
        self._connect()

    def _build_context(self):
        # Server verification behavior:
        # - insecure=True => CERT_NONE
        # - insecure=False + cafile provided => CERT_REQUIRED using cafile
        # - insecure=False + cafile None => CERT_NONE (CA not required)
        if self.insecure or not self.cafile:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context(cafile=self.cafile)
            ctx.check_hostname = False  # TAK commonly uses IPs / non-matching names

        # Client cert for mTLS (PKCS#12 takes precedence if provided)
        if self.p12file:
            cert_path, key_path = self._materialize_p12_to_pem(self.p12file, self.p12pass)
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        elif self.certfile:
            # Preserve your existing behavior; keyfile may be None if certfile contains both
            ctx.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)

        self.ctx = ctx

    def _materialize_p12_to_pem(self, p12file: str, p12pass: str | None) -> tuple[str, str]:
        """
        Convert .p12/.pfx into temp PEM files (cert + key) for ssl.load_cert_chain().
        Returns (cert_pem_path, key_pem_path).
        """
        try:
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PrivateFormat,
                NoEncryption,
            )
            from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates
        except ImportError as e:
            raise RuntimeError(
                "PKCS#12 client cert requires the 'cryptography' package. Install with: pip install cryptography"
            ) from e

        p12_bytes = Path(p12file).read_bytes()
        password_bytes = p12pass.encode("utf-8") if p12pass else None

        key, cert, _extras = load_key_and_certificates(p12_bytes, password_bytes)
        if key is None or cert is None:
            raise ValueError(f"PKCS#12 file did not contain both private key and certificate: {p12file}")

        key_pem = key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=NoEncryption(),
        )
        cert_pem = cert.public_bytes(Encoding.PEM)

        # Write temp files (delete=False avoids Windows file-lock issues)
        key_tf = tempfile.NamedTemporaryFile("wb", delete=False, suffix=".key.pem")
        cert_tf = tempfile.NamedTemporaryFile("wb", delete=False, suffix=".crt.pem")
        try:
            key_tf.write(key_pem); key_tf.flush()
            cert_tf.write(cert_pem); cert_tf.flush()
        finally:
            key_tf.close()
            cert_tf.close()

        # Track paths for cleanup
        self._p12_temp_files.extend([key_tf.name, cert_tf.name])
        return cert_tf.name, key_tf.name

    def _connect(self):
        self.close()
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        if self.bind_ip:
            raw.bind((self.bind_ip, 0))
        raw.connect((self.host, self.port))

        assert self.ctx is not None
        self.sock = self.ctx.wrap_socket(
            raw,
            server_hostname=self.host if (not self.insecure and self.cafile) else None
        )

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

        # NEW: cleanup any temp files created for p12
        if self._p12_temp_files:
            for p in self._p12_temp_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            self._p12_temp_files.clear()

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
def api_is_live(info_json: dict) -> bool:
    """
    Google Santa API isn't truly live until now >= takeoff (and duration exists).
    When it's not live, show Santa at North Pole.
    """
    now_ms = info_json.get("now")
    takeoff_ms = info_json.get("takeoff")
    duration_ms = info_json.get("duration")

    if now_ms is None or takeoff_ms is None or duration_ms is None:
        return False
    if duration_ms <= 0:
        return False
    return now_ms >= takeoff_ms

def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def format_countdown(info_json: dict) -> str:
    now_ms = info_json.get("now")
    takeoff_ms = info_json.get("takeoff")
    if now_ms is None or takeoff_ms is None:
        return "Waiting for takeoff…"
    delta_s = max(0, int((takeoff_ms - now_ms) / 1000))
    h = delta_s // 3600
    m = (delta_s % 3600) // 60
    s = delta_s % 60
    return f"Takeoff in {h:02d}:{m:02d}:{s:02d}"

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

def altitude_bump_m(t: float, max_alt_m: float = MAX_ALT_M) -> float:
    """
    Smooth 0->peak->0 altitude profile.
    t: progress along leg [0..1]
    """
    t = max(0.0, min(1.0, float(t)))
    return max_alt_m * math.sin(math.pi * t)

def dest_coords_from_obj(dest: dict):
    """Try to extract lat/lon directly from a destination object."""
    if not dest:
        return None

    loc = dest.get("location")

    # NEW: location is an object: {"lat": ..., "lng": ...}
    if isinstance(loc, dict):
        for lat_k, lon_k in (("lat", "lng"), ("lat", "lon"), ("latitude", "longitude")):
            if lat_k in loc and lon_k in loc:
                try:
                    return float(loc[lat_k]), float(loc[lon_k])
                except Exception:
                    pass

    # Existing: location is a string: "lat, lon"
    if isinstance(loc, str) and "," in loc:
        try:
            lat_str, lon_str = [s.strip() for s in loc.split(",", 1)]
            return float(lat_str), float(lon_str)
        except Exception:
            pass

    # Existing: numeric fields at top-level
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
    print(f"[DEST RAW_ID] {raw_id}")

    coords = dest_coords_from_obj(dest)
    if coords:
        lat, lon = coords

        # Prefer API-provided labels
        name = dest.get("city") or format_destination_name(raw_id)
        admin1 = dest.get("region") or ""

        return {
            "name": name,
            "lat": lat,
            "lon": lon,
            "admin1": admin1,
            "country_code": "",  # Leave blank; do not guess
        }

    # No coordinates available from API object -> strict mode: do not guess
    print(f"[DEST NO COORDS] {raw_id}")
    return None

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
# Santa API + route / presents
# ---------------------------------------------------------------------------

def santa_pos_from_route(
    route_data: dict,
    now_ms: int,
    takeoff_ms: int | None = None,
) -> tuple[float, float, float, int, int]:
    """
    Returns (lat, lon, hae_m, idx, next_idx)

    Uses route schedule as-is:
      - Ground: arrival..departure  -> position exactly at destination, hae=0
      - Travel: departure..next_arrival -> interpolate, hae=bump
    """
    dests = route_data.get("destinations") or route_data.get("stops") or []
    if not dests:
        raise RuntimeError("Route has no destinations/stops")

    dests = sorted(dests, key=lambda d: int(d.get("arrival", 0) or 0))

    def get_latlon(d):
        loc = d.get("location") or {}
        return float(loc["lat"]), float(loc.get("lng", loc.get("lon")))

    def get_arr_dep(d):
        arr = int(d.get("arrival", 0) or 0)
        dep = int(d.get("departure", arr) or arr)
        return arr, dep

    # Optional: shift a "historic year" schedule to this year's takeoff
    shift = 0
    if takeoff_ms:
        first_arr, first_dep = get_arr_dep(dests[0])
        if abs(first_dep - int(takeoff_ms)) > 7 * 24 * 3600 * 1000:
            shift = int(takeoff_ms) - first_dep

    def shifted_arr_dep(d):
        arr, dep = get_arr_dep(d)
        return arr + shift, dep + shift

    now_ms = int(now_ms)

    # Before first arrival -> at first point
    first_arr_s, _first_dep_s = shifted_arr_dep(dests[0])
    if now_ms < first_arr_s:
        lat, lon = get_latlon(dests[0])
        idx = 0
        next_idx = 1 if len(dests) > 1 else 0
        return lat, lon, 0.0, idx, next_idx

    for i in range(len(dests)):
        d = dests[i]
        arr_s, dep_s = shifted_arr_dep(d)

        # On ground at this destination
        if arr_s <= now_ms <= dep_s:
            lat, lon = get_latlon(d)
            idx = i
            next_idx = min(i + 1, len(dests) - 1)
            return lat, lon, 0.0, idx, next_idx

        # Traveling to next destination
        if i < len(dests) - 1:
            nxt = dests[i + 1]
            nxt_arr_s, _nxt_dep_s = shifted_arr_dep(nxt)

            if dep_s < now_ms < nxt_arr_s:
                lat1, lon1 = get_latlon(d)
                lat2, lon2 = get_latlon(nxt)

                span = max(1, (nxt_arr_s - dep_s))
                t = (now_ms - dep_s) / span  # 0..1

                lat = lat1 + (lat2 - lat1) * t
                lon = lon1 + (lon2 - lon1) * t
                hae_m = altitude_bump_m(t, MAX_ALT_M)

                idx = i
                next_idx = i + 1
                return float(lat), float(lon), float(hae_m), idx, next_idx

    # After last departure -> at last destination
    lat, lon = get_latlon(dests[-1])
    last = len(dests) - 1
    return lat, lon, 0.0, last, last

def get_santa_location_and_route():
    resp = requests.get(SANTA_INFO_URL, timeout=5)
    resp.raise_for_status()
    info = resp.json()

    now_ms = int(info.get("now", 0))
    takeoff_ms = int(info.get("takeoff", 0)) if info.get("takeoff") else None

    route_url = (info.get("route") or [None])[0]
    if not route_url:
        raise RuntimeError("No route URL in info JSON")

    r = requests.get(route_url, timeout=10)
    r.raise_for_status()
    route_data = r.json()

    destinations = route_data.get("destinations") or route_data.get("stops") or []
    if not destinations:
        raise RuntimeError("No destinations in route JSON")

    lat, lon, hae_m, idx, next_idx = santa_pos_from_route(route_data, now_ms, takeoff_ms=takeoff_ms)

    return lat, lon, hae_m, idx, next_idx, destinations, info

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
# Presents interpolation helpers
# ---------------------------------------------------------------------------

def _sorted_dests(destinations: list[dict]) -> list[dict]:
    return sorted(destinations or [], key=lambda d: int(d.get("arrival", 0) or 0))

def _compute_shift_ms(dests: list[dict], takeoff_ms: int | None) -> int:
    """
    Mirror the 'historic year schedule' shift logic used in santa_pos_from_route().
    Returns a shift (ms) to apply to arrival/departure times.
    """
    if not dests or not takeoff_ms:
        return 0

    first = dests[0]
    first_dep = int(first.get("departure", first.get("arrival", 0) or 0) or 0)

    # If the route schedule looks like it's from a different year, shift to this year's takeoff.
    if abs(first_dep - int(takeoff_ms)) > 7 * 24 * 3600 * 1000:
        return int(takeoff_ms) - first_dep
    return 0

def _shifted_arr_dep_ms(d: dict, shift_ms: int) -> tuple[int, int]:
    arr = int(d.get("arrival", 0) or 0) + shift_ms
    dep = int(d.get("departure", arr) or arr) + shift_ms
    return arr, dep

def presents_dynamic_live(destinations: list[dict], idx: int, next_idx: int, now_ms: int, takeoff_ms: int | None) -> int:
    """
    Smoothly increases presents from:
      X = presents at current idx
      Y = presents at next_idx
    over time from:
      t0 = departure(current)
      t1 = arrival(next) + 60s
    Clamped so:
      - before t0 => X
      - after t1 => Y
    """
    dests = _sorted_dests(destinations)
    if not dests:
        return 0

    idx = max(0, min(idx, len(dests) - 1))
    next_idx = max(0, min(next_idx, len(dests) - 1))

    # If there's no "next", just return current
    if next_idx == idx:
        return int(dests[idx].get("presentsDelivered", 0) or 0)

    shift_ms = _compute_shift_ms(dests, takeoff_ms)

    cur = dests[idx]
    nxt = dests[next_idx]

    x = int(cur.get("presentsDelivered", 0) or 0)
    y = int(nxt.get("presentsDelivered", x) or x)

    _cur_arr, cur_dep = _shifted_arr_dep_ms(cur, shift_ms)
    nxt_arr, _nxt_dep = _shifted_arr_dep_ms(nxt, shift_ms)

    t0 = cur_dep
    t1 = nxt_arr + 60_000  # + 1 minute on the ground at next stop

    if t1 <= t0:
        return y

    t = (now_ms - t0) / (t1 - t0)
    t = max(0.0, min(1.0, float(t)))

    return int(round(lerp(x, y, t)))

# ---------------------------------------------------------------------------
# CoT builders (kept as your working versions)
# ---------------------------------------------------------------------------

def build_santa_cot(lat, lon, hae_m, presents_delivered, next_display, uid, stale_minutes=5, now_dt_utc: datetime | None = None):
    now = now_dt_utc or datetime.now(timezone.utc)
    time_str = iso_z(now)
    stale = iso_z(now + timedelta(minutes=stale_minutes))

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
        "hae": f"{hae_m:.1f}",
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
    remarks.text = f"Presents Delivered: {presents_delivered:,}\nNext: {next_display}"

    return ET.tostring(event, encoding="utf-8", xml_declaration=True).decode("utf-8")


def build_goto_cot(dest_info, uid, stale_hours=24, now_dt_utc: datetime | None = None):
    now = now_dt_utc or datetime.now(timezone.utc)
    time_str = iso_z(now)
    stale = iso_z(now + timedelta(hours=stale_hours))

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


def build_rb_cot(origin_lat, origin_lon, origin_hae, dest_info, parent_uid, range_uid, uid, stale_minutes=1, now_dt_utc: datetime | None = None):
    dest_lat = dest_info["lat"]
    dest_lon = dest_info["lon"]
    dest_hae = 0.0

    r, bearing_deg, incl_rad = compute_range_bearing_inclination(
        origin_lat, origin_lon, origin_hae,
        dest_lat, dest_lon, dest_hae
    )

    now = now_dt_utc or datetime.now(timezone.utc)
    time_str = iso_z(now)
    stale = iso_z(now + timedelta(minutes=stale_minutes))

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

def run_once(sender: SenderBase, args):
    # Fetch API once per tick
    lat_api, lon_api, hae_api, idx_api, next_idx, destinations, info = get_santa_location_and_route()

    live = api_is_live(info)
    api_now_ms = int(info.get("now", 0))
    api_now_dt = datetime.fromtimestamp(api_now_ms / 1000.0, tz=timezone.utc)

    # --- PRE-LIVE ---
    if not live:
        lat, lon = NORTH_POLE_LAT, NORTH_POLE_LON
        presents_now = 0
        next_display = format_countdown(info)
        hae_m = 0.0

        santa_cot = build_santa_cot(lat, lon, hae_m, presents_now, next_display, uid=SANTA_UUID, now_dt_utc=api_now_dt)
        sender.send(santa_cot)

        if args.verbose:
            print(f"[Santa] PRE-LIVE {lat:.6f},{lon:.6f}  next={next_display}")

        # clear lingering RB
        try:
            sender.send(build_delete_cot(RB_UUID))
        except Exception:
            pass

        return

    # --- LIVE MODE ---
    lat, lon, hae_m = lat_api, lon_api, hae_api
    idx = idx_api

    takeoff_ms = int(info.get("takeoff", 0)) if info.get("takeoff") else None

    # 1) Dynamic presents ramp X->Y until next arrival + 1 minute
    presents_now = presents_dynamic_live(
        destinations=destinations,
        idx=idx_api,
        next_idx=next_idx,
        now_ms=api_now_ms,
        takeoff_ms=takeoff_ms,
    )

    # 2) One-time push of visited locations
    global VISITED_PUSH_DONE
    if not VISITED_PUSH_DONE:
        # If Santa is on the ground at idx, include idx as "visited".
        include_current = (hae_m <= 1.0)  # meters; route uses 0.0 on ground
        visited_upto = idx + (1 if include_current else 0)

        dests_sorted = _sorted_dests(destinations)

        for i in range(0, visited_upto):
            d = dests_sorted[i]
            raw_id = d.get("id") or f"visited_{i}"
            di = resolve_destination(d)
            if not di:
                continue
            sender.send(build_goto_cot(di, uid=raw_id, now_dt_utc=api_now_dt))

        VISITED_PUSH_DONE = True
        if args.verbose:
            print(f"[BOOT] Pushed {max(0, visited_upto)} visited locations (include_current={include_current})")

    # --- next destination display + markers (your existing logic) ---
    if destinations and idx < len(destinations) - 1:
        next_dest_obj = destinations[next_idx]
    else:
        next_dest_obj = {"id": "landing"}

    raw_next_id = next_dest_obj.get("id") or "landing"
    pretty_next_name = format_destination_name(raw_next_id)

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

    santa_cot = build_santa_cot(lat, lon, hae_m, presents_now, next_display, uid=SANTA_UUID, now_dt_utc=api_now_dt)
    sender.send(santa_cot)

    if args.verbose:
        print(f"[Santa] {lat:.6f},{lon:.6f}  presents={presents_now:,}  next={next_display}")

    if dest_info:
        dest_uid = raw_next_id

        goto_cot = build_goto_cot(dest_info, uid=dest_uid, now_dt_utc=api_now_dt)
        sender.send(goto_cot)

        rb_cot = build_rb_cot(
            origin_lat=lat,
            origin_lon=lon,
            origin_hae=hae_m,
            dest_info=dest_info,
            parent_uid=SANTA_UUID,
            range_uid=dest_uid,
            uid=RB_UUID,
            now_dt_utc=api_now_dt
        )
        sender.send(rb_cot)

        if args.verbose:
            print(f"[GOTO] uid={dest_uid}  {dest_info['lat']:.6f},{dest_info['lon']:.6f}")

        if args.verbose:
            local_ms = int(time.time() * 1000)
            print(f"[CLOCK] local-api = {(local_ms - api_now_ms) / 1000.0:+.1f}s")

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

    # NEW: p12 defaults
    ns.p12file = None
    ns.p12pass = None

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
        ns.port = int(input(f"TLS Port [{DEFAULT_PORT}]: ").strip() or str(DEFAULT_PORT))

        ns.cafile = input("CA file path (optional): ").strip() or None

        print("\nClient authentication (mTLS) (optional):")
        print("1) None")
        print("2) PEM cert + key")
        print("3) PKCS#12 (.p12/.pfx)")
        client_choice = input("Select (1/2/3) [1]: ").strip() or "1"

        if client_choice == "2":
            ns.certfile = input("Client cert path (PEM): ").strip() or None
            ns.keyfile = input("Client key path (PEM): ").strip() or None
        elif client_choice == "3":
            ns.p12file = input("Client P12/PFX path: ").strip() or None
            # password optional; allow empty for no password
            ns.p12pass = input("P12 password (optional): ").strip() or None
        elif client_choice != "1":
            raise SystemExit("Invalid client auth selection")

        insecure = input("Disable TLS verification? (y/N): ").strip().lower()
        ns.insecure = (insecure == "y")

        b = input("Bind IP (optional): ").strip()
        if b:
            ns.bind = b

    else:
        raise SystemExit("Invalid selection")

    # --- Keep runtime prompt behavior consistent with CLI validation ---
    if getattr(ns, "keyfile", None) and not getattr(ns, "certfile", None):
        raise SystemExit("--keyfile requires --certfile")
    if getattr(ns, "p12pass", None) and not getattr(ns, "p12file", None):
        raise SystemExit("--p12pass requires --p12file")
    if getattr(ns, "certfile", None) and getattr(ns, "p12file", None):
        raise SystemExit("Use either PEM (--certfile/--keyfile) OR PKCS#12 (--p12file), not both")

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
            # NEW:
            p12file=args.p12file,
            p12pass=args.p12pass,
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
    p.add_argument("--p12file", help="Client certificate bundle (PKCS#12 .p12/.pfx) for mTLS")
    p.add_argument("--p12pass", help="Password for --p12file (optional)")

    args = p.parse_args()
    args.verbose = (not args.quiet)
    # --- TLS client auth validation ---
    if args.keyfile and not args.certfile:
        raise SystemExit("--keyfile requires --certfile")

    if args.p12pass and not args.p12file:
        raise SystemExit("--p12pass requires --p12file")

    if args.certfile and args.p12file:
        raise SystemExit("Use either --certfile/--keyfile OR --p12file, not both")

    return args

def main():
    args = parse_args()
    if not args.mode:
        args = prompt_runtime_config()

    sender = build_sender_from_args(args)

    if args.verbose:
        print(f"Running every {args.interval:.1f}s via mode={args.mode}. Ctrl+C to stop.")

    with sender:
        try:
            # run once or loop
            if args.once:
                run_once(sender, args=args)
                return

            while True:
                run_once(sender, args=args)
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