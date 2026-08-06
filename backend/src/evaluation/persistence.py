"""Atomic local persistence for terminal research-run metrics."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class RunMetricsStore:
    """Persist one JSON artifact per run without exposing partial files."""

    def __init__(self, directory: Path, *, enabled: bool = True) -> None:
        """Initialize the artifact directory without creating it eagerly."""
        self.directory = Path(directory)
        self.enabled = enabled

    def persist(self, metrics: Mapping[str, Any]) -> Path | None:
        """Write metrics atomically and return the final artifact path."""
        if not self.enabled:
            return None

        run_id = str(metrics.get("run_id", ""))
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains characters that are unsafe for a filename")

        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.directory / f"{run_id}.json"
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=f".{run_id}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                json.dump(
                    dict(metrics),
                    temporary,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())

            os.replace(temporary_path, target)
            os.chmod(target, 0o600)
            return target
        except BaseException:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
