"""Issuing versioned reports.

Every issued report is a frozen :class:`ReportRun` committed to the journal.
It pins three things that together make the report reproducible and
attributable:

* the caliber *version* and the *digest* of the rules that version stood for,
* the journal *watermark* the metrics were read from, and
* a digest of the metrics document itself.

Issuing also writes a ``report.issued`` audit entry that carries the same run
id, caliber digest and metrics digest, so the report, the audit chain and any
CSV export can be checked against each other. Recomputation is always a
forward fold of the original journal prefix — see :mod:`bagsort.reporting.recompute`.
"""

from __future__ import annotations

from typing import Any

from bagsort.audit.chain import AuditChain
from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.ids import IdFactory
from bagsort.model.entities import ReportRun
from bagsort.model.state import State
from bagsort.reporting.caliber import compute_metrics, metrics_digest
from bagsort.reporting.book import CaliberBook
from bagsort.store.store import Store

#: How a report's numbers were produced. ``forward`` recomputes from the raw
#: journal; ``reverse`` was derived from an already issued result.
FORWARD = "forward"
REVERSE = "reverse"


class ReportOffice:
    """Issues, lists and imports historical report runs."""

    def __init__(
        self,
        store: Store,
        calibers: CaliberBook,
        audit: AuditChain,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._store = store
        self._calibers = calibers
        self._audit = audit
        self._clock = clock
        self._ids = ids

    def issue(self) -> ReportRun:
        """Issue a report under the single caliber currently in force."""

        caliber = self._calibers.active()
        if caliber is None:
            raise ConflictError("no active reporting caliber; activate one first")
        return self._issue(caliber, FORWARD, self._store.state(), self._store.watermark)

    def import_legacy(
        self,
        version: str,
        watermark: int,
        metrics: dict[str, Any],
        at: str = "",
    ) -> ReportRun:
        """Register a report that predates caliber versioning.

        Legacy reports carry ``basis='reverse'``: their numbers are the ones
        that were published at the time, not a forward fold of the (possibly
        compacted away) original commands. Recomputation treats that premise
        as unverifiable rather than pretending otherwise.
        """

        caliber = self._calibers.require(version)
        if watermark < 0 or watermark > self._store.watermark:
            raise ValidationError("legacy report watermark is outside the retained journal")
        run_id = self._ids.next()
        run = ReportRun(
            run_id=run_id,
            caliber_version=caliber.version,
            caliber_digest=caliber.digest,
            watermark=watermark,
            metrics=dict(metrics),
            metrics_digest=metrics_digest(dict(metrics)),
            basis=REVERSE,
            at=at or self._clock.now_iso(),
            audit_sequence=0,
        )
        with self._store.session() as session:
            session.state.report_runs.append(run)
            session.record("report.issue", {"run": run.as_dict()}, run.at)
        return run

    def _issue(
        self, caliber, basis: str, state: State, watermark: int
    ) -> ReportRun:
        run_id = self._ids.next()
        at = self._clock.now_iso()
        metrics = compute_metrics(state, caliber.rules)
        digest = metrics_digest(metrics)
        # The next audit entry sequence is knowable: issuing writes no audit
        # entry itself, so the entry we are about to record is count + 1.
        audit_sequence = self._audit.count() + 1
        run = ReportRun(
            run_id=run_id,
            caliber_version=caliber.version,
            caliber_digest=caliber.digest,
            watermark=watermark,
            metrics=metrics,
            metrics_digest=digest,
            basis=basis,
            at=at,
            audit_sequence=audit_sequence,
        )
        with self._store.session() as session:
            session.state.report_runs.append(run)
            session.record("report.issue", {"run": run.as_dict()}, at)

        entry = self._audit.record(
            "report.issued",
            run_id,
            "governance",
            {
                "caliber_version": caliber.version,
                "caliber_digest": caliber.digest,
                "watermark": watermark,
                "metrics_digest": digest,
                "basis": basis,
            },
            at=at,
        )
        if entry.sequence != audit_sequence:
            raise ConflictError(
                f"report {run_id} audit link drifted: expected {audit_sequence}, "
                f"got {entry.sequence}"
            )
        return run

    # -- read side ---------------------------------------------------------

    def list(self) -> list[ReportRun]:
        return list(self._store.state().report_runs)

    def get(self, run_id: str) -> ReportRun | None:
        return next(
            (run for run in self._store.state().report_runs if run.run_id == run_id),
            None,
        )

    def require(self, run_id: str) -> ReportRun:
        run = self.get(run_id)
        if run is None:
            raise NotFoundError(f"report run {run_id!r} does not exist")
        return run
