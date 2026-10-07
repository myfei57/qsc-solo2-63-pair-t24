"""Forward recomputation of an issued report and new/old caliber comparison.

Recomputation premise (deliberately chosen, and recorded on every result):

* **Forward only.** A report is recomputed by folding the *original raw
  commands* from the journal prefix ``1..run.watermark`` and applying the exact
  caliber rules captured on the run. It is never derived backwards from a
  previously published result: reverse inference assumes the old answer and so
  cannot independently confirm it.
* **Compaction feasibility.** Folding needs every command up to the run's
  watermark. When the retained journal starts above that watermark the raw
  data has been compacted away and recomputation is ``infeasible``. Reports
  imported as ``reverse`` legacy runs are infeasible for the same reason —
  their premise is a published number, not raw data.
"""

from __future__ import annotations

from typing import Any

from bagsort.model.state import State, fold
from bagsort.reporting.book import CaliberBook
from bagsort.reporting.caliber import compute_metrics, metrics_digest
from bagsort.reporting.issuer import FORWARD, ReportOffice
from bagsort.store.store import Store

FORWARD_PREMISE = (
    "forward recomputation: fold the retained journal commands 1..watermark "
    "and re-apply the caliber rules captured on the run; requires every raw "
    "command through the watermark to still be retained"
)
REVERSE_PREMISE = (
    "reverse inference: derive figures from an already published result; "
    "not used for verification because it assumes the answer under test"
)

#: Metric keys that can be laid out side by side.
METRIC_KEYS = (
    "flights",
    "open_flights",
    "closed_flights",
    "bags_total",
    "active_bags",
    "diverted_bags",
    "loaded_bags",
    "assignments",
    "sort_records",
    "load_records",
)


def prefix_state(store: Store, watermark: int) -> State:
    """Fold the retained journal commands up to and including ``watermark``."""

    pairs = []
    for index, entry in enumerate(store.entries(), start=1):
        if index > watermark:
            break
        pairs.append((entry.kind, entry.payload))
    return fold(pairs)


