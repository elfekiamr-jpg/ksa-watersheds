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
"""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

# Saudi Arabia bounding box, plus a small buffer. (lon_min, lat_min, lon_max, lat_max)
KSA_BBOX = (33.0, 14.5, 56.5, 33.5)

UPSTREAM_URL = "https://mghydro.com/app/getwshed"


def _in_ksa_bbox(lat, lng):
    lon_min, lat_min, lon_max, lat_max = KSA_BBOX
    return lon_min <= lng <= lon_max and lat_min <= lat <= lat_max


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status" or path.endswith("/status"):
            self._send_json(200, {"status": "proxy running", "received_path": self.path})
            return
        self._send_json(404, {"error": "Not found", "received_path": self.path})

    def do_POST(self):
        path = urlparse(self.path).path
        if not (path == "/api/delineate" or path.endswith("/delineate")):
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

        self._send_json(200, {
            "watershed": data.get("watershed"),
            "rivers": data.get("rivers"),
            "outlets": data.get("outlet"),
        })
