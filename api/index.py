from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import urllib.request
import urllib.parse
import json
import math
import io
import datetime
import concurrent.futures

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

    try:
        area_km2 = float(area_km2_hint) if area_km2_hint else None
    except (TypeError, ValueError):
        area_km2 = None
    if not area_km2:
        area_km2 = polygon_area_km2(ring, outlet_lat)

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
        'lag_time_min': None,
        'avg_basin_slope': None,
    }

    try:
        elevations = get_elevations([(outlet_lat, outlet_lng), far_pt])
        if len(elevations) == 2 and elevations[0] is not None and elevations[1] is not None:
            main_len_for_tc = result['main_stream_length_km'] or basin_length_km
            tc_min, slope = kirpich_tc_minutes(main_len_for_tc, elevations[1], elevations[0])
            if tc_min:
                result['time_of_concentration_min'] = round(tc_min, 1)
                result['lag_time_min'] = round(0.6 * tc_min, 1)
            result['avg_basin_slope'] = round(slope, 5) if slope else None
    except Exception:
        pass

    return result


# ---------- site-info helpers (reverse geocoding + Wikipedia, best-effort) ----------

def reverse_geocode(lat, lng):
    """Look up place name / admin region for a point via OSM Nominatim. Returns {} on any failure."""
    try:
        url = ("https://nominatim.openstreetmap.org/reverse?format=json"
               f"&lat={lat}&lon={lng}&zoom=10&addressdetails=1&accept-language=en")
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0 (contact: elfekiamr@gmail.com)'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        addr = data.get('address', {}) or {}
        place = (addr.get('city') or addr.get('town') or addr.get('village')
                 or addr.get('county') or addr.get('state') or None)
        region = addr.get('state') or addr.get('region') or None
        country = addr.get('country') or None
        return {
            'display_name': data.get('display_name'),
            'place': place,
            'region': region,
            'country': country,
        }
    except Exception:
        return {}


def wikipedia_summary(title):
    """Fetch a short Wikipedia extract for a place name. Returns None on any failure or no match."""
    if not title:
        return None
    try:
        search_url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
                      f"&srsearch={urllib.parse.quote(title)}&format=json&srlimit=1")
        req = urllib.request.Request(search_url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            search_data = json.loads(resp.read().decode('utf-8'))
        hits = search_data.get('query', {}).get('search', [])
        if not hits:
            return None
        page_title = hits[0].get('title')
        if not page_title:
            return None
        summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(page_title)}"
        req2 = urllib.request.Request(summary_url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req2, timeout=8) as resp2:
            summary_data = json.loads(resp2.read().decode('utf-8'))
        extract = summary_data.get('extract')
        if not extract:
            return None
        return {'title': summary_data.get('title', page_title), 'extract': extract}
    except Exception:
        return None


def _compass_direction(deg):
    dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    try:
        idx = int((float(deg) / 22.5) + 0.5) % 16
        return dirs[idx]
    except Exception:
        return None


def fetch_current_weather(lat, lng):
    """Current relative humidity + wind, from Open-Meteo (free, no API key)."""
    out = {}
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}"
               "&current=relative_humidity_2m,wind_speed_10m,wind_direction_10m&timezone=auto")
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        cur = data.get('current', {}) or {}
        if cur.get('relative_humidity_2m') is not None:
            out['relative_humidity_pct'] = cur['relative_humidity_2m']
        if cur.get('wind_speed_10m') is not None:
            out['wind_speed_kmh'] = cur['wind_speed_10m']
        if cur.get('wind_direction_10m') is not None:
            out['wind_direction_deg'] = cur['wind_direction_10m']
            out['wind_direction_compass'] = _compass_direction(cur['wind_direction_10m'])
    except Exception:
        pass
    return out


def _archive_date_range():
    end = datetime.date.today() - datetime.timedelta(days=5)  # archive lags a few days
    start = end - datetime.timedelta(days=365)
    return start, end


_NORMALS_YEARS = 10  # how many years of history to average per calendar month


def _normals_date_range(years=_NORMALS_YEARS):
    end = datetime.date.today() - datetime.timedelta(days=5)  # archive lags a few days
    start = datetime.date(end.year - years, 1, 1)
    return start, end


def fetch_annual_climate(lat, lng):
    """Trailing-12-month rainfall/ET0 totals (recent-year figures shown in the
    summary table) plus true calendar-month climatological averages of
    rainfall/ET0/wind — each calendar month averaged across ~10 years of
    history, not just one arbitrary year — from Open-Meteo's free historical
    archive (no API key). One HTTP call covers both."""
    out = {}
    try:
        start, end = _normals_date_range()
        url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lng}"
               f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
               "&daily=precipitation_sum,et0_fao_evapotranspiration,windspeed_10m_max,winddirection_10m_dominant"
               "&timezone=auto")
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        daily = data.get('daily', {}) or {}
        dates = daily.get('time', []) or []
        precip = daily.get('precipitation_sum', []) or []
        et0 = daily.get('et0_fao_evapotranspiration', []) or []
        wspd = daily.get('windspeed_10m_max', []) or []
        wdir = daily.get('winddirection_10m_dominant', []) or []
        if not dates:
            return out

        # Trailing-12-month totals (most recent 365 days of the same response) —
        # the single "recent year" figures shown in the summary table.
        cutoff = (end - datetime.timedelta(days=365)).isoformat()
        recent_precip = [precip[i] for i, d in enumerate(dates) if d >= cutoff and i < len(precip) and precip[i] is not None]
        recent_et0 = [et0[i] for i, d in enumerate(dates) if d >= cutoff and i < len(et0) and et0[i] is not None]
        if recent_precip:
            out['annual_rainfall_mm'] = round(sum(recent_precip), 1)
        if recent_et0:
            out['et0_annual_mm'] = round(sum(recent_et0), 1)

        # Calendar-month climatological averages: bucket every day by (year, month)
        # first, then average each calendar month's per-year totals across however
        # many years of data are actually present.
        year_month = {}  # (year, month) -> accumulators
        years_seen = set()
        for i, d in enumerate(dates):
            y, m = int(d[:4]), int(d[5:7])
            key = (y, m)
            years_seen.add(y)
            if key not in year_month:
                year_month[key] = {'rain': 0.0, 'et0': 0.0, 'wspd_sum': 0.0, 'wspd_n': 0, 'wdir': []}
            b = year_month[key]
            if i < len(precip) and precip[i] is not None:
                b['rain'] += precip[i]
            if i < len(et0) and et0[i] is not None:
                b['et0'] += et0[i]
            if i < len(wspd) and wspd[i] is not None:
                b['wspd_sum'] += wspd[i]
                b['wspd_n'] += 1
            if i < len(wdir) and wdir[i] is not None:
                b['wdir'].extend(wdir[i:i + 1])

        month_labels = [_MONTH_ABBR[m] for m in range(1, 13)]
        rain_vals, et0_vals, wspd_vals, wdir_vals = [], [], [], []
        for m in range(1, 13):
            month_entries = [b for (y, mo), b in year_month.items() if mo == m]
            if not month_entries:
                rain_vals.append(None); et0_vals.append(None); wspd_vals.append(None); wdir_vals.append(None)
                continue
            rain_vals.append(round(sum(e['rain'] for e in month_entries) / len(month_entries), 1))
            et0_vals.append(round(sum(e['et0'] for e in month_entries) / len(month_entries), 1))
            wspd_daily_means = [e['wspd_sum'] / e['wspd_n'] for e in month_entries if e['wspd_n']]
            wspd_vals.append(round(sum(wspd_daily_means) / len(wspd_daily_means), 1) if wspd_daily_means else None)
            all_dirs = [a for e in month_entries for a in e['wdir']]
            if all_dirs:
                sx = sum(math.sin(math.radians(a)) for a in all_dirs)
                sy = sum(math.cos(math.radians(a)) for a in all_dirs)
                mean_deg = (math.degrees(math.atan2(sx, sy)) + 360) % 360
                wdir_vals.append(_compass_direction(mean_deg))
            else:
                wdir_vals.append(None)

        out['monthly'] = {
            'months': month_labels,
            'rainfall_mm': rain_vals,
            'et0_mm': et0_vals,
            'wind_speed_kmh': wspd_vals,
            'wind_direction_compass': wdir_vals,
            'year_start': min(years_seen) if years_seen else None,
            'year_end': max(years_seen) if years_seen else None,
        }
    except Exception:
        pass
    return out


