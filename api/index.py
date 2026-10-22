"""Vercel entry point: the same FastAPI app as local runs (UI at /ui, API at /api/v1)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from southcity_assistant.app import create_app  # noqa: E402

app = create_app()
