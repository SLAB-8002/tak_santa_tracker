import requests

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
    Return the number of presents delivered.

    mode="current" -> approximate based on now vs takeoff/duration.
    mode="final"   -> value at the final destination (landing).
    """
    destinations = get_route_destinations(info_json)

    # All destinations have cumulative presentsDelivered
    # final total is the last one
    final_total = destinations[-1].get("presentsDelivered")

    if mode == "final":
        return final_total

    # For "current", map time fraction -> index into destinations
    now_ms = info_json.get("now")
    takeoff_ms = info_json.get("takeoff")
    duration_ms = info_json.get("duration") or 1

    # If timing info is missing, fall back to final
    if now_ms is None or takeoff_ms is None:
        return final_total

    frac = (now_ms - takeoff_ms) / duration_ms

    # Before takeoff: clamp to first stop
    if frac <= 0:
        idx = 0
    # After landing: clamp to last stop
    elif frac >= 1:
        idx = len(destinations) - 1
    else:
        idx = int(frac * (len(destinations) - 1))

    # Safety clamp
    idx = max(0, min(idx, len(destinations) - 1))

    current_total = destinations[idx].get("presentsDelivered")
    return current_total


if __name__ == "__main__":
    lat, lon, data = get_santa_location()
    print("Santa location:", lat, lon)
    print("Raw JSON:", data)

    try:
        current_presents = get_presents_delivered(data, mode="current")
        final_presents = get_presents_delivered(data, mode="final")

        print(f"\nApprox presents delivered *now*: {current_presents:,}")
        print(f"Presents delivered at *landing*: {final_presents:,}")
    except Exception as e:
        print("\nError while fetching presentsDelivered info:", e)
