"""Issued shift reports (班报): pinned to a caliber version, reproducible later.

A shift report is issued against the one active caliber and records, alongside
the numbers, the caliber version used and the journal watermark at issuance.
That is what lets a later recalculation answer "which standard produced this
report" without guessing.

复算路线（选定）：按当时的原始数据重算。
    Recalculation replays the command journal up to the report's watermark and
    re-evaluates the pinned caliber's rules over that rebuilt state. The issued
    numbers are only ever a comparison baseline, never an input — a summary is
    lossy, so back-deriving the original data from issued results is not
    possible and is not attempted.

复算前提 (recalculation prerequisites):
    1. The command journal must still reach back to sequence 1 and forward to
       the report's watermark. Once the log is compacted away (数据压实) or
       reset, raw-data recalculation is infeasible; the service refuses with a
       StaleError instead of returning plausible-looking numbers.
    2. Caliber definitions live in the same journal (``caliber.define``), so
       every historical version's rules remain available after upgrades.
    3. Replay and rule evaluation are deterministic: the same journal prefix
       plus the same caliber version always yields the same numbers, and the
       report digest makes any drift detectable.

Every issuance also lands on the audit chain (kind ``report_issue``) with the
report id, caliber version and digest, and the CSV export carries the same
fields — so report store, audit chain and export corroborate each other.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bagsort.clock import Clock
from bagsort.errors import NotFoundError, StaleError
from bagsort.model.entities import Caliber, ShiftReport
from bagsort.model.enums import BagState, FlightState
from bagsort.model.state import State
from bagsort.model.validate import is_active_bag_state, is_open_state
from bagsort.store.replay import replay
from bagsort.store.store import Store
from .caliber import CaliberBook

REPORT_AUDIT_KIND = "report_issue"
CALIBER_AUDIT_KIND = "caliber_define"
AUDIT_STAGE = "audit"


def report_digest(report: ShiftReport) -> str:
    """Deterministic digest over the fields that identify a report's content."""

    material = json.dumps(
        [
            report.id,
            report.caliber_version,
            report.watermark,
            report.metrics,
            report.flights,
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_metrics(state: State, caliber: Caliber) -> dict[str, Any]:
    """Evaluate one caliber's rules over a (possibly historical) state."""

    rules = caliber.rules
    flights = list(state.flights.values())
    if not rules.get("include_closed_flights", False):
        flights = [flight for flight in flights if flight.state != FlightState.CLOSED]
    bags = list(state.bags.values())
    if not rules.get("count_diverted_bags", False):
        bags = [bag for bag in bags if bag.state != BagState.DIVERTED]
    return {
        "flights": len(flights),
        "open_flights": sum(1 for flight in flights if is_open_state(flight.state)),
        "closed_flights": sum(
            1 for flight in state.flights.values() if flight.state == FlightState.CLOSED
        ),
        "bags": len(bags),
        "active_bags": sum(1 for bag in bags if is_active_bag_state(bag.state)),
        "diverted_bags": sum(
            1 for bag in state.bags.values() if bag.state == BagState.DIVERTED
        ),
        "sorted": len(state.sort_records),
        "loads": len(state.load_records),
        "recheck_pending": len(state.recheck_queue),
    }


def compute_flight_rows(state: State, caliber: Caliber) -> list[dict[str, Any]]:
    """Per-flight rows under one caliber, in flight id order."""

    rules = caliber.rules
    rows: list[dict[str, Any]] = []
    for flight_id in sorted(state.flights):
        flight = state.flights[flight_id]
        if flight.state == FlightState.CLOSED and not rules.get(
            "include_closed_flights", False
        ):
            continue
        bags = [
            bag
            for bag in state.bags.values()
            if state.mappings.get(bag.id) == flight_id
            and (rules.get("count_diverted_bags", False) or bag.state != BagState.DIVERTED)
        ]
        rows.append(
            {
                "flight_id": flight_id,
                "code": flight.code,
                "state": flight.state.value,
                "bags": len(bags),
                "sorted": sum(1 for record in state.sort_records if record.flight_id == flight_id),
            }
        )
    return rows


def diff_metrics(issued: dict[str, Any], recomputed: dict[str, Any]) -> dict[str, Any]:
    """Field-by-field difference between two metric payloads."""

    diffs: dict[str, Any] = {}
    for key in sorted(set(issued) | set(recomputed)):
        before, after = issued.get(key), recomputed.get(key)
        if before != after:
            diffs[key] = {"issued": before, "recomputed": after}
    return diffs


class ShiftReportService:
    """Issues, recalculates and cross-checks shift reports."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        calibers: CaliberBook,
        audit: object,
    ) -> None:
        self._store = store
        self._clock = clock
        self._calibers = calibers
        self._audit = audit

    # -- issuing -----------------------------------------------------------

    def issue(self) -> ShiftReport:
        """Issue a shift report under the one active caliber.

        The watermark is captured before the report command is journaled, so
        replaying up to it reproduces exactly the state the report describes.
        """

        caliber = self._calibers.ensure_current()
        watermark = self._store.watermark
        state = self._store.state()
        # Reports are never removed, so the count gives a restart-safe id.
        report = ShiftReport(
            id=f"rpt-{len(state.reports) + 1:06d}",
            caliber_version=caliber.version,
            watermark=watermark,
            issued_at=self._clock.now_iso(),
            metrics=compute_metrics(state, caliber),
            flights=compute_flight_rows(state, caliber),
        )
        report = ShiftReport(
            id=report.id,
            caliber_version=report.caliber_version,
            watermark=report.watermark,
            issued_at=report.issued_at,
            metrics=report.metrics,
            flights=report.flights,
            digest=report_digest(report),
        )
        with self._store.session() as session:
            session.state.reports[report.id] = report
            session.record("report.issue", {"report": report.as_dict()}, report.issued_at)
        self._audit.record(
            REPORT_AUDIT_KIND,
            report.id,
            AUDIT_STAGE,
            {
                "caliber_version": report.caliber_version,
                "watermark": report.watermark,
                "digest": report.digest,
            },
        )
        return report

    # -- read side ---------------------------------------------------------

    def list(self) -> list[ShiftReport]:
        reports = self._store.state().reports
        return [reports[key] for key in sorted(reports)]

    def get(self, report_id: str) -> ShiftReport:
        report = self._store.state().reports.get(report_id)
        if report is None:
            raise NotFoundError(f"shift report {report_id!r} is not on record")
        return report

    # -- recalculation -----------------------------------------------------

    def _state_at(self, watermark: int) -> State:
        """Rebuild the state the report saw, or refuse when the log was compacted.

        复算前提的硬性守卫：日志必须仍从 sequence 1 起完整保留到报告水位。
        """

        entries = self._store.entries()
        if entries and entries[0].sequence > 1:
            raise StaleError(
                "journal was compacted: entries before sequence "
                f"{entries[0].sequence} are gone, raw-data recalculation is infeasible"
            )
        if self._store.watermark < watermark:
            raise StaleError(
                f"journal watermark {self._store.watermark} no longer reaches "
                f"report watermark {watermark}; raw-data recalculation is infeasible"
            )
        return replay([entry for entry in entries if entry.sequence <= watermark])

    def recalculate(self, report_id: str) -> dict[str, Any]:
        """Recompute a report from the raw journal under its pinned caliber.

        The issued payload is only compared against, never fed back in. The
        recalculated numbers must equal the issued ones field by field; any
        difference means the log, the caliber record or the issued report was
        tampered with.
        """

        report = self.get(report_id)
        caliber = self._calibers.get(report.caliber_version)
        state = self._state_at(report.watermark)
        metrics = compute_metrics(state, caliber)
        flights = compute_flight_rows(state, caliber)
        return {
            "report_id": report.id,
            "basis": "journal_replay",
            "caliber_version": caliber.version,
            "caliber_name": caliber.name,
            "watermark": report.watermark,
            "issued": {"metrics": report.metrics, "flights": report.flights},
            "recalculated": {"metrics": metrics, "flights": flights},
            "metric_diffs": diff_metrics(report.metrics, metrics),
            "matches_issued": metrics == report.metrics and flights == report.flights,
        }

    def compare(self, report_id: str, other_version: int | None = None) -> dict[str, Any]:
        """Run the pinned and another caliber side by side over the same history.

        Defaults the other side to the currently active caliber, which is how
        an auditor sees what the new standard would have said about the same
        shift next to what was actually reported.
        """

        report = self.get(report_id)
        pinned = self._calibers.get(report.caliber_version)
        other = self._calibers.get(other_version) if other_version else self._calibers.current()
        state = self._state_at(report.watermark)
        pinned_metrics = compute_metrics(state, pinned)
        other_metrics = compute_metrics(state, other)
        return {
            "report_id": report.id,
            "basis": "journal_replay",
            "watermark": report.watermark,
            "pinned": {
                "version": pinned.version,
                "name": pinned.name,
                "metrics": pinned_metrics,
                "flights": compute_flight_rows(state, pinned),
                "matches_issued": pinned_metrics == report.metrics,
            },
            "other": {
                "version": other.version,
                "name": other.name,
                "metrics": other_metrics,
                "flights": compute_flight_rows(state, other),
            },
            "metric_diffs": diff_metrics(pinned_metrics, other_metrics),
        }

    # -- cross-checking ----------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Corroborate every issued report against the journal and audit chain.

        For each report three independent records must agree: the stored
        report (its digest recomputes), the ``report.issue`` command in the
        journal, and the ``report_issue`` entry on the audit chain. The
        caliber side must hold too: the version chain is consistent and the
        pinned version still exists.
        """

        issued_commands: dict[str, dict[str, Any]] = {}
        for entry in self._store.entries():
            if entry.kind == "report.issue":
                raw = entry.payload.get("report", {})
                issued_commands[str(raw.get("id", ""))] = raw
        audit_by_subject: dict[str, list[dict[str, Any]]] = {}
        for entry in self._store.state().audit:
            if entry.kind == REPORT_AUDIT_KIND:
                audit_by_subject.setdefault(entry.subject, []).append(entry.detail)

        rows: list[dict[str, Any]] = []
        for report in self.list():
            command = issued_commands.get(report.id)
            audit_details = audit_by_subject.get(report.id, [])
            checks = {
                "digest_intact": report_digest(report) == report.digest,
                "caliber_known": report.caliber_version in self._calibers.versions(),
                "journal_logged": bool(command) and command.get("digest") == report.digest,
                "audit_logged": any(
                    detail.get("digest") == report.digest for detail in audit_details
                ),
            }
            rows.append(
                {
                    "report_id": report.id,
                    "caliber_version": report.caliber_version,
                    "watermark": report.watermark,
                    "checks": checks,
                    "ok": all(checks.values()),
                }
            )
        caliber = self._calibers.verify()
        return {
            "caliber": caliber,
            "audit_intact": self._audit.verify(),
            "reports": rows,
            "ok": caliber["ok"] and self._audit.verify() and all(row["ok"] for row in rows),
        }
