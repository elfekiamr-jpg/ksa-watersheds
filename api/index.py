from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import urllib.request
import urllib.parse
import json
import math
import io
import datetime
import concurrent.futures
import bisect
from collections import defaultdict, deque

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


# ---------- Geomorphological Instantaneous Unit Hydrograph (GIUH) ----------
#
# Rodriguez-Iturbe & Valdes (1979): the IUH shape can be derived entirely from
# a basin's Horton ratios (bifurcation RB, length RL, area RA), with no
# calibration against an observed hydrograph. We use the Rosso (1984) closed
# form, which expresses the IUH as a two-parameter gamma density.
#
# Data available: each MERIT-Basins reach returned by mghydro's
# upstream_rivers_api already carries a Strahler stream order ('sorder'), so
# Nw (stream count) and Lw (mean length) per order come directly from
# grouping the returned segments — no network topology needs to be inferred
# for those two ratios.
#
# RA (area ratio) is the one ratio that normally requires a sub-basin polygon
# per stream order, which isn't available here. We approximate it in two
# steps: (1) reconstruct the upstream/downstream topology of the reach
# network purely from shared endpoint coordinates, rooted at the outlet
# (MERIT reach lines are not guaranteed to be given upstream to downstream,
# and carry no explicit up/downstream reach id); (2) estimate the drainage
# area upstream of each reach as (cumulative upstream stream length) /
# (basin-average drainage density) — a standard estimator for basins with
# roughly uniform drainage density, avoiding extra delineation calls per
# reach. This is an approximation, not a true zonal computation, and is
# reported as such.

def _round_node(pt, tol=5):
    """Snap a line endpoint to a fixed precision so two segments that share a
    confluence (but were digitized independently) land on the same node key."""
    return (round(pt[0], tol), round(pt[1], tol))


def _extract_river_segments(rivers_geojson):
    """Flattens the rivers GeoJSON into a list of segment dicts carrying the
    raw coordinates, Strahler order, and length — the same flattening
    rivers_metrics() does, but keeping order/geometry instead of collapsing
    straight to totals."""
    segs = []
    if not rivers_geojson or 'features' not in rivers_geojson:
        return segs
    for feat in rivers_geojson['features']:
        geom = feat.get('geometry') or {}
        props = feat.get('properties') or {}
        gtype = geom.get('type')
        lines = []
        if gtype == 'LineString':
            lines = [geom.get('coordinates', [])]
        elif gtype == 'MultiLineString':
            lines = geom.get('coordinates', [])
        sorder = props.get('sorder')
        try:
            sorder = int(sorder) if sorder is not None else None
        except (TypeError, ValueError):
            sorder = None
        for line in lines:
            if len(line) < 2:
                continue
            segs.append({
                'coords': line,
                'sorder': sorder,
                'length_km': line_length_km(line),
            })
    return segs


def _resolve_river_topology(segments, outlet_lat, outlet_lng):
    """Roots the (undirected) endpoint graph at the node nearest the outlet
    and BFS's outward, labelling each segment's downstream/upstream endpoint.
    Mutates and returns `segments`; a segment the BFS never reaches (a
    disconnected fragment from an endpoint-snapping mismatch) is left with
    resolved=False and is excluded from the cumulative-length pass but still
    counted toward Nw/Lw."""
    node_segs = defaultdict(list)
    for i, seg in enumerate(segments):
        a = _round_node(seg['coords'][0])
        b = _round_node(seg['coords'][-1])
        seg['node_a'], seg['node_b'] = a, b
        seg['resolved'] = False
        node_segs[a].append(i)
        node_segs[b].append(i)

    if not segments:
        return segments

    outlet_pt = (float(outlet_lng), float(outlet_lat))
    root = min(node_segs.keys(), key=lambda n: (n[0] - outlet_pt[0]) ** 2 + (n[1] - outlet_pt[1]) ** 2)

    visited_nodes = {root}
    visited_segs = set()
    queue = deque([root])
    while queue:
        node = queue.popleft()
        for si in node_segs[node]:
            if si in visited_segs:
                continue
            seg = segments[si]
            a, b = seg['node_a'], seg['node_b']
            downstream_node = node
            upstream_node = b if a == node else a
            seg['downstream_node'] = downstream_node
            seg['upstream_node'] = upstream_node
            seg['resolved'] = True
            visited_segs.add(si)
            if upstream_node not in visited_nodes:
                visited_nodes.add(upstream_node)
                queue.append(upstream_node)

    return segments


def _cumulative_upstream_lengths_km(segments):
    """For each resolved segment, its own length plus the length of every
    segment upstream of it (its whole upstream subtree). Returns a dict
    keyed by segment index."""
    children = defaultdict(list)
    for i, seg in enumerate(segments):
        if seg.get('resolved'):
            children[seg['downstream_node']].append(i)

    memo = {}

    def cum(i):
        if i in memo:
            return memo[i]
        seg = segments[i]
        total = seg['length_km']
        for j in children.get(seg['upstream_node'], []):
            if j != i:
                total += cum(j)
        memo[i] = total
        return total

    for i, seg in enumerate(segments):
        if seg.get('resolved'):
            cum(i)
    return memo


def _geometric_mean_step_ratio(values_by_order, orders, invert=False):
    """Geometric mean of values[order+1]/values[order] across consecutive
    orders present in both. invert=True is for stream counts, which
    decrease with order (Horton's RB is conventionally Nw/Nw+1 > 1)."""
    ratios = []
    for o in orders[:-1]:
        v0, v1 = values_by_order.get(o), values_by_order.get(o + 1)
        if v0 and v1 and v0 > 0:
            ratios.append(v1 / v0)
    if not ratios:
        return None
    product = 1.0
    for r in ratios:
        product *= r
    gm = product ** (1.0 / len(ratios))
    return (1.0 / gm) if invert else gm


def compute_giuh(rivers_geojson, outlet_lat, outlet_lng, area_km2, drainage_density,
                  main_stream_length_km, tc_minutes):
    """Geomorphological Instantaneous Unit Hydrograph via Rodriguez-Iturbe &
    Valdes (1979) / Rosso (1984). Returns {'available': False} if the reach
    network doesn't carry enough distinct stream orders, or a dict with the
    Horton ratios, the gamma-IUH parameters, and a plotted (t, u) curve."""
    result = {'available': False}
    if not rivers_geojson or not area_km2 or not drainage_density or not tc_minutes:
        return result

    segments = _extract_river_segments(rivers_geojson)
    segments = _resolve_river_topology(segments, outlet_lat, outlet_lng)

    by_order = defaultdict(list)
    for i, seg in enumerate(segments):
        if seg['sorder']:
            by_order[seg['sorder']].append(i)
    orders = sorted(by_order.keys())
    if len(orders) < 2:
        return result
    omega = orders[-1]

    N = {o: len(by_order[o]) for o in orders}
    L = {o: sum(segments[i]['length_km'] for i in by_order[o]) / len(by_order[o]) for o in orders}

    cum_lengths = _cumulative_upstream_lengths_km(segments)
    A = {}
    for o in orders:
        idxs = [i for i in by_order[o] if segments[i].get('resolved')]
        if idxs:
            A[o] = (sum(cum_lengths[i] for i in idxs) / len(idxs)) / drainage_density
    # Fall back on the basin-scale estimate at the two ends if the endpoint-
    # matching topology reconstruction left them without resolved segments.
    if omega not in A:
        A[omega] = area_km2
    if orders[0] not in A and N.get(orders[0]):
        A[orders[0]] = area_km2 / N[orders[0]]

    RB = _geometric_mean_step_ratio(N, orders, invert=True)
    RL = _geometric_mean_step_ratio(L, orders, invert=False)
    RA = _geometric_mean_step_ratio(A, orders, invert=False)

    if not RB or not RL or not RA or RB <= 1 or RL <= 1 or RA <= 1:
        return result

    L_omega = L.get(omega) or main_stream_length_km
    if not L_omega:
        return result

    # Characteristic channel velocity backed out from the basin's own Kirpich
    # time of concentration (V = L_omega / Tc), so no new empirical constant
    # is introduced beyond what Manabi already computes.
    tc_hr = tc_minutes / 60.0
    if tc_hr <= 0:
        return result
    V_kmh = L_omega / tc_hr
    if V_kmh <= 0:
        return result

    n_shape = 3.29 * ((RB / RA) ** 0.78) * (RL ** 0.07)
    k_scale_hr = 0.70 * ((RB / RA) ** -0.48) * (RL ** 0.48) * (L_omega / V_kmh)

    if n_shape <= 1 or k_scale_hr <= 0:
        return result

    tp_hr = (n_shape - 1) * k_scale_hr

    def u(t_hr):
        if t_hr <= 0:
            return 0.0
        return ((1.0 / (k_scale_hr * math.gamma(n_shape)))
                * ((t_hr / k_scale_hr) ** (n_shape - 1))
                * math.exp(-t_hr / k_scale_hr))

    qp = u(tp_hr)
    t_max = max(tp_hr * 5.0, k_scale_hr * (n_shape + 4 * math.sqrt(n_shape)))
    n_points = 60
    curve = [{'t_hr': round(t_max * i / n_points, 3), 'u': u(t_max * i / n_points)}
             for i in range(n_points + 1)]

    result.update({
        'available': True,
        'omega': omega,
        'orders': orders,
        'N': N,
        'L_km': {o: round(v, 3) for o, v in L.items()},
        'A_km2': {o: round(v, 2) for o, v in A.items()},
        'RB': round(RB, 3),
        'RL': round(RL, 3),
        'RA': round(RA, 3),
        'main_stream_length_km': round(L_omega, 2),
        'velocity_km_per_hr': round(V_kmh, 3),
        'n_shape': round(n_shape, 3),
        'k_scale_hr': round(k_scale_hr, 3),
        'tp_hr': round(tp_hr, 3),
        'tp_min': round(tp_hr * 60.0, 1),
        'qp_per_hr': round(qp, 5),
        'curve': curve,
    })
    return result


