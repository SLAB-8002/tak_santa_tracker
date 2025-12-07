import requests
from datetime import datetime, timedelta, timezone
import uuid
import xml.etree.ElementTree as ET

SANTA_INFO_URL = (
    "https://santa-api.appspot.com/info"
    "?client=web&language=en&fingerprint=&routeOffset=0&streamOffset=0"
)


def get_santa_location():
    """Return (lat, lon, raw_json) from the Google Santa API."""
    resp = requests.get(SANTA_INFO_URL, timeout=5)
    resp.raise_for_status()
    data = resp.json()

    loc = data.get("location")
    if not loc:
        raise RuntimeError(f"No 'location' field in Santa API response: {data!r}")

    lat_str, lon_str = [s.strip() for s in loc.split(",", 1)]
    lat = float(lat_str)
    lon = float(lon_str)
    return lat, lon, data


def get_route_destinations(info_json):
    """
    Fetch the first route JSON and return its destinations list.
    """
    routes = info_json.get("route") or []
    if not routes:
        raise RuntimeError("No 'route' URLs found in info JSON.")

    route_url = routes[0]
    resp = requests.get(route_url, timeout=10)
    resp.raise_for_status()
    route_data = resp.json()

    destinations = (
        route_data.get("destinations")
        or route_data.get("stops")
        or []
    )
    if not destinations:
        raise RuntimeError("No 'destinations' or 'stops' found in route JSON.")
    return destinations


def get_presents_delivered(info_json, mode="current"):
    """
    Return number of presents delivered.

    mode="current" -> approximate based on now vs takeoff/duration.
    mode="final"   -> value at the final destination (landing).
    """
    destinations = get_route_destinations(info_json)

    # cumulative, last is final total
    final_total = destinations[-1].get("presentsDelivered")

    if mode == "final":
        return final_total

    now_ms = info_json.get("now")
    takeoff_ms = info_json.get("takeoff")
    duration_ms = info_json.get("duration") or 1

    if now_ms is None or takeoff_ms is None:
        # Fall back to final if timing info is missing
        return final_total

    frac = (now_ms - takeoff_ms) / duration_ms

    if frac <= 0:
        idx = 0
    elif frac >= 1:
        idx = len(destinations) - 1
    else:
        idx = int(frac * (len(destinations) - 1))

    idx = max(0, min(idx, len(destinations) - 1))
    current_total = destinations[idx].get("presentsDelivered")
    return current_total


def build_santa_cot(lat, lon, presents_delivered, uid="SANTA-TRACKER", stale_minutes=5):
    """
    Build a CoT event for Santa at the given lat/lon and presents count.
    Returns the CoT XML string.
    """
    now = datetime.now(timezone.utc)
    time_str = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stale = (now + timedelta(minutes=stale_minutes)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")

    if uid is None:
        uid = f"SANTA-{uuid.uuid4()}"

    # Neutral > Air Track > Civil Aircraft > Lighter than Air
    event = ET.Element("event", {
        "version": "2.0",
        "uid": uid,
        "type": "a-n-A-C-L",
        "time": time_str,
        "start": time_str,
        "stale": stale,
        "how": "m-g",  # machine-generated, GPS-ish
    })

    ET.SubElement(event, "point", {
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "hae": "0",            # you can change this to put Santa at altitude
        "ce": "9999999.0",
        "le": "9999999.0",
    })

    detail = ET.SubElement(event, "detail")

    # Group attributes per your spec
    ET.SubElement(detail, "group", {
        "exrole": "Santa",
        "role": "Team Lead",
        "name": "Red",
        "abbr": "S",
    })

    # Optional contact/callsign
    ET.SubElement(detail, "contact", {"callsign": "SANTA"})

    # Remarks with present count
    remarks = ET.SubElement(detail, "remarks")
    remarks.text = f"Present Delivered: {presents_delivered}"

    xml_bytes = ET.tostring(event, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


if __name__ == "__main__":
    lat, lon, info = get_santa_location()
    print("Santa location:", lat, lon)

    presents_now = get_presents_delivered(info, mode="current")
    print("Presents delivered (approx now):", presents_now)

    cot_xml = build_santa_cot(lat, lon, presents_now, uid="SANTA-GOOGLE-TRACKER")
    print("\nCoT message:\n")
    print(cot_xml)
