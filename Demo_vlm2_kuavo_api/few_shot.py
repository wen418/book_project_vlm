#!/usr/bin/env python3
"""Load labelled reference images and build multimodal in-context messages."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FewShotExample:
    image_path: Path
    door_closed: Optional[bool]
    up_pressed: Optional[bool]
    down_pressed: Optional[bool]
    priority: int = 0
    enabled: bool = True

    @property
    def labelled_fields(self) -> Tuple[str, ...]:
        return tuple(
            name
            for name in ("door_closed", "up_pressed", "down_pressed")
            if getattr(self, name) is not None
        )

    @property
    def class_key(self) -> Tuple[Any, ...]:
        """Return the class used by the balanced sampler."""
        if self.door_closed is not None and self.up_pressed is None and self.down_pressed is None:
            return ("door", self.door_closed)
        if self.door_closed is None and self.up_pressed is not None and self.down_pressed is not None:
            return ("buttons", self.up_pressed, self.down_pressed)
        return ("joint", self.door_closed, self.up_pressed, self.down_pressed)


def _bool_or_none(value: Any, name: str) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("few-shot %s 必须是 true、false 或 null" % name)


def load_examples(manifest_path: Path) -> List[FewShotExample]:
    manifest_path = manifest_path.resolve()
    root = manifest_path.parent
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    examples = []
    seen_paths = set()
    for item in payload.get("examples", []):
        image_path = (root / str(item["image"])).resolve()
        try:
            image_path.relative_to(root)
        except ValueError:
            raise ValueError("few-shot 图片不能位于样例目录之外：%s" % image_path)
        if not image_path.is_file():
            raise FileNotFoundError("few-shot 图片不存在：%s" % image_path)
        if image_path in seen_paths:
            raise ValueError("few-shot 图片重复：%s" % image_path.name)
        seen_paths.add(image_path)
        examples.append(
            FewShotExample(
                image_path=image_path,
                door_closed=_bool_or_none(item.get("door_closed"), "door_closed"),
                up_pressed=_bool_or_none(item.get("up_pressed"), "up_pressed"),
                down_pressed=_bool_or_none(item.get("down_pressed"), "down_pressed"),
                priority=int(item.get("priority", 0)),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return examples


def _matches_fields(example: FewShotExample, required_fields: Iterable[str]) -> bool:
    wanted = set(required_fields)
    return bool(wanted.intersection(example.labelled_fields))


def select_examples(
    examples: Sequence[FewShotExample],
    maximum: int,
    required_fields: Sequence[str] = ("door_closed", "up_pressed", "down_pressed"),
) -> List[FewShotExample]:
    """Select class-balanced examples instead of adjacent near-duplicate frames.

    With ``maximum=6`` and the supplied manifest this yields one door-open,
    one door-closed and one example for each of the four button combinations.
    """
    if maximum <= 0:
        return []
    candidates = sorted(
        [
            item
            for item in examples
            if item.enabled and _matches_fields(item, required_fields)
        ],
        key=lambda item: (-item.priority, item.image_path.name),
    )
    preferred_keys = [
        ("door", False),
        ("door", True),
        ("buttons", False, False),
        ("buttons", True, False),
        ("buttons", False, True),
        ("buttons", True, True),
    ]
    selected: List[FewShotExample] = []
    for key in preferred_keys:
        if len(selected) >= maximum:
            break
        match = next((item for item in candidates if item.class_key == key), None)
        if match is not None:
            selected.append(match)
    for item in candidates:
        if len(selected) >= maximum:
            break
        if item not in selected:
            selected.append(item)
    return selected


def image_to_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    return bytes_to_data_url(path.read_bytes(), mime)


def bytes_to_data_url(data: bytes, mime: str = "image/jpeg") -> str:
    if mime not in ("image/jpeg", "image/png"):
        raise ValueError("不支持的图片 MIME 类型：%s" % mime)
    return "data:%s;base64,%s" % (
        mime,
        base64.b64encode(data).decode("ascii"),
    )


def answer_for(example: FewShotExample) -> str:
    def probability_true(value: Optional[bool]) -> Optional[float]:
        if value is None:
            return None
        return 1.0 if value else 0.0

    payload = {
        "door_closed": example.door_closed,
        "up_pressed": example.up_pressed,
        "down_pressed": example.down_pressed,
        "probability_true": {
            "door_closed": probability_true(example.door_closed),
            "up_pressed": probability_true(example.up_pressed),
            "down_pressed": probability_true(example.down_pressed),
        },
        "evidence": {
            "door_closed": "人工标注参考样例",
            "up_pressed": "样例未标注" if example.up_pressed is None else "人工标注参考样例",
            "down_pressed": "样例未标注" if example.down_pressed is None else "人工标注参考样例",
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_messages(
    system_prompt: str,
    examples: Sequence[FewShotExample],
    current_images: Sequence[Dict[str, str]],
    question: str,
) -> List[Dict[str, Any]]:
    messages = [{"role": "system", "content": system_prompt}]
    for example in examples:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是一个人工标注的参考样例，请学习判定口径。"},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(example.image_path)}},
                ],
            }
        )
        messages.append({"role": "assistant", "content": answer_for(example)})
    content = []
    for image in current_images:
        content.append({"type": "text", "text": image["label"]})
        content.append({"type": "image_url", "image_url": {"url": image["data_url"]}})
    content.append({"type": "text", "text": question})
    messages.append({"role": "user", "content": content})
    return messages