def fetch_monthly_humidity(lat, lng):
    """Best-effort calendar-month average relative humidity, across the same
    ~10-year window as fetch_annual_climate. Kept as its own request/try-except
    since this daily aggregate isn't guaranteed available — a failure here
    should never take down the rainfall/ET0/wind charts."""
    try:
        start, end = _normals_date_range()
        url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lng}"
               f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
               "&daily=relative_humidity_2m_mean&timezone=auto")
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        daily = data.get('daily', {}) or {}
        dates = daily.get('time', []) or []
        hum = daily.get('relative_humidity_2m_mean', []) or []
        if not dates or not hum:
            return None

        year_month = {}
        for i, d in enumerate(dates):
            if i >= len(hum) or hum[i] is None:
                continue
            key = (int(d[:4]), int(d[5:7]))
            year_month.setdefault(key, []).append(hum[i])

        month_labels = [_MONTH_ABBR[m] for m in range(1, 13)]
        vals = []
        for m in range(1, 13):
            month_entries = [v for (y, mo), vs in year_month.items() if mo == m for v in vs]
            vals.append(round(sum(month_entries) / len(month_entries), 1) if month_entries else None)
        if not any(v is not None for v in vals):
            return None
        return {'months': month_labels, 'humidity_pct': vals}
    except Exception:
        return None


def fetch_land_use_population(lat, lng):
    """Point-level land-use/land-cover tag and, when the point falls in or near
    a named place with that data on OSM, its population. Via Nominatim (free,
    no API key)."""
    out = {}
    try:
        url = ("https://nominatim.openstreetmap.org/reverse?format=json"
               f"&lat={lat}&lon={lng}&zoom=14&addressdetails=1&extratags=1&accept-language=en")
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0 (contact: elfekiamr@gmail.com)'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        category = data.get('category')
        type_ = (data.get('type') or '').replace('_', ' ').strip()
        label = type_ or None
        if category and category not in ('place', 'boundary') and category != type_:
            label = f'{category}: {label}' if label else category
        if label:
            out['land_use'] = label.strip(': ').capitalize()
        extratags = data.get('extratags') or {}
        pop = extratags.get('population')
        if pop:
            try:
                out['population'] = int(pop)
            except (TypeError, ValueError):
                out['population'] = pop
    except Exception:
        pass
    return out


def fetch_environmental_context(lat, lng):
    """Best-effort meteorological/environmental data for the outlet point, run
    concurrently since each lookup is an independent, unrelated web request.
    Any field that can't be obtained is simply absent from the result — the
    PDF renders those as 'NA'."""
    result = {
        'annual_rainfall_mm': None,
        'et0_annual_mm': None,
        'relative_humidity_pct': None,
        'wind_speed_kmh': None,
        'wind_direction_deg': None,
        'wind_direction_compass': None,
        'land_use': None,
        'population': None,
        'landcover_class': None,   # Land cover class (Esri/Impact Observatory Sentinel-2 10m) at the outlet point
        'soil_class': None,        # ISRIC SoilGrids WRB dominant class at the outlet point
        'hsg_class': None,         # Hydrologic soil group A-D (HYSOGs250m) at the outlet point
        'monthly': None,           # {'months','rainfall_mm','et0_mm','wind_speed_kmh','wind_direction_compass'}
        'monthly_humidity': None,  # {'months','humidity_pct'}
    }
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            f_weather = ex.submit(fetch_current_weather, lat, lng)
            f_climate = ex.submit(fetch_annual_climate, lat, lng)
            f_land = ex.submit(fetch_land_use_population, lat, lng)
            f_humidity = ex.submit(fetch_monthly_humidity, lat, lng)
            f_lcsoil = ex.submit(fetch_landcover_soil_labels, lat, lng)
            f_hsg = ex.submit(fetch_hsg_point_class, lat, lng)
            for f in (f_weather, f_climate, f_land):
                try:
                    result.update(f.result(timeout=20) or {})
                except Exception:
                    pass
            try:
                result['monthly_humidity'] = f_humidity.result(timeout=20)
            except Exception:
                result['monthly_humidity'] = None
            try:
                result['hsg_class'] = f_hsg.result(timeout=15)
            except Exception:
                result['hsg_class'] = None
            try:
                lcsoil = f_lcsoil.result(timeout=15) or {}
                result['landcover_class'] = lcsoil.get('landcover')
                result['soil_class'] = lcsoil.get('soil')
            except Exception:
                pass
    except Exception:
        pass
    return result


MORPH_LABELS = [
    ('area_km2', 'Drainage area', 'km2'),
    ('perimeter_km', 'Perimeter', 'km'),
    ('basin_length_km', 'Basin length', 'km'),
    ('main_stream_length_km', 'Main stream length', 'km'),
    ('form_factor', 'Form factor', ''),
    ('circularity_ratio', 'Circularity ratio', ''),
    ('elongation_ratio', 'Elongation ratio', ''),
    ('compactness_coefficient', 'Compactness coefficient', ''),
    ('total_stream_length_km', 'Total stream length', 'km'),
    ('num_stream_segments', 'Stream segments', ''),
    ('drainage_density_km_per_km2', 'Drainage density', 'km/km2'),
    ('stream_frequency_per_km2', 'Stream frequency', '/km2'),
    ('length_of_overland_flow_km', 'Overland flow length', 'km'),
    ('time_of_concentration_min', 'Time of concentration', 'min'),
    ('lag_time_min', 'Time lag', 'min'),
    ('avg_basin_slope', 'Average basin slope', 'm/m'),
]

# (label, unit, value-key-in-env_info, always-NA note-or-None)
ENV_LABELS = [
    ('Rainfall (trailing 12 months, total)', 'mm/yr', 'annual_rainfall_mm', None),
    ('Reference evapotranspiration — ET0 (trailing 12 months)', 'mm/yr', 'et0_annual_mm', None),
    ('Open-water / pan evaporation', 'mm/yr', None, 'not available from a free public API for an arbitrary point'),
    ('Runoff', '', None, 'no free public point-query API available'),
    ('Land use / land cover (OSM tag, at outlet point)', '', 'land_use', None),
    ('Land cover class (Sentinel-2 10m Land Cover, at outlet point)', '', 'landcover_class', None),
    ('Dominant soil type (ISRIC SoilGrids WRB, at outlet point)', '', 'soil_class', None),
    ('Hydrologic soil group (SCS runoff class, at outlet point)', '', 'hsg_class', None),
    ('Population (nearest named place, if on record)', 'people', 'population', None),
    ('Relative humidity (current)', '%', 'relative_humidity_pct', None),
    ('Wind speed (current)', 'km/h', 'wind_speed_kmh', None),
    ('Wind direction (current)', '', 'wind_direction_compass', None),
]


