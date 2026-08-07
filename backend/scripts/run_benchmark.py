#!/usr/bin/env python3
"""Run the fixed Deep Research benchmark against a real deployed SSE API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from evaluation.benchmark import BenchmarkRunner, load_questions  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse command-line options without accepting secrets directly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="API root, without /research")
    parser.add_argument(
        "--questions",
        type=Path,
        default=BACKEND_ROOT / "benchmarks" / "questions.json",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--password-env", default="BENCHMARK_ACCESS_PASSWORD")
    parser.add_argument("--search-api", default="duckduckgo")
    parser.add_argument("--model")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-web-research-loops", type=int, default=3)
    parser.add_argument("--limit", type=int, help="Run only the first N questions")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="With --resume, rerun only non-completed terminal artifacts",
    )
    parser.add_argument("--disable-provenance", action="store_true")
    parser.add_argument("--skip-accessibility", action="store_true")
    parser.add_argument(
        "--skip-capacity-check",
        action="store_true",
        help="Skip the default /readyz budget preflight",
    )
    return parser.parse_args()


def main() -> int:
    """Execute the benchmark and return a CI-friendly process status."""
    args = parse_args()
    _, questions = load_questions(args.questions)
    if args.limit is not None:
        if args.limit <= 0:
            raise SystemExit("--limit must be positive")
        questions = questions[: args.limit]
    if args.resume and args.output_dir is None:
        raise SystemExit("--resume requires --output-dir")
    if args.rerun_failed and not args.resume:
        raise SystemExit("--rerun-failed requires --resume")
    output_dir = args.output_dir or (
        BACKEND_ROOT
        / "benchmarks"
        / "results"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    password = os.getenv(args.password_env)
    runner = BenchmarkRunner(
        base_url=args.base_url,
        output_dir=output_dir,
        password=password,
        search_api=args.search_api,
        enable_source_provenance=not args.disable_provenance,
        check_accessibility=not args.skip_accessibility,
        model=args.model,
        repetitions=args.repetitions,
        resume=args.resume,
        rerun_failed=args.rerun_failed,
        max_web_research_loops=args.max_web_research_loops,
    )
    try:
        pending_runs = runner.pending_run_count(questions)
        if not args.skip_capacity_check and pending_runs:
            runner.preflight_capacity(pending_runs)
        summary = runner.run(questions)
    finally:
        runner.close()
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.write(f"Artifacts: {output_dir.resolve()}\n")
    return 0 if summary.get("completed_count") == summary.get("run_count") else 2


if __name__ == "__main__":
    raise SystemExit(main())