# ---------- Relief, hypsometry, and main-channel longitudinal profile ----------
#
# Total relief (H = Zmax - Zmin), the hypsometric curve/integral, and the
# channel profile all need elevations at many points, not just the two
# (outlet, farthest point) used for Kirpich Tc. To keep this to a single
# extra network round trip, both point sets are built first (a grid inside
# the watershed for hypsometry, a resampled main-channel path for the
# profile) and their elevations are fetched together in one batched
# OpenTopoData call.

def _trace_main_channel(rivers_geojson, outlet_lat, outlet_lng):
    """Traces the main channel from the outlet to its farthest headwater by
    following, at every confluence, whichever tributary has the greater
    cumulative upstream stream length — reusing the same topology
    reconstruction as the GIUH computation (reconstructed purely from
    shared endpoint coordinates, since MERIT reaches carry no explicit
    up/downstream reach id). Returns an ordered list of [lon, lat]
    coordinates from the outlet to the headwater, or [] if the network
    couldn't be resolved."""
    segments = _extract_river_segments(rivers_geojson)
    segments = _resolve_river_topology(segments, outlet_lat, outlet_lng)
    resolved_idxs = [i for i, s in enumerate(segments) if s.get('resolved')]
    if not resolved_idxs:
        return []
    cum = _cumulative_upstream_lengths_km(segments)

    children = defaultdict(list)
    for i in resolved_idxs:
        children[segments[i]['downstream_node']].append(i)

    start = max(resolved_idxs, key=lambda i: cum.get(i, 0.0))

    path_coords = []
    visited = set()
    cur = start
    while cur is not None and cur not in visited:
        visited.add(cur)
        seg = segments[cur]
        coords = seg['coords']
        ordered = coords if _round_node(coords[0]) == seg['downstream_node'] else list(reversed(coords))
        path_coords.extend(ordered if not path_coords else ordered[1:])
        kids = [k for k in children.get(seg['upstream_node'], []) if k != cur]
        cur = max(kids, key=lambda i: cum.get(i, 0.0)) if kids else None
    return path_coords


def _resample_path_by_distance(coords, n_samples):
    """Resamples a [lon, lat] polyline to n_samples points evenly spaced by
    cumulative haversine distance along it. Returns a list of
    (lat, lon, cumulative_distance_km) tuples, distance measured from
    coords[0]."""
    if len(coords) < 2 or n_samples < 2:
        return []
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_km(coords[i - 1][1], coords[i - 1][0], coords[i][1], coords[i][0]))
    total = cum[-1]
    if total <= 0:
        return [(coords[0][1], coords[0][0], 0.0)]

    out = []
    j = 0
    for k in range(n_samples):
        t = total * k / (n_samples - 1)
        while j < len(cum) - 2 and cum[j + 1] < t:
            j += 1
        seg_len = cum[j + 1] - cum[j]
        frac = (t - cum[j]) / seg_len if seg_len > 0 else 0.0
        lon = coords[j][0] + frac * (coords[j + 1][0] - coords[j][0])
        lat = coords[j][1] + frac * (coords[j + 1][1] - coords[j][1])
        out.append((lat, lon, t))
    return out


def compute_hypsometry_sample_points(watershed_geojson, target_points=50):
    """Grid-samples points inside the watershed polygon for elevation lookup
    (same oversample-then-keep-inside approach as compute_composite_cn).
    Returns a list of (lat, lon) tuples, or [] on failure."""
    try:
        min_lon, max_lon, min_lat, max_lat = _compute_watershed_bbox(watershed_geojson)
    except Exception:
        return []
    grid_n = 12
    candidates = []
    for i in range(grid_n):
        for j in range(grid_n):
            lon = min_lon + (max_lon - min_lon) * (i + 0.5) / grid_n
            lat = min_lat + (max_lat - min_lat) * (j + 0.5) / grid_n
            if _point_in_watershed(lon, lat, watershed_geojson):
                candidates.append((lat, lon))
    if len(candidates) > target_points:
        step = len(candidates) / target_points
        candidates = [candidates[int(i * step)] for i in range(target_points)]
    return candidates


def compute_relief_hypsometry_and_profile(watershed_geojson, rivers_geojson, outlet_lat, outlet_lng, area_km2=None):
    """Total relief, hypsometric curve/integral, main-channel longitudinal
    profile, and (when `area_km2` is supplied) the outlet area-elevation and
    capacity(storage)-elevation curves — fetches every elevation this needs
    (the hypsometric grid plus the resampled channel path) in a single
    batched OpenTopoData request. Returns
    {'hypsometry': {...}, 'profile': {...}, 'area_capacity': {...}}, each
    with 'available': False if it couldn't be computed."""
    result = {
        'hypsometry': {'available': False},
        'profile': {'available': False},
        'area_capacity': {'available': False},
    }

    hyp_coords = compute_hypsometry_sample_points(watershed_geojson)
    channel_path = _trace_main_channel(rivers_geojson, outlet_lat, outlet_lng)
    prof_samples = _resample_path_by_distance(channel_path, 30) if len(channel_path) >= 2 else []

    all_coords = hyp_coords + [(lat, lon) for lat, lon, _ in prof_samples]
    if not all_coords:
        return result
    try:
        elevations = get_elevations(all_coords)
    except Exception:
        return result

    n_hyp = len(hyp_coords)
    hyp_elevs = [e for e in elevations[:n_hyp] if e is not None]
    prof_elevs = elevations[n_hyp:n_hyp + len(prof_samples)]

    if len(hyp_elevs) >= 8:
        zmax, zmin = max(hyp_elevs), min(hyp_elevs)
        zmean = sum(hyp_elevs) / len(hyp_elevs)
        H = zmax - zmin
        if H > 0:
            sorted_desc = sorted(hyp_elevs, reverse=True)
            n = len(sorted_desc)
            curve = []
            for i, z in enumerate(sorted_desc):
                rel_area = i / (n - 1) if n > 1 else 0.0
                rel_elev = (z - zmin) / H
                curve.append({'rel_area': round(rel_area, 4), 'rel_elev': round(rel_elev, 4)})
            hi = 0.0
            for i in range(1, len(curve)):
                x0, x1 = curve[i - 1]['rel_area'], curve[i]['rel_area']
                y0, y1 = curve[i - 1]['rel_elev'], curve[i]['rel_elev']
                hi += (x1 - x0) * (y0 + y1) / 2.0
            result['hypsometry'] = {
                'available': True,
                'n_points': n,
                'z_max_m': round(zmax, 1),
                'z_min_m': round(zmin, 1),
                'z_mean_m': round(zmean, 1),
                'total_relief_m': round(H, 1),
                'hypsometric_integral': round(hi, 4),
                'curve': curve,
            }

            # ---- area-elevation and capacity(storage)-elevation curves at the
            # outlet, derived from the same hypsometric elevation sample. Treats
            # the whole upstream watershed as the flood extent at each elevation
            # (area of the basin with elevation <= z) — a standard DEM/hypsometry-
            # based estimator when no dedicated reservoir bathymetry/rim survey is
            # available; see the caveat note drawn alongside it in the report.
            if area_km2 and area_km2 > 0:
                sorted_asc = sorted(hyp_elevs)
                n_pts = len(sorted_asc)
                n_levels = 21
                dz = H / (n_levels - 1)
                levels = [zmin + dz * i for i in range(n_levels)]
                rows = []
                cum_vol_m3 = 0.0
                prev_area_m2 = 0.0
                for i, z in enumerate(levels):
                    cnt = bisect.bisect_right(sorted_asc, z)
                    area_at_z_km2 = (cnt / n_pts) * area_km2
                    area_at_z_m2 = area_at_z_km2 * 1.0e6
                    if i > 0:
                        cum_vol_m3 += (prev_area_m2 + area_at_z_m2) / 2.0 * dz
                    prev_area_m2 = area_at_z_m2
                    rows.append({
                        'elev_m': round(z, 1),
                        'area_km2': round(area_at_z_km2, 3),
                        'cum_volume_mcm': round(cum_vol_m3 / 1.0e6, 4),
                    })
                result['area_capacity'] = {
                    'available': True,
                    'area_km2_total': round(area_km2, 2),
                    'z_min_m': round(zmin, 1),
                    'z_max_m': round(zmax, 1),
                    'total_capacity_mcm': rows[-1]['cum_volume_mcm'] if rows else 0.0,
                    'rows': rows,
                }

    if prof_samples and any(e is not None for e in prof_elevs):
        points = [{'dist_km': round(d, 3), 'elev_m': round(e, 1)}
                  for (_, _, d), e in zip(prof_samples, prof_elevs) if e is not None]
        if len(points) >= 2:
            result['profile'] = {
                'available': True,
                'total_length_km': round(prof_samples[-1][2], 2),
                'elev_outlet_m': points[0]['elev_m'],
                'elev_headwater_m': points[-1]['elev_m'],
                'points': points,
            }

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