class Recomputer:
    """Re-derives report numbers from raw commands and compares calibers."""

    def __init__(self, store: Store, calibers: CaliberBook, reports: ReportOffice) -> None:
        self._store = store
        self._calibers = calibers
        self._reports = reports

    def recompute(self, run_id: str, compare_version: str = "") -> dict[str, Any]:
        """Forward recompute a run, optionally under a counterfactual caliber."""

        run = self._reports.require(run_id)
        result: dict[str, Any] = {
            "run_id": run.run_id,
            "basis": run.basis,
            "premise": FORWARD_PREMISE if run.basis == FORWARD else REVERSE_PREMISE,
            "watermark": run.watermark,
            "caliber_version": run.caliber_version,
            "feasible": False,
            "reason": "",
            "original_digest": run.metrics_digest,
            "recomputed_digest": "",
            "matches": False,
            "caliber_was_in_force": False,
            "comparison": None,
        }

        if run.basis != FORWARD:
            result["reason"] = "run was imported from a published result, not raw data"
            return result
        if not self._raw_data_retained(run.watermark):
            result["reason"] = (
                f"raw commands through watermark {run.watermark} are no longer retained"
            )
            return result

        state = prefix_state(self._store, run.watermark)
        caliber = self._calibers.require(run.caliber_version)
        in_force = self._calibers.active_at(run.watermark)
        metrics = compute_metrics(state, caliber.rules)
        digest = metrics_digest(metrics)
        result.update(
            feasible=True,
            recomputed_digest=digest,
            matches=digest == run.metrics_digest,
            caliber_was_in_force=bool(in_force and in_force.version == caliber.version),
            comparison=self._comparison(run.metrics, metrics),
        )
        if compare_version and compare_version != run.caliber_version:
            other = self._calibers.require(compare_version)
            other_metrics = compute_metrics(state, other.rules)
            result["counterfactual"] = {
                "caliber_version": other.version,
                "caliber_digest": other.digest,
                "metrics_digest": metrics_digest(other_metrics),
                "comparison": self._comparison(metrics, other_metrics),
            }
        return result

    def compare_calibers(self, left_version: str, right_version: str) -> dict[str, Any]:
        """Apply two calibers to the same current data, side by side."""

        left = self._calibers.require(left_version)
        right = self._calibers.require(right_version)
        state = self._store.state()
        left_metrics = compute_metrics(state, left.rules)
        right_metrics = compute_metrics(state, right.rules)
        return {
            "watermark": self._store.watermark,
            "left": {
                "caliber_version": left.version,
                "caliber_digest": left.digest,
                "metrics_digest": metrics_digest(left_metrics),
            },
            "right": {
                "caliber_version": right.version,
                "caliber_digest": right.digest,
                "metrics_digest": metrics_digest(right_metrics),
            },
            "rule_changes": rule_diff(left.rules, right.rules),
            "comparison": self._comparison(left_metrics, right_metrics),
        }

    def corroborate(self, run_id: str, export_row: dict[str, Any] | None = None) -> dict[str, Any]:
        """Cross check one report across the journal, audit chain and an export.

        Each of the four evidence sources must agree: the run's stored digest,
        the caliber version's rule digest, the linked audit entry, and (when
        supplied) the CSV row that was handed out. ``intact`` is true only when
        every individual check passes.
        """

        run = self._reports.require(run_id)
        caliber = self._calibers.get(run.caliber_version)
        audit_entries = self._store.state().audit
        audit_entry = (
            next(
                (
                    entry
                    for entry in audit_entries
                    if entry.kind == "report.issued" and entry.subject == run.run_id
                ),
                None,
            )
            if run.audit_sequence == 0
            else (
                audit_entries[run.audit_sequence - 1]
                if 0 < run.audit_sequence <= len(audit_entries)
                else None
            )
        )

        caliber_digest_ok = bool(caliber and caliber.digest == run.caliber_digest)
        metrics_digest_ok = metrics_digest(run.metrics) == run.metrics_digest
        audit_ok = bool(
            audit_entry
            and audit_entry.kind == "report.issued"
            and audit_entry.subject == run.run_id
            and str(audit_entry.detail.get("metrics_digest", "")) == run.metrics_digest
            and str(audit_entry.detail.get("caliber_digest", "")) == run.caliber_digest
            and int(audit_entry.detail.get("watermark", -1)) == run.watermark
        )
        journal_ok = run.watermark <= self._store.watermark
        checks = {
            "metrics_digest": metrics_digest_ok,
            "caliber_digest": caliber_digest_ok,
            "audit_entry": audit_ok,
            "journal_watermark": journal_ok,
        }
        if export_row is not None:
            checks["export_row"] = (
                str(export_row.get("run_id", "")) == run.run_id
                and str(export_row.get("caliber_version", "")) == run.caliber_version
                and str(export_row.get("caliber_digest", "")) == run.caliber_digest
                and str(export_row.get("metrics_digest", "")) == run.metrics_digest
                and int(export_row.get("watermark", -1)) == run.watermark
            )
        return {"run_id": run.run_id, "intact": all(checks.values()), "checks": checks}

    # -- helpers -----------------------------------------------------------

    def _raw_data_retained(self, watermark: int) -> bool:
        entries = self._store.entries()
        if not entries:
            return watermark == 0
        return entries[0].sequence == 1 and len(entries) >= watermark

    @staticmethod
    def _comparison(original: dict[str, Any], recomputed: dict[str, Any]) -> list[dict[str, Any]]:
        """Lay the two metric sets out, one row per metric, differences flagged."""

        rows: list[dict[str, Any]] = []
        for key in METRIC_KEYS:
            old = original.get(key)
            new = recomputed.get(key)
            rows.append(
                {
                    "metric": key,
                    "original": old,
                    "recomputed": new,
                    "delta": _number(new) - _number(old),
                    "changed": old != new,
                }
            )
        return rows


def rule_diff(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per rule whose value differs between two calibers."""

    rows: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        old = left.get(key)
        new = right.get(key)
        rows.append({"rule": key, "from": old, "to": new, "changed": old != new})
    return rows


def caliber_history(store: Store, calibers: CaliberBook) -> list[dict[str, Any]]:
    """Activation ledger joined with the journal watermark each change landed at."""

    rows: list[dict[str, Any]] = []
    activations = [
        entry for entry in store.entries() if entry.kind == "caliber.activate"
    ]
    for index, entry in enumerate(activations, start=1):
        version = str(entry.payload["version"])
        caliber = calibers.require(version)
        rows.append(
            {
                "sequence": index,
                "version": version,
                "name": caliber.name,
                "digest": caliber.digest,
                "reason": str(entry.payload.get("reason", "")),
                "watermark": entry.sequence,
                "at": entry.at,
            }
        )
    return rows


def _number(value: Any) -> int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
