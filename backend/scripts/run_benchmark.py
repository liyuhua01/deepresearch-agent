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

from evaluation.benchmark import (  # noqa: E402
    BenchmarkRunner,
    load_questions,
    select_question_batch,
    select_questions,
)


def parse_args() -> argparse.Namespace:
    """Parse command-line options without accepting secrets directly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="API root, without /research")
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
    parser.add_argument(
        "--question-id",
        action="append",
        default=[],
        help="Select an exact question ID; repeat for multiple IDs",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Select a category; repeat for multiple categories",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Select a question tag; repeat for multiple tags",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help="Run one stable batch while retaining the full filtered campaign",
    )
    parser.add_argument(
        "--batch-index",
        type=int,
        default=1,
        help="1-based batch to run with --batch-size",
    )
    parser.add_argument(
        "--list-questions",
        action="store_true",
        help="Print the filtered/batched question selection without running it",
    )
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
    question_schema, loaded_questions = load_questions(args.questions)
    try:
        questions = select_questions(
            loaded_questions,
            question_ids=args.question_id,
            categories=args.category,
            tags=args.tag,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.limit is not None:
        if args.limit <= 0:
            raise SystemExit("--limit must be positive")
        questions = questions[: args.limit]
    campaign_questions = questions
    if args.batch_size is not None:
        if args.output_dir is None and not args.list_questions:
            raise SystemExit("--batch-size requires --output-dir")
        try:
            questions = select_question_batch(
                campaign_questions,
                batch_size=args.batch_size,
                batch_index=args.batch_index,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if args.batch_index > 1 and not args.resume and not args.list_questions:
            raise SystemExit("batch 2 or later requires --resume")
    elif args.batch_index != 1:
        raise SystemExit("--batch-index requires --batch-size")
    if args.list_questions:
        sys.stdout.write(
            json.dumps(
                {
                    "question_schema_version": question_schema,
                    "campaign_question_count": len(campaign_questions),
                    "selected_question_count": len(questions),
                    "batch_size": args.batch_size,
                    "batch_index": args.batch_index if args.batch_size else None,
                    "questions": [
                        {
                            "id": item.id,
                            "category": item.category,
                            "tags": list(item.tags),
                            "topic": item.topic,
                        }
                        for item in questions
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return 0
    if not args.base_url:
        raise SystemExit("--base-url is required unless --list-questions is used")
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
        summary = runner.run(questions, campaign_questions=campaign_questions)
    finally:
        runner.close()
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.write(f"Artifacts: {output_dir.resolve()}\n")
    return 0 if summary.get("completed_count") == summary.get("run_count") else 2


if __name__ == "__main__":
    raise SystemExit(main())