def build_pdf_report(lat, lng, watershed_geojson, rivers_geojson, outlets_geojson, morphology, geo_info, wiki_info, env_info=None, cn_info=None, giuh_info=None, relief_info=None):
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

    # Pre-fetch every map raster + legend concurrently, up front. These were
    # previously fetched one at a time inside each _draw_*_map() call — five
    # sequential HTTP round trips (each with its own internal timeout of up
    # to 15s) stacked back to back is enough on its own to exceed Vercel's
    # function time budget when any one of the tile/WMS services is slow.
    # Running them in parallel caps the wait on the slowest single service
    # instead of their sum.
    prefetched_images = {}
    try:
        map_bbox = _compute_watershed_bbox(watershed_geojson, pad_frac=0.18)
    except Exception:
        map_bbox = None
    if map_bbox:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as _img_ex:
            _f_sat = _img_ex.submit(fetch_satellite_image_bytes, *map_bbox, width_px=900)
            _f_lc = _img_ex.submit(fetch_landcover_image_bytes, *map_bbox, width_px=900)
            _f_soil = _img_ex.submit(fetch_soil_image_bytes, *map_bbox, width_px=900)
            _f_hsg = _img_ex.submit(fetch_hsg_image_bytes, *map_bbox, width_px=900)
            _f_soil_legend = _img_ex.submit(fetch_soil_legend_bytes)

            def _wait(fut, timeout=20):
                try:
                    return fut.result(timeout=timeout)
                except Exception:
                    return None

            prefetched_images = {
                'satellite': _wait(_f_sat),
                'landcover': _wait(_f_lc),
                'soil': _wait(_f_soil),
                'hsg': _wait(_f_hsg),
                'soil_legend': _wait(_f_soil_legend),
            }
    # If prefetching was skipped or a given fetch failed, the per-map draw
    # calls below fall back to fetching (and failing/reporting "unavailable")
    # individually, exactly as before this change.

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
                                     x, map_top2 - map_h, map_w, map_h, TEAL, TEAL_DARK, GOLD,
                                     img_bytes=prefetched_images.get('satellite'))
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
                                       fetch_landcover_image_bytes,
                                       img_bytes=prefetched_images.get('landcover'))
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
                                       fetch_soil_image_bytes,
                                       img_bytes=prefetched_images.get('soil'))
        _draw_extent_labels(c, *bbox4, x, map_top4 - map_h, map_w, map_h)
    except Exception:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawCentredString(x + map_w / 2, map_top4 - map_h / 2, 'Soil type layer unavailable')

    c.setFillColor(GREY)
    c.setFont('Helvetica-Oblique', 7)
    c.drawString(x, map_top4 - map_h - 5 * mm, 'Map 4 — dominant soil type, World Reference Base classification (ISRIC SoilGrids, 250m resolution).')
    y = map_top4 - map_h - 9 * mm
    y = _draw_soil_legend(c, x, y, map_w, legend_bytes=prefetched_images.get('soil_legend'))
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
                                       fetch_hsg_image_bytes,
                                       img_bytes=prefetched_images.get('hsg'))
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
    y -= 6 * mm

    # Classification basis — what determines the A-D class, so the map isn't a black box.
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 8.5)
    c.drawString(x, y, 'How the class is determined')
    y -= 4.5 * mm
    hsg_basis_text = (
        "Classes are based on the soil's intrinsic ability to absorb water, not on rainfall, land cover, or slope: "
        '(1) soil texture — sand/silt/clay proportions, the main driver of infiltration rate; '
        '(2) depth to a restrictive layer — bedrock, hardpan, or another dense layer that blocks downward drainage; '
        '(3) depth to the water table — how much unsaturated capacity the soil has left; and '
        '(4) saturated hydraulic conductivity (Ksat) — the direct physical measurement the other three are proxies for. '
        'A = deep, well-drained, high infiltration (low runoff); D = clay-dominated, shallow, or a high water table, '
        'with infiltration severely restricted (high runoff). This dataset estimates the class from texture and bedrock-depth '
        'data, since Ksat is not measured directly at global scale.'
    )
    c.setFont('Helvetica', 7.5)
    c.setFillColor(colors.HexColor('#333333'))
    hsg_lines = simpleSplit(hsg_basis_text, 'Helvetica', 7.5, map_w)
    for line in hsg_lines:
        if y < margin + 8 * mm:
            c.showPage()
            y = page_h - margin
            c.setFont('Helvetica', 7.5)
            c.setFillColor(colors.HexColor('#333333'))
        c.drawString(x, y, line)
        y -= 3.6 * mm
    y -= 8 * mm

    # ---- Composite curve number (SCS/NRCS method) ----
    if y < margin + 40 * mm:
        c.showPage()
        y = page_h - margin
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Composite curve number (SCS/NRCS method)')
    y -= 7 * mm

    if cn_info and cn_info.get('composite_cn') is not None:
        cn_val = cn_info['composite_cn']
        n_valid = cn_info.get('n_valid', 0)
        n_sampled = cn_info.get('n_sampled', 0)
        c.setFillColor(TEAL_DARK)
        c.setFont('Helvetica-Bold', 22)
        c.drawString(x, y - 5 * mm, f'CN = {cn_val:.0f}')
        c.setFillColor(GREY)
        c.setFont('Helvetica', 8.5)
        c.drawString(x + 40 * mm, y - 3.5 * mm,
                     f'Area-weighted across {n_valid} of {n_sampled} sampled points inside the watershed boundary,')
        c.drawString(x + 40 * mm, y - 7.5 * mm,
                     'each classified by land cover and hydrologic soil group, per TR-55.')
        y -= 15 * mm

        breakdown = cn_info.get('breakdown') or []
        if breakdown:
            col_a, col_b, col_c, col_d = x, x + 62 * mm, x + 90 * mm, x + 118 * mm
            c.setFont('Helvetica-Bold', 8.5)
            c.setFillColor(GREY)
            c.drawString(col_a, y, 'Land cover')
            c.drawString(col_b, y, 'Soil group')
            c.drawString(col_c, y, 'Area share')
            c.drawString(col_d, y, 'CN')
            y -= 3 * mm
            c.setStrokeColor(colors.HexColor('#cccccc'))
            c.line(x, y, page_w - margin, y)
            y -= 5 * mm
            c.setFont('Helvetica', 8.5)
            row_i = 0
            for row in breakdown:
                if y < margin + 10 * mm:
                    c.showPage()
                    y = page_h - margin
                    c.setFont('Helvetica', 8.5)
                if row_i % 2 == 0:
                    c.setFillColor(colors.HexColor('#f9f8f5'))
                    c.rect(x, y - 1.5 * mm, page_w - 2 * margin, 6 * mm, fill=1, stroke=0)
                c.setFillColor(colors.black)
                c.drawString(col_a + 1 * mm, y, row['landcover'])
                c.drawString(col_b, y, row['hsg'])
                c.drawString(col_c, y, f"{row['pct']:.0f}%")
                c.drawString(col_d, y, str(row['cn']))
                y -= 6 * mm
                row_i += 1
            y -= 3 * mm
    else:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawString(x, y, 'NA — could not sample enough classified points across the watershed to compute a composite CN.')
        y -= 10 * mm

    cn_note = (
        'Curve numbers assume "good" hydrologic condition and average antecedent moisture (AMC II), per TR-55 Table 2-2. '
        'Land-cover classes are crosswalked to the nearest standard TR-55 cover type (see the note above); Built area uses a '
        'generic residential (~38% impervious) proxy since remote sensing does not indicate density. Water and Snow/Ice/Clouds '
        'pixels are excluded from, or fixed in, the weighting as noted above. This is a grid-sample estimate, not a full zonal-'
        'statistics computation over the exact watershed polygon — treat it as indicative, and verify against site-specific '
        'soil survey and land-use data before using it in design calculations.'
    )
    c.setFont('Helvetica-Oblique', 7)
    c.setFillColor(GREY)
    cn_note_lines = simpleSplit(cn_note, 'Helvetica-Oblique', 7, map_w)
    for line in cn_note_lines:
        if y < margin + 8 * mm:
            c.showPage()
            y = page_h - margin
            c.setFont('Helvetica-Oblique', 7)
            c.setFillColor(GREY)
        c.drawString(x, y, line)
        y -= 3.4 * mm
    y -= 8 * mm

    # ---- Geomorphological Instantaneous Unit Hydrograph (GIUH) ----
    if y < margin + 70 * mm:
        c.showPage()
        y = page_h - margin
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Geomorphological Instantaneous Unit Hydrograph (GIUH)')
    y -= 6 * mm
    c.setFont('Helvetica-Oblique', 8)
    c.setFillColor(GREY)
    c.drawString(x, y, 'Rodriguez-Iturbe & Valdes (1979) / Rosso (1984) — the IUH shape derived from the basin\'s own stream network, no calibration.')
    y -= 8 * mm

    if giuh_info and giuh_info.get('available'):
        g = giuh_info
        # Horton ratios, as a compact 3-up stat row
        stat_w = map_w / 3.0
        stats = [('RB — bifurcation ratio', f"{g['RB']:.2f}"),
                 ('RL — length ratio', f"{g['RL']:.2f}"),
                 ('RA — area ratio', f"{g['RA']:.2f}")]
        for i, (label, val) in enumerate(stats):
            sx = x + i * stat_w
            c.setFillColor(TEAL_DARK)
            c.setFont('Helvetica-Bold', 18)
            c.drawString(sx, y - 6 * mm, val)
            c.setFillColor(GREY)
            c.setFont('Helvetica', 7.5)
            c.drawString(sx, y - 10.5 * mm, label)
        y -= 16 * mm

        c.setFillColor(colors.black)
        c.setFont('Helvetica', 8.5)
        c.drawString(x, y, f"Basin (Strahler) order Ω = {g['omega']}   ·   "
                            f"main-stream length LΩ = {g['main_stream_length_km']:.2f} km   ·   "
                            f"characteristic velocity V = {g['velocity_km_per_hr']:.2f} km/h")
        y -= 5 * mm
        c.drawString(x, y, f"Gamma-IUH shape n = {g['n_shape']:.2f}   ·   scale k = {g['k_scale_hr']:.2f} h   ·   "
                            f"peak time tp = {g['tp_min']:.0f} min   ·   peak ordinate qp = {g['qp_per_hr']:.4f} /h")
        y -= 9 * mm

        chart_h = 42 * mm
        if y - chart_h < margin + 20 * mm:
            c.showPage()
            y = page_h - margin
        curve = g.get('curve') or []
        t_vals = [pt['t_hr'] for pt in curve]
        u_vals = [pt['u'] for pt in curve]
        _draw_line_chart(c, x, y - chart_h, map_w, chart_h, t_vals, u_vals,
                          'hours', 'u(t), 1/h', TEAL_DARK, 'Instantaneous unit hydrograph u(t)', GREY, DARK,
                          mark_x=g['tp_hr'])
        y -= (chart_h + 6 * mm)

        giuh_note = (
            'RB and RL come directly from the Strahler stream order carried on each delineated reach (grouped counts and mean '
            'lengths per order). RA (area ratio) has no per-order sub-basin polygon available, so it is approximated by '
            'reconstructing the reach network\'s upstream/downstream topology from shared endpoint coordinates and estimating '
            'each order\'s mean upstream drainage area as (cumulative upstream stream length) / (basin-average drainage '
            'density) — a standard estimator for basins with roughly uniform drainage density, not a true zonal computation. '
            'The characteristic channel velocity V is back-calculated from the basin\'s own Kirpich time of concentration '
            '(V = LΩ / Tc) so no additional empirical constant is introduced. Convolve u(t) with excess rainfall '
            '(from the composite curve number above) to obtain a direct-runoff hydrograph for a design storm.'
        )
        c.setFont('Helvetica-Oblique', 7)
        c.setFillColor(GREY)
        giuh_note_lines = simpleSplit(giuh_note, 'Helvetica-Oblique', 7, map_w)
        for line in giuh_note_lines:
            if y < margin + 8 * mm:
                c.showPage()
                y = page_h - margin
                c.setFont('Helvetica-Oblique', 7)
                c.setFillColor(GREY)
            c.drawString(x, y, line)
            y -= 3.4 * mm
        y -= 8 * mm
    else:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawString(x, y, 'NA — the delineated reach network did not carry enough distinct Strahler stream orders to derive Horton ratios.')
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

    # ---- Relief, hypsometry & main-channel longitudinal profile ----
    y -= 6 * mm
    if y < margin + 70 * mm:
        c.showPage()
        y = page_h - margin
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Relief, hypsometry & main-channel profile')
    y -= 8 * mm

    relief_info = relief_info or {}
    hyps = relief_info.get('hypsometry') or {'available': False}
    prof = relief_info.get('profile') or {'available': False}

    if hyps.get('available'):
        stat_w = map_w / 3.0
        stats = [('H — total relief (Zmax − Zmin)', f"{hyps['total_relief_m']:.0f} m"),
                 ('Hypsometric integral (HI)', f"{hyps['hypsometric_integral']:.3f}"),
                 ('Zmax / Zmean / Zmin', f"{hyps['z_max_m']:.0f} / {hyps['z_mean_m']:.0f} / {hyps['z_min_m']:.0f} m")]
        for i, (label, val) in enumerate(stats):
            sx = x + i * stat_w
            c.setFillColor(TEAL_DARK)
            c.setFont('Helvetica-Bold', 15)
            c.drawString(sx, y - 6 * mm, val)
            c.setFillColor(GREY)
            c.setFont('Helvetica', 7.5)
            c.drawString(sx, y - 10.5 * mm, label)
        y -= 15 * mm

        hi = hyps['hypsometric_integral']
        stage = ('youthful — high remaining relief/erosion potential' if hi > 0.6
                 else 'mature — basin near geomorphic equilibrium' if hi > 0.35
                 else 'old age (peneplain-like) — most relief eroded away')
        c.setFillColor(colors.black)
        c.setFont('Helvetica-Oblique', 8)
        c.drawString(x, y, f'Strahler (1952) stage reading: {stage}, from {hyps["n_points"]} elevation-sampled points inside the watershed.')
        y -= 8 * mm

        chart_h = 42 * mm
        if y - chart_h < margin + 20 * mm:
            c.showPage()
            y = page_h - margin
        curve = hyps.get('curve') or []
        rel_area = [pt['rel_area'] for pt in curve]
        rel_elev = [pt['rel_elev'] for pt in curve]
        _draw_line_chart(c, x, y - chart_h, map_w, chart_h, rel_area, rel_elev,
                          'a/A', 'h/H', GOLD, 'Hypsometric curve — relative area vs. relative elevation', GREY, DARK)
        # note: both axes are dimensionless ratios in [0,1]; y_from_zero/x_from_zero
        # defaults (both True) are the correct, meaningful baseline here.
        y -= (chart_h + 8 * mm)
    else:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawString(x, y, 'NA — could not sample enough elevation points across the watershed for relief/hypsometry.')
        y -= 10 * mm

    if prof.get('available'):
        if y < margin + 55 * mm:
            c.showPage()
            y = page_h - margin
        c.setFillColor(colors.black)
        c.setFont('Helvetica', 8.5)
        drop_m = prof['elev_headwater_m'] - prof['elev_outlet_m']
        c.drawString(x, y, f"Main channel: {prof['total_length_km']:.2f} km, outlet {prof['elev_outlet_m']:.0f} m — "
                            f"headwater {prof['elev_headwater_m']:.0f} m (drop {drop_m:.0f} m).")
        y -= 8 * mm

        chart_h = 42 * mm
        if y - chart_h < margin:
            c.showPage()
            y = page_h - margin
        pts = prof.get('points') or []
        dist_vals = [p['dist_km'] for p in pts]
        elev_vals = [p['elev_m'] for p in pts]
        _draw_line_chart(c, x, y - chart_h, map_w, chart_h, dist_vals, elev_vals,
                          'km from outlet', 'elev (m)', TEAL_DARK, 'Main-channel longitudinal profile', GREY, DARK,
                          y_from_zero=False)
        y -= (chart_h + 6 * mm)
    else:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawString(x, y, 'NA — could not trace/sample the main channel for a longitudinal profile.')
        y -= 10 * mm

    relief_note = (
        'Hypsometry samples elevation on the same watershed-interior grid used for the composite curve number, via '
        'OpenTopoData (SRTM 90m). The hypsometric integral is the area under the relative-elevation vs. relative-area '
        'curve (trapezoidal integration), equivalently (Zmean − Zmin) / (Zmax − Zmin) — Pike & Wilson (1971). '
        'The main channel is traced from the outlet by following, at each confluence, the tributary with the greater '
        'cumulative upstream stream length (the same reach topology reconstructed for the GIUH above), then resampled '
        'to 30 points along its length for elevation lookup. Both are grid/point-sample estimates, not a full DEM zonal '
        'computation — treat the exact integral and profile shape as indicative.'
    )
    c.setFont('Helvetica-Oblique', 7)
    c.setFillColor(GREY)
    for line in simpleSplit(relief_note, 'Helvetica-Oblique', 7, map_w):
        if y < margin + 8 * mm:
            c.showPage()
            y = page_h - margin
            c.setFont('Helvetica-Oblique', 7)
            c.setFillColor(GREY)
        c.drawString(x, y, line)
        y -= 3.4 * mm
    y -= 6 * mm

    # ---- Area-elevation and capacity-elevation curves at the outlet ----
    if y < margin + 110 * mm:
        c.showPage()
        y = page_h - margin
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Area–elevation and capacity (storage)–elevation curves at the outlet')
    y -= 8 * mm

    ac = relief_info.get('area_capacity') or {'available': False}
    if ac.get('available'):
        rows = ac.get('rows') or []
        stat_w = map_w / 3.0
        stats = [('Elevation range sampled', f"{ac['z_min_m']:.0f} – {ac['z_max_m']:.0f} m"),
                 ('Max flooded area (at Zmax)', f"{ac['area_km2_total']:.2f} km2"),
                 ('Total capacity (Zmin → Zmax)', f"{ac['total_capacity_mcm']:.2f} Mm3")]
        for i, (label, val) in enumerate(stats):
            sx = x + i * stat_w
            c.setFillColor(TEAL_DARK)
            c.setFont('Helvetica-Bold', 15)
            c.drawString(sx, y - 6 * mm, val)
            c.setFillColor(GREY)
            c.setFont('Helvetica', 7.5)
            c.drawString(sx, y - 10.5 * mm, label)
        y -= 15 * mm

        elev_vals = [r['elev_m'] for r in rows]
        area_vals = [r['area_km2'] for r in rows]
        vol_vals = [r['cum_volume_mcm'] for r in rows]

        chart_h = 42 * mm
        chart_w = (map_w - 8 * mm) / 2.0
        if y - chart_h < margin + 90 * mm:
            c.showPage()
            y = page_h - margin
        _draw_line_chart(c, x, y - chart_h, chart_w, chart_h, area_vals, elev_vals,
                          'area, km2', 'elev (m)', TEAL_DARK, 'Area–elevation curve', GREY, DARK,
                          y_from_zero=False)
        _draw_line_chart(c, x + chart_w + 8 * mm, y - chart_h, chart_w, chart_h, vol_vals, elev_vals,
                          'capacity, Mm3', 'elev (m)', GOLD, 'Capacity (storage)–elevation curve', GREY, DARK,
                          y_from_zero=False)
        y -= (chart_h + 8 * mm)

        # ---- table ----
        if y < margin + 20 * mm:
            c.showPage()
            y = page_h - margin
        c.setFillColor(DARK)
        c.setFont('Helvetica-Bold', 10.5)
        c.drawString(x, y, 'Area–capacity table')
        y -= 7 * mm

        # NOTE: uses its own local ac_* column/row names rather than the shared
        # col2_x/row_h used by the Morphology and Meteorology tables above/below —
        # reusing those names here previously leaked this table's narrower layout
        # into the Meteorology table that follows, causing its long parameter
        # labels to overlap the value column.
        ac_row_h = 5.6 * mm
        ac_col2_x = x + 55 * mm
        ac_col3_x = x + 105 * mm
        c.setFont('Helvetica-Bold', 8.5)
        c.setFillColor(GREY)
        c.drawString(x, y, 'Elevation (m)')
        c.drawString(ac_col2_x, y, 'Flooded area (km2)')
        c.drawString(ac_col3_x, y, 'Cumulative capacity (Mm3)')
        y -= 2.5 * mm
        c.setStrokeColor(colors.HexColor('#cccccc'))
        c.line(x, y, page_w - margin, y)
        y -= 4.5 * mm

        c.setFont('Helvetica', 8.5)
        for i, r in enumerate(rows):
            if y < margin + 15 * mm:
                c.showPage()
                y = page_h - margin
                c.setFont('Helvetica-Bold', 8.5)
                c.setFillColor(GREY)
                c.drawString(x, y, 'Elevation (m)')
                c.drawString(ac_col2_x, y, 'Flooded area (km2)')
                c.drawString(ac_col3_x, y, 'Cumulative capacity (Mm3)')
                y -= 2.5 * mm
                c.setStrokeColor(colors.HexColor('#cccccc'))
                c.line(x, y, page_w - margin, y)
                y -= 4.5 * mm
                c.setFont('Helvetica', 8.5)
            if i % 2 == 0:
                c.setFillColor(colors.HexColor('#f9f8f5'))
                c.rect(x, y - 1.3 * mm, page_w - 2 * margin, ac_row_h, fill=1, stroke=0)
            c.setFillColor(colors.black)
            c.drawString(x + 1 * mm, y, f"{r['elev_m']:.1f}")
            c.drawString(ac_col2_x, y, f"{r['area_km2']:.3f}")
            c.drawString(ac_col3_x, y, f"{r['cum_volume_mcm']:.4f}")
            y -= ac_row_h
        y -= 4 * mm

        ac_note = (
            'Both curves are derived from the same watershed-interior elevation sample used for the hypsometric curve '
            'above (21 evenly spaced elevation levels between the sampled Zmin and Zmax). At each level z, the flooded '
            'area is the fraction of sampled points with elevation ≤ z, scaled to the delineated drainage area; the '
            'cumulative capacity is the trapezoidal integral of that area-elevation relationship from Zmin to z. This '
            'treats the whole upstream watershed as the potential flood extent at each elevation — a standard '
            'hypsometry-based estimator when no dedicated reservoir bathymetry or rim survey is available, but it is '
            'not a substitute for one: a real dam’s pool is bounded by the valley walls up to the dam crest, which is '
            'normally a much smaller footprint than the full contributing watershed except very close to the outlet. '
            'Treat these curves as an upper-bound, order-of-magnitude estimate for reconnaissance-level siting, and '
            'verify against a topographic/bathymetric survey of the actual reservoir rim before any design use.'
        )
        c.setFont('Helvetica-Oblique', 7)
        c.setFillColor(GREY)
        for line in simpleSplit(ac_note, 'Helvetica-Oblique', 7, map_w):
            if y < margin + 8 * mm:
                c.showPage()
                y = page_h - margin
                c.setFont('Helvetica-Oblique', 7)
                c.setFillColor(GREY)
            c.drawString(x, y, line)
            y -= 3.4 * mm
        y -= 6 * mm
    else:
        c.setFillColor(GREY)
        c.setFont('Helvetica', 9)
        c.drawString(x, y, 'NA — needs both a valid hypsometric elevation sample and a known drainage area to compute.')
        y -= 10 * mm

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

    # ---- Equations reference (appendix): every derived quantity above, as computed ----
    c.showPage()
    y = page_h - margin
    c.setFillColor(DARK)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(x, y, 'Equations reference')
    y -= 6 * mm
    c.setFont('Helvetica-Oblique', 8)
    c.setFillColor(GREY)
    c.drawString(x, y, 'Every derived quantity in this report, as computed — not reconstructed from a textbook.')
    y -= 5 * mm
    c.setFont('Helvetica', 7.5)
    c.setFillColor(colors.HexColor('#333333'))
    var_note = simpleSplit(
        'A = drainage area (km2); P = perimeter (km); L = basin length, LΩ = main (highest-order) stream length (km); '
        'Lstream = total stream length (km); N = number of stream segments; S = basin slope (m/m); Tc = time of '
        'concentration (min); Zmax/Zmean/Zmin = maximum/mean/minimum sampled elevation (m); '
        'phi/lambda = latitude/longitude (radians); R = Earth radius.',
        'Helvetica', 7.5, map_w)
    for line in var_note:
        c.drawString(x, y, line)
        y -= 3.4 * mm
    y -= 6 * mm

    def eq_heading(txt):
        nonlocal y
        if y < margin + 16 * mm:
            c.showPage()
            y = page_h - margin
        c.setFillColor(DARK)
        c.setFont('Helvetica-Bold', 9.5)
        c.drawString(x, y, txt)
        y -= 5.5 * mm

    def eq_line(txt, size=11.5):
        nonlocal y
        if y < margin + 10 * mm:
            c.showPage()
            y = page_h - margin
        _draw_formula(c, x + 4 * mm, y, txt, size=size)
        y -= (size / 72.0 * 72 * 0.62) + 5.5

    def eq_note(txt):
        nonlocal y
        c.setFont('Helvetica', 7.5)
        c.setFillColor(colors.HexColor('#333333'))
        for line in simpleSplit(txt, 'Helvetica', 7.5, map_w):
            if y < margin + 8 * mm:
                c.showPage()
                y = page_h - margin
                c.setFont('Helvetica', 7.5)
                c.setFillColor(colors.HexColor('#333333'))
            c.drawString(x, y, line)
            y -= 3.4 * mm
        y -= 4 * mm

    eq_heading('Distance between two points (haversine) — underlies every length in this report')
    eq_line('a = sin^{2}(dphi / 2) + cos(phi_{1}) · cos(phi_{2}) · sin^{2}(dlambda / 2)')
    eq_line('d = 2R · arcsin(√a)')
    y -= 2 * mm

    eq_heading('Watershed area (shoelace formula, local equirectangular projection)')
    eq_line('A = ½ |Σ (x_{i} y_{i+1} − x_{i+1} y_{i})|')
    y -= 2 * mm

    eq_heading('Morphological shape indices')
    eq_line('Form factor:  F_{f} = A / L^{2}')
    eq_line('Circularity ratio:  R_{c} = 4πA / P^{2}')
    eq_line('Elongation ratio:  R_{e} = (2 / L) · √(A / π)')
    eq_line('Compactness coefficient:  C_{c} = 0.2821 · P / √A')
    y -= 2 * mm

    eq_heading('Drainage network')
    eq_line('Drainage density:  D_{d} = L_{stream} / A')
    eq_line('Stream frequency:  F_{s} = N / A')
    eq_line('Length of overland flow:  L_{g} = 1 / (2 D_{d})')
    y -= 2 * mm

    eq_heading('Relief and hypsometry')
    eq_line('Total relief:  H = Z_{max} − Z_{min}')
    eq_line('Hypsometric integral:  HI = area under the (h/H) vs. (a/A) curve   =   (Z_{mean} − Z_{min}) / (Z_{max} − Z_{min})', size=11)
    eq_note('Zmax/Zmean/Zmin from elevation-sampled points across the watershed; h/H = relative elevation, a/A = '
            'relative area above that elevation (the plotted hypsometric curve). The integral (computed here by '
            'trapezoidal integration of the sampled curve) equals the elevation-relief ratio — Pike & Wilson (1971).')

    eq_heading('Time of concentration (Kirpich, 1940) and lag time')
    eq_line('T_{c} = 0.0195 · L^{0.77} · S^{-0.385}    (minutes)')
    eq_line('Lag time = 0.6 · T_{c}')
    y -= 2 * mm

    eq_heading('Composite curve number (SCS/NRCS)')
    eq_line('CN_{composite} = Σ (CN_{i} · n_{i}) / Σ n_{i}', size=12.5)
    eq_note('n(i) = number of sampled points inside the watershed classified with curve number CN(i) '
            '(from the land-cover / hydrologic-soil-group pair at that point).')

    eq_heading('Geomorphological Instantaneous Unit Hydrograph (GIUH) — Horton ratios')
    eq_line('R_{B} = geometric mean of (N_{ω} / N_{ω+1})   — bifurcation ratio')
    eq_line('R_{L} = geometric mean of (L_{ω+1} / L_{ω})   — length ratio')
    eq_line('R_{A} = geometric mean of (A_{ω+1} / A_{ω})   — area ratio')
    eq_note('ω = Strahler stream order (1 … Ω, the outlet’s order); Nω = number of streams of order ω; '
            'Lω = their mean length; Aω = their mean upstream drainage area (see the GIUH note above for how '
            'Aω is estimated when no per-order sub-basin polygon is available).')

    eq_heading('GIUH — Rosso (1984) two-parameter gamma instantaneous unit hydrograph')
    eq_line('n = 3.29 · (R_{B}/R_{A})^{0.78} · R_{L}^{0.07}     — shape parameter', size=12.5)
    eq_line('k = 0.70 · (R_{B}/R_{A})^{-0.48} · R_{L}^{0.48} · (L_{Ω} / V)     — scale parameter (hours)', size=12.5)
    eq_line('t_{p} = (n − 1) · k     — time to peak')
    eq_line('u(t) = [1 / (k · Γ(n))] · (t/k)^{n−1} · e^{−t/k}     — the plotted hydrograph ordinate')
    eq_note('V = characteristic channel velocity, back-calculated as LΩ / Tc so no additional empirical '
            'constant is introduced beyond what this report already computes. Γ(n) is the gamma function.')

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


