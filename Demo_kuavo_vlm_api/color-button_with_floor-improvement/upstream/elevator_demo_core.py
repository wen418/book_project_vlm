"""Upper-perception parsing and compatibility exports from the standalone controller.

The lower computer only needs elevator_button_floor_main.py. The upper computer
keeps its existing package; these exports keep one implementation of the shared
contracts in the locally distributed package.
"""
from __future__ import annotations
import json
from elevator_button_floor_main import (
    HEAD_COMMAND_LIMITS, EvidenceGate, execute_demo, finite, load_config, validate_config,
)


def parse_observation(text, mode):
    """Never coerce strings, probabilities or target instructions into facts."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines[-1].strip() != "```":
            raise ValueError("JSON 输出不完整")
        stripped = "\n".join(lines[1:-1])
    raw = json.loads(stripped)
    field = {"button_verify": "button_lit", "floor_monitor": "floor"}[mode]
    if not isinstance(raw, dict) or set(raw) != {field, "confidence", "evidence"}:
        raise ValueError("模型字段不符合本次任务")
    value = raw[field]
    if mode == "button_verify":
        if value is not None and type(value) is not bool:
            raise ValueError("button_lit 必须为布尔值或 null")
    elif value is not None and (type(value) is not int or not -10 <= value <= 150):
        raise ValueError("floor 必须为整数或 null")
    confidence = finite(raw["confidence"], "confidence", 0, 1)
    if not isinstance(raw["evidence"], str) or (value is not None and not raw["evidence"].strip()):
        raise ValueError("已知状态必须提供可见证据")
    return {field: value, "confidence": {field: confidence},
            "evidence": {field: raw["evidence"].strip()[:120]}}
