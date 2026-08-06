"""Shared test isolation for local telemetry artifacts."""

from __future__ import annotations

import os

os.environ.setdefault("PERSIST_RUN_METRICS", "false")
