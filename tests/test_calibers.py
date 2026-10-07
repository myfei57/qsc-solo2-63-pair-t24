"""Versioned reporting calibers: issuance, recomputation and corroboration."""

from __future__ import annotations

import pytest

from bagsort.reporting import csv_export
from bagsort.reporting.caliber import digest_rules
from tests.support import check_in


def _setup(plant, flight):
    plant.seed_calibers()
    plant.activate_caliber("v1", reason="initial")
    bags = check_in(plant, flight.id, 3)
    plant.assign_chute(flight.id, "T1-CHA")
    plant.push_bag(bags[0].id, "T1-CHA")
    plant.divert_bag(bags[2].id)
    return bags


# -- caliber registration and the single-active rule -------------------------


def test_issue_is_rejected_without_an_active_caliber(plant, flight):
    check_in(plant, flight.id, 1)
    plant.seed_calibers()
    with pytest.raises(Exception):
        plant.issue_report()


def test_calibers_are_registered_with_a_rule_digest(plant, flight):
    plant.seed_calibers()
    v1 = plant.calibers.require("v1")
    assert v1.digest == digest_rules(v1.rules)


def test_a_version_cannot_be_registered_twice_with_other_rules(plant, flight):
    plant.seed_calibers()
    with pytest.raises(Exception):
        plant.register_caliber(
            "v1", "dup", {"count_diverted_active": True, "count_loaded_in_total": True}
        )


def test_only_one_caliber_is_active_at_a_time(plant, flight):
    _setup(plant, flight)
    assert plant.calibers.active_version() == "v1"
    plant.activate_caliber("v2")
    assert plant.calibers.active_version() == "v2"
    plant.activate_caliber("v1", reason="rollback")
    assert plant.calibers.active_version() == "v1"
    assert [row["version"] for row in plant.calibers.history()] == ["v1", "v2", "v1"]


# -- issuance pins the caliber and the input watermark -----------------------


def test_an_issued_report_pins_version_digest_watermark_and_metrics(plant, flight):
    _setup(plant, flight)
    before = plant.store.watermark
    run = plant.issue_report()
    assert run.caliber_version == "v1"
    assert run.caliber_digest == plant.calibers.require("v1").digest
    assert run.basis == "forward"
    # The run pins the data prefix it read, which precedes its own journal write.
    assert run.watermark == before
    assert plant.store.watermark > before


def test_v1_and_v2_count_diverted_capacity_differently(plant, flight):
    _setup(plant, flight)
    v1 = plant.issue_report().metrics
    assert v1["active_bags"] == 2
    plant.activate_caliber("v2")
    v2 = plant.issue_report().metrics
    assert v2["active_bags"] == 3
    # The historical v1 run is never rewritten by the upgrade.
    assert plant.reports.list()[0].metrics["active_bags"] == 2


# -- forward recomputation ---------------------------------------------------


