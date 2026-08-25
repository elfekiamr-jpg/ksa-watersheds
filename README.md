# Manabi (منابع) — Saudi Arabia Watersheds

A Saudi-Arabia-scoped clone of [mghydro.com/watersheds](https://mghydro.com/watersheds),
built on the open-source [`delineator`](https://pypi.org/project/delineator/) Python
package (MIT license, Matthew Heberger), which reimplements the same MERIT-Hydro /
MERIT-Basins hybrid vector-raster method the original site uses.

## How it's scoped to Saudi Arabia

The whole Arabian Peninsula falls inside a single MERIT/HydroSHEDS "megabasin"
(Pfafstetter level-2 code **29**) — confirmed by checking `megabasins.db`
directly rather than guessing off the region map. `delineator` auto-detects
the megabasin from the clicked point and downloads *only that basin's* data
files the first time it's needed, so you never touch the global dataset.

The frontend restricts the map's clickable area to a Saudi-Arabia-plus-buffer
bounding box; the backend independently re-validates that server-side.
A watershed can legitimately extend a little past the political border
(e.g. a wadi whose headwaters are just inside Jordan or Yemen) — that's
correct hydrology, not a bug.

## Setup

```bash
python -m venv venv
source venv/bin/activate       # venv\Scripts\activate on Windows
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. The **first click** will trigger a one-time
download of basin 29's data (unit catchments, rivers, flow direction, flow
accumulation) to your machine's local data directory — this needs an
internet connection and may take a minute or two. Every click after that
is fast and works offline.

If you'd rather pre-fetch the data before your first demo/deploy:

```python
from delineator.core import downloader
downloader(29)
```

## What's implemented

- Click-to-delineate upstream watershed, with river network and outlet points
- Snapped-outlet coordinates and drainage area (km²)
- GeoJSON download for the watershed boundary and river network
- Map and clicks restricted to Saudi Arabia + a small border buffer

## What's not (yet) — good next steps

- **Downstream flow-path tracing.** The original site lets you trace
  *downstream* from a point too; the `delineator` package only does
  upstream watersheds. You'd implement this yourself by walking
  `flowdir29.tif` with `pysheds` (already a dependency) — flow-direction
  rasters are exactly what downstream tracing needs.
- **Precision toggle / profile plots.** The original's low-res mode and
  elevation profile plots aren't wired up here; `DelineatorConfig` in
  `app.py` already exposes the low-res knobs (`high_res=False`,
  `simplify_tolerance`, etc.) if you want to add a toggle.
- **Deployment.** This runs Flask's dev server. For anything public-facing,
  put it behind gunicorn/uwsgi + nginx, and consider pre-downloading the
  basin-29 data into the Docker image / server so there's no cold-start
  download on first request.
- **Arabic UI.** The header already carries an Arabic label (منابع) as a
  placeholder; a full RTL pass would be a nice fit given the audience.

## Credit

Delineation engine: `delineator` by Matthew Heberger (MIT license).
Data: MERIT-Hydro (Yamazaki et al.) and MERIT-Basins (Lin et al.).
This project is an independent frontend/scoping layer around that engine —
not affiliated with mghydro.com.
## Documentation
See [Manabi_User_Manual.pdf](Manabi_User_Manual.pdf) for full usage instructions, 
morphological parameter definitions, and validation results against real HEC-GeoHMS 
data for Wadi Allith (5 sub-basins, 91.8–99.1% shape agreement).
