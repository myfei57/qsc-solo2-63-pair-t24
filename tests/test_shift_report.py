"""Shift reports with versioned calibers: issue, recalculate, compare, verify."""

from __future__ import annotations

import json

import pytest

from bagsort.errors import StaleError, ValidationError
from bagsort.reporting import csv_export
from bagsort.reporting.caliber import LEGACY_RULES
from bagsort.store.snapshot import build
from bagsort.store.store import Store
from tests.support import check_in, make_plant, make_server, register_flight

NEW_RULES = {"include_closed_flights": False, "count_diverted_bags": False}


def _shift_with_history(plant):
    """One flight, three bags (one diverted), then the flight is closed."""

    flight = register_flight(plant)
    bags = check_in(plant, flight.id, 3)
    plant.divert_bag(bags[-1].id)
    plant.close_flight(flight.id)
    return flight


def test_issue_pins_the_active_caliber_and_watermark(plant):
    _shift_with_history(plant)
    report = plant.shift_reports.issue()
    assert report.caliber_version == 1  # bootstrapped legacy caliber
    assert plant.caliber.current().rules == LEGACY_RULES
    assert report.watermark < plant.store.watermark  # captured before journaling
    assert report.metrics["flights"] == 1  # legacy counts the closed flight
    assert report.metrics["bags"] == 3  # legacy counts the diverted bag
    assert report.digest


def test_recalculate_replays_the_pinned_caliber_after_upgrade(plant):
    _shift_with_history(plant)
    report = plant.shift_reports.issue()
    plant.caliber.define(2, "current", dict(NEW_RULES))

    outcome = plant.shift_reports.recalculate(report.id)
    assert outcome["basis"] == "journal_replay"
    assert outcome["caliber_version"] == 1
    assert outcome["matches_issued"] is True
    assert outcome["metric_diffs"] == {}
    assert outcome["issued"]["metrics"] == outcome["recalculated"]["metrics"]


def test_compare_runs_old_and_new_caliber_side_by_side(plant):
    _shift_with_history(plant)
    report = plant.shift_reports.issue()
    plant.caliber.define(2, "current", dict(NEW_RULES))

    outcome = plant.shift_reports.compare(report.id)
    assert outcome["pinned"]["version"] == 1
    assert outcome["other"]["version"] == 2
    assert outcome["pinned"]["matches_issued"] is True
    # Same history, two calibers: the difference is visible field by field.
    assert outcome["pinned"]["metrics"]["bags"] == 3
    assert outcome["other"]["metrics"]["bags"] == 2
    assert outcome["pinned"]["metrics"]["flights"] == 1
    assert outcome["other"]["metrics"]["flights"] == 0
    assert outcome["metric_diffs"]["bags"] == {"issued": 3, "recomputed": 2}
    assert outcome["metric_diffs"]["flights"] == {"issued": 1, "recomputed": 0}


def test_new_reports_use_the_new_caliber_old_reports_stay_pinned(plant):
    _shift_with_history(plant)
    first = plant.shift_reports.issue()
    plant.caliber.define(2, "current", dict(NEW_RULES))
    second = plant.shift_reports.issue()

    assert first.caliber_version == 1
    assert second.caliber_version == 2
    assert second.metrics["bags"] == 2
    assert second.metrics["flights"] == 0
    # The historical report still recalculates under its own caliber.
    assert plant.shift_reports.recalculate(first.id)["matches_issued"] is True


def test_caliber_change_lands_in_the_command_log_and_audit_chain(plant):
    plant.shift_reports.issue()  # bootstraps legacy v1
    plant.caliber.define(2, "current", dict(NEW_RULES))

    kinds = [entry.kind for entry in plant.store.entries()]
    assert kinds.count("caliber.define") == 2
    defines = [entry for entry in plant.store.entries() if entry.kind == "caliber.define"]
    assert [int(entry.payload["caliber"]["version"]) for entry in defines] == [1, 2]

    audited = [
        entry for entry in plant.audit.entries() if entry.kind == "caliber_define"
    ]
    assert [entry.detail["version"] for entry in audited] == [1, 2]
    assert plant.audit.verify() is True