def test_recompute_folds_the_original_raw_commands_and_matches(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    result = plant.recomputer.recompute(run.run_id)
    assert result["feasible"] is True
    assert result["matches"] is True
    assert result["caliber_was_in_force"] is True
    assert result["premise"].startswith("forward")
    assert not any(row["changed"] for row in result["comparison"])


def test_historical_report_still_recomputes_after_an_upgrade(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    plant.activate_caliber("v2")
    plant.issue_report()
    result = plant.recomputer.recompute(run.run_id)
    assert result["matches"] is True
    assert result["caliber_version"] == "v1"


def test_counterfactual_compares_new_caliber_on_the_old_input(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    result = plant.recomputer.recompute(run.run_id, compare_version="v2")
    counterfactual = result["counterfactual"]
    active = next(row for row in counterfactual["comparison"] if row["metric"] == "active_bags")
    assert active["original"] == 2
    assert active["recomputed"] == 3
    assert active["delta"] == 1


def test_side_by_side_compares_two_calibers_on_current_data(plant, flight):
    _setup(plant, flight)
    diff = plant.recomputer.compare_calibers("v1", "v2")
    changed_rules = [row["rule"] for row in diff["rule_changes"] if row["changed"]]
    assert changed_rules == ["count_diverted_active", "count_loaded_in_total"]
    assert all("delta" in row for row in diff["comparison"])


def test_legacy_reverse_report_is_marked_unverifiable(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    legacy = plant.reports.import_legacy("v1", run.watermark, run.metrics)
    assert legacy.basis == "reverse"
    result = plant.recomputer.recompute(legacy.run_id)
    assert result["feasible"] is False
    assert result["matches"] is False
    assert result["reason"]
    assert result["premise"].startswith("reverse")


# -- audit chain, journal and export corroboration ---------------------------


def test_caliber_changes_land_in_both_journal_and_audit_chain(plant, flight):
    _setup(plant, flight)
    kinds = plant.audit.by_kind()
    assert kinds.get("caliber_register", 0) >= 2
    assert kinds.get("caliber_change", 0) >= 1
    journal = plant.store.kinds()
    assert journal.get("caliber.register", 0) >= 2
    assert journal.get("caliber.activate", 0) >= 1
    assert plant.audit.verify() is True


def test_report_audit_entry_carries_the_cross_check_digests(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    entry = plant.audit.entries()[run.audit_sequence - 1]
    assert entry.kind == "report.issued"
    assert entry.subject == run.run_id
    assert entry.detail["metrics_digest"] == run.metrics_digest
    assert entry.detail["caliber_digest"] == run.caliber_digest
    assert entry.detail["watermark"] == run.watermark


def test_corroboration_matches_journal_audit_and_export(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    export_row = csv_export.report_rows([run])[0]
    result = plant.recomputer.corroborate(run.run_id, export_row)
    assert result["intact"] is True
    assert result["checks"]["export_row"] is True


def test_corroboration_flags_a_tampered_metric(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    state = plant.store.state()
    tampered = state.report_runs[0]
    state.report_runs[0] = tampered.__class__(
        **{**tampered.as_dict(), "metrics": {**tampered.metrics, "active_bags": 99}}
    )
    result = plant.recomputer.corroborate(run.run_id)
    assert result["checks"]["metrics_digest"] is False
    assert result["intact"] is False


def test_caliber_change_history_is_reviewable_row_by_row(plant, flight):
    from bagsort.reporting.recompute import caliber_history

    _setup(plant, flight)
    plant.activate_caliber("v2", reason="capacity review")
    rows = caliber_history(plant.store, plant.calibers)
    assert [row["version"] for row in rows] == ["v1", "v2"]
    assert all(row["digest"] and row["watermark"] for row in rows)
    assert rows[1]["reason"] == "capacity review"


def test_report_and_caliber_exports_carry_trace_columns(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    report_csv = csv_export.render(
        csv_export.REPORT_COLUMNS, csv_export.report_rows([run])
    )
    assert "caliber_version" in report_csv.splitlines()[0]
    assert run.run_id in report_csv
    caliber_csv = csv_export.render(
        csv_export.CALIBER_COLUMNS,
        csv_export.caliber_rows(plant.calibers.history()),
    )
    assert "count_diverted_active" in caliber_csv.splitlines()[0]


# -- persistence keeps historical reports reproducible -----------------------


def test_restart_keeps_calibers_runs_and_recomputation(plant, flight):
    _setup(plant, flight)
    run = plant.issue_report()
    plant.activate_caliber("v2")
    path = plant.store.path
    plant.store.persist()

    from bagsort.app import Plant
    from bagsort.store.store import Store

    restarted = Plant.build(Store.open(path))
    assert [c.version for c in restarted.calibers.list()] == ["v1", "v2"]
    assert restarted.calibers.active_version() == "v2"
    assert restarted.recomputer.recompute(run.run_id)["matches"] is True
    assert restarted.audit.verify() is True
