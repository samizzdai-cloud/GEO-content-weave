"""GEO product research, grounded content and image workbench."""
from pathlib import Path
from dotenv import load_dotenv

# Load before store.ROOT or any adapter reads configuration, including uvicorn imports.
load_dotenv(Path(__file__).resolve().parents[1]/'.env',override=False)
