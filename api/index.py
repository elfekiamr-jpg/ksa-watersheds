"""
KSA Watersheds — Vercel proxy backend (temporary/fragile path)

This is a lightweight stand-in for the real backend (app.py, which runs
the `delineator` package locally against ~2.7 GB of baked-in MERIT-Hydro
data). That real backend can't run on Vercel's serverless functions —
the dataset is far bigger than the memory/bundle limits allow.

Instead, this proxies each request to the same global watershed engine
that mghydro.com/watersheds itself calls in the browser (found by
inspecting its Network tab): GET https://mghydro.com/app/getwshed

This is meant as a short-term stopgap to get a working public link
quickly. It depends on mghydro.com's endpoint staying available and
willing to serve non-browser traffic — there's no guarantee of that
long-term. The real fix is deploying app.py to a VPS (see DEPLOY.md),
which has no dataset-size ceiling problem.

Morphological parameters are computed here directly, in plain Python
(haversine-based spherical approximations — no geopandas/shapely/pyproj),
so the function stays small and fast enough for Vercel. Verified against
the same hand-calculable test case used for the geopandas version in
morphology.py — see chat notes. Slightly less precise than a proper local
UTM projection, but well within the accuracy this app needs at basin
scale.
"""
import json
import math
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

# Saudi Arabia bounding box, plus a small buffer. (lon_min, lat_min, lon_max, lat_max)
KSA_BBOX = (33.0, 14.5, 56.5, 33.5)
UPSTREAM_URL = "https://mghydro.com/app/getwshed"


def _in_ksa_bbox(lat, lng):
    lon_min, lat_min, lon_max, lat_max = KSA_BBOX
    return lon_min <= lng <= lon_max and lat_min <= lat <= lat_max


def _ring_area_km2(ring):
    """Spherical polygon area (standard equirectangular approximation) for
    one [lon, lat] ring, in km^2. Accurate enough for regional-sized
    watersheds; doesn't need geopandas/shapely (too heavy for Vercel)."""
    if len(ring) < 3:
        return 0.0
    r = 6371000.0  # Earth radius, meters
    total = 0.0
    n = len(ring)
    for i in range(n):
        lon1, lat1 = ring[i][0], ring[i][1]
        lon2, lat2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += math.radians(lon2 - lon1) * (
            2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(total * r * r / 2.0) / 1e6


def _geometry_area_km2(geometry):
    """Area for a Polygon or MultiPolygon geometry dict. Exterior ring
    only (index 0) minus holes (remaining rings), per ring."""
    if not geometry:
        return 0.0
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        return 0.0
    area = 0.0
    for rings in polygons:
        if not rings:
            continue
        area += _ring_area_km2(rings[0])  # exterior
        for hole in rings[1:]:
            area -= _ring_area_km2(hole)
    return area


def _snapped_outlet_latlng(outlet_fc, fallback_lat, fallback_lng):
    """Pull the snapped-to-river outlet coordinates out of mghydro's
    outlet FeatureCollection, falling back to the originally-clicked
    point if no 'snapped' feature is present."""
    features = (outlet_fc or {}).get("features") or []
    for f in features:
        props = f.get("properties") or {}
        if props.get("type") == "snapped":
            coords = (f.get("geometry") or {}).get("coordinates")
            if coords and len(coords) >= 2:
                return coords[1], coords[0]  # geometry is [lon, lat]
    return fallback_lat, fallback_lng


def _wrap_watershed(watershed_geom, outlet_fc, req_lat, req_lng):
    """Wrap mghydro's bare watershed Polygon/MultiPolygon into the
    FeatureCollection-with-properties shape the frontend expects
    (matching what the real `delineator`-backed app.py produces)."""
    if not watershed_geom:
        return None
    outlet_lat, outlet_lng = _snapped_outlet_latlng(outlet_fc, req_lat, req_lng)
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "area_km2": round(_geometry_area_km2(watershed_geom), 2),
                "outlet_lat": outlet_lat,
                "outlet_lng": outlet_lng,
            },
            "geometry": watershed_geom,
        }],
    }


# ---------- Morphological parameters (pure Python, no dependencies) ----------

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088  # mean Earth radius, km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _largest_exterior_ring(geometry):
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        return coords[0] if coords else []
    elif gtype == "MultiPolygon":
        if not coords:
            return []
        best = max(coords, key=lambda poly: len(poly[0]) if poly else 0)
        return best[0] if best else []
    return []


def _ring_perimeter_km(ring):
    total = 0.0
    n = len(ring)
    for i in range(n):
        lon1, lat1 = ring[i][0], ring[i][1]
        lon2, lat2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += _haversine_km(lat1, lon1, lat2, lon2)
    return total


