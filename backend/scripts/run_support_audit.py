#!/usr/bin/env python3
"""Export or score human semantic-support review datasets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evaluation.semantic_support import (  # noqa: E402
    build_review_items,
    load_review_items,
    merge_independent_reviews,
    sample_review_items,
    score_review_items,
    write_review_template,
)


def main() -> int:
    """Run the selected semantic-support export or scoring command."""
    parser = argparse.ArgumentParser(
        description="Export and score independently reviewed claim-citation pairs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export")
    export.add_argument("--report", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--run-id", required=True)
    export.add_argument("--question-id")

    export_benchmark = subparsers.add_parser("export-benchmark")
    export_benchmark.add_argument("--results-dir", type=Path, required=True)
    export_benchmark.add_argument("--output", type=Path, required=True)
    export_benchmark.add_argument(
        "--sample-size",
        type=int,
        help="Deterministically sample this many items across questions",
    )

    score = subparsers.add_parser("score")
    score.add_argument("--annotations", type=Path, required=True)
    score.add_argument("--output", type=Path)
    score.add_argument("--minimum-reviewers", type=int, default=2)

    merge = subparsers.add_parser("merge")
    merge.add_argument(
        "--review",
        action="append",
        type=Path,
        required=True,
        help="One independently annotated review file; pass at least twice",
    )
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Allow missing per-item labels while retaining identity checks",
    )

    args = parser.parse_args()
    if args.command == "export":
        report = args.report.read_text(encoding="utf-8")
        items = build_review_items(
            report,
            run_id=args.run_id,
            question_id=args.question_id,
        )
        write_review_template(args.output, items)
        sys.stdout.write(
            json.dumps({"output": str(args.output), "items": len(items)}) + "\n"
        )
        return 0

    if args.command == "export-benchmark":
        items = []
        for run_path in sorted((args.results_dir / "runs").glob("*.json")):
            run = json.loads(run_path.read_text(encoding="utf-8"))
            if run.get("status") != "completed":
                continue
            report_path = args.results_dir / str(run.get("report_path") or "")
            if not report_path.is_file():
                raise FileNotFoundError(f"missing report for {run_path.name}")
            items.extend(
                build_review_items(
                    report_path.read_text(encoding="utf-8"),
                    run_id=str(run.get("run_id") or run_path.stem),
                    question_id=str(run.get("question_id") or "") or None,
                )
            )
        if args.sample_size is not None:
            items = sample_review_items(items, sample_size=args.sample_size)
        write_review_template(args.output, items)
        sys.stdout.write(
            json.dumps({"output": str(args.output), "items": len(items)}) + "\n"
        )
        return 0

    if args.command == "merge":
        if len(args.review) < 2:
            raise SystemExit("merge requires at least two --review files")
        merged = merge_independent_reviews(
            (load_review_items(path) for path in args.review),
            require_complete=not args.allow_incomplete,
        )
        write_review_template(args.output, merged)
        sys.stdout.write(
            json.dumps(
                {
                    "output": str(args.output),
                    "items": len(merged),
                    "reviewers": sorted(
                        {
                            annotation["reviewer"]
                            for item in merged
                            for annotation in item.annotations
                        }
                    ),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0

    items = load_review_items(args.annotations)
    summary = score_review_items(items, minimum_reviewers=args.minimum_reviewers)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
