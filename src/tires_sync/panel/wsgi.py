from __future__ import annotations

import os

from .__main__ import build_app

app = build_app(os.environ, os.environ["TIRES_DB"], os.environ["TIRES_UNLOAD"], os.environ["TIRES_REFERENCE"])
