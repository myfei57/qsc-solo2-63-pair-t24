"""Registration and activation of reporting caliber versions.

Calibers live in the command journal like every other piece of state.
Registration only introduces a version; activation appends to a separate,
append only ledger whose latest row names the single caliber in force. That
split keeps two invariants real:

* the same version can never be registered twice with different rules, and
* at any moment exactly one caliber is active, even across a rollback.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.model.entities import Caliber, CaliberActivation
from bagsort.model.validate import require_text
from bagsort.reporting.caliber import digest_rules, normalize_rules
from bagsort.store.store import Store


class CaliberBook:
    """The catalog of registered calibers and the activation ledger."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- write side --------------------------------------------------------

    def register(self, version: str, name: str, rules: dict[str, object]) -> Caliber:
        version = require_text(version, "version")
        name = require_text(name, "name")
        normalized = normalize_rules(dict(rules))
        existing = self._store.state().calibers.get(version)
        if existing is not None:
            raise ConflictError(f"caliber {version!r} is already registered")
        digest = digest_rules(normalized)
        caliber = Caliber(
            version=version,
            name=name,
            rules=normalized,
            digest=digest,
            registered_at=self._clock.now_iso(),
        )
        with self._store.session() as session:
            session.state.calibers[version] = caliber
            session.record(
                "caliber.register",
                {"caliber": caliber.as_dict()},
                caliber.registered_at,
            )
        return caliber

    def activate(self, version: str, reason: str = "") -> Caliber:
        """Make one registered caliber the single caliber in force."""

        caliber = self.require(version)
        at = self._clock.now_iso()
        activation = CaliberActivation(version=caliber.version, at=at, reason=reason)
        with self._store.session() as session:
            session.state.caliber_activations.append(activation)
            session.record("caliber.activate", activation.as_dict(), at)
        return caliber

    # -- read side ---------------------------------------------------------

    def list(self) -> list[Caliber]:
        return [self._store.state().calibers[key] for key in sorted(self._store.state().calibers)]

    def get(self, version: str) -> Caliber | None:
        return self._store.state().calibers.get(version)

    def require(self, version: str) -> Caliber:
        caliber = self.get(version)
        if caliber is None:
            raise NotFoundError(f"caliber {version!r} is not registered")
        return caliber

    def history(self) -> list[dict[str, object]]:
        """Every activation, in ledger order, with the caliber rules attached."""

        rows: list[dict[str, object]] = []
        for activation in self._store.state().caliber_activations:
            caliber = self.require(activation.version)
            rows.append(
                {
                    "sequence": len(rows) + 1,
                    "version": caliber.version,
                    "name": caliber.name,
                    "digest": caliber.digest,
                    "rules": caliber.rules,
                    "at": activation.at,
                    "reason": activation.reason,
                }
            )
        return rows

    def active(self) -> Caliber | None:
        activations = self._store.state().caliber_activations
        if not activations:
            return None
        return self.require(activations[-1].version)

    def active_version(self) -> str:
        caliber = self.active()
        return caliber.version if caliber else ""

    def active_at(self, watermark: int):
        """The caliber in force at a given journal watermark.

        Scanning the committed journal up to ``watermark`` reproduces which
        caliber was live when a historical report was issued, without the
        materialised state needing to remember every past activation itself.
        """

        if watermark < 0:
            raise ValidationError("watermark must not be negative")
        current = None
        for index, entry in enumerate(self._store.entries(), start=1):
            if index > watermark:
                break
            if entry.kind == "caliber.activate":
                current = str(entry.payload["version"])
        return self.require(current) if current is not None else None
