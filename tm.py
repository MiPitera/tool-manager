#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "typer>=0.12",
#     "rich>=13",
#     "httpx>=0.27",
#     "pydantic>=2.6",
# ]
# ///
"""tm — tool manager. Entry point; the real code lives in the `tm` package next to this file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tm.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