def _point_in_ring(lon, lat, ring):
    """Standard ray-casting point-in-polygon test against a single ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_watershed(lon, lat, watershed_geojson):
    """True if (lon, lat) falls inside the watershed polygon(s). Rings within
    one feature are combined with the even-odd rule (so holes are respected);
    features are OR'd together (for a MultiPolygon-as-features case)."""
    for feat in (watershed_geojson or {}).get('features', []):
        rings = _all_ring_coords(feat.get('geometry') or {})
        if not rings:
            continue
        inside = False
        for ring in rings:
            if len(ring) >= 3 and _point_in_ring(lon, lat, ring):
                inside = not inside
        if inside:
            return True
    return False


# SCS/NRCS curve numbers (average antecedent moisture, "good" hydrologic
# condition) by hydrologic soil group A-D, from TR-55 Table 2-2, crosswalked
# from this app's Sentinel-2 land-cover classes onto the nearest standard
# TR-55 cover type. This is necessarily an approximation — remote-sensing
# land cover doesn't carry TR-55's treatment/condition detail — documented
# assumptions:
#   Trees              -> "Woods, good condition"
#   Rangeland          -> "Pasture/grassland/range, good condition"
#   Crops              -> "Row crops, straight row, good condition"
#   Built area         -> "Residential, 1/4-acre lot (~38% impervious)" as a
#                          general-purpose proxy (remote sensing doesn't
#                          distinguish density/imperviousness)
#   Bare ground        -> "Fallow, bare soil"
#   Water              -> CN 98 (conventional: negligible infiltration)
#   Flooded vegetation -> CN 95 (wetland proxy — not a standard TR-55 class)
# Snow/Ice and Clouds are excluded from the composite (not real ground cover).
CN_TABLE = {
    'Trees':              {'A': 30, 'B': 55, 'C': 70, 'D': 77},
    'Rangeland':          {'A': 39, 'B': 61, 'C': 74, 'D': 80},
    'Crops':              {'A': 67, 'B': 78, 'C': 85, 'D': 89},
    'Built area':         {'A': 61, 'B': 75, 'C': 83, 'D': 87},
    'Bare ground':        {'A': 77, 'B': 86, 'C': 91, 'D': 94},
    'Water':              {'A': 98, 'B': 98, 'C': 98, 'D': 98},
    'Flooded vegetation': {'A': 95, 'B': 95, 'C': 95, 'D': 95},
}
CN_EXCLUDED_LANDCOVER = {'Snow / ice', 'Clouds'}


