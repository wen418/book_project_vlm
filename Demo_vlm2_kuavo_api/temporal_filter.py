#!/usr/bin/env python3
"""Conservative temporal debouncing for noisy frame-level VLM decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from perception_schema import FIELDS, ModelDecision


class TemporalStabilizer:
    """Require repeated observations before publishing a state transition.

    Door-open (``door_closed=false``) is published immediately for safety. A
    closed door and button transitions require repeated matching frames.
    Unknown input is always published as unknown rather than recycling stale
    state.
    """

    def __init__(
        self,
        confirmations: int = 2,
        state_path: Optional[Path] = None,
    ) -> None:
        if confirmations < 1:
            raise ValueError("confirmations 必须 >= 1")
        self.confirmations = confirmations
        self.state_path = state_path
        self.stable: Dict[str, Optional[bool]] = {name: None for name in FIELDS}
        self.pending: Dict[str, Dict[str, Any]] = {
            name: {"value": None, "count": 0} for name in FIELDS
        }
        if state_path and state_path.is_file():
            self._load()

    def _load(self) -> None:
        assert self.state_path is not None
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        for name in FIELDS:
            stable_value = payload.get("stable", {}).get(name)
            if stable_value is None or isinstance(stable_value, bool):
                self.stable[name] = stable_value
            item = payload.get("pending", {}).get(name, {})
            value = item.get("value")
            count = item.get("count", 0)
            if (value is None or isinstance(value, bool)) and isinstance(count, int):
                self.pending[name] = {"value": value, "count": max(0, count)}

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"stable": self.stable, "pending": self.pending}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def update(self, decision: ModelDecision) -> ModelDecision:
        output: Dict[str, Optional[bool]] = {}
        for name in FIELDS:
            observed = getattr(decision, name)
            if observed is None:
                self.pending[name] = {"value": None, "count": 0}
                output[name] = None
                continue
            if observed == self.stable[name]:
                self.pending[name] = {"value": None, "count": 0}
                output[name] = observed
                continue

            item = self.pending[name]
            count = item["count"] + 1 if item["value"] is observed else 1
            self.pending[name] = {"value": observed, "count": count}
            needed = 1 if name == "door_closed" and observed is False else self.confirmations
            if count >= needed:
                self.stable[name] = observed
                self.pending[name] = {"value": None, "count": 0}
                output[name] = observed
            else:
                output[name] = None
        self._save()
        return ModelDecision(
            door_closed=output["door_closed"],
            up_pressed=output["up_pressed"],
            down_pressed=output["down_pressed"],
            confidence=dict(decision.confidence),
            evidence=dict(decision.evidence),
        )
