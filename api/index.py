from flask import Flask, request, jsonify
from flask_cors import CORS
import urllib.request
import urllib.parse
import json
import math

app = Flask(__name__)
CORS(app)


# ---------- morphology helpers (pure Python, no geopandas) ----------

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def extract_exterior_ring(geometry):
    gtype = geometry.get('type')
    if gtype == 'Polygon':
        return geometry['coordinates'][0]
    elif gtype == 'MultiPolygon':
        rings = [poly[0] for poly in geometry['coordinates']]
        return max(rings, key=len) if rings else []
    return []


def polygon_perimeter_km(ring):
    total = 0.0
    for i in range(len(ring) - 1):
        lon1, lat1 = ring[i][0], ring[i][1]
        lon2, lat2 = ring[i + 1][0], ring[i + 1][1]
        total += haversine_km(lat1, lon1, lat2, lon2)
    return total


def polygon_area_km2(ring, lat_ref):
    R = 6371.0088
    lat0 = math.radians(lat_ref)
    pts = []
    for lon, lat in ring:
        x = math.radians(lon) * math.cos(lat0) * R
        y = math.radians(lat) * R
        pts.append((x, y))
    area = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def farthest_point_km(ring, outlet_lat, outlet_lng):
    max_d = 0.0
    far_pt = (outlet_lat, outlet_lng)
    for lon, lat in ring:
        d = haversine_km(outlet_lat, outlet_lng, lat, lon)
        if d > max_d:
            max_d = d
            far_pt = (lat, lon)
    return max_d, far_pt


def line_length_km(coords):
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i][0], coords[i][1]
        lon2, lat2 = coords[i + 1][0], coords[i + 1][1]
        total += haversine_km(lat1, lon1, lat2, lon2)
    return total


def rivers_metrics(rivers_geojson):
    result = {'total_length_km': 0.0, 'segment_count': 0, 'main_stream_length_km': 0.0}
    if not rivers_geojson or 'features' not in rivers_geojson:
        return result
    for feat in rivers_geojson['features']:
        geom = feat.get('geometry') or {}
        gtype = geom.get('type')
        lines = []
        if gtype == 'LineString':
            lines = [geom.get('coordinates', [])]
        elif gtype == 'MultiLineString':
            lines = geom.get('coordinates', [])
        for line in lines:
            if len(line) < 2:
                continue
            length = line_length_km(line)
            result['total_length_km'] += length
            result['main_stream_length_km'] = max(result['main_stream_length_km'], length)
            result['segment_count'] += 1
    return result


def get_elevations(points):
    locs = '|'.join(f"{lat},{lng}" for lat, lng in points)
    url = f"https://api.opentopodata.org/v1/srtm90m?locations={urllib.parse.quote(locs, safe='|,')}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return [r.get('elevation') for r in data.get('results', [])]


def kirpich_tc_minutes(main_length_km, elev_high_m, elev_low_m):
    L_m = main_length_km * 1000.0
    if L_m <= 0:
        return None, None
    drop = max(elev_high_m - elev_low_m, 0.1)
    slope = drop / L_m
    tc_min = 0.0195 * (L_m ** 0.77) * (slope ** -0.385)
    return tc_min, slope


