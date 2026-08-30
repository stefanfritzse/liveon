"""Keep what was published true after it was published.

With no human in the loop, automatic correction is the only correction this system has.
Everything before this module decides what to publish; this one is the only part that can
change its mind afterwards, which makes it the last line rather than an afterthought.

Three jobs, in descending order of how sure we are of them:

* **Retractions and corrections.** Re-check every source that published content cites. A
  paper that has since been retracted, or put under an expression of concern, must stop
  supporting a live claim. This is definite: the signal comes from PubMed, and the response
  is mechanical.
* **Supersession.** When a newer bundle covers a topic, the older ones stop being the
  answer. Useful and safe — nothing is deleted, and the coach simply stops quoting a
  bundle that has been replaced.
* **Contradiction.** When newly acquired evidence disagrees with what was published, the
  topic is worth revisiting. This is the least certain of the three: disagreement is
  detected from extracted outcome directions, which are model output, so it does not
  retract anything. It lifts the repetition cooldown, and a contradiction becomes a
  candidate story rather than a correction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from typing import Any, Callable, Iterable, Protocol, Sequence

from app.models.evidence import EvidenceRecord, parse_source_key

LOGGER = logging.getLogger(__name__)

__all__ = [
    "MaintenanceReport",
    "MaintenanceSweep",
    "retraction_policy",
]

#: What a retraction does to content already published from the paper.
_POLICIES = ("annotate", "unpublish")


def retraction_policy() -> str:
    """How to treat published content whose source was retracted.

    ``annotate`` leaves the piece up with a correction notice, which keeps the record of
    what was said and tells the reader it was wrong. ``unpublish`` takes it off the site.
    Annotating is the default because a silently vanished article is indistinguishable
    from one that never existed.
    """

    raw = (os.getenv("LIVEON_RETRACTION_POLICY") or "").strip().lower()
    return raw if raw in _POLICIES else "annotate"


class SupportsRefetch(Protocol):
    """The slice of the PubMed client the sweep needs."""

    def fetch(self, pmids: Sequence[str]) -> list[EvidenceRecord]:
        ...


class SupportsContentRepository(Protocol):
    def get_article(self, article_id: str) -> Any: ...

    def save_article(self, article: Any) -> Any: ...

    def get_tip(self, tip_id: str) -> Any: ...

    def save_tip(self, tip: Any) -> Any: ...


@dataclass(slots=True)
class MaintenanceReport:
    """What one sweep found and did."""

    checked: int = 0
    retracted: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)
    annotated: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    contradicted_topics: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(
            self.retracted
            or self.corrected
            or self.annotated
            or self.withdrawn
            or self.superseded
            or self.contradicted_topics
        )

    def summary(self) -> str:
        return (
            f"checked {self.checked} source(s); "
            f"{len(self.retracted)} retracted, {len(self.corrected)} corrected, "
            f"{len(self.annotated)} annotated, {len(self.withdrawn)} withdrawn, "
            f"{len(self.superseded)} superseded, "
            f"{len(self.contradicted_topics)} topic(s) reopened"
        )


#: Wording is fixed here rather than generated. A correction notice is the one piece of
#: text on the site that absolutely must not be improvised.
_RETRACTION_NOTICE = (
    "Correction: a study this article relied on has since been retracted. We have left the "
    "article up so the record is clear, but its conclusions should not be relied on."
)
_CONCERN_NOTICE = (
    "Correction: a study this article relied on is now subject to an expression of concern. "
    "Treat its conclusions with caution until that is resolved."
)
_CORRECTION_NOTICE = (
    "Note: a study this article relied on has since been corrected by its authors. The "
    "correction may or may not affect what is described here."
)

_NOTICES = {
    "retracted": _RETRACTION_NOTICE,
    "concern": _CONCERN_NOTICE,
    "corrected": _CORRECTION_NOTICE,
}


@dataclass(slots=True)
class MaintenanceSweep:
    """Re-check published evidence and act on what changed."""

    store: Any
    repository: Any = None
    acquirer: SupportsRefetch | None = None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    policy: str | None = None

    def run(self) -> MaintenanceReport:
        """Run every maintenance job, reporting what changed."""

        report = MaintenanceReport()
        self.check_retractions(report)
        self.mark_superseded(report)
        self.find_contradictions(report)

        LOGGER.info(
            "Maintenance sweep: %s",
            report.summary(),
            extra={"event": "maintenance.finished", "acted": report.acted},
        )
        return report

    # -- retractions ---------------------------------------------------

    def check_retractions(self, report: MaintenanceReport) -> MaintenanceReport:
        """Re-fetch every cited source and act on any change in its standing."""

        if self.acquirer is None:
            return report

        keys = self.store.used_source_keys()
        report.checked = len(keys)
        if not keys:
            return report

        by_pmid: dict[str, str] = {}
        for key in keys:
            record = self.store.get_record(key)
            pmid = _pmid_for(record) if record else None
            if pmid:
                by_pmid[pmid] = key

        if not by_pmid:
            return report

        try:
            refreshed = self.acquirer.fetch(list(by_pmid))
        except Exception as exc:  # noqa: BLE001 - an unreachable source is not a retraction
            LOGGER.warning(
                "Retraction check could not reach the source: %s",
                exc,
                extra={"event": "maintenance.refetch_failed"},
            )
            report.errors.append(str(exc))
            return report

        for record in refreshed:
            key = self._match_key(record, by_pmid)
            if key is None:
                continue
            self._apply_state_change(key, record.retraction_state, record.retraction_notes, report)

        return report

    def _match_key(self, record: EvidenceRecord, by_pmid: dict[str, str]) -> str | None:
        """Find the stored key this freshly fetched record corresponds to."""

        pmid = _pmid_for(record)
        if pmid and pmid in by_pmid:
            return by_pmid[pmid]
        return self.store.resolve(record.source_key)

    def _apply_state_change(
        self,
        key: str,
        state: str,
        notes: Sequence[str],
        report: MaintenanceReport,
    ) -> None:
        stored = self.store.get_record(key)
        if stored is None or state == stored.retraction_state or state == "none":
            return

        self.store.set_retraction(key, state, notes=list(notes))

        if state == "retracted":
            report.retracted.append(key)
        elif state == "corrected":
            report.corrected.append(key)

        LOGGER.warning(
            "A cited source is now %s",
            state,
            extra={"event": "maintenance.source_changed", "source_key": key, "state": state},
        )
        self._act_on_published(key, state, report)

    def _act_on_published(self, key: str, state: str, report: MaintenanceReport) -> None:
        """Annotate or withdraw everything published from this source."""

        if self.repository is None:
            return

        notice = _NOTICES.get(state)
        if notice is None:
            return

        # Withdrawal is for retractions only. An expression of concern or a correction is
        # a caveat, not a reason to erase what was said.
        withdraw = state == "retracted" and (self.policy or retraction_policy()) == "unpublish"

        for usage in self.store.usage_for_source(key):
            content = self._load(usage)
            if content is None:
                continue

            changed = False
            if withdraw and not content.withdrawn:
                content.withdrawn = True
                report.withdrawn.append(usage["content_id"])
                changed = True
            if content.correction_notice != notice:
                content.correction_notice = notice
                if not withdraw:
                    report.annotated.append(usage["content_id"])
                changed = True

            if changed:
                self._save(usage, content)
                LOGGER.warning(
                    "Published %s affected by a %s source",
                    usage["content_type"],
                    state,
                    extra={
                        "event": "maintenance.content_corrected",
                        "content_id": usage["content_id"],
                        "withdrawn": withdraw,
                    },
                )

    def _load(self, usage: dict[str, Any]) -> Any:
        getter = (
            self.repository.get_article
            if usage["content_type"] == "article"
            else self.repository.get_tip
        )
        try:
            return getter(usage["content_id"])
        except Exception:  # noqa: BLE001 - a missing row is not a failure of the sweep
            return None

    def _save(self, usage: dict[str, Any], content: Any) -> None:
        saver = (
            self.repository.save_article
            if usage["content_type"] == "article"
            else self.repository.save_tip
        )
        saver(content)

    # -- supersession --------------------------------------------------

    def mark_superseded(self, report: MaintenanceReport) -> MaintenanceReport:
        """Point older bundles at the newer one covering the same topic.

        Nothing is deleted: the older bundle is what was believed at the time, and the run
        log refers to it. It simply stops being what the coach answers from.
        """

        for topic in self.store.topic_keys():
            bundles = [
                bundle
                for bundle in self.store.bundles_for_topic(topic)
                if bundle.review_status in ("approved", "downgraded")
            ]
            if len(bundles) < 2:
                continue

            newest, *older = bundles  # bundles_for_topic returns newest first
            for bundle in older:
                if bundle.superseded_by == newest.bundle_id:
                    continue
                bundle.superseded_by = newest.bundle_id
                self.store.save_bundle(bundle)
                report.superseded.append(bundle.bundle_id)
                LOGGER.info(
                    "Bundle superseded",
                    extra={
                        "event": "maintenance.superseded",
                        "bundle_id": bundle.bundle_id,
                        "by": newest.bundle_id,
                    },
                )
        return report

    # -- contradiction -------------------------------------------------

    def find_contradictions(self, report: MaintenanceReport) -> MaintenanceReport:
        """Note topics where newly approved evidence disagrees with what was published.

        Deliberately weak: disagreement is read from extracted outcome directions, which
        are model output, so this never retracts anything. It reopens the topic — a
        contradiction is a story worth telling, not a correction to make silently.
        """

        for topic in self.store.topic_keys():
            bundles = [
                bundle
                for bundle in self.store.bundles_for_topic(topic)
                if bundle.review_status in ("approved", "downgraded")
                and not bundle.superseded_by
            ]
            if not bundles:
                continue

            published = bundles[0]
            if any(claim.contradicted_by for claim in published.claims):
                report.contradicted_topics.append(topic)
                LOGGER.info(
                    "Topic carries recorded disagreement and is worth revisiting",
                    extra={"event": "maintenance.contradiction", "topic_key": topic},
                )
        return report


def _pmid_for(record: EvidenceRecord | None) -> str | None:
    """The PubMed ID for a record, from its key or its aliases."""

    if record is None:
        return None

    for candidate in (record.source_key, *record.aliases):
        try:
            scheme, value = parse_source_key(candidate)
        except ValueError:
            continue
        if scheme == "pmid" and value:
            return value
    return None


def reopened_topics(report: MaintenanceReport) -> Iterable[str]:
    """Topics the sweep thinks are worth covering again."""

    return report.contradicted_topics