def test_caliber_history_is_reviewable_entry_by_entry(plant):
    plant.shift_reports.issue()
    plant.caliber.define(2, "current", dict(NEW_RULES))

    history = plant.caliber.history()
    assert [row["version"] for row in history] == [1, 2]
    assert [row["active"] for row in history] == [False, True]
    # Every row points at the journal command that introduced it.
    sequences = {
        entry.sequence: int(entry.payload["caliber"]["version"])
        for entry in plant.store.entries()
        if entry.kind == "caliber.define"
    }
    for row in history:
        assert sequences[row["journal_sequence"]] == row["version"]
        assert row["journal_at"]


def test_caliber_versions_are_linear_and_single_active(plant):
    plant.caliber.define(1, "legacy", dict(LEGACY_RULES))
    with pytest.raises(ValidationError):
        plant.caliber.define(3, "skip-a-version", dict(NEW_RULES))
    with pytest.raises(ValidationError):
        plant.caliber.define(1, "again", dict(LEGACY_RULES))
    plant.caliber.define(2, "current", dict(NEW_RULES))
    assert plant.caliber.current().version == 2
    assert plant.caliber.consistent() is True
    verify = plant.caliber.verify()
    assert verify["single_active"] is True
    assert verify["ok"] is True


def test_unknown_caliber_rule_is_rejected(plant):
    with pytest.raises(ValidationError):
        plant.caliber.define(1, "legacy", {"count_diverted_bag": True})  # typo
    with pytest.raises(ValidationError):
        plant.caliber.define(1, "legacy", {"count_diverted_bags": "yes"})


def test_recalculate_refuses_once_the_journal_is_compacted(plant, tmp_path):
    _shift_with_history(plant)
    report = plant.shift_reports.issue()

    # Simulate 数据压实: keep only the tail of the journal, sequences intact.
    tail = [entry for entry in plant.store.entries() if entry.kind in ("caliber.define", "report.issue", "audit.append")]
    assert tail and tail[0].sequence > 1
    compacted = Store(str(tmp_path / "compacted.json"))
    compacted.restore(build(tail))

    from bagsort.app import Plant

    rebuilt = Plant.build(compacted, clock=plant.clock)
    assert rebuilt.shift_reports.get(report.id).id == report.id
    with pytest.raises(StaleError):
        rebuilt.shift_reports.recalculate(report.id)
    with pytest.raises(StaleError):
        rebuilt.shift_reports.compare(report.id)


def test_verify_corroborates_reports_journal_and_audit(plant):
    _shift_with_history(plant)
    plant.shift_reports.issue()
    plant.caliber.define(2, "current", dict(NEW_RULES))
    plant.shift_reports.issue()

    outcome = plant.shift_reports.verify()
    assert outcome["ok"] is True
    assert outcome["audit_intact"] is True
    assert outcome["caliber"]["ok"] is True
    assert [row["ok"] for row in outcome["reports"]] == [True, True]
    for row in outcome["reports"]:
        assert row["checks"] == {
            "digest_intact": True,
            "caliber_known": True,
            "journal_logged": True,
            "audit_logged": True,
        }


def test_verify_detects_a_tampered_report(plant):
    plant.shift_reports.issue()
    report = plant.shift_reports.list()[0]
    plant.store.state().reports[report.id].metrics["bags"] = 999  # tamper
    outcome = plant.shift_reports.verify()
    assert outcome["ok"] is False
    assert outcome["reports"][0]["checks"]["digest_intact"] is False


def test_report_export_carries_caliber_version_and_digest(plant):
    _shift_with_history(plant)
    report = plant.shift_reports.issue()
    rows = csv_export.report_rows(plant.shift_reports.list())
    assert rows[0]["caliber_version"] == report.caliber_version
    assert rows[0]["digest"] == report.digest
    text = csv_export.render(csv_export.REPORT_COLUMNS, rows)
    assert text.splitlines()[0] == ",".join(csv_export.REPORT_COLUMNS)
    assert str(report.watermark) in text