def compute_composite_cn(watershed_geojson, target_points=25):
    """Area-weighted composite SCS/NRCS curve number for the whole watershed:
    samples a grid of points inside the polygon, classifies each by land
    cover + hydrologic soil group (the same point APIs used for the outlet
    row elsewhere in this report), looks up CN per TR-55, and averages
    weighted by how many sample points fall in each land-cover/HSG pair —
    which approximates area weighting for a reasonably dense, even grid.
    Returns None on total failure; otherwise a dict with 'composite_cn'
    (None if no point could be classified), 'n_sampled', 'n_valid', and a
    'breakdown' list of the contributing land-cover/HSG pairs."""
    try:
        min_lon, max_lon, min_lat, max_lat = _compute_watershed_bbox(watershed_geojson)
    except Exception:
        return None

    # Oversample a regular grid across the bbox, then keep only points that
    # actually fall inside the (possibly irregular/concave) watershed shape.
    grid_n = 10
    candidates = []
    for i in range(grid_n):
        for j in range(grid_n):
            lon = min_lon + (max_lon - min_lon) * (i + 0.5) / grid_n
            lat = min_lat + (max_lat - min_lat) * (j + 0.5) / grid_n
            if _point_in_watershed(lon, lat, watershed_geojson):
                candidates.append((lat, lon))
    if not candidates:
        return None
    if len(candidates) > target_points:
        step = len(candidates) / target_points
        candidates = [candidates[int(i * step)] for i in range(target_points)]

    def sample(pt):
        lat, lon = pt
        try:
            lc = fetch_landcover_point_class(lat, lon)
        except Exception:
            lc = None
        try:
            hsg = fetch_hsg_point_class(lat, lon)
        except Exception:
            hsg = None
        return (lc, hsg)

    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            futures = [ex.submit(sample, pt) for pt in candidates]
            for f in futures:
                try:
                    results.append(f.result(timeout=12))
                except Exception:
                    results.append((None, None))
    except Exception:
        return None

    counts = {}
    total_valid = 0
    for lc, hsg in results:
        if not lc or not hsg or lc in CN_EXCLUDED_LANDCOVER:
            continue
        cn_row = CN_TABLE.get(lc)
        if not cn_row or hsg not in cn_row:
            continue
        key = (lc, hsg)
        counts[key] = counts.get(key, 0) + 1
        total_valid += 1

    if total_valid == 0:
        return {'composite_cn': None, 'n_sampled': len(candidates), 'n_valid': 0, 'breakdown': []}

    weighted_sum = sum(CN_TABLE[lc][hsg] * n for (lc, hsg), n in counts.items())
    composite = weighted_sum / total_valid
    breakdown = sorted(
        [{'landcover': lc, 'hsg': hsg, 'count': n, 'pct': 100.0 * n / total_valid, 'cn': CN_TABLE[lc][hsg]}
         for (lc, hsg), n in counts.items()],
        key=lambda r: -r['count']
    )
    return {'composite_cn': composite, 'n_sampled': len(candidates), 'n_valid': total_valid, 'breakdown': breakdown}


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
                           teal, teal_dark, gold, fetch_fn, img_bytes=None):
    """Watershed overlay drawn on top of a WMS raster (land cover or soil
    type). Pass `img_bytes` if it was already fetched (e.g. concurrently,
    up front in build_pdf_report) to skip fetching again; otherwise it's
    fetched here via `fetch_fn`. Raises on any failure so the caller can
    show a fallback."""
    from reportlab.lib.utils import ImageReader
    from reportlab.lib import colors

    min_lon, max_lon, min_lat, max_lat = _compute_watershed_bbox(watershed_geojson, pad_frac=0.18)
    if img_bytes is None:
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


