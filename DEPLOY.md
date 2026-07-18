# Deploying as a real website

## Pick a host

The app needs a real VPS (not a bare static host) since it runs Python +
geopandas/rasterio and caches a few hundred MB–low GB of hydrology data for
megabasin 29 (the whole Arabian Peninsula — bigger than the Iceland sample
bundled with `delineator`, so don't undersize this).

Recommended, cheapest-that-won't-struggle:

- **Hetzner Cloud CX22** — 2 vCPU / 4 GB RAM / 40 GB disk, ~€4.6/month.
  Best value for this workload.
- **DigitalOcean Basic Droplet** — 2 GB RAM / 50 GB disk, $12/month.
  Slightly more expensive but very well documented if you haven't used a
  VPS before.

Avoid free-tier PaaS (Render/Railway free, Heroku free) for this one —
512 MB RAM is tight for loading a basin-29-sized spatial index, and free
tiers often have no persistent disk, which is fine here (the Dockerfile
bakes the data into the image) but the RAM ceiling is the real risk.

You'll also want a domain (or subdomain) pointed at the server — any
registrar works, you just need an **A record** aiming at the VPS's IP.

## One-time server setup

SSH into the fresh VPS, then:

```bash
# Install Docker + Compose plugin (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh
apt-get install -y docker-compose-plugin
```

## Deploy

```bash
# From your machine: copy the project to the server
scp -r ksa-watersheds root@YOUR_SERVER_IP:/root/

# On the server
cd /root/ksa-watersheds
```

Edit `Caddyfile` and replace `your-domain.com` with your actual domain —
Caddy uses this to request a free Let's Encrypt certificate automatically
on first boot. Then:

```bash
docker compose up -d --build
```

The build step downloads megabasin 29's data (unit catchments, rivers,
flow direction, flow accumulation) into the image — this is the slow part,
give it a few minutes depending on file size and your connection. Once it's
up, visit `https://your-domain.com` — Caddy handles HTTPS for you, no
certbot/nginx config needed.

## After deploying

```bash
docker compose logs -f app       # tail the app's logs
docker compose restart app       # restart just the app
docker compose down && docker compose up -d --build   # rebuild after code changes
```

Since the data is baked into the image, redeploys after a *code* change
(editing `app.py` or `index.html`) will re-run the data download step too
unless you reorder the Dockerfile so `COPY app.py .` / `COPY static/` come
after the `RUN downloader(29)` line — they already do in the Dockerfile
provided, so Docker's layer cache will skip re-downloading as long as
`requirements.txt` hasn't changed.

## Costs to expect

- VPS: ~€4.60–$12/month depending on which you pick above
- Domain: ~$10–15/year if you don't already have one
- Data transfer/bandwidth: negligible at portfolio-project traffic levels
