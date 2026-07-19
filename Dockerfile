FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Bake the Saudi Arabia / Arabian Peninsula data (megabasin 29) into the
# image at build time. This means the container never depends on
# mghydro.com being reachable at runtime, and works even on hosts with
# no persistent disk between deploys.
ENV DELINEATOR_DATA_DIR=/data
RUN mkdir -p /data && \
    python -c "from delineator.core import downloader; downloader(29)"

COPY app.py .
COPY data_dir.py .
COPY vercel_skimage_fix.py .
COPY vercel_numba_fix.py .
COPY static/ static/


EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "120", "app:app"]
