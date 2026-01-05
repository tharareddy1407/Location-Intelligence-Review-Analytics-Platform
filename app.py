# app.py
import os
import pandas as pd
import streamlit as st

from src.config import load_settings
from src.http_client import HttpClient
from src.geo import miles_to_meters, generate_tile_centers
from src.places_collector import collect_places
from src.text_search_collector import collect_places_textsearch
from src.reviews_collector import collect_reviews
from src.insights import add_insights

# Optional: load local .env (safe on Render too)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


# -------------------------
# Page setup
# -------------------------
st.set_page_config(page_title="Google Places Review Insights", layout="wide")
st.title("Google Places Review Insights (Tableau-ready)")
st.caption("App version: v3.0 (AB modes: Brand Search + Geo Coverage)")

api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
if not api_key:
    st.error("Missing GOOGLE_MAPS_API_KEY. Add it in Render → Environment Variables.")
    st.stop()

settings = load_settings()
client = HttpClient(timeout_sec=settings.timeout_sec, sleep_sec=settings.sleep_between_requests_sec)

if "run_counter" not in st.session_state:
    st.session_state.run_counter = 0

if st.button("Reset / New Search"):
    st.session_state.run_counter = 0
    st.rerun()

# -------------------------
# Helpers: Autocomplete + Resolve + Geocode
# -------------------------
AUTOCOMPLETE_URL = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"


def parse_components(address_components: list) -> dict:
    out = {"city": None, "state": None, "zip": None, "country": None}
    for c in address_components or []:
        types = c.get("types", [])
        if "locality" in types:
            out["city"] = c.get("long_name")
        if "administrative_area_level_1" in types:
            out["state"] = c.get("short_name")
        if "postal_code" in types:
            out["zip"] = c.get("long_name")
        if "country" in types:
            out["country"] = c.get("short_name")
    return out


def get_address_suggestions(user_input: str, limit: int = 6):
    params = {"input": user_input, "types": "geocode", "key": settings.api_key}
    data = client.get_json(AUTOCOMPLETE_URL, params=params)
    preds = data.get("predictions", []) or []
    return [{"description": p.get("description"), "place_id": p.get("place_id")} for p in preds[:limit]]


def resolve_place(place_id: str) -> dict:
    params = {
        "place_id": place_id,
        "fields": "formatted_address,address_component,geometry",
        "key": settings.api_key,
    }
    data = client.get_json(DETAILS_URL, params=params)
    status = data.get("status")
    if status != "OK":
        raise RuntimeError(f"Place Details (resolve) error: status={status}, msg={data.get('error_message')}")
    return data.get("result", {}) or {}


def geocode_address(address: str):
    params = {"address": address, "key": settings.api_key}
    data = client.get_json(settings.geocode_url, params=params)
    status = data.get("status")
    if status != "OK":
        raise RuntimeError(f"Geocode error: status={status}, msg={data.get('error_message')}")
    loc = data["results"][0]["geometry"]["location"]
    formatted = data["results"][0].get("formatted_address")
    return float(loc["lat"]), float(loc["lng"]), formatted


# -------------------------
# UI inputs
# -------------------------
search_mode = st.selectbox(
    "Search Mode",
    [
        "B) Brand Search (Text Search) — faster, ranked results",
        "A) Geo Coverage (Tiled Nearby Search) — slower, more geographic coverage",
    ],
)

user_input = st.text_input("City/Address", "Plano, TX")
keyword = st.text_input("Keyword (restaurant, mcdonalds, pizza...)", "mcdonalds")
radius_miles = st.number_input("Radius (miles)", min_value=1, max_value=200, value=10, step=1)

st.caption("Tip: Type at least 3 characters to see address suggestions. Select one for a normalized address.")

suggestions = []
selected = None
resolved = None

if user_input and len(user_input.strip()) >= 3:
    try:
        suggestions = get_address_suggestions(user_input.strip(), limit=6)
    except Exception as e:
        st.warning(f"Autocomplete unavailable (fallback to geocode): {e}")

if suggestions:
    selected = st.selectbox(
        "Select the best match",
        options=suggestions,
        format_func=lambda x: x["description"],
        key="location_selectbox",
    )
    if selected and selected.get("place_id"):
        try:
            resolved = resolve_place(selected["place_id"])
        except Exception as e:
            st.warning(f"Could not resolve selection. Fallback to geocode. Details: {e}")

run_btn = st.button("Run Analysis")

