"""
Resolves a single, consistent local data directory for `delineator`'s
cached basin files, used by both `app.py` (runtime) and `predownload.py`
(Vercel's build-time pre-fetch step) so they agree on where to look.

Using a path relative to this file (rather than the current working
directory) means it resolves the same way whether it's invoked during
Vercel's build step, inside the deployed function, in Docker, or when
you just run `python app.py` locally.
"""
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delineator_data")

# setdefault: if something else (e.g. the Dockerfile's ENV) already set
# DELINEATOR_DATA_DIR, that takes precedence — this is only a fallback.
os.environ.setdefault("DELINEATOR_DATA_DIR", DATA_DIR)
