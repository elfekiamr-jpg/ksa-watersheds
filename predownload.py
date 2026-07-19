"""
One-time build step: pre-downloads megabasin 29 (Saudi Arabia / Arabian
Peninsula) data so it's baked into the deployed bundle. Serverless
platforms like Vercel have no persistent disk between invocations, so
downloading it at *runtime* (the normal `delineator` behavior) would mean
re-downloading it on every cold start — this avoids that entirely.

Run automatically by Vercel's `buildCommand` (see vercel.json).
Can also be run manually: `python predownload.py`.
"""
import os

import data_dir  # sets DELINEATOR_DATA_DIR to a path next to this file

os.makedirs(data_dir.DATA_DIR, exist_ok=True)

from delineator.core import downloader  # noqa: E402  (import after env var is set)

print(f"Downloading megabasin 29 (Saudi Arabia) data to {data_dir.DATA_DIR} ...")
downloader(29)
print("Done.")
