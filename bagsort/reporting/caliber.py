"""Versioned calculation standards (口径) for the shift report.

A caliber is a named, versioned bundle of rules that decides what the shift
report counts. Rules are stored as data in the command journal
(``caliber.define``), never as code, so every historical version remains
computable after a newer one takes over — that is what makes "口径升级后历史
班报仍可复算" a property of the design rather than a promise.

Versions form a linear chain: the first caliber is version 1 and every new
definition must be exactly the next integer. The active caliber is always the
highest defined version, so "同一时间只有一个生效口径" holds structurally —
there is no separate active pointer that could drift out of sync.
"""

from __future__ import annotations

from typing import Any

from bagsort.clock import Clock
from bagsort.errors import NotFoundError, ValidationError
from bagsort.model.entities import Caliber
from bagsort.store.store import Store

# The rule keys the report engine understands. Unknown keys are rejected so a
# typo cannot silently change what a report counts.
RULE_KEYS = ("include_closed_flights", "count_diverted_bags")

# The built-in legacy caliber: the standard historical shift reports were
# issued under. It counted every flight on record, closed or not, and counted
# diverted bags in the bag totals.
LEGACY_NAME = "legacy"
LEGACY_RULES: dict[str, Any] = {
    "include_closed_flights": True,
    "count_diverted_bags": True,
}


def normalize_rules(raw: dict[str, Any] | None) -> dict[str, bool]:
    """Validate a rules payload, filling omitted keys with ``False``."""

    rules: dict[str, bool] = {}
    for key, value in (raw or {}).items():
        if key not in RULE_KEYS:
            raise ValidationError(f"unknown caliber rule {key!r}")
        if not isinstance(value, bool):
            raise ValidationError(f"caliber rule {key!r} must be a boolean")
        rules[key] = value
    return {key: rules.get(key, False) for key in RULE_KEYS}


class CaliberBook:
    """Keeps the linear version chain of report calibers."""

    def __init__(self, store: Store, clock: Clock, audit: object = None) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit

    # -- read side ---------------------------------------------------------

    def defined(self) -> bool:
        return bool(self._store.state().calibers)

    def versions(self) -> list[int]:
        return sorted(self._store.state().calibers)

    def get(self, version: int) -> Caliber:
        caliber = self._store.state().calibers.get(version)
        if caliber is None:
            raise NotFoundError(f"caliber version {version} is not defined")
        return caliber

    def current(self) -> Caliber:
        """The one active caliber: always the highest defined version."""

        versions = self.versions()
        if not versions:
            raise NotFoundError("no caliber is defined yet")
        return self.get(versions[-1])

    def history(self) -> list[dict[str, Any]]:
        """Every caliber change, entry by entry, cross-referenced with the log.

        Each row carries the journal sequence of the ``caliber.define`` command
        that introduced the version, so the change history can be checked
        against the command log one command at a time.
        """

        definitions: dict[int, tuple[int, str]] = {}
        for entry in self._store.entries():
            if entry.kind == "caliber.define":
                version = int(entry.payload.get("caliber", {}).get("version", 0))
                definitions[version] = (entry.sequence, entry.at)
        rows: list[dict[str, Any]] = []
        for version in self.versions():
            caliber = self.get(version)
            sequence, logged_at = definitions.get(version, (0, ""))
            rows.append(
                {
                    **caliber.as_dict(),
                    "active": version == self.versions()[-1],
                    "journal_sequence": sequence,
                    "journal_at": logged_at,
                }
            )
        return rows

    def consistent(self) -> bool:
        """Version chain and caliber records agree with each other.

        Holds when the defined versions are exactly ``1..N`` with no gaps and
        every version traces back to a ``caliber.define`` command in the log.
        """

        versions = self.versions()
        if versions != list(range(1, len(versions) + 1)):
            return False
        logged = {
            int(entry.payload.get("caliber", {}).get("version", 0))
            for entry in self._store.entries()
            if entry.kind == "caliber.define"
        }
        return all(version in logged for version in versions)

    def verify(self) -> dict[str, Any]:
        """Corroborate the caliber chain against the journal and audit chain.

        Every defined version must trace back to one ``caliber.define`` journal
        command and one ``caliber_define`` audit entry, and exactly one version
        — the highest — may be active.
        """

        versions = self.versions()
        journal_logged = {
            int(entry.payload.get("caliber", {}).get("version", 0))
            for entry in self._store.entries()
            if entry.kind == "caliber.define"
        }
        audit_logged = {
            int(entry.detail.get("version", 0))
            for entry in self._store.state().audit
            if entry.kind == "caliber_define"
        }
        rows = [
            {
                "version": version,
                "journal_logged": version in journal_logged,
                "audit_logged": version in audit_logged,
                "active": version == versions[-1],
            }
            for version in versions
        ]
        linear = versions == list(range(1, len(versions) + 1))
        single_active = sum(1 for row in rows if row["active"]) <= 1
        ok = (
            linear
            and single_active
            and all(row["journal_logged"] and row["audit_logged"] for row in rows)
        )
        return {"linear": linear, "single_active": single_active, "versions": rows, "ok": ok}

    # -- write side --------------------------------------------------------

    def ensure_current(self) -> Caliber:
        """Bootstrap the built-in legacy caliber on first use.

        Only ever called on a write path (issuing a report), so read paths
        never mutate the store. The bootstrap is itself a journaled command,
        which keeps even the first caliber auditable.
        """

        if self.defined():
            return self.current()
        return self.define(1, LEGACY_NAME, dict(LEGACY_RULES))

    def define(self, version: int, name: str, rules: dict[str, Any] | None) -> Caliber:
        """Append the next caliber version; it becomes the one active caliber."""

        if not name.strip():
            raise ValidationError("caliber name must not be empty")
        expected = (self.versions()[-1] if self.defined() else 0) + 1
        if version != expected:
            raise ValidationError(
                f"caliber versions are linear: next version must be {expected}, got {version}"
            )
        caliber = Caliber(
            version=version,
            name=name.strip(),
            rules=normalize_rules(rules),
            defined_at=self._clock.now_iso(),
        )
        with self._store.session() as session:
            session.state.calibers[version] = caliber
            session.record("caliber.define", {"caliber": caliber.as_dict()}, caliber.defined_at)
        if self._audit is not None:
            self._audit.record(
                "caliber_define",
                f"caliber-v{version}",
                "audit",
                {"version": version, "name": caliber.name, "rules": caliber.rules},
            )
        return caliber
