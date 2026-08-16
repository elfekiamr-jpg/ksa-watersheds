import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delineator_data")
os.environ.setdefault("DELINEATOR_DATA_DIR", DATA_DIR)
