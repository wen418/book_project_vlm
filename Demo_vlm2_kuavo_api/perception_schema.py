#!/usr/bin/env python3
"""Strict model-output and planner-payload schemas for elevator perception."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence


FIELDS = ("door_closed", "up_pressed", "down_pressed")


def _json_object(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("模型响应中没有 JSON 对象")
    # Decode the entire answer, including an unfinished evidence string. A
    # greedy brace match can hide that string and report a misleading position.
    value, end = json.JSONDecoder().raw_decode(cleaned, start)
    if cleaned[end:].strip():
        raise ValueError("模型 JSON 对象后包含多余内容")
    if not isinstance(value, dict):
        raise ValueError("模型响应 JSON 必须是对象")
    return value


def _nullable_bool(value: Any, name: str) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("%s 必须是 true、false 或 null" % name)


def _confidence(value: Any) -> float:
    number = float(value)
    return min(1.0, max(0.0, number))


def _probability(value: Any) -> Optional[float]:
    if value is None:
        return None
    return _confidence(value)


def _evidence(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()[:80]


@dataclass
class ModelDecision:
    door_closed: Optional[bool]
    up_pressed: Optional[bool]
    down_pressed: Optional[bool]
    confidence: Dict[str, float] = field(default_factory=dict)
    evidence: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_text(
        cls,
        text: str,
        required_fields: Sequence[str] = FIELDS,
    ) -> "ModelDecision":
        payload = _json_object(text)
        unknown = [name for name in required_fields if name not in FIELDS]
        if unknown:
            raise ValueError("未知字段：%s" % ", ".join(unknown))
        missing = [name for name in required_fields if name not in payload]
        if missing:
            raise ValueError("模型响应缺少字段：%s" % ", ".join(missing))
        probability_raw = payload.get("probability_true")
        confidence_raw = payload.get("confidence")
        evidence_raw = payload.get("evidence") or {}
        if isinstance(evidence_raw, str) and tuple(required_fields) == ("door_closed",):
            evidence_raw = {"door_closed": evidence_raw}
        if probability_raw is not None and not isinstance(probability_raw, dict):
            raise ValueError("probability_true 必须是对象")
        if confidence_raw is not None and not isinstance(confidence_raw, dict):
            raise ValueError("confidence 必须是对象")
        if not isinstance(evidence_raw, dict):
            raise ValueError("evidence 必须是对象")

        values = {
            name: _nullable_bool(payload.get(name), name)
            for name in FIELDS
        }
        if probability_raw is not None:
            confidence = {}
            for name in FIELDS:
                value = values[name]
                probability = _probability(probability_raw.get(name))
                if value is None or probability is None:
                    confidence[name] = 0.0
                elif value:
                    confidence[name] = probability
                else:
                    confidence[name] = 1.0 - probability
        else:
            # Backward compatibility for older recorded responses. In the legacy
            # contract confidence means certainty of the selected true/false value.
            legacy = confidence_raw or {}
            confidence = {
                name: _confidence(legacy.get(name, 0.0))
                for name in FIELDS
            }
        evidence = {
            name: _evidence(evidence_raw.get(name))
            for name in FIELDS
        }
        return cls(
            door_closed=values["door_closed"],
            up_pressed=values["up_pressed"],
            down_pressed=values["down_pressed"],
            confidence=confidence,
            evidence=evidence,
        )

    def apply_threshold(self, threshold: float) -> "ModelDecision":
        values = {}
        for name in FIELDS:
            value = getattr(self, name)
            values[name] = value if self.confidence.get(name, 0.0) >= threshold else None
        return ModelDecision(
            confidence=dict(self.confidence),
            evidence=dict(self.evidence),
            **values
        )


@dataclass
class PlannerPayload:
    sequence: int
    status: str
    door_closed: Optional[bool]
    up_pressed: Optional[bool]
    down_pressed: Optional[bool]
    confidence: Dict[str, float]
    evidence: Dict[str, str]
    latency_ms: Optional[float]
    model: str
    few_shot_count: int
    image_stamp: Optional[float] = None
    mode: str = ""
    error: str = ""
    schema_version: int = 2
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def error_payload(
    sequence: int,
    model: str,
    message: str,
    mode: str = "",
    image_stamp: Optional[float] = None,
) -> PlannerPayload:
    return PlannerPayload(
        sequence=sequence,
        status="error",
        door_closed=None,
        up_pressed=None,
        down_pressed=None,
        confidence={name: 0.0 for name in FIELDS},
        evidence={name: "" for name in FIELDS},
        latency_ms=None,
        model=model,
        few_shot_count=0,
        mode=mode,
        image_stamp=image_stamp,
        error=message[:300],
    )
