"""
Computes standard catchment morphological/geomorphological parameters from
a delineated watershed boundary and its river network.

These are the classical, widely-used indices from fluvial geomorphology
(Horton, Schumm, Miller, Strahler-era formulas), computed purely from
geometry — no elevation data required, so no new data source needed
beyond what `delineate()` already returns.

All linear/area measurements are done in a local UTM zone (chosen based
on the outlet's coordinates), not in raw lat/lon degrees, so the results
are metrically accurate rather than distorted by geographic projection.

Deliberately NOT included here, and why:
  - Strahler stream order / bifurcation ratio: requires real topological
    network analysis (which segments flow into which), not just a flat
    list of line geometries. A real addition, but a separate piece of
    work from this geometry-only pass.
  - Basin relief, relief ratio, ruggedness number, average slope: these
    need actual elevation values sampled from a DEM. `delineator` gives
    us flow-direction and flow-accumulation rasters, not raw elevation,
    so this would need a different data source to do properly rather
    than a rough proxy.
"""
import math

import geopandas as gpd
from shapely.geometry import Point


def _utm_crs_for(lon: float, lat: float) -> str:
    """Pick the local UTM zone for a given point, so metric measurements
    (area, length) are accurate rather than distorted lat/lon degrees."""
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


def compute_morphology(watershed_gdf, rivers_gdf, outlet_lat: float, outlet_lng: float) -> dict:
    """
    Returns a dict of morphological parameters for the given delineated
    watershed. `watershed_gdf` and `rivers_gdf` are the GeoDataFrames
    `delineator.core.delineate()` returns (rivers_gdf may be None).
    """
    utm_crs = _utm_crs_for(outlet_lng, outlet_lat)
    watershed_utm = watershed_gdf.to_crs(utm_crs)
    geom = watershed_utm.geometry.iloc[0]

    props = watershed_gdf.iloc[0]
    area_km2 = props["area_km2"] if "area_km2" in props and props["area_km2"] is not None else geom.area / 1e6

    perimeter_km = geom.length / 1000

    outlet_utm = gpd.GeoSeries([Point(outlet_lng, outlet_lat)], crs="EPSG:4326").to_crs(utm_crs).iloc[0]

    # Basin length: distance from the outlet to the farthest point on the
    # watershed boundary. This matches the standard hydrology definition
    # (axial length from outlet to the most remote point on the divide),
    # and works correctly because a real outlet sits at the basin's edge,
    # not in the middle of it.
    if geom.geom_type == "MultiPolygon":
        boundary_geom = max(geom.geoms, key=lambda g: g.area).exterior
    else:
        boundary_geom = geom.exterior
    coords = list(boundary_geom.coords)
    basin_length_km = max(outlet_utm.distance(Point(c)) for c in coords) / 1000

    form_factor = area_km2 / (basin_length_km ** 2) if basin_length_km else None
    circularity_ratio = (4 * math.pi * area_km2) / (perimeter_km ** 2) if perimeter_km else None
    elongation_ratio = (2 / basin_length_km) * math.sqrt(area_km2 / math.pi) if basin_length_km else None
    compactness_coefficient = 0.2821 * perimeter_km / math.sqrt(area_km2) if area_km2 else None

    result = {
        "area_km2": round(area_km2, 2),
        "perimeter_km": round(perimeter_km, 2),
        "basin_length_km": round(basin_length_km, 2),
        "form_factor": round(form_factor, 4) if form_factor else None,
        "circularity_ratio": round(circularity_ratio, 4) if circularity_ratio else None,
        "elongation_ratio": round(elongation_ratio, 4) if elongation_ratio else None,
        "compactness_coefficient": round(compactness_coefficient, 4) if compactness_coefficient else None,
    }

    if rivers_gdf is not None and len(rivers_gdf):
        rivers_utm = rivers_gdf.to_crs(utm_crs)
        total_stream_length_km = rivers_utm.geometry.length.sum() / 1000
        num_segments = len(rivers_utm)
        drainage_density = total_stream_length_km / area_km2 if area_km2 else None
        stream_frequency = num_segments / area_km2 if area_km2 else None
        overland_flow_length = 1 / (2 * drainage_density) if drainage_density else None

        result.update({
            "total_stream_length_km": round(total_stream_length_km, 2),
            "num_stream_segments": num_segments,
            "drainage_density_km_per_km2": round(drainage_density, 3) if drainage_density else None,
            "stream_frequency_per_km2": round(stream_frequency, 3) if stream_frequency else None,
            "length_of_overland_flow_km": round(overland_flow_length, 3) if overland_flow_length else None,
        })

    return result
