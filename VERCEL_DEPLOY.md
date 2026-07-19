# Deploying to Vercel (experimental path)

**Read this first:** I couldn't test this end-to-end myself — my sandbox
can't reach vercel.com or mghydro.com, so I can't actually run a Vercel
build here. Everything below is built from Vercel's current docs and
should work, but treat the first deploy as a debugging session, not a
sure thing. If it breaks, copy me the exact build log text and I'll help
from there.

## Why this is trickier than Render/Docker

Vercel Functions have **no persistent disk** — every cold start begins
from a blank slate. `delineator` normally downloads basin data once and
caches it on disk forever; that doesn't work here. The fix: **bake the
data into the deployed bundle at build time** instead of downloading it
at request time. That's what `predownload.py` and `vercel.json`'s
`buildCommand` do together.

This also means the deploy needs Vercel's newer, larger bundle size limit
("Large Functions", up to 5 GB) rather than the older 250–500 MB limit,
since megabasin 29's data (unit catchments, rivers, flow direction, flow
accumulation for the whole Arabian Peninsula) plus `rasterio`/`geopandas`'s
own bundled GDAL binaries will likely add up to more than 500 MB.

## Steps

1. **Push these new files to the same GitHub repo** (`data_dir.py`,
   `predownload.py`, `vercel.json`) — same process as before (GitHub's
   web upload, or GitHub Desktop).

2. **On vercel.com:** sign up / log in → **Add New... → Project** →
   import the `ksa-watersheds` repo from GitHub.

3. **Before deploying**, go to the project's **Settings → Environment
   Variables** and check whether `VERCEL_SUPPORT_LARGE_FUNCTIONS` needs
   to be added manually. Per Vercel's docs, new projects get this by
   default — but if the build later fails with a bundle-size error, add:
   - Key: `VERCEL_SUPPORT_LARGE_FUNCTIONS`
   - Value: `1`
   then redeploy.

4. **Deploy.** Vercel installs `requirements.txt`, then runs
   `python3 predownload.py` (the `buildCommand`), which downloads
   megabasin 29's data into the bundle. This is the slow, failure-prone
   step — watch the build log here closely.

5. If it succeeds, you get a live `*.vercel.app` URL — same as before.

## Likely failure points, and what they'd mean

- **Build times out** — megabasin 29's data may be large enough that the
  download doesn't finish within Vercel's build time limit. If this
  happens, this approach may not be viable without a different data
  strategy (e.g. hosting the data yourself somewhere faster, or a
  smaller/clipped subset) — tell me the exact log output and we'll
  figure out next steps.
- **"exceeded unzipped maximum size"** — means Large Functions isn't
  active; try the environment variable from step 3.
- **`python3: command not found` on buildCommand** — Vercel's Python
  build environment naming can shift between versions; if this happens,
  tell me the exact error and I'll adjust `vercel.json`.
- **App loads but delineation fails at runtime** — check that
  `DELINEATOR_DATA_DIR` (set by `data_dir.py`) actually persisted into
  the deployed bundle; the build log from `predownload.py` should show
  where it downloaded files to.

## If this path stalls out

Render (with the small refundable card hold) and the VPS route from
earlier are both already known-working for this app — this Vercel path
is worth trying since you asked, but isn't the only option if it turns
out to be more trouble than it's worth.
