"""Versioned reporting calibers and the metrics engine they drive.

A *caliber* is a named, immutable set of counting rules. Every metric the
reporting service emits is computed by :func:`compute_metrics`, so changing a
rule means registering a new caliber version rather than editing code
silently. The digest of a caliber is taken over its rules alone, which lets an
audit prove "version v2 really was this rule set" at any later date.

The two calibers shipped here differ on purpose so the old/new split is visible:

* ``v1`` — diverted bags are excluded from the active count and loaded bags
  are excluded from the total ("in plant" view).
* ``v2`` — diverted bags are counted as active (they still occupy handling
  capacity until reconciled) and the total includes every bag ever checked in
  ("total handled" view).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bagsort.errors import ValidationError
from bagsort.model.enums import BagState
from bagsort.model.state import State

#: Rules keys every caliber must carry.
RULE_KEYS = ("count_diverted_active", "count_loaded_in_total")

BUILTIN_CALIBERS: tuple[dict[str, Any], ...] = (
    {
        "version": "v1",
        "name": "in-plant",
        "rules": {"count_diverted_active": False, "count_loaded_in_total": False},
    },
    {
        "version": "v2",
        "name": "total-handled",
        "rules": {"count_diverted_active": True, "count_loaded_in_total": True},
    },
)


def digest_rules(rules: dict[str, Any]) -> str:
    """Stable digest over the rule set that defines a caliber."""

    material = json.dumps(
        rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_rules(raw: dict[str, Any]) -> dict[str, bool]:
    """Coerce and validate the counting switches, rejecting unknown keys."""

    rules: dict[str, bool] = {}
    for key in RULE_KEYS:
        value = raw.get(key)
        if not isinstance(value, bool):
            raise ValidationError(f"caliber rule {key!r} must be a boolean")
        rules[key] = value
    unknown = set(raw) - set(RULE_KEYS)
    if unknown:
        raise ValidationError(f"unknown caliber rules: {sorted(unknown)!r}")
    return rules


def metrics_digest(metrics: dict[str, Any]) -> str:
    """Stable digest over a computed metrics document."""

    material = json.dumps(
        metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _active_states(rules: dict[str, bool]) -> set[BagState]:
    states = {
        BagState.CHECKED_IN,
        BagState.INJECTED,
        BagState.SORTED,
        BagState.RECHECKED,
    }
    if rules["count_diverted_active"]:
        states.add(BagState.DIVERTED)
    return states


def compute_metrics(state: State, rules: dict[str, Any]) -> dict[str, Any]:
    """Compute every report metric from one folded state and one rule set.

    The result is deterministic: the same journal prefix folded with the same
    rules always produces the same document (and therefore the same digest),
    which is what forward recomputation relies on.
    """

    rules = normalize_rules(rules)
    active_states = _active_states(rules)

    bag_states = [bag.state for bag in state.bags.values()]
    flights = list(state.flights.values())

    bags_total = (
        len(bag_states)
        if rules["count_loaded_in_total"]
        else sum(1 for item in bag_states if item is not BagState.LOADED)
    )
    active_bags = sum(1 for item in bag_states if item in active_states)
    diverted_bags = sum(1 for item in bag_states if item is BagState.DIVERTED)
    loaded_bags = sum(1 for item in bag_states if item is BagState.LOADED)

    return {
        "flights": len(flights),
        "open_flights": sum(1 for flight in flights if flight.state.value in ("open", "boarding")),
        "closed_flights": sum(1 for flight in flights if flight.state.value == "closed"),
        "bags_total": bags_total,
        "active_bags": active_bags,
        "diverted_bags": diverted_bags,
        "loaded_bags": loaded_bags,
        "assignments": len(state.assignments),
        "sort_records": len(state.sort_records),
        "load_records": len(state.load_records),
    }
