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
    # Force the program name to "tm" (not "tm.py"): we run via `uv run --script tm.py`,
    # and Click derives shell-completion names from argv[0]. Without this the completion
    # binds to "tm.py" with an invalid env var (_TM.PY_COMPLETE) and never fires.
    app(prog_name="tm")