def build_pdf_report(lat, lng, watershed_geojson, rivers_geojson, outlets_geojson, morphology, geo_info, wiki_info, env_info=None):
    """Builds the PDF entirely with reportlab vector drawing (no external map-tile/image
    dependency, so it stays reliable on Vercel's serverless Python runtime)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.utils import simpleSplit

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    margin = 18 * mm
    x = margin
    y = page_h - margin

    TEAL = colors.HexColor('#12938a')
    TEAL_DARK = colors.HexColor('#1f7a72')
    GOLD = colors.HexColor('#b08d53')
    GREY = colors.HexColor('#555555')
    DARK = colors.HexColor('#222222')

    # ---- Header ----
    c.setFillColor(TEAL_DARK)
    c.setFont('Helvetica-Bold', 20)
    c.drawString(x, y, 'Manabi — Watershed Report')
    y -= 8 * mm
    c.setFillColor(GREY)
    c.setFont('Helvetica', 9)
    generated = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    c.drawString(x, y, f'Generated {generated}   |   Outlet: {lat:.5f}, {lng:.5f}')
    y -= 6 * mm
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(x, y, page_w - margin, y)
    y -= 10 * mm

    # ---- Site information ----
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Site information')
    y -= 7 * mm
    c.setFont('Helvetica', 10)
    c.setFillColor(colors.black)
    place_line = None
    if geo_info:
        parts = [p for p in [geo_info.get('place'), geo_info.get('region'), geo_info.get('country')] if p]
        if parts:
            place_line = ', '.join(dict.fromkeys(parts))  # dedupe while preserving order
    if place_line:
        c.drawString(x, y, f'Nearest named location: {place_line}')
        y -= 5.5 * mm
    else:
        c.drawString(x, y, 'Nearest named location: not available')
        y -= 5.5 * mm
    c.drawString(x, y, f'Coordinates (outlet): {lat:.5f}, {lng:.5f}')
    y -= 5.5 * mm

    if wiki_info and wiki_info.get('extract'):
        y -= 2 * mm
        c.setFont('Helvetica-Bold', 10)
        c.drawString(x, y, f"About {wiki_info.get('title')} (Wikipedia)")
        y -= 5 * mm
        c.setFont('Helvetica', 9)
        max_width = page_w - 2 * margin
        lines = simpleSplit(wiki_info['extract'], 'Helvetica', 9, max_width)
        for line in lines[:10]:
            c.drawString(x, y, line)
            y -= 4.6 * mm
        c.setFont('Helvetica-Oblique', 7)
        c.setFillColor(GREY)
        c.drawString(x, y - 1 * mm, 'Source: Wikipedia (en.wikipedia.org), retrieved automatically for the nearest named place.')
        y -= 7 * mm
        c.setFillColor(colors.black)

    y -= 4 * mm

    # ---- Map 1: schematic vector outline (no basemap tiles) ----
    map_h = 78 * mm
    map_w = page_w - 2 * margin
    map_top = y
    c.setStrokeColor(colors.HexColor('#dddddd'))
    c.setFillColor(colors.HexColor('#f6f4ef'))
    c.rect(x, map_top - map_h, map_w, map_h, fill=1, stroke=1)

    try:
        bbox1 = _draw_watershed_vector(c, watershed_geojson, rivers_geojson, outlets_geojson,
                                        x, map_top - map_h, map_w, map_h, TEAL, TEAL_DARK, GOLD)
        _draw_extent_labels(c, *bbox1, x, map_top - map_h, map_w, map_h)
    except Exception:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawCentredString(x + map_w / 2, map_top - map_h / 2, 'Map preview unavailable')

    c.setFillColor(GREY)
    c.setFont('Helvetica-Oblique', 7)
    c.drawString(x, map_top - map_h - 5 * mm, 'Map 1 — schematic outline (not to scale) — teal fill: watershed boundary, teal line: river network, gold dot: outlet.')
    y = map_top - map_h - 12 * mm

    # ---- Map 2: watershed over satellite imagery ----
    if y < margin + map_h + 12 * mm:
        c.showPage()
        y = page_h - margin
    map_top2 = y
    c.setStrokeColor(colors.HexColor('#dddddd'))
    c.setFillColor(colors.HexColor('#f6f4ef'))
    c.rect(x, map_top2 - map_h, map_w, map_h, fill=1, stroke=1)

    try:
        bbox2 = _draw_satellite_map(c, watershed_geojson, rivers_geojson, outlets_geojson,
                                     x, map_top2 - map_h, map_w, map_h, TEAL, TEAL_DARK, GOLD)
        _draw_extent_labels(c, *bbox2, x, map_top2 - map_h, map_w, map_h)
    except Exception:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawCentredString(x + map_w / 2, map_top2 - map_h / 2, 'Satellite imagery unavailable')

    c.setFillColor(GREY)
    c.setFont('Helvetica-Oblique', 7)
    c.drawString(x, map_top2 - map_h - 5 * mm, 'Map 2 — watershed over satellite imagery (Esri World Imagery) — same legend as Map 1.')
    y = map_top2 - map_h - 12 * mm

    # ---- Map 3: land cover (Esri / Impact Observatory Sentinel-2 10m) ----
    c.showPage()
    y = page_h - margin
    map_top3 = y
    c.setStrokeColor(colors.HexColor('#dddddd'))
    c.setFillColor(colors.HexColor('#f6f4ef'))
    c.rect(x, map_top3 - map_h, map_w, map_h, fill=1, stroke=1)
    legend_bottom = None
    try:
        bbox3 = _draw_wms_overlay_map(c, watershed_geojson, rivers_geojson, outlets_geojson,
                                       x, map_top3 - map_h, map_w, map_h, TEAL, TEAL_DARK, GOLD,
                                       fetch_landcover_image_bytes)
        _draw_extent_labels(c, *bbox3, x, map_top3 - map_h, map_w, map_h)
    except Exception:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawCentredString(x + map_w / 2, map_top3 - map_h / 2, 'Land cover layer unavailable')

    c.setFillColor(GREY)
    c.setFont('Helvetica-Oblique', 7)
    c.drawString(x, map_top3 - map_h - 5 * mm, 'Map 3 — land cover (Esri / Impact Observatory Sentinel-2 10m Land Cover) — watershed boundary outlined in teal.')
    y = map_top3 - map_h - 9 * mm
    legend_bottom = _draw_landcover_legend(c, x, y, map_w)
    y = legend_bottom - 10 * mm

    # ---- Map 4: soil type (ISRIC SoilGrids WRB) ----
    if y < margin + map_h + 30 * mm:
        c.showPage()
        y = page_h - margin
    map_top4 = y
    c.setStrokeColor(colors.HexColor('#dddddd'))
    c.setFillColor(colors.HexColor('#f6f4ef'))
    c.rect(x, map_top4 - map_h, map_w, map_h, fill=1, stroke=1)
    try:
        bbox4 = _draw_wms_overlay_map(c, watershed_geojson, rivers_geojson, outlets_geojson,
                                       x, map_top4 - map_h, map_w, map_h, TEAL, TEAL_DARK, GOLD,
                                       fetch_soil_image_bytes)
        _draw_extent_labels(c, *bbox4, x, map_top4 - map_h, map_w, map_h)
    except Exception:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawCentredString(x + map_w / 2, map_top4 - map_h / 2, 'Soil type layer unavailable')

    c.setFillColor(GREY)
    c.setFont('Helvetica-Oblique', 7)
    c.drawString(x, map_top4 - map_h - 5 * mm, 'Map 4 — dominant soil type, World Reference Base classification (ISRIC SoilGrids, 250m resolution).')
    y = map_top4 - map_h - 9 * mm
    y = _draw_soil_legend(c, x, y, map_w)
    y -= 10 * mm

    # ---- Map 5: hydrologic soil group (SCS runoff class) ----
    if y < margin + map_h + 22 * mm:
        c.showPage()
        y = page_h - margin
    map_top5 = y
    c.setStrokeColor(colors.HexColor('#dddddd'))
    c.setFillColor(colors.HexColor('#f6f4ef'))
    c.rect(x, map_top5 - map_h, map_w, map_h, fill=1, stroke=1)
    try:
        bbox5 = _draw_wms_overlay_map(c, watershed_geojson, rivers_geojson, outlets_geojson,
                                       x, map_top5 - map_h, map_w, map_h, TEAL, TEAL_DARK, GOLD,
                                       fetch_hsg_image_bytes)
        _draw_extent_labels(c, *bbox5, x, map_top5 - map_h, map_w, map_h)
    except Exception:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawCentredString(x + map_w / 2, map_top5 - map_h / 2, 'Hydrologic soil group layer unavailable')

    c.setFillColor(GREY)
    c.setFont('Helvetica-Oblique', 7)
    c.drawString(x, map_top5 - map_h - 5 * mm,
                 'Map 5 — hydrologic soil group (SCS/NRCS runoff-potential class, HYSOGs250m, 250m resolution) — used by the curve-number runoff method.')
    y = map_top5 - map_h - 9 * mm
    y = _draw_hsg_legend(c, x, y, map_w)
    y -= 10 * mm

    # ---- Morphology table ----
    if y < 60 * mm:
        c.showPage()
        y = page_h - margin
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Morphological parameters')
    y -= 8 * mm

    row_h = 6.2 * mm
    col2_x = x + 95 * mm
    c.setFont('Helvetica-Bold', 9)
    c.setFillColor(GREY)
    c.drawString(x, y, 'Parameter')
    c.drawString(col2_x, y, 'Value')
    y -= 3 * mm
    c.setStrokeColor(colors.HexColor('#cccccc'))
    c.line(x, y, page_w - margin, y)
    y -= 5 * mm

    c.setFont('Helvetica', 9.5)
    row_i = 0
    for key, label, unit in MORPH_LABELS:
        val = (morphology or {}).get(key)
        if val is None:
            continue
        if y < margin + 15 * mm:
            c.showPage()
            y = page_h - margin
            c.setFont('Helvetica', 9.5)
        if row_i % 2 == 0:
            c.setFillColor(colors.HexColor('#f9f8f5'))
            c.rect(x, y - 1.5 * mm, page_w - 2 * margin, row_h, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.drawString(x + 1 * mm, y, label)
        val_str = f'{val}{(" " + unit) if unit else ""}'
        c.drawString(col2_x, y, val_str)
        y -= row_h
        row_i += 1

    # ---- Meteorology & environmental context (best-effort, NA when unavailable) ----
    y -= 6 * mm
    if y < margin + 45 * mm:
        c.showPage()
        y = page_h - margin
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Meteorology & environmental context')
    y -= 6 * mm
    c.setFont('Helvetica-Oblique', 8)
    c.setFillColor(GREY)
    c.drawString(x, y, 'Best-effort lookups from free public data sources for the outlet point — not part of the delineation itself.')
    y -= 6 * mm

    c.setFont('Helvetica-Bold', 9)
    c.setFillColor(GREY)
    c.drawString(x, y, 'Parameter')
    c.drawString(col2_x, y, 'Value')
    y -= 3 * mm
    c.setStrokeColor(colors.HexColor('#cccccc'))
    c.line(x, y, page_w - margin, y)
    y -= 5 * mm

    c.setFont('Helvetica', 9.5)
    row_i = 0
    env_info = env_info or {}
    for label, unit, key, na_note in ENV_LABELS:
        val = env_info.get(key) if key else None
        if y < margin + 15 * mm:
            c.showPage()
            y = page_h - margin
            c.setFont('Helvetica', 9.5)
        if row_i % 2 == 0:
            c.setFillColor(colors.HexColor('#f9f8f5'))
            c.rect(x, y - 1.5 * mm, page_w - 2 * margin, row_h, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.drawString(x + 1 * mm, y, label)
        if val is not None and val != '':
            val_str = f'{val}{(" " + unit) if unit else ""}'
            c.drawString(col2_x, y, val_str)
        else:
            c.setFillColor(GREY)
            c.drawString(col2_x, y, 'NA')
            if na_note:
                note_x = col2_x + c.stringWidth('NA  ', 'Helvetica', 9.5)
                c.setFont('Helvetica-Oblique', 7.5)
                c.drawString(note_x, y, f'({na_note})')
                c.setFont('Helvetica', 9.5)
        y -= row_h
        row_i += 1

    c.setFillColor(GREY)
    c.setFont('Helvetica-Oblique', 7)
    c.drawString(x, y - 1 * mm,
                 'Sources: Open-Meteo (open-meteo.com) for rainfall/ET0/humidity/wind; OpenStreetMap Nominatim for land use and population, where tagged; '
                 'Esri/Impact Observatory Sentinel-2 10m Land Cover for land cover class; ISRIC SoilGrids for dominant soil type; '
                 'HYSOGs250m (ORNL DAAC) for hydrologic soil group.')
    y -= 8 * mm

    # ---- Monthly climate averages (multi-year calendar-month normals) ----
    env_info = env_info or {}
    monthly = env_info.get('monthly')
    monthly_hum = env_info.get('monthly_humidity')
    if monthly and monthly.get('months'):
        chart_h = 40 * mm
        chart_gap = 8 * mm
        chart_w = page_w - 2 * margin

        y0_range = monthly.get('year_start')
        y1_range = monthly.get('year_end')
        range_str = f"{y0_range}–{y1_range}" if y0_range and y1_range else f"~{_NORMALS_YEARS} years"

        c.showPage()
        y = page_h - margin
        c.setFillColor(DARK)
        c.setFont('Helvetica-Bold', 13)
        c.drawString(x, y, 'Monthly climate averages')
        y -= 6 * mm
        c.setFont('Helvetica-Oblique', 8)
        c.setFillColor(GREY)
        c.drawString(x, y, f"Each calendar month averaged across {range_str} of Open-Meteo's historical archive — not a single year's values.")
        y -= 8 * mm

        months = monthly['months']

        # Rainfall
        if y - chart_h < margin:
            c.showPage()
            y = page_h - margin
        _draw_bar_chart(c, x, y - chart_h, chart_w, chart_h, months, monthly.get('rainfall_mm', []),
                         'mm/mo avg', TEAL_DARK, 'Average monthly rainfall', GREY, DARK)
        y -= (chart_h + chart_gap)

        # ET0
        if y - chart_h < margin:
            c.showPage()
            y = page_h - margin
        _draw_bar_chart(c, x, y - chart_h, chart_w, chart_h, months, monthly.get('et0_mm', []),
                         'mm/mo avg', GOLD, 'Average monthly reference evapotranspiration (ET0)', GREY, DARK)
        y -= (chart_h + chart_gap)

        # Wind speed, with dominant direction labeled above each bar
        if y - chart_h < margin:
            c.showPage()
            y = page_h - margin
        _draw_bar_chart(c, x, y - chart_h, chart_w, chart_h, months, monthly.get('wind_speed_kmh', []),
                         'km/h avg', TEAL, 'Average monthly wind speed (of daily max) & dominant direction', GREY, DARK,
                         top_labels=monthly.get('wind_direction_compass'))
        y -= (chart_h + chart_gap)

        # Humidity (best-effort, separate fetch — may be entirely unavailable)
        if y - chart_h < margin:
            c.showPage()
            y = page_h - margin
        if monthly_hum and monthly_hum.get('months'):
            _draw_bar_chart(c, x, y - chart_h, chart_w, chart_h, monthly_hum['months'], monthly_hum.get('humidity_pct', []),
                             '% avg', colors.HexColor('#7a8fa6'), 'Average monthly relative humidity', GREY, DARK)
        else:
            c.setFillColor(DARK)
            c.setFont('Helvetica-Bold', 10)
            c.drawString(x, y - 8, 'Average monthly relative humidity')
            c.setFillColor(GREY)
            c.setFont('Helvetica', 8)
            c.drawString(x, y - chart_h / 2, 'NA — monthly humidity series not available from the free data source used.')
        y -= (chart_h + chart_gap)

        c.setFillColor(GREY)
        c.setFont('Helvetica-Oblique', 7)
        c.drawString(x, max(y, margin),
                     'Source: Open-Meteo historical archive (open-meteo.com), ERA5-based reanalysis. Wind speed/direction: daily maximum, monthly-averaged.')

    # ---- Footer ----
    c.setFont('Helvetica-Oblique', 7)
    c.setFillColor(GREY)
    c.drawString(margin, 10 * mm,
                 'Manabi — free, open-source watershed delineation for Saudi Arabia. '
                 'Delineation: MERIT-Hydro/MERIT-Basins via mghydro.com. Elevation: OpenTopoData (SRTM 90m).')

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def _project_factory(min_lon, max_lon, min_lat, max_lat, x0, y0, w, h, pad=6):
    lon_range = max(max_lon - min_lon, 1e-9)
    lat_range = max(max_lat - min_lat, 1e-9)
    scale = min((w - 2 * pad) / lon_range, (h - 2 * pad) / lat_range)
    off_x = x0 + (w - lon_range * scale) / 2
    off_y = y0 + (h - lat_range * scale) / 2

    def transform(lon, lat):
        return (off_x + (lon - min_lon) * scale, off_y + (lat - min_lat) * scale)

    return transform


def _all_ring_coords(geometry):
    gtype = geometry.get('type')
    rings = []
    if gtype == 'Polygon':
        rings = geometry.get('coordinates', [])
    elif gtype == 'MultiPolygon':
        for poly in geometry.get('coordinates', []):
            rings.extend(poly)
    return rings


def _all_line_coords(geometry):
    gtype = geometry.get('type')
    if gtype == 'LineString':
        return [geometry.get('coordinates', [])]
    elif gtype == 'MultiLineString':
        return geometry.get('coordinates', [])
    return []


def _compute_watershed_bbox(watershed_geojson, pad_frac=0.0):
    """Returns (min_lon, max_lon, min_lat, max_lat), optionally padded by a fraction
    of the extent on each side. Raises ValueError if there is no usable geometry."""
    min_lon, max_lon, min_lat, max_lat = 180.0, -180.0, 90.0, -90.0
    found = False
    for feat in (watershed_geojson or {}).get('features', []):
        for ring in _all_ring_coords(feat.get('geometry') or {}):
            for pt in ring:
                lon, lat = pt[0], pt[1]
                min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
                min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
                found = True
    if not found:
        raise ValueError('no watershed geometry to draw')
    if pad_frac:
        lon_pad = max((max_lon - min_lon) * pad_frac, 0.005)
        lat_pad = max((max_lat - min_lat) * pad_frac, 0.005)
        min_lon -= lon_pad
        max_lon += lon_pad
        min_lat -= lat_pad
        max_lat += lat_pad
    return min_lon, max_lon, min_lat, max_lat


def _draw_overlay(c, watershed_geojson, rivers_geojson, outlets_geojson, transform, teal, teal_dark, gold, fill_alpha=0.22):
    """Draws the watershed polygon, river network and outlet markers using an
    already-built lon/lat -> page-point transform. Shared by the schematic
    (plain background) and satellite (image background) map renderers."""
    from reportlab.lib import colors as rl_colors

    # watershed fill + outline
    c.setFillColor(rl_colors.Color(teal_dark.red, teal_dark.green, teal_dark.blue, alpha=fill_alpha))
    c.setStrokeColor(gold)
    c.setLineWidth(1.2)
    for feat in (watershed_geojson or {}).get('features', []):
        for ring in _all_ring_coords(feat.get('geometry') or {}):
            if len(ring) < 3:
                continue
            p = c.beginPath()
            x0_, y0_ = transform(ring[0][0], ring[0][1])
            p.moveTo(x0_, y0_)
            for pt in ring[1:]:
                px, py = transform(pt[0], pt[1])
                p.lineTo(px, py)
            p.close()
            c.drawPath(p, fill=1, stroke=1)

    # rivers
    c.setStrokeColor(teal)
    c.setLineWidth(1.4)
    for feat in (rivers_geojson or {}).get('features', []) if rivers_geojson else []:
        for line in _all_line_coords(feat.get('geometry') or {}):
            if len(line) < 2:
                continue
            p = c.beginPath()
            px, py = transform(line[0][0], line[0][1])
            p.moveTo(px, py)
            for pt in line[1:]:
                px, py = transform(pt[0], pt[1])
                p.lineTo(px, py)
            c.drawPath(p, fill=0, stroke=1)

    # outlet points
    for feat in (outlets_geojson or {}).get('features', []) if outlets_geojson else []:
        geom = feat.get('geometry') or {}
        if geom.get('type') != 'Point':
            continue
        lon, lat = geom['coordinates'][0], geom['coordinates'][1]
        px, py = transform(lon, lat)
        is_snapped = (feat.get('properties') or {}).get('type') == 'snapped'
        c.setFillColor(gold if is_snapped else teal_dark)
        r = 2.4 if is_snapped else 1.6
        c.circle(px, py, r, fill=1, stroke=0)


def _draw_watershed_vector(c, watershed_geojson, rivers_geojson, outlets_geojson, x0, y0, w, h, teal, teal_dark, gold):
    """Plain schematic map: no basemap image, just the watershed/rivers/outlet drawn
    to fill the box (tight bbox around the geometry, small pixel margin)."""
    min_lon, max_lon, min_lat, max_lat = _compute_watershed_bbox(watershed_geojson, pad_frac=0.0)
    transform = _project_factory(min_lon, max_lon, min_lat, max_lat, x0, y0, w, h)
    _draw_overlay(c, watershed_geojson, rivers_geojson, outlets_geojson, transform, teal, teal_dark, gold)
    return (min_lon, max_lon, min_lat, max_lat)


def fetch_satellite_image_bytes(min_lon, max_lon, min_lat, max_lat, width_px=900):
    """Fetches a static satellite (Esri World Imagery) export for the given
    lon/lat bbox. Returns raw image bytes, or None on any failure (no network,
    service unavailable, etc.) so the PDF can fall back gracefully."""
    try:
        lon_range = max(max_lon - min_lon, 1e-6)
        lat_range = max(max_lat - min_lat, 1e-6)
        aspect = lon_range / lat_range
        height_px = int(round(width_px / aspect)) if aspect > 0 else width_px
        height_px = max(300, min(height_px, 1400))
        url = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
               f"?bbox={min_lon},{min_lat},{max_lon},{max_lat}&bboxSR=4326&imageSR=4326"
               f"&size={width_px},{height_px}&format=jpg&f=image")
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


def fetch_wms_image_bytes(base_url, layer, min_lon, max_lon, min_lat, max_lat, width_px=900, styles=''):
    """Generic OGC WMS 1.1.1 GetMap fetch (lon/lat = EPSG:4326, axis order lon,lat
    for CRS via SRS param). Returns raw PNG bytes or None on any failure."""
    try:
        lon_range = max(max_lon - min_lon, 1e-6)
        lat_range = max(max_lat - min_lat, 1e-6)
        aspect = lon_range / lat_range
        height_px = int(round(width_px / aspect)) if aspect > 0 else width_px
        height_px = max(300, min(height_px, 1400))
        params = {
            'SERVICE': 'WMS', 'VERSION': '1.1.1', 'REQUEST': 'GetMap',
            'LAYERS': layer, 'STYLES': styles, 'SRS': 'EPSG:4326',
            'BBOX': f'{min_lon},{min_lat},{max_lon},{max_lat}',
            'WIDTH': str(width_px), 'HEIGHT': str(height_px),
            'FORMAT': 'image/png', 'TRANSPARENT': 'TRUE',
        }
        url = base_url + ('&' if '?' in base_url else '?') + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0 (contact: elfekiamr@gmail.com)'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


LANDCOVER_SERVICE = 'https://ic.imagery1.arcgis.com/arcgis/rest/services/Sentinel2_10m_LandCover/ImageServer'

# Esri/Impact Observatory Sentinel-2 10m Land Cover — official 9-class palette.
# (pixel value, label, hex color)
LANDCOVER_CLASSES = [
    (1, 'Water', '#419bdf'),
    (2, 'Trees', '#397d49'),
    (4, 'Flooded vegetation', '#7a87c6'),
    (5, 'Crops', '#e49635'),
    (7, 'Built area', '#c4281b'),
    (8, 'Bare ground', '#a59b8f'),
    (9, 'Snow / ice', '#a8ebff'),
    (10, 'Clouds', '#616161'),
    (11, 'Rangeland', '#e3e2c3'),
]
_LANDCOVER_CLASS_NAMES = {v: label for v, label, _ in LANDCOVER_CLASSES}


def fetch_landcover_image_bytes(min_lon, max_lon, min_lat, max_lat, width_px=900):
    """Fetches a static land-cover export (Esri/Impact Observatory Sentinel-2
    10m Land Cover) for the given lon/lat bbox via the same ArcGIS ImageServer
    export pattern already used for satellite imagery. Returns raw PNG bytes,
    or None on any failure."""
    try:
        lon_range = max(max_lon - min_lon, 1e-6)
        lat_range = max(max_lat - min_lat, 1e-6)
        aspect = lon_range / lat_range
        height_px = int(round(width_px / aspect)) if aspect > 0 else width_px
        height_px = max(300, min(height_px, 1400))
        url = (f'{LANDCOVER_SERVICE}/exportImage'
               f'?bbox={min_lon},{min_lat},{max_lon},{max_lat}&bboxSR=4326&imageSR=4326'
               f'&size={width_px},{height_px}&format=png&f=image')
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


def fetch_landcover_point_class(lat, lng):
    """Best-effort ImageServer /identify point query at the outlet. Returns
    the class label string, or None on any failure."""
    try:
        geometry = json.dumps({'x': lng, 'y': lat, 'spatialReference': {'wkid': 4326}})
        params = {
            'geometry': geometry, 'geometryType': 'esriGeometryPoint',
            'sr': '4326', 'returnCatalogItems': 'false', 'f': 'json',
        }
        url = f'{LANDCOVER_SERVICE}/identify?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        raw = data.get('value')
        if raw is None:
            return None
        code = int(float(str(raw).strip()))
        return _LANDCOVER_CLASS_NAMES.get(code)
    except Exception:
        return None


def fetch_wms_image_bytes_generic(base_url, layer, min_lon, max_lon, min_lat, max_lat, width_px=900, styles=''):
    return fetch_wms_image_bytes(base_url, layer, min_lon, max_lon, min_lat, max_lat, width_px, styles)


def fetch_soil_image_bytes(min_lon, max_lon, min_lat, max_lat, width_px=900):
    return fetch_wms_image_bytes('https://maps.isric.org/mapserv?map=/map/wrb.map', 'MostProbable',
                                  min_lon, max_lon, min_lat, max_lat, width_px)


def fetch_wms_point_info(base_url, layer, lon, lat, delta=0.02):
    """GetFeatureInfo point query for a single-pixel WMS bbox centered on
    (lon, lat). Returns the parsed value as text, or None on any failure."""
    try:
        min_lon, max_lon = lon - delta, lon + delta
        min_lat, max_lat = lat - delta, lat + delta
        params = {
            'SERVICE': 'WMS', 'VERSION': '1.1.1', 'REQUEST': 'GetFeatureInfo',
            'LAYERS': layer, 'QUERY_LAYERS': layer, 'STYLES': '', 'SRS': 'EPSG:4326',
            'BBOX': f'{min_lon},{min_lat},{max_lon},{max_lat}',
            'WIDTH': '101', 'HEIGHT': '101', 'X': '50', 'Y': '50',
            'INFO_FORMAT': 'text/plain', 'FEATURE_COUNT': '1',
        }
        url = base_url + ('&' if '?' in base_url else '?') + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0 (contact: elfekiamr@gmail.com)'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode('utf-8', errors='ignore').strip()
        if not text or 'no feature' in text.lower() or 'exception' in text.lower():
            return None
        return text
    except Exception:
        return None


def fetch_wms_legend_bytes(base_url, layer):
    """Fetches the source WMS's own GetLegendGraphic PNG, so the report shows
    the layer's real color key instead of a guessed/reconstructed one.
    Returns raw PNG bytes, or None on any failure."""
    try:
        params = {
            'SERVICE': 'WMS', 'VERSION': '1.1.1', 'REQUEST': 'GetLegendGraphic',
            'LAYER': layer, 'FORMAT': 'image/png',
        }
        url = base_url + ('&' if '?' in base_url else '?') + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0 (contact: elfekiamr@gmail.com)'})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.read()
    except Exception:
        return None


def fetch_soil_legend_bytes():
    return fetch_wms_legend_bytes('https://maps.isric.org/mapserv?map=/map/wrb.map', 'MostProbable')


# Hydrologic Soil Group (HSG) — the SCS/NRCS runoff-potential classification
# (A = low runoff potential / well-drained sandy soils, through D = high
# runoff potential / poorly-drained clayey soils), used directly by the
# SCS curve-number method already referenced elsewhere in this report.
# Source: HYSOGs250m (Ross et al. 2018, ORNL DAAC, DOI:10.3334/ORNLDAAC/1566),
# a 250m global raster derived from SoilGrids texture + bedrock depth,
# served as a titiler Cloud-Optimized-GeoTIFF tile/point API.
HSG_TIF_URL = ('https://data.naturalcapitalalliance.stanford.edu/download/global/'
               'HYSOGs250m/HYSOGs250m_Soil_Groups_reclassified.tif')
HSG_TITILER_BASE = 'https://titiler-897938321824.us-west1.run.app/cog'
HSG_CLASSES = [
    (1, 'A', 'Low runoff potential — deep, well-drained, sandy soils', '#1a9850'),
    (2, 'B', 'Moderately low runoff potential — moderately fine to moderately coarse', '#91cf60'),
    (3, 'C', 'Moderately high runoff potential — fine texture, slow infiltration', '#fc8d59'),
    (4, 'D', 'High runoff potential — clayey soils, shallow or poorly drained', '#d73027'),
]
_HSG_LETTER_BY_VALUE = {v: letter for v, letter, _, _ in HSG_CLASSES}
_HSG_COLOR_BY_VALUE = {v: hexcol for v, _, _, hexcol in HSG_CLASSES}


def fetch_hsg_image_bytes(min_lon, max_lon, min_lat, max_lat, width_px=900):
    """Fetches a cropped Hydrologic Soil Group raster (HYSOGs250m) for the
    given bbox, colored by class A-D, via a titiler COG bbox-crop request.
    Returns raw PNG bytes, or None on any failure."""
    try:
        lon_range = max(max_lon - min_lon, 1e-6)
        lat_range = max(max_lat - min_lat, 1e-6)
        aspect = lon_range / lat_range
        height_px = int(round(width_px / aspect)) if aspect > 0 else width_px
        height_px = max(300, min(height_px, 1400))
        colormap = json.dumps({str(v): [int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), 255]
                                for v, _, _, c in HSG_CLASSES})
        params = {
            'url': HSG_TIF_URL, 'bidx': '1', 'colormap': colormap,
            'width': str(width_px), 'height': str(height_px),
        }
        url = (f'{HSG_TITILER_BASE}/bbox/{min_lon},{min_lat},{max_lon},{max_lat}.png'
               + '?' + urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


def fetch_hsg_point_class(lat, lng):
    """Best-effort HSG point query at the outlet via titiler's COG point
    endpoint. Returns the class letter ('A'-'D'), or None on any failure."""
    try:
        params = {'url': HSG_TIF_URL}
        url = f'{HSG_TITILER_BASE}/point/{lng},{lat}?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'Manabi-Watershed-App/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        values = data.get('values') or data.get('data')
        if not values:
            return None
        code = int(round(float(values[0])))
        return _HSG_LETTER_BY_VALUE.get(code)
    except Exception:
        return None


# The 32 official WRB Reference Soil Groups (World Reference Base for Soil
# Resources) — used to pull the real classification out of the WMS
# GetFeatureInfo response text, whatever its exact template/layout is,
# instead of naively parsing "the first line with a colon" (which can just
# as easily be response boilerplate like "GetFeatureInfo results:").
WRB_SOIL_GROUPS = [
    'Acrisols', 'Albeluvisols', 'Alisols', 'Andosols', 'Arenosols', 'Calcisols',
    'Cambisols', 'Chernozems', 'Cryosols', 'Durisols', 'Ferralsols', 'Fluvisols',
    'Gleysols', 'Gypsisols', 'Histosols', 'Kastanozems', 'Leptosols', 'Lixisols',
    'Luvisols', 'Nitisols', 'Phaeozems', 'Planosols', 'Plinthosols', 'Podzols',
    'Regosols', 'Solonchaks', 'Solonetz', 'Stagnosols', 'Umbrisols', 'Vertisols',
    'Technosols', 'Anthrosols',
]


def _extract_wrb_class(text):
    """Finds a real WRB soil-group name anywhere in a GetFeatureInfo response,
    regardless of the surrounding template text. Returns the class name, or
    None if no known class name appears (e.g. an empty/no-data pixel, or the
    server returned something other than actual feature data)."""
    if not text:
        return None
    import re
    for name in WRB_SOIL_GROUPS:
        if re.search(r'\b' + name + r'\b', text, re.IGNORECASE):
            return name
    return None


def fetch_landcover_soil_labels(lat, lng):
    """Best-effort point classification for the outlet: land-cover class name
    (ArcGIS ImageServer identify) and ISRIC dominant soil group (WMS
    GetFeatureInfo). Returns a dict with 'landcover' / 'soil' keys (each may
    be None — never raw/unparsed response text)."""
    out = {'landcover': None, 'soil': None}
    try:
        out['landcover'] = fetch_landcover_point_class(lat, lng)
    except Exception:
        pass
    try:
        soil_text = fetch_wms_point_info('https://maps.isric.org/mapserv?map=/map/wrb.map', 'MostProbable', lng, lat)
        out['soil'] = _extract_wrb_class(soil_text)
    except Exception:
        pass
    return out


def _draw_wms_overlay_map(c, watershed_geojson, rivers_geojson, outlets_geojson, x0, y0, w, h,
                           teal, teal_dark, gold, fetch_fn):
    """Watershed overlay drawn on top of a fetched WMS raster (land cover or
    soil type). Raises on any failure so the caller can show a fallback."""
    from reportlab.lib.utils import ImageReader
    from reportlab.lib import colors

    min_lon, max_lon, min_lat, max_lat = _compute_watershed_bbox(watershed_geojson, pad_frac=0.18)
    img_bytes = fetch_fn(min_lon, max_lon, min_lat, max_lat, width_px=900)
    if not img_bytes:
        raise ValueError('layer imagery unavailable')

    # Light basemap fill first, since the WMS layer is semi-opaque but not full-bleed.
    c.setFillColor(colors.HexColor('#eeeeee'))
    c.rect(x0, y0, w, h, fill=1, stroke=0)

    img = ImageReader(io.BytesIO(img_bytes))
    c.drawImage(img, x0, y0, width=w, height=h, preserveAspectRatio=False, mask='auto')

    transform = _project_factory(min_lon, max_lon, min_lat, max_lat, x0, y0, w, h, pad=0)
    _draw_overlay(c, watershed_geojson, rivers_geojson, outlets_geojson, transform, teal, teal_dark, gold, fill_alpha=0.0)
    return (min_lon, max_lon, min_lat, max_lat)


def _draw_swatch_legend(c, x0, y0, w, entries):
    """Compact wrapping color-swatch legend strip; entries = [(hexcolor, label), ...].
    Returns the y-coordinate of the last row drawn."""
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    sw = 3.2 * mm
    fx = x0
    fy = y0
    c.setFont('Helvetica', 6.8)
    for hexcol, label in entries:
        tw = c.stringWidth(label, 'Helvetica', 6.8)
        if fx + sw + tw + 5 * mm > x0 + w:
            fx = x0
            fy -= 4.6 * mm
        c.setFillColor(colors.HexColor(hexcol))
        c.rect(fx, fy, sw, sw, fill=1, stroke=0)
        c.setFillColor(colors.HexColor('#333333'))
        c.drawString(fx + sw + 1 * mm, fy, label)
        fx += sw + tw + 6 * mm
    return fy


def _draw_landcover_legend(c, x0, y0, w):
    """Land-cover legend strip using the same 9-class palette as the map
    itself (Esri / Impact Observatory Sentinel-2 10m Land Cover)."""
    entries = [(hexcol, label) for _, label, hexcol in LANDCOVER_CLASSES]
    return _draw_swatch_legend(c, x0, y0, w, entries)


def _draw_soil_legend(c, x0, y0, w):
    """Soil-type legend: embeds ISRIC's own GetLegendGraphic image (the
    authoritative color key for the ~30 WRB classes) under the map. Falls
    back to a short text note if the legend image can't be fetched."""
    from reportlab.lib.utils import ImageReader
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.units import mm
    try:
        legend_bytes = fetch_soil_legend_bytes()
        if not legend_bytes:
            raise ValueError('no legend image')
        img = ImageReader(io.BytesIO(legend_bytes))
        iw, ih = img.getSize()
        scale = min(1.0, (w * 0.9) / iw) if iw else 1.0
        # Cap the legend's rendered height so it can't blow out the page.
        max_h = 55 * mm
        draw_w = iw * scale
        draw_h = ih * scale
        if draw_h > max_h:
            scale2 = max_h / draw_h
            draw_w *= scale2
            draw_h *= scale2
        c.drawImage(img, x0, y0 - draw_h, width=draw_w, height=draw_h,
                    preserveAspectRatio=True, mask='auto')
        return y0 - draw_h
    except Exception:
        c.setFillColor(rl_colors.HexColor('#333333'))
        c.setFont('Helvetica-Oblique', 7.5)
        c.drawString(x0, y0 - 4, 'Soil-class color key unavailable — the dominant class at the outlet point is listed in the table below.')
        return y0 - 8


