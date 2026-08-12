"""
Location search support: free-text geocoding (OpenStreetMap Nominatim, no API
key required) + H3 hexagonal spatial indexing for "find nearby" search.

Every founder/investor profile gets an optional {country, city, district,
lat, lon, h3_index} location block. Exact filtering (country/city/district)
never needs geocoding to have succeeded — it's a plain text match. Only the
"nearby / radius" search needs lat/lon + h3_index, so a failed geocode just
means that one profile is invisible to nearby search, not a hard failure.
"""
import math
from typing import Optional

import h3
import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Resolution 7 hexagons have a ~1.4km edge — fine-grained enough to distinguish
# neighborhoods/districts while keeping grid_disk() sizes small for city-wide radii.
H3_RESOLUTION = 7

# One shared, keep-alive client reused across every geocode() call instead of
# opening a fresh TCP/TLS connection per lookup — httpx.Client is safe to
# share across threads, which is how this is called (via asyncio.to_thread).
_client = httpx.Client(timeout=6.0, headers={"User-Agent": "TechFlixx-Location-Search/1.0"})

# A place name's coordinates are effectively permanent, so results (including
# "not found") are cached indefinitely for the process's lifetime — every
# location search that doesn't specify `near` falls back to the caller's own
# saved location, and repeat searches for the same city/district would
# otherwise re-hit Nominatim's free, rate-limited (1 req/sec) API on every
# call. Caching failures too avoids hammering it with the same bad query.
_geocode_cache: dict = {}


def geocode(query: str) -> Optional[dict]:
    """Look up a free-text place name via Nominatim. Returns None on any failure —
    never raises, since geocoding is a best-effort enrichment, not a hard requirement."""
    query = (query or "").strip()
    if not query:
        return None
    cache_key = query.lower()
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]
    resolved = None
    try:
        res = _client.get(NOMINATIM_URL, params={"q": query, "format": "json", "limit": 1})
        if res.status_code == 200:
            results = res.json()
            if results:
                resolved = {
                    "lat": float(results[0]["lat"]),
                    "lon": float(results[0]["lon"]),
                    "display_name": results[0].get("display_name", query),
                }
    except Exception as e:
        print(f"Geocode error for '{query}': {e}")
    _geocode_cache[cache_key] = resolved
    return resolved


def build_location(country: str = "", city: str = "", district: str = "") -> dict:
    """Turn the onboarding form's Country/City/District text into a location
    block. Always returns the text fields as typed; lat/lon/h3_index are only
    filled in if geocoding actually resolves a real place. Never raises —
    geocode() already swallows its own failures — so callers don't need their
    own try/except around this."""
    country, city, district = (country or "").strip(), (city or "").strip(), (district or "").strip()
    location = {
        "country": country,
        "city": city,
        "district": district,
        "lat": None,
        "lon": None,
        "h3_index": None,
    }

    query_parts = [p for p in (district, city, country) if p]
    if not query_parts:
        return location

    geo = geocode(", ".join(query_parts))
    if geo:
        location["lat"] = geo["lat"]
        location["lon"] = geo["lon"]
        location["h3_index"] = h3.latlng_to_cell(geo["lat"], geo["lon"], H3_RESOLUTION)
    return location


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Real great-circle distance in km — used to give an exact, human-readable
    distance and to trim H3's hex-shaped search area down to a true circle."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def nearby_h3_cells(lat: float, lon: float, radius_km: float, resolution: int = H3_RESOLUTION) -> set:
    """All H3 cells within radius_km of (lat, lon), at the given resolution.
    Slightly over-covers a true circle (hexagons vs. a circle) on purpose —
    callers should still confirm with haversine_km before treating a result
    as truly "within radius", this is just the fast index-based first pass.
    Not used by search_pool() below (see its docstring) — kept for a future
    bounded-radius feature that actually excludes far-away results."""
    origin = h3.latlng_to_cell(lat, lon, resolution)
    edge_km = h3.average_hexagon_edge_length(resolution, unit="km")
    k = max(1, math.ceil(radius_km / edge_km))
    return set(h3.grid_disk(origin, k))


def _location_of(entry: dict, search_type: str) -> dict:
    if search_type == "investors":
        return entry.get("location") or {}
    return (entry.get("founder") or {}).get("location") or {}


def _matches_filters(entry: dict, search_type: str, country: str, city: str, district: str, keyword: str) -> bool:
    loc = _location_of(entry, search_type)
    if country and country.lower() not in (loc.get("country") or "").lower():
        return False
    if city and city.lower() not in (loc.get("city") or "").lower():
        return False
    if district and district.lower() not in (loc.get("district") or "").lower():
        return False
    if keyword:
        if search_type == "investors":
            haystack = [
                entry.get("name", ""), entry.get("firm", ""), entry.get("designation", ""),
                *(entry.get("focus_sectors") or []),
            ]
        else:
            founder = entry.get("founder") or {}
            haystack = [
                entry.get("title", ""), entry.get("domain", ""), entry.get("problem", ""),
                founder.get("name", ""), founder.get("specialization", ""),
                *(founder.get("skills") or []),
            ]
        if not any(keyword in str(field).lower() for field in haystack):
            return False
    return True


def _summarize(entry: dict, search_type: str, distance_km: Optional[float]) -> dict:
    """Keep responses light — full github_insights/roadmap/etc. aren't needed to render a search card."""
    base = {"id": entry.get("id"), "location": _location_of(entry, search_type), "distance_km": distance_km}
    if search_type == "investors":
        return {
            **base,
            "name": entry.get("name"),
            "firm": entry.get("firm"),
            "designation": entry.get("designation"),
            "focus_sectors": entry.get("focus_sectors", []),
            "min_ticket": entry.get("min_ticket"),
            "max_ticket": entry.get("max_ticket"),
        }
    founder = entry.get("founder") or {}
    return {
        **base,
        "title": entry.get("title"),
        "domain": entry.get("domain"),
        "problem": entry.get("problem"),
        "founder_name": founder.get("name"),
        "specialization": founder.get("specialization"),
    }


def search_pool(
    pool: list,
    search_type: str,
    *,
    country: str = "",
    city: str = "",
    district: str = "",
    q: str = "",
    origin: Optional[dict] = None,
    offset: int = 0,
    limit: int = 10,
) -> dict:
    """
    Filters `pool` (investor dicts, or idea dicts carrying a `founder` block)
    by exact country/city/district substrings and a free-text keyword, then —
    if `origin` ({lat, lon}) is given — sorts every match by real distance
    from it, closest first, with location-less entries sinking to the
    bottom. Nothing is ever excluded by distance; text/keyword filters are
    the only thing that narrows candidates. Paginated via offset/limit.
    """
    keyword = q.strip().lower()
    country, city, district = country.strip(), city.strip(), district.strip()

    matched = [e for e in pool if _matches_filters(e, search_type, country, city, district, keyword)]

    if origin:
        scored = []
        for entry in matched:
            loc = _location_of(entry, search_type)
            if loc.get("lat") is not None and loc.get("lon") is not None:
                scored.append((entry, round(haversine_km(origin["lat"], origin["lon"], loc["lat"], loc["lon"]), 1)))
            else:
                scored.append((entry, None))
        scored.sort(key=lambda pair: (pair[1] is None, pair[1] or 0))
    else:
        scored = [(entry, None) for entry in matched]

    total = len(scored)
    offset = max(0, offset)
    limit = max(1, min(limit, 50))
    page = scored[offset: offset + limit]

    return {
        "search_type": search_type,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
        "results": [_summarize(entry, search_type, dist) for entry, dist in page],
    }
