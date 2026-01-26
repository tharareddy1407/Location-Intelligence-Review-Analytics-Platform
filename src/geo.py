# src/geo.py
import math
from typing import List, Tuple

EARTH_RADIUS_M = 6_371_000.0  # meters


def miles_to_meters(mi: float) -> float:
    return mi * 1609.344


def meters_to_miles(m: float) -> float:
    return m / 1609.344


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    return meters_to_miles(haversine_m(lat1, lon1, lat2, lon2))


def meters_to_lat_deg(m: float) -> float:
    """Convert meters to degrees latitude."""
    return (m / EARTH_RADIUS_M) * (180.0 / math.pi)


def meters_to_lon_deg(m: float, at_lat_deg: float) -> float:
    """Convert meters to degrees longitude at a given latitude."""
    lat_rad = math.radians(at_lat_deg)
    # protect against cos(lat)=0 near poles
    return (m / (EARTH_RADIUS_M * max(0.000001, math.cos(lat_rad)))) * (180.0 / math.pi)


def generate_tile_centers(
    lat: float,
    lon: float,
    radius_m: float,
    tile_radius_m: float,
) -> List[Tuple[float, float]]:
    """
    Cover a circle of radius_m with a grid of smaller circles tile_radius_m.
    Simple grid approach: good enough for portfolio + business insights.
    """
    if radius_m <= tile_radius_m:
        return [(lat, lon)]

    step_m = tile_radius_m * 1.5  # overlap a bit to reduce misses
    dlat = meters_to_lat_deg(step_m)
    dlon = meters_to_lon_deg(step_m, lat)

    lat_extent = meters_to_lat_deg(radius_m)
    lon_extent = meters_to_lon_deg(radius_m, lat)

    centers: List[Tuple[float, float]] = []
    lat_min, lat_max = lat - lat_extent, lat + lat_extent
    lon_min, lon_max = lon - lon_extent, lon + lon_extent

    r2 = radius_m * radius_m

    cur_lat = lat_min
    while cur_lat <= lat_max:
        cur_lon = lon_min
        while cur_lon <= lon_max:
            # keep only grid points inside circle (approx)
            dy = (cur_lat - lat) * (math.pi / 180.0) * EARTH_RADIUS_M
            dx = (cur_lon - lon) * (math.pi / 180.0) * EARTH_RADIUS_M * math.cos(
                math.radians(lat)
            )
            if (dx * dx + dy * dy) <= r2:
                centers.append((cur_lat, cur_lon))
            cur_lon += dlon
        cur_lat += dlat

    # Always include center
    centers.append((lat, lon))

    # Deduplicate (rounded)
    uniq = {}
    for a, b in centers:
        uniq[(round(a, 5), round(b, 5))] = (a, b)
    return list(uniq.values())