def _draw_soil_legend(c, x0, y0, w, legend_bytes=None):
    """Soil-type legend: embeds ISRIC's own GetLegendGraphic image (the
    authoritative color key for the ~30 WRB classes) under the map. Pass
    `legend_bytes` if it was already fetched up front; otherwise it's
    fetched here. Falls back to a short text note if the legend image
    can't be fetched."""
    from reportlab.lib.utils import ImageReader
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.units import mm
    try:
        if legend_bytes is None:
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


def _draw_satellite_map(c, watershed_geojson, rivers_geojson, outlets_geojson, x0, y0, w, h, teal, teal_dark, gold, img_bytes=None):
    """Watershed overlay drawn on top of a satellite image. Pass `img_bytes`
    if it was already fetched up front; otherwise it's fetched here. Raises
    on any failure (missing geometry or unreachable imagery service) so the
    caller can show a fallback message instead."""
    from reportlab.lib.utils import ImageReader

    min_lon, max_lon, min_lat, max_lat = _compute_watershed_bbox(watershed_geojson, pad_frac=0.18)
    if img_bytes is None:
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


def _draw_formula(c, x, y, s, size=11.5, color=None):
    """Draws one inline formula at (x, y) with real typographic super/subscripts
    instead of unicode super/subscript glyphs (which render as blank gaps in
    this PDF's base Helvetica encoding — verified separately; plain Greek
    letters, radicals, minus signs etc. do render fine and are used directly).
    Syntax: ^{...} for superscript, _{...} for subscript; everything else is
    drawn literally. Does not touch `y` — the caller advances it."""
    base_font = 'Helvetica'
    sub_size = size * 0.68
    if color:
        c.setFillColor(color)
    xi = x
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in ('^', '_') and i + 1 < n and s[i + 1] == '{':
            close = s.find('}', i + 2)
            token = s[i + 2:close] if close != -1 else s[i + 2:]
            is_sup = ch == '^'
            c.setFont(base_font, sub_size)
            dy = (size * 0.32) if is_sup else (-size * 0.14)
            c.drawString(xi, y + dy, token)
            xi += c.stringWidth(token, base_font, sub_size)
            i = (close + 1) if close != -1 else n
        else:
            j = i
            while j < n and s[j] not in ('^', '_'):
                j += 1
            chunk = s[i:j]
            c.setFont(base_font, size)
            c.drawString(xi, y, chunk)
            xi += c.stringWidth(chunk, base_font, size)
            i = j


