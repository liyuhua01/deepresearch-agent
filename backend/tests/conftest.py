"""Shared test isolation for local telemetry artifacts."""

from __future__ import annotations

import os

os.environ.setdefault("PERSIST_RUN_METRICS", "false")
# A developer's local .env may enable Basic Auth. HTTP contract tests exercise
# auth separately and must not depend on machine-local credentials.
os.environ.setdefault("APP_ACCESS_PASSWORD", "")
