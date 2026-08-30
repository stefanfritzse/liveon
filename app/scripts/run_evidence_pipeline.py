"""Run the evidence pipeline by hand, and read back what it did.

This exists for the first live run. Triggering the pipeline from the scheduler puts it in
a background task inside the web process, where the only evidence of what happened is the
log; for a path that has never met a real model, that is the wrong place to find out.

    # See what it would do, publishing nothing:
    python -m app.scripts.run_evidence_pipeline --job articles --dry-run

    # Do it for real:
    python -m app.scripts.run_evidence_pipeline --job articles

    # Read back any run:
    python -m app.scripts.run_evidence_pipeline --show-runs
    python -m app.scripts.run_evidence_pipeline --show-run <run_id>

``--dry-run`` runs acquisition, extraction, ranking, synthesis, review and the post-edit
re-check exactly as a real run does, then declines to store the result. Everything that
can refuse still refuses, so a dry run that publishes nothing has told you something real.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.models.evidence import EvidenceBundle, EvidenceRecord
from app.models.run_outcome import policy_for
from app.services.evidence.runlog import RunLog
from app.services.evidence.writers import ArticleWriter, TipWriter
from app.services.evidence_jobs import (
    _publish_article,
    _publish_tip,
    build_evidence_pipeline,
)
from app.services.evidence_pipeline import run_article, run_tip
from app.services.sqlite_repo import create_repository
from app.services.tip_publisher import TipPublisher

LOGGER = logging.getLogger("liveon.evidence")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--job",
        choices=("articles", "tips"),
        default="articles",
        help="Which product to generate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every stage but store nothing.",
    )
    parser.add_argument(
        "--show-runs",
        action="store_true",
        help="List recent runs and exit.",
    )
    parser.add_argument(
        "--show-run",
        metavar="RUN_ID",
        help="Print one run and its events, then exit.",
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="How many runs --show-runs lists."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Log every stage as it happens."
    )
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )


# ----------------------------------------------------------------------
# Reading the log
# ----------------------------------------------------------------------


def _show_runs(limit: int) -> int:
    with RunLog() as log:
        runs = log.recent(limit=limit)

    if not runs:
        print("No runs recorded yet.")
        print(f"(database: {os.getenv('LIVEON_DB_PATH') or 'default'})")
        return 0

    print(f"{'run id':34}  {'job':9}  {'started':20}  outcome")
    for run in runs:
        print(
            f"{run.run_id:34}  {run.job:9}  "
            f"{run.started_at.strftime('%Y-%m-%d %H:%M:%S'):20}  {run.outcome or 'unfinished'}"
        )
    return 0


def _show_run(run_id: str) -> int:
    with RunLog() as log:
        run = log.get(run_id)
        events = log.events(run_id) if run else []

    if run is None:
        print(f"No run with id {run_id!r}.")
        return 1

    print(f"run     {run.run_id}")
    print(f"job     {run.job}")
    print(f"started {run.started_at.isoformat()}")
    print(f"outcome {run.outcome or 'unfinished'}")
    print(f"model   {run.model_id or 'unrecorded'}")
    if run.prompt_versions:
        print(f"prompts {run.prompt_versions}")
    if run.data:
        print(f"summary {run.data}")

    print("\nevents:")
    for event in events:
        stamp = event["at"].strftime("%H:%M:%S") if event["at"] else "--:--:--"
        print(f"  {stamp}  {event['stage']:8} {event['event']}")
        if event["data"] is not None:
            print(f"            {_condense(event['data'])}")
    return 0


def _condense(data: Any, width: int = 160) -> str:
    text = str(data)
    return text if len(text) <= width else text[: width - 1] + "…"


# ----------------------------------------------------------------------
# Running
# ----------------------------------------------------------------------


def _describe(result: Any, *, dry_run: bool) -> None:
    policy = policy_for(result.outcome)
    print()
    print(f"outcome    {result.outcome.value}  ({policy.note})")
    print(f"run id     {result.run_id or 'not recorded'}")
    print(f"acquired   {result.acquired} new source(s)")
    print(f"considered {result.considered} candidate topic(s)")

    if result.bundle is not None:
        print(f"topic      {result.bundle.topic_key}")
        print(f"grade      {result.bundle.grade}")
        for line in result.bundle.grade_rationale:
            print(f"           - {line}")
        for claim in result.bundle.claims:
            print(f"claim      [{claim.claim_type}] {claim.text}")

    for violation in result.violations:
        print(f"refused    {violation.gate}: {violation.detail}")

    for note in result.notes:
        if note:
            print(f"note       {note}")

    if dry_run:
        print("\nDry run: nothing was stored.")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    if args.show_runs:
        return _show_runs(args.limit)
    if args.show_run:
        return _show_run(args.show_run)

    moment = datetime.now(timezone.utc)
    pipeline = build_evidence_pipeline()
    pipeline.dry_run = args.dry_run
    writer_llm = pipeline.synthesizer.llm
    model_id = pipeline.synthesizer.model_id

    if args.job == "articles":
        repository = None if args.dry_run else create_repository()

        def publish(draft: Any, bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]):
            if args.dry_run:
                return _Withheld(draft)
            return _publish_article(
                draft, bundle, records, repository=repository, published_at=moment
            )

        result = run_article(pipeline, ArticleWriter(llm=writer_llm, model_id=model_id), publish)
    else:
        publisher = None if args.dry_run else TipPublisher(create_repository())

        def publish(draft: Any, bundle: EvidenceBundle, records: Mapping[str, EvidenceRecord]):
            if args.dry_run:
                return _Withheld(draft)
            return _publish_tip(
                draft, bundle, records, publisher=publisher, published_at=moment
            )

        result = run_tip(pipeline, TipWriter(llm=writer_llm, model_id=model_id), publish)

    _describe(result, dry_run=args.dry_run)

    # A refusal is a successful run of a fail-closed pipeline, so it exits 0. Only a run
    # that could not reach a conclusion is an error worth a non-zero status.
    return 0 if not policy_for(result.outcome).retry else 1


class _Withheld:
    """Stands in for stored content during a dry run.

    The pipeline skips usage recording in this mode, so rehearsing a topic does not put
    it inside the repetition window and block the real run it was meant to de-risk.
    """

    def __init__(self, draft: Any) -> None:
        self.id = "dry-run"
        self.draft = draft


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
