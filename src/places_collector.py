# src/places_collector.py
import time
import math
from typing import Dict, List, Set, Tuple, Optional

from .config import Settings
from .http_client import HttpClient

EARTH_RADIUS_M = 6371000.0

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distance in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))

def nearby_search_tile(
    client: HttpClient,
    settings: Settings,
    lat: float,
    lon: float,
    radius_m: int,
    keyword: str,
) -> List[Dict]:
    params = {
        "location": f"{lat},{lon}",
        "radius": radius_m,
        "keyword": keyword,
        "key": settings.api_key,
    }

    all_results: List[Dict] = []
    page = 0

    while True:
        data = client.get_json(settings.nearby_url, params=params)
        status = data.get("status")

        if status not in ("OK", "ZERO_RESULTS"):
            raise RuntimeError(f"Nearby Search error: status={status}, msg={data.get('error_message')}")

        results = data.get("results", []) or []
        all_results.extend(results)

        token = data.get("next_page_token")
        page += 1
        if not token or page >= settings.max_pages_per_tile:
            break

        time.sleep(settings.next_page_token_wait_sec)
        params["pagetoken"] = token

    return all_results

def collect_places(
    client: HttpClient,
    settings: Settings,
    tile_centers: List[Tuple[float, float]],
    search_radius_m: int,
    keyword: str,
    # NEW: true radius filter
    filter_center: Optional[Tuple[float, float]] = None,
    filter_radius_m: Optional[float] = None,
) -> List[Dict]:
    """
    Returns unique place rows:
      { place_id, name, vicinity, lat, lon, types, distance_m, distance_miles }
    If filter_center + filter_radius_m are provided, results are filtered to that radius.
    """
    seen: Set[str] = set()
    places: List[Dict] = []

    c_lat, c_lon = (filter_center if filter_center else (None, None))

    for (lat, lon) in tile_centers:
        results = nearby_search_tile(client, settings, lat, lon, search_radius_m, keyword)

        for p in results:
            pid = p.get("place_id")
            if not pid or pid in seen:
                continue

            geo = p.get("geometry", {}).get("location", {}) or {}
            p_lat = geo.get("lat")
            p_lon = geo.get("lng")
            if p_lat is None or p_lon is None:
                continue

            # Compute distance from chosen center (if provided)
            dist_m = None
            dist_miles = None
            if filter_center and filter_radius_m is not None:
                dist_m = haversine_m(c_lat, c_lon, p_lat, p_lon)
                if dist_m > filter_radius_m:
                    continue  # outside user radius
                dist_miles = dist_m / 1609.344

            seen.add(pid)
            places.append({
                "place_id": pid,
                "name": p.get("name"),
                "vicinity": p.get("vicinity"),
                "lat": p_lat,
                "lon": p_lon,
                "types": ",".join(p.get("types", []) or []),
                "distance_m": dist_m,
                "distance_miles": dist_miles,
            })

        time.sleep(settings.sleep_between_requests_sec)

    # Sort by distance when available (closest first)
    if filter_center and filter_radius_m is not None:
        places.sort(key=lambda x: (x["distance_m"] is None, x["distance_m"]))

    return places
