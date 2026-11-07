"""Allow running the package with ``python -m fractal_adjust``."""
from __future__ import annotations

import sys

from .cli import main

sys.exit(main())
