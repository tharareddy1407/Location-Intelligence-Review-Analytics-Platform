# app.py
import os
import math
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

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
st.caption("App version: v4.0 (AB search modes + in-app visualizations)")

api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
if not api_key:
    st.error("Missing GOOGLE_MAPS_API_KEY. Add it in Render → Environment Variables.")
    st.stop()

settings = load_settings()
client = HttpClient(timeout_sec=settings.timeout_sec, sleep_sec=settings.sleep_between_requests_sec)

if "run_counter" not in st.session_state:
    st.session_state.run_counter = 0

# -------------------------
# Helpers: Autocomplete + Resolve + Geocode
# -------------------------
AUTOCOMPLETE_URL = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distance between two coords in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


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
    params = {"place_id": place_id, "fields": "formatted_address,address_component,geometry", "key": settings.api_key}
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
# Reset
# -------------------------
col_a, col_b = st.columns([1, 5])
with col_a:
    if st.button("Reset / New Search"):
        st.session_state.run_counter = 0
        st.rerun()

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

st.caption("Tip: Type at least 3 characters to see suggestions and select the best match.")

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

    # Resolve center lat/lon
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

    if radius_miles > 25:
        st.warning(
            "Note: Google Places returns a ranked subset per query (not guaranteed complete coverage). "
            "Geo Coverage mode increases coverage but still may not return every store in dense areas."
        )

    # -------------------------
    # MODE B: Text Search
    # -------------------------
    if search_mode.startswith("B)"):
        query = f"{keyword.strip()} near {formatted_address or user_input.strip()}"
        st.info(
            f"Mode: Brand Search (Text Search) | Query: {query} | "
            f"User radius: {radius_miles:.1f} miles | Run #{st.session_state.run_counter}"
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
    # MODE A: Geo Coverage (Tiled Nearby)
    # -------------------------
    else:
        tile_radius_m = min(settings.tile_radius_m, settings.max_nearby_radius_m)

        if user_radius_m <= tile_radius_m:
            tile_centers = [(lat, lon)]
        else:
            tile_centers = generate_tile_centers(lat, lon, radius_m=user_radius_m, tile_radius_m=tile_radius_m)

        search_radius_m = int(min(user_radius_m, tile_radius_m))

        st.info(
            f"Mode: Geo Coverage (Tiled Nearby) | User radius: {radius_miles:.1f} miles | "
            f"Tiles: {len(tile_centers)} | Per-tile radius: {search_radius_m/1609.344:.1f} miles | "
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
            except Exception as e:
                st.error(f"Geo Coverage failed: {e}")
                st.stop()

        st.success(f"Places within {radius_miles:.1f} miles (Geo Coverage): {len(places)}")

    if not places:
        st.warning("No places found within the selected radius. Try increasing radius or changing keyword.")
        st.stop()

    # Preview nearest 10
    if places and places[0].get("distance_miles") is not None:
        nearest_df = pd.DataFrame(places)[["name", "vicinity", "distance_miles"]].copy()
        nearest_df["distance_miles"] = nearest_df["distance_miles"].astype(float).round(2)
        st.subheader("Nearest places (distance check)")
        st.dataframe(nearest_df.head(10), use_container_width=True)

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
        st.warning("No reviews returned. Google often returns only a limited set of reviews per place.")
        st.download_button(
            "Download places.csv",
            places_df.to_csv(index=False).encode("utf-8"),
            "places.csv",
            "text/csv",
        )
        st.stop()

    tableau_df = add_insights(reviews_df)

    # -------------------------
    # VISUALIZATIONS
    # -------------------------
    st.markdown("## 📊 Visualizations (Business Insights)")

    # KPI cards
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Places", places_df["place_id"].nunique() if "place_id" in places_df else len(places_df))
    k2.metric("Reviews", len(reviews_df))
    k3.metric("Avg Rating", round(float(reviews_df["rating"].mean()), 2) if "rating" in reviews_df else 0.0)
    k4.metric("Unique Authors", int(reviews_df["author"].nunique()) if "author" in reviews_df else 0)

    # Sentiment distribution (Bar + Pie)
    if "sentiment" in tableau_df.columns:
        st.subheader("Sentiment Distribution")

        sentiment_counts = tableau_df["sentiment"].value_counts()

        fig1 = plt.figure()
        plt.bar(sentiment_counts.index.astype(str), sentiment_counts.values)
        plt.xlabel("Sentiment")
        plt.ylabel("Count")
        plt.title("Sentiment (Bar)")
        st.pyplot(fig1)

        fig2 = plt.figure()
        plt.pie(sentiment_counts.values, labels=sentiment_counts.index.astype(str), autopct="%1.1f%%")
        plt.title("Sentiment Share (Pie)")
        st.pyplot(fig2)

    # Rating distribution
    if "rating" in reviews_df.columns:
        st.subheader("Rating Distribution (1–5)")
        rating_counts = reviews_df["rating"].value_counts().sort_index()

        fig3 = plt.figure()
        plt.bar(rating_counts.index.astype(str), rating_counts.values)
        plt.xlabel("Rating")
        plt.ylabel("Count")
        plt.title("Ratings Distribution")
        st.pyplot(fig3)

    # Top stores by negative reviews
    if {"restaurant_name", "sentiment"}.issubset(tableau_df.columns):
        st.subheader("Top Stores by Negative Reviews (Top 10)")
        neg = tableau_df[tableau_df["sentiment"] == "Negative"]
        top_neg = neg.groupby("restaurant_name").size().sort_values(ascending=False).head(10)
        st.dataframe(top_neg.reset_index(name="negative_reviews"), use_container_width=True)

    # Reviews over time (if a datetime column exists)
    date_col = None
    for c in ["date_utc", "date", "review_date"]:
        if c in tableau_df.columns:
            date_col = c
            break

    if date_col:
        st.subheader("Review Volume Over Time")
        tmp = tableau_df.copy()
        tmp[date_col] = pd.to_datetime(tmp[date_col], errors="coerce")
        tmp = tmp.dropna(subset=[date_col])

        if not tmp.empty:
            daily = tmp.groupby(tmp[date_col].dt.date).size()

            fig4 = plt.figure()
            plt.plot(daily.index.astype(str), daily.values)
            plt.xticks(rotation=45, ha="right")
            plt.xlabel("Date")
            plt.ylabel("Reviews")
            plt.title("Reviews Over Time")
            st.pyplot(fig4)

    # -------------------------
    # Preview + downloads
    # -------------------------
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
