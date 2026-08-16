import os

import data_dir  # sets DELINEATOR_DATA_DIR to a path next to this file

os.makedirs(data_dir.DATA_DIR, exist_ok=True)

from delineator.core import downloader  # noqa: E402

print(f"Downloading megabasin 29 (Saudi Arabia) data to {data_dir.DATA_DIR} ...")
downloader(29)
print("Done.")