def test_report_ids_survive_a_restart(plant, tmp_path):
    plant.shift_reports.issue()
    plant.store.persist()

    from bagsort.app import Plant

    reopened = Plant.build(Store.open(plant.store.path), clock=plant.clock)
    second = reopened.shift_reports.issue()
    ids = [report.id for report in reopened.shift_reports.list()]
    assert len(ids) == len(set(ids)) == 2
    assert second.id != reopened.shift_reports.list()[0].id


# ---------------------------------------------------------------------------
# console endpoints
# ---------------------------------------------------------------------------


def _call(server, method, path, payload=None, query=None):
    response = server.call(method, path, payload, query)
    if response.content_type.startswith("application/json"):
        return response.status, json.loads(response.body)
    return response.status, response.body.decode("utf-8")


def test_console_caliber_and_report_flow(server):
    plant = server.plant
    flight = register_flight(plant)
    bags = check_in(plant, flight.id, 2)
    plant.divert_bag(bags[-1].id)

    status, caliber = _call(server, "GET", "/api/caliber")
    assert (status, caliber["defined"]) == (200, False)

    status, report = _call(server, "POST", "/api/reports")
    assert status == 201
    assert report["caliber_version"] == 1
    report_id = report["id"]

    status, caliber = _call(server, "GET", "/api/caliber")
    assert caliber["defined"] is True
    assert caliber["active"]["version"] == 1
    assert caliber["consistent"] is True

    status, defined = _call(
        server,
        "POST",
        "/api/caliber/define",
        {"version": 2, "name": "current", "rules": NEW_RULES},
    )
    assert (status, defined["version"]) == (201, 2)

    status, recalculated = _call(server, "POST", f"/api/reports/{report_id}/recalculate")
    assert status == 200
    assert recalculated["matches_issued"] is True
    assert recalculated["caliber_version"] == 1

    status, compared = _call(server, "GET", f"/api/reports/{report_id}/compare")
    assert compared["pinned"]["version"] == 1
    assert compared["other"]["version"] == 2
    assert compared["metric_diffs"]["bags"] == {"issued": 2, "recomputed": 1}

    status, history = _call(server, "GET", "/api/caliber/history")
    assert [row["version"] for row in history] == [1, 2]

    status, verify = _call(server, "GET", "/api/reports/verify")
    assert verify["ok"] is True
    status, caliber_verify = _call(server, "GET", "/api/caliber/verify")
    assert caliber_verify["ok"] is True

    status, listing = _call(server, "GET", "/api/reports")
    assert [row["id"] for row in listing] == [report_id]
    status, fetched = _call(server, "GET", f"/api/reports/{report_id}")
    assert fetched["digest"] == report["digest"]

    status, csv_text = _call(server, "GET", "/api/export/reports.csv")
    assert status == 200
    assert report["digest"] in csv_text
    assert "caliber_version" in csv_text.splitlines()[0]


def test_console_recalculated_and_issued_are_side_by_side(server):
    plant = server.plant
    flight = register_flight(plant)
    check_in(plant, flight.id, 1)
    _, report = _call(server, "POST", "/api/reports")

    _, outcome = _call(server, "POST", f"/api/reports/{report['id']}/recalculate")
    assert set(outcome["issued"]) == set(outcome["recalculated"]) == {"metrics", "flights"}
    assert outcome["issued"]["metrics"] == outcome["recalculated"]["metrics"]
    assert outcome["issued"]["flights"] == outcome["recalculated"]["flights"]


def test_console_define_rejects_a_version_skip(server):
    status, error = _call(
        server, "POST", "/api/caliber/define", {"version": 2, "name": "skip"}
    )
    assert status == 400
    assert "linear" in error["error"]


def test_console_unknown_report_is_a_404(server):
    status, _ = _call(server, "GET", "/api/reports/rpt-0099")
    assert status == 404
    status, _ = _call(server, "POST", "/api/reports/rpt-0099/recalculate")
    assert status == 404