def _nice_num(range_val, round_result):
    """Classic 'nice numbers' axis helper (Heckbert). Snaps a raw span or
    step to a visually clean 1/2/5x10^n value."""
    if range_val <= 0:
        return 1.0
    exponent = math.floor(math.log10(range_val))
    fraction = range_val / (10 ** exponent)
    if round_result:
        if fraction < 1.5:
            nice_fraction = 1.0
        elif fraction < 3:
            nice_fraction = 2.0
        elif fraction < 7:
            nice_fraction = 5.0
        else:
            nice_fraction = 10.0
    else:
        if fraction <= 1:
            nice_fraction = 1.0
        elif fraction <= 2:
            nice_fraction = 2.0
        elif fraction <= 5:
            nice_fraction = 5.0
        else:
            nice_fraction = 10.0
    return nice_fraction * (10 ** exponent)


def _nice_ticks(vmin, vmax, target_n=5):
    """Returns (ticks, nice_min, nice_max) — evenly spaced, human-friendly
    axis tick values that bracket [vmin, vmax]."""
    if vmin is None or vmax is None:
        vmin, vmax = 0.0, 1.0
    if vmax <= vmin:
        vmax = vmin + 1.0
    span = _nice_num(vmax - vmin, False)
    step = _nice_num(span / max(target_n - 1, 1), True)
    nice_min = math.floor(vmin / step) * step
    nice_max = math.ceil(vmax / step) * step
    ticks = []
    v = nice_min
    guard = 0
    while v <= nice_max + step * 0.5 and guard < 50:
        ticks.append(round(v, 10))
        v += step
        guard += 1
    return ticks, nice_min, nice_max


