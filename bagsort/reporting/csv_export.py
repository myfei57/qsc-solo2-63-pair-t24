"""CSV rendering used by the export endpoints."""

from __future__ import annotations

import csv
import io
from typing import Iterable, Sequence


def render(columns: Sequence[str], rows: Iterable[dict[str, object]]) -> str:
    """Render a header plus one line per row, in the column order given."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue()


def audit_rows(entries: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "sequence": entry.sequence,
            "kind": entry.kind,
            "subject": entry.subject,
            "stage": entry.detail.get("stage", ""),
            "at": entry.at,
            "digest": entry.digest,
        }
        for entry in entries
    ]


def sort_rows(records: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "bag_id": record.bag_id,
            "flight_id": record.flight_id,
            "chute_id": record.chute_id,
            "sequence": record.sequence,
            "epoch": record.epoch,
            "at": record.at,
        }
        for record in records
    ]


def load_rows(records: Iterable[object]) -> list[dict[str, object]]:
    return [
        {
            "bag_id": record.bag_id,
            "carousel": record.carousel,
            "batch": record.batch,
            "at": record.at,
        }
        for record in records
    ]


def report_rows(runs: Iterable[object]) -> list[dict[str, object]]:
    """Frozen report runs with the caliber and digest an auditor can trace."""

    return [
        {
            "run_id": run.run_id,
            "caliber_version": run.caliber_version,
            "caliber_digest": run.caliber_digest,
            "watermark": run.watermark,
            "basis": run.basis,
            "bags_total": run.metrics.get("bags_total", ""),
            "active_bags": run.metrics.get("active_bags", ""),
            "diverted_bags": run.metrics.get("diverted_bags", ""),
            "loaded_bags": run.metrics.get("loaded_bags", ""),
            "metrics_digest": run.metrics_digest,
            "audit_sequence": run.audit_sequence,
            "at": run.at,
        }
        for run in runs
    ]


def caliber_rows(history: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """The activation ledger with the rules that were in force each time."""

    rows: list[dict[str, object]] = []
    for item in history:
        rules = item.get("rules", {})
        rows.append(
            {
                "sequence": item.get("sequence", ""),
                "version": item.get("version", ""),
                "name": item.get("name", ""),
                "digest": item.get("digest", ""),
                "count_diverted_active": rules.get("count_diverted_active", "")
                if isinstance(rules, dict)
                else "",
                "count_loaded_in_total": rules.get("count_loaded_in_total", "")
                if isinstance(rules, dict)
                else "",
                "reason": item.get("reason", ""),
                "at": item.get("at", ""),
            }
        )
    return rows


AUDIT_COLUMNS = ("sequence", "kind", "subject", "stage", "at", "digest")
SORT_COLUMNS = ("bag_id", "flight_id", "chute_id", "sequence", "epoch", "at")
LOAD_COLUMNS = ("bag_id", "carousel", "batch", "at")
REPORT_COLUMNS = (
    "run_id",
    "caliber_version",
    "caliber_digest",
    "watermark",
    "basis",
    "bags_total",
    "active_bags",
    "diverted_bags",
    "loaded_bags",
    "metrics_digest",
    "audit_sequence",
    "at",
)
CALIBER_COLUMNS = (
    "sequence",
    "version",
    "name",
    "digest",
    "count_diverted_active",
    "count_loaded_in_total",
    "reason",
    "at",
)