def compute_morphology(watershed_geom, rivers_fc, outlet_lat, outlet_lng, area_km2_hint):
    """Same parameters as the real backend's morphology.py, computed with
    haversine-distance approximations instead of a local UTM projection
    (which would need pyproj — too heavy to add here). Verified against
    the same hand-calculable rectangle test case; see chat notes."""
    ring = _largest_exterior_ring(watershed_geom)
    if not ring or len(ring) < 3:
        return None

    perimeter_km = _ring_perimeter_km(ring)
    basin_length_km = max(_haversine_km(outlet_lat, outlet_lng, pt[1], pt[0]) for pt in ring)
    area_km2 = area_km2_hint

    form_factor = area_km2 / (basin_length_km ** 2) if basin_length_km else None
    circularity_ratio = (4 * math.pi * area_km2) / (perimeter_km ** 2) if perimeter_km else None
    elongation_ratio = (2 / basin_length_km) * math.sqrt(area_km2 / math.pi) if basin_length_km else None
    compactness_coefficient = 0.2821 * perimeter_km / math.sqrt(area_km2) if area_km2 else None

    result = {
        "perimeter_km": round(perimeter_km, 2),
        "basin_length_km": round(basin_length_km, 2),
        "form_factor": round(form_factor, 4) if form_factor else None,
        "circularity_ratio": round(circularity_ratio, 4) if circularity_ratio else None,
        "elongation_ratio": round(elongation_ratio, 4) if elongation_ratio else None,
        "compactness_coefficient": round(compactness_coefficient, 4) if compactness_coefficient else None,
    }

    features = (rivers_fc or {}).get("features") or []
    if features:
        total_len = 0.0
        for f in features:
            geom = f.get("geometry") or {}
            gtype = geom.get("type")
            lines = geom.get("coordinates") or []
            if gtype == "LineString":
                lines = [lines]
            elif gtype != "MultiLineString":
                continue
            for line in lines:
                for i in range(len(line) - 1):
                    lon1, lat1 = line[i][0], line[i][1]
                    lon2, lat2 = line[i + 1][0], line[i + 1][1]
                    total_len += _haversine_km(lat1, lon1, lat2, lon2)
        num_segments = len(features)
        drainage_density = total_len / area_km2 if area_km2 else None
        stream_frequency = num_segments / area_km2 if area_km2 else None
        overland_flow_length = 1 / (2 * drainage_density) if drainage_density else None
        result.update({
            "total_stream_length_km": round(total_len, 2),
            "num_stream_segments": num_segments,
            "drainage_density_km_per_km2": round(drainage_density, 3) if drainage_density else None,
            "stream_frequency_per_km2": round(stream_frequency, 3) if stream_frequency else None,
            "length_of_overland_flow_km": round(overland_flow_length, 3) if overland_flow_length else None,
        })

    return result


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        """The real requested path arrives as ?path=... (see vercel.json's
        rewrite destination), since Vercel replaces self.path with the
        rewrite's destination path itself, not the original request path."""
        query = parse_qs(urlparse(self.path).query)
        return (query.get("path", [""])[0] or "").strip("/")

    def do_GET(self):
        route = self._route()
        if route == "status":
            self._send_json(200, {"status": "proxy running", "received_path": self.path})
            return
        self._send_json(404, {"error": "Not found", "received_path": self.path})

    def do_POST(self):
        route = self._route()
        if route != "delineate":
            self._send_json(404, {"error": "Not found", "received_path": self.path})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON body."})
            return

        lat, lng = body.get("lat"), body.get("lng")
        mode = body.get("mode", "up")

        if lat is None or lng is None:
            self._send_json(400, {"error": "Request body must include 'lat' and 'lng'."})
            return

        if mode == "down":
            self._send_json(501, {
                "error": "Downstream flow-path tracing isn't implemented in this build."
            })
            return

        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "'lat' and 'lng' must be numeric."})
            return

        if not _in_ksa_bbox(lat, lng):
            self._send_json(400, {
                "error": "That point is outside the Saudi Arabia / Arabian Peninsula "
                         "coverage area of this tool."
            })
            return

        try:
            resp = requests.get(
                UPSTREAM_URL,
                params={
                    "task": "watershed",
                    "lat": lat,
                    "lng": lng,
                    "precision": "high",
                    "simplify": "true",
                    "source": "merit",
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=9,
            )
        except requests.RequestException as e:
            self._send_json(502, {"error": f"Upstream request failed: {e}"})
            return

        if resp.status_code != 200:
            self._send_json(502, {
                "error": f"Upstream delineation service returned {resp.status_code}."
            })
            return

        try:
            data = resp.json()
        except ValueError:
            self._send_json(502, {"error": "Upstream service returned an invalid response."})
            return

        outlet_fc = data.get("outlet")
        watershed_geom = data.get("watershed")
        rivers_fc = data.get("rivers")
        watershed_wrapped = _wrap_watershed(watershed_geom, outlet_fc, lat, lng)

        morphology = None
        if watershed_wrapped:
            area_km2 = watershed_wrapped["features"][0]["properties"]["area_km2"]
            outlet_lat = watershed_wrapped["features"][0]["properties"]["outlet_lat"]
            outlet_lng = watershed_wrapped["features"][0]["properties"]["outlet_lng"]
            try:
                morphology = compute_morphology(watershed_geom, rivers_fc, outlet_lat, outlet_lng, area_km2)
            except Exception:
                morphology = None  # don't let a morphology bug break the whole response

        self._send_json(200, {
            "watershed": watershed_wrapped,
            "rivers": rivers_fc,
            "outlets": outlet_fc,
            "morphology": morphology,
        })