_HSG_SHORT_LABEL = {
    'A': 'A — low runoff potential',
    'B': 'B — moderately low',
    'C': 'C — moderately high',
    'D': 'D — high runoff potential',
}


def _draw_hsg_legend(c, x0, y0, w):
    """Hydrologic Soil Group legend: 4-class swatch strip (A-D, low to high
    runoff potential) matching the colors used to render the HSG map."""
    entries = [(hexcol, _HSG_SHORT_LABEL[letter]) for _, letter, _, hexcol in HSG_CLASSES]
    return _draw_swatch_legend(c, x0, y0, w, entries)


def _draw_satellite_map(c, watershed_geojson, rivers_geojson, outlets_geojson, x0, y0, w, h, teal, teal_dark, gold):
    """Watershed overlay drawn on top of a fetched satellite image. Raises on
    any failure (missing geometry or unreachable imagery service) so the
    caller can show a fallback message instead."""
    from reportlab.lib.utils import ImageReader

    min_lon, max_lon, min_lat, max_lat = _compute_watershed_bbox(watershed_geojson, pad_frac=0.18)
    img_bytes = fetch_satellite_image_bytes(min_lon, max_lon, min_lat, max_lat, width_px=900)
    if not img_bytes:
        raise ValueError('satellite imagery unavailable')

    img = ImageReader(io.BytesIO(img_bytes))
    c.drawImage(img, x0, y0, width=w, height=h, preserveAspectRatio=False, mask='auto')

    # pad=0: the image already covers exactly [min_lon,max_lon] x [min_lat,max_lat]
    transform = _project_factory(min_lon, max_lon, min_lat, max_lat, x0, y0, w, h, pad=0)
    _draw_overlay(c, watershed_geojson, rivers_geojson, outlets_geojson, transform, teal, teal_dark, gold, fill_alpha=0.28)
    return (min_lon, max_lon, min_lat, max_lat)