def compute_morphology_lite(watershed_geojson, rivers_geojson, outlet_lat, outlet_lng, area_km2_hint=None):
    features = watershed_geojson.get('features') or []
    if not features:
        return {}
    geometry = features[0].get('geometry') or {}
    ring = extract_exterior_ring(geometry)
    if not ring:
        return {}

    perimeter_km = polygon_perimeter_km(ring)
    area_km2 = area_km2_hint if area_km2_hint else polygon_area_km2(ring, outlet_lat)
    basin_length_km, far_pt = farthest_point_km(ring, outlet_lat, outlet_lng)

    form_factor = area_km2 / (basin_length_km ** 2) if basin_length_km > 0 else None
    circularity_ratio = (4 * math.pi * area_km2) / (perimeter_km ** 2) if perimeter_km > 0 else None
    elongation_ratio = (2.0 / basin_length_km) * math.sqrt(area_km2 / math.pi) if basin_length_km > 0 else None
    compactness_coefficient = 0.2821 * perimeter_km / math.sqrt(area_km2) if area_km2 > 0 else None

    river_stats = rivers_metrics(rivers_geojson)
    drainage_density = river_stats['total_length_km'] / area_km2 if area_km2 > 0 else None
    stream_frequency = river_stats['segment_count'] / area_km2 if area_km2 > 0 else None
    overland_flow_length_km = (1.0 / (2 * drainage_density)) if drainage_density else None

    result = {
        'area_km2': round(area_km2, 2),
        'perimeter_km': round(perimeter_km, 2),
        'basin_length_km': round(basin_length_km, 2),
        'form_factor': round(form_factor, 4) if form_factor else None,
        'circularity_ratio': round(circularity_ratio, 4) if circularity_ratio else None,
        'elongation_ratio': round(elongation_ratio, 4) if elongation_ratio else None,
        'compactness_coefficient': round(compactness_coefficient, 4) if compactness_coefficient else None,
        'total_stream_length_km': round(river_stats['total_length_km'], 2),
        'main_stream_length_km': round(river_stats['main_stream_length_km'], 2),
        'num_stream_segments': river_stats['segment_count'],
        'drainage_density_km_per_km2': round(drainage_density, 4) if drainage_density else None,
        'stream_frequency_per_km2': round(stream_frequency, 4) if stream_frequency else None,
        'length_of_overland_flow_km': round(overland_flow_length_km, 4) if overland_flow_length_km else None,
        'time_of_concentration_min': None,
        'avg_basin_slope': None,
    }

    try:
        elevations = get_elevations([(outlet_lat, outlet_lng), far_pt])
        if len(elevations) == 2 and elevations[0] is not None and elevations[1] is not None:
            main_len_for_tc = result['main_stream_length_km'] or basin_length_km
            tc_min, slope = kirpich_tc_minutes(main_len_for_tc, elevations[1], elevations[0])
            result['time_of_concentration_min'] = round(tc_min, 1) if tc_min else None
            result['avg_basin_slope'] = round(slope, 5) if slope else None
    except Exception:
        pass

    return result


# ---------- routes ----------

@app.route('/api/delineate', methods=['POST', 'GET'])
@app.route('/delineate', methods=['POST', 'GET'])
def delineate():
    if request.method == 'GET':
        return jsonify({'status': 'API endpoint active. Send POST request with lat/lng.'}), 200
    try:
        data = request.get_json(force=True, silent=True) or {}
        lat = data.get('lat')
        lng = data.get('lng')
        if lat is None or lng is None:
            return jsonify({'error': 'Latitude and longitude parameters are required.'}), 400

        headers = {'User-Agent': 'Mozilla/5.0'}

        wshed_url = f"https://mghydro.com/app/watershed_api?lat={lat}&lng={lng}&precision=high"
        req = urllib.request.Request(wshed_url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as response:
            watershed_data = json.loads(response.read().decode('utf-8'))

        rivers_data = None
        try:
            rivers_url = f"https://mghydro.com/app/upstream_rivers_api?lat={lat}&lng={lng}"
            req2 = urllib.request.Request(rivers_url, headers=headers)
            with urllib.request.urlopen(req2, timeout=25) as response2:
                rivers_data = json.loads(response2.read().decode('utf-8'))
        except Exception:
            rivers_data = None

        props = {}
        if watershed_data.get('features'):
            props = watershed_data['features'][0].get('properties', {})

        area_hint = props.get('area_km2') or props.get('area')

        try:
            morphology = compute_morphology_lite(watershed_data, rivers_data, float(lat), float(lng), area_hint)
        except Exception:
            morphology = props

        snapped_lat = props.get('outlet_lat', lat)
        snapped_lng = props.get('outlet_lng', lng)

        outlets_geojson = {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [float(lng), float(lat)]},
                    'properties': {'type': 'clicked'}
                },
                {
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [float(snapped_lng), float(snapped_lat)]},
                    'properties': {'type': 'snapped'}
                }
            ]
        }

        return jsonify({
            'watershed': watershed_data,
            'rivers': rivers_data,
            'outlets': outlets_geojson,
            'morphology': morphology
        }), 200
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return jsonify({'message': 'KSA Watersheds API active'}), 200