# -------------------------
# Run analysis
# -------------------------
if run_btn:
    st.session_state.run_counter += 1

    # Resolve to center lat/lon
    try:
        if resolved:
            loc = (resolved.get("geometry") or {}).get("location") or {}
            lat, lon = float(loc.get("lat")), float(loc.get("lng"))
            formatted_address = resolved.get("formatted_address")
            comp = parse_components(resolved.get("address_components", []))
        else:
            lat, lon, formatted_address = geocode_address(user_input.strip())
            comp = {"city": None, "state": None, "zip": None, "country": None}

        st.success(
            f"Resolved: {formatted_address or user_input} | "
            f"Lat/Lon: {lat:.5f}, {lon:.5f} | "
            f"City: {comp.get('city')} | State: {comp.get('state')} | ZIP: {comp.get('zip')}"
        )
    except Exception as e:
        st.error(f"Failed to resolve address: {e}")
        st.stop()

    user_radius_m = miles_to_meters(radius_miles)

    # Explain limits for large radii
    if radius_miles > 25:
        st.warning(
            "Note: Google Places returns a ranked subset per query (not guaranteed complete coverage). "
            "Geo Coverage mode increases coverage but still may not return every store in dense areas."
        )

    # -------------------------
    # MODE B: Text Search (Brand Search)
    # -------------------------
    if search_mode.startswith("B)"):
        # Build query that works well for brands
        # Example: "mcdonalds near Plano, TX, USA"
        query = f"{keyword.strip()} near {formatted_address or user_input.strip()}"

        st.info(
            f"Mode: Brand Search (Text Search) | "
            f"Query: {query} | "
            f"User radius: {radius_miles:.1f} miles | "
            f"Run #{st.session_state.run_counter}"
        )

        with st.spinner("Collecting places (Text Search) + radius filtering..."):
            try:
                places = collect_places_textsearch(
                    client,
                    settings,
                    query=query,
                    filter_center=(lat, lon),
                    filter_radius_m=user_radius_m,
                )
            except Exception as e:
                st.error(f"Text Search failed: {e}")
                st.stop()

        st.success(f"Places within {radius_miles:.1f} miles (Text Search): {len(places)}")

    # -------------------------
    # MODE A: Geo Coverage (Tiled Nearby Search)
    # -------------------------
    else:
        # Tile radius is an internal chunk size (must stay <= Google nearby cap)
        tile_radius_m = min(settings.tile_radius_m, settings.max_nearby_radius_m)

        # If large radius, you get more tiles; if small radius, 1 tile
        if user_radius_m <= tile_radius_m:
            tile_centers = [(lat, lon)]
        else:
            tile_centers = generate_tile_centers(lat, lon, radius_m=user_radius_m, tile_radius_m=tile_radius_m)

        search_radius_m = int(min(user_radius_m, tile_radius_m))

        st.info(
            f"Mode: Geo Coverage (Tiled Nearby) | "
            f"User radius: {radius_miles:.1f} miles | Tiles: {len(tile_centers)} | "
            f"Per-tile search radius: {search_radius_m/1609.344:.1f} miles | "
            f"Run #{st.session_state.run_counter}"
        )

        with st.spinner("Collecting places (Nearby Search tiles) + strict radius filtering..."):
            try:
                places = collect_places(
                    client,
                    settings,
                    tile_centers,
                    search_radius_m,
                    keyword.strip(),
                    filter_center=(lat, lon),
                    filter_radius_m=user_radius_m,
                )
            except TypeError:
                st.error(
                    "Your src/places_collector.py is not updated to support filter_center/filter_radius_m.\n\n"
                    "Update it to the radius-filter version I provided earlier."
                )
                st.stop()
            except Exception as e:
                st.error(f"Geo Coverage failed: {e}")
                st.stop()

        st.success(f"Places within {radius_miles:.1f} miles (Geo Coverage): {len(places)}")

    # Show nearest 10 for sanity check
    if places and places[0].get("distance_miles") is not None:
        nearest_df = pd.DataFrame(places)[["name", "vicinity", "distance_miles"]].copy()
        nearest_df["distance_miles"] = nearest_df["distance_miles"].astype(float).round(2)
        st.subheader("Nearest places (distance check)")
        st.dataframe(nearest_df.head(10), use_container_width=True)

    if not places:
        st.warning("No places found within the selected radius. Try increasing radius or changing keyword.")
        st.stop()

    # Reviews + store addresses
    with st.spinner("Collecting reviews + store addresses (Place Details)..."):
        try:
            results = collect_reviews(client, settings, places)
        except Exception as e:
            st.error(f"Failed collecting reviews: {e}")
            st.stop()

    places_df = pd.DataFrame(results["places"])
    reviews_df = pd.DataFrame(results["reviews"])

    if reviews_df.empty:
        st.warning(
            "No reviews returned for the found places. Google often returns only a small set of reviews per place."
        )
        st.download_button(
            "Download places.csv",
            places_df.to_csv(index=False).encode("utf-8"),
            "places.csv",
            "text/csv",
        )
        st.stop()

    tableau_df = add_insights(reviews_df)

    st.subheader("Preview: Tableau-ready data (includes store address + ZIP)")
    st.dataframe(tableau_df.head(50), use_container_width=True)

    st.download_button(
        "Download places.csv",
        places_df.to_csv(index=False).encode("utf-8"),
        "places.csv",
        "text/csv",
    )
    st.download_button(
        "Download reviews.csv",
        reviews_df.to_csv(index=False).encode("utf-8"),
        "reviews.csv",
        "text/csv",
    )
    st.download_button(
        "Download tableau_reviews.csv",
        tableau_df.to_csv(index=False).encode("utf-8"),
        "tableau_reviews.csv",
        "text/csv",
    )

    st.caption("Use tableau_reviews.csv in Tableau Public to build dashboards.")