def _fmt_tick(v):
    if abs(v - round(v)) < 1e-6:
        return f'{int(round(v)):,}'
    return f'{v:,.2f}'


def _draw_line_chart(c, x0, y0, w, h, x_vals, y_vals, x_unit, y_unit, line_color, title, grey, dark,
                      mark_x=None, y_from_zero=True, x_from_zero=True):
    """A minimal, dependency-free x/y line chart drawn straight onto the
    reportlab canvas, styled consistently with _draw_bar_chart. Used for the
    GIUH curve, the hypsometric curve, and the main-channel elevation profile.
    Draws a real labeled y-axis (tick values + horizontal gridlines) and
    labeled x-axis ticks. `mark_x`, if given, draws a thin vertical guide
    (e.g. at the peak time). Set `y_from_zero=False` for charts (like an
    elevation profile) where forcing the axis to 0 would waste vertical
    space and compress the visible variation — the axis then starts near
    the data minimum instead."""
    from reportlab.lib import colors as rl_colors

    c.setFillColor(dark)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(x0, y0 + h - 8, title)

    n = len(x_vals)
    if n < 2 or all(v is None for v in y_vals):
        c.setFillColor(grey)
        c.setFont('Helvetica', 8)
        c.drawString(x0, y0 + h / 2, 'No data available')
        return

    numeric_y = [v for v in y_vals if v is not None]
    data_y_min = min(numeric_y) if numeric_y else 0.0
    data_y_max = max(numeric_y) if numeric_y else 1.0
    data_x_min = min(x_vals)
    data_x_max = max(x_vals)

    y_ticks, y_min, y_max = _nice_ticks(0.0 if y_from_zero else data_y_min, data_y_max, target_n=5)
    x_ticks, x_min, x_max = _nice_ticks(0.0 if x_from_zero else data_x_min, data_x_max, target_n=6)
    if y_max <= y_min:
        y_max = y_min + 1.0
    if x_max <= x_min:
        x_max = x_min + 1.0

    # reserve left margin for the widest y tick label, plus a column for the
    # rotated y-axis unit label
    c.setFont('Helvetica', 6.5)
    y_label_w = max((c.stringWidth(_fmt_tick(t), 'Helvetica', 6.5) for t in y_ticks), default=10)
    unit_col_w = 9

    title_h = 14
    x_axis_h = 26
    plot_top = y0 + h - title_h
    plot_bottom = y0 + x_axis_h
    plot_h = max(plot_top - plot_bottom, 1)
    plot_left = x0 + unit_col_w + y_label_w + 8
    plot_right = x0 + w - 2
    plot_w = max(plot_right - plot_left, 1)

    def px(xv):
        return plot_left + ((xv - x_min) / (x_max - x_min)) * plot_w

    def py(yv):
        return plot_bottom + ((yv - y_min) / (y_max - y_min)) * plot_h

    # horizontal gridlines + y tick labels
    c.setFont('Helvetica', 6.5)
    for t in y_ticks:
        ty = py(t)
        if ty < plot_bottom - 0.5 or ty > plot_top + 0.5:
            continue
        c.setStrokeColor(rl_colors.HexColor('#e3e3e3'))
        c.setLineWidth(0.5)
        c.line(plot_left, ty, plot_right, ty)
        c.setFillColor(grey)
        c.drawRightString(plot_left - 4, ty - 2, _fmt_tick(t))

    # y-axis line
    c.setStrokeColor(rl_colors.HexColor('#999999'))
    c.setLineWidth(0.8)
    c.line(plot_left, plot_bottom, plot_left, plot_top)
    # x-axis line
    c.line(plot_left, plot_bottom, plot_right, plot_bottom)

    # x tick marks + labels
    c.setFont('Helvetica', 6.5)
    for t in x_ticks:
        if t < x_min - 1e-9 or t > x_max + 1e-9:
            continue
        tx = px(t)
        c.setStrokeColor(rl_colors.HexColor('#999999'))
        c.setLineWidth(0.6)
        c.line(tx, plot_bottom, tx, plot_bottom - 2.5)
        c.setFillColor(grey)
        c.drawCentredString(tx, plot_bottom - 10, _fmt_tick(t))

    # axis unit labels: x unit centered on its own row below the tick values;
    # y unit rotated vertically in the reserved left column
    c.setFillColor(grey)
    c.setFont('Helvetica-Oblique', 6.5)
    c.drawCentredString((plot_left + plot_right) / 2.0, y0 + 2, x_unit)
    c.saveState()
    c.translate(x0 + unit_col_w / 2.0 + 2, (plot_top + plot_bottom) / 2.0)
    c.rotate(90)
    c.drawCentredString(0, 0, y_unit)
    c.restoreState()

    if mark_x is not None and x_min <= mark_x <= x_max:
        c.setStrokeColor(rl_colors.HexColor('#c9982f'))
        c.setLineWidth(0.7)
        c.setDash(2, 2)
        c.line(px(mark_x), plot_bottom, px(mark_x), plot_top)
        c.setDash()

    # filled area under the curve, then the stroked line on top
    baseline_y = py(y_min)
    path = c.beginPath()
    path.moveTo(px(x_vals[0]), baseline_y)
    for xv, yv in zip(x_vals, y_vals):
        path.lineTo(px(xv), py(yv if yv is not None else y_min))
    path.lineTo(px(x_vals[-1]), baseline_y)
    path.close()
    fill_color = rl_colors.Color(line_color.red, line_color.green, line_color.blue, alpha=0.15)
    c.setFillColor(fill_color)
    c.drawPath(path, fill=1, stroke=0)

    c.setStrokeColor(line_color)
    c.setLineWidth(1.3)
    line_path = c.beginPath()
    line_path.moveTo(px(x_vals[0]), py(y_vals[0] if y_vals[0] is not None else y_min))
    for xv, yv in zip(x_vals[1:], y_vals[1:]):
        line_path.lineTo(px(xv), py(yv if yv is not None else y_min))
    c.drawPath(line_path, fill=0, stroke=1)


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
        relief_area_km2 = (morphology or {}).get('area_km2')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            f_geo = ex.submit(reverse_geocode, lat, lng)
            f_env = ex.submit(fetch_environmental_context, lat, lng)
            f_cn = ex.submit(compute_composite_cn, watershed_geojson)
            f_relief = ex.submit(compute_relief_hypsometry_and_profile, watershed_geojson, rivers_geojson, lat, lng, relief_area_km2)
            try:
                geo_info = f_geo.result(timeout=15)
            except Exception:
                geo_info = {}
            try:
                env_info = f_env.result(timeout=25)
            except Exception:
                env_info = {}
            try:
                cn_info = f_cn.result(timeout=25)
            except Exception:
                cn_info = None
            try:
                relief_info = f_relief.result(timeout=25)
            except Exception:
                relief_info = None

        wiki_title = geo_info.get('place') or geo_info.get('region')
        wiki_info = wikipedia_summary(wiki_title)

        try:
            morph = morphology or {}
            giuh_info = compute_giuh(
                rivers_geojson, lat, lng,
                morph.get('area_km2'),
                morph.get('drainage_density_km_per_km2'),
                morph.get('main_stream_length_km'),
                morph.get('time_of_concentration_min'),
            )
        except Exception:
            giuh_info = None

        pdf_bytes = build_pdf_report(lat, lng, watershed_geojson, rivers_geojson, outlets_geojson,
                                      morphology, geo_info, wiki_info, env_info, cn_info, giuh_info, relief_info)

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