def _draw_extent_labels(c, min_lon, max_lon, min_lat, max_lat, x0, y0, w, h):
    """Small lat/lon coordinate chips at the top-left and bottom-right corners
    of a map box, so each image carries its own geographic reference."""
    from reportlab.lib import colors as rl_colors

    def chip(text, cx, cy, align='left'):
        c.setFont('Helvetica', 6.5)
        tw = c.stringWidth(text, 'Helvetica', 6.5)
        pad = 1.6
        rx = cx if align == 'left' else cx - tw
        c.setFillColor(rl_colors.Color(1, 1, 1, alpha=0.8))
        c.rect(rx - pad, cy - pad, tw + 2 * pad, 7.8, fill=1, stroke=0)
        c.setFillColor(rl_colors.HexColor('#222222'))
        c.drawString(rx, cy, text)

    label_tl = f'{max_lat:.4f}°N, {min_lon:.4f}°E'
    label_br = f'{min_lat:.4f}°N, {max_lon:.4f}°E'
    chip(label_tl, x0 + 3, y0 + h - 10, align='left')
    chip(label_br, x0 + w - 3, y0 + 3, align='right')


_MONTH_ABBR = {1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr', 5: 'May', 6: 'Jun',
               7: 'Jul', 8: 'Aug', 9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'}


def _month_short_label(ym, is_endpoint=False):
    try:
        year, month = ym.split('-')
        abbr = _MONTH_ABBR.get(int(month), month)
        return f"{abbr} '{year[2:]}" if is_endpoint else abbr
    except Exception:
        return ym


def _draw_bar_chart(c, x0, y0, w, h, months, values, unit, bar_color, title, grey, dark, top_labels=None):
    """A minimal, dependency-free monthly bar chart drawn straight onto the
    reportlab canvas — consistent with how the maps are drawn (no external
    charting library). `months` are 'YYYY-MM' strings in chronological order."""
    from reportlab.lib import colors as rl_colors

    c.setFillColor(dark)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(x0, y0 + h - 8, title)

    n = len(months)
    if n == 0 or all(v is None for v in values):
        c.setFillColor(grey)
        c.setFont('Helvetica', 8)
        c.drawString(x0, y0 + h / 2, 'No data available')
        return

    axis_label_h = 9
    top_label_h = 8 if top_labels else 0
    plot_top = y0 + h - 16 - top_label_h
    plot_bottom = y0 + axis_label_h
    plot_h = max(plot_top - plot_bottom, 1)

    numeric_vals = [v for v in values if v is not None]
    max_val = max(numeric_vals) if numeric_vals else 1
    if max_val <= 0:
        max_val = 1

    bar_gap = 1.2
    bar_w = (w - bar_gap * (n - 1)) / n if n else w

    c.setStrokeColor(rl_colors.HexColor('#cccccc'))
    c.setLineWidth(0.6)
    c.line(x0, plot_bottom, x0 + w, plot_bottom)

    c.setFont('Helvetica', 5.6)
    for i, (ym, val) in enumerate(zip(months, values)):
        bx = x0 + i * (bar_w + bar_gap)
        if val is not None:
            bar_h = (val / max_val) * plot_h
            c.setFillColor(bar_color)
            c.rect(bx, plot_bottom, bar_w, bar_h, fill=1, stroke=0)
            if top_labels and top_labels[i]:
                c.setFillColor(grey)
                c.setFont('Helvetica', 5.2)
                c.drawCentredString(bx + bar_w / 2, plot_bottom + bar_h + 1.5, str(top_labels[i]))
                c.setFont('Helvetica', 5.6)
        is_endpoint = (i == 0 or i == n - 1)
        c.setFillColor(grey)
        c.drawCentredString(bx + bar_w / 2, y0 + 1, _month_short_label(ym, is_endpoint))

    c.setFillColor(grey)
    c.setFont('Helvetica', 6.5)
    c.drawString(x0, plot_top + 2, f'max {max_val:g} {unit}'.strip())


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


@app.route('/api/report', methods=['POST', 'GET'])
@app.route('/report', methods=['POST', 'GET'])
def report():
    if request.method == 'GET':
        return jsonify({'status': 'API endpoint active. Send POST request with lat/lng/watershed/rivers/outlets/morphology.'}), 200
    try:
        data = request.get_json(force=True, silent=True) or {}
        lat = data.get('lat')
        lng = data.get('lng')
        watershed_geojson = data.get('watershed')
        rivers_geojson = data.get('rivers')
        outlets_geojson = data.get('outlets')
        morphology = data.get('morphology')

        if lat is None or lng is None or not watershed_geojson:
            return jsonify({'error': 'lat, lng and a watershed GeoJSON are required. Delineate a watershed first.'}), 400

        lat = float(lat)
        lng = float(lng)

        # Run the geocoding lookup and the environmental-data lookups concurrently —
        # they're independent, unrelated web requests, so there's no reason to
        # wait on one before starting the others.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_geo = ex.submit(reverse_geocode, lat, lng)
            f_env = ex.submit(fetch_environmental_context, lat, lng)
            try:
                geo_info = f_geo.result(timeout=15)
            except Exception:
                geo_info = {}
            try:
                env_info = f_env.result(timeout=25)
            except Exception:
                env_info = {}

        wiki_title = geo_info.get('place') or geo_info.get('region')
        wiki_info = wikipedia_summary(wiki_title)

        pdf_bytes = build_pdf_report(lat, lng, watershed_geojson, rivers_geojson, outlets_geojson,
                                      morphology, geo_info, wiki_info, env_info)

        filename = f"manabi_watershed_report_{lat:.4f}_{lng:.4f}.pdf"
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return jsonify({'message': 'KSA Watersheds API active'}), 200
