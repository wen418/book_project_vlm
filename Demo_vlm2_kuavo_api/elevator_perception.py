#!/usr/bin/env python3
"""End-to-end elevator perception pipeline usable from a robot process."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from few_shot import build_messages, image_to_data_url, load_examples, select_examples
from perception_schema import ModelDecision, PlannerPayload
from prompts import (
    DOOR_ONLY_QUESTION,
    DOOR_ONLY_SYSTEM_PROMPT,
    QUESTION,
    SYSTEM_PROMPT,
)
from temporal_filter import TemporalStabilizer
from vlm_client import OpenAICompatibleVLM, VLMOutputError


@dataclass(frozen=True)
class Settings:
    api_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 120.0
    max_tokens: int = 300
    temperature: float = 0.0
    confidence_threshold: float = 0.72
    few_shot_count: int = 6
    temporal_confirmations: int = 2
    use_json_object: bool = True
    max_retries: int = 1
    door_only: bool = False
    enable_thinking: Optional[bool] = None

    def __post_init__(self) -> None:
        if not self.api_url.startswith(("http://", "https://")):
            raise ValueError("api_url 必须以 http:// 或 https:// 开头")
        if not self.model.strip():
            raise ValueError("model 不能为空")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold 必须在 0 到 1 之间")
        if self.few_shot_count < 0:
            raise ValueError("few_shot_count 不能小于 0")
        if self.temporal_confirmations < 1:
            raise ValueError("temporal_confirmations 必须 >= 1")
        if self.max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking 必须是布尔值或 null")

    @classmethod
    def from_json(cls, path: Path) -> "Settings":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        env_overrides = {
            "api_key": os.environ.get("VLM_API_KEY"),
            "api_url": os.environ.get("VLM_API_URL"),
            "model": os.environ.get("VLM_MODEL"),
        }
        for name, value in env_overrides.items():
            if value:
                payload[name] = value
        return cls(**payload)


class ElevatorPerception:
    def __init__(
        self,
        settings: Settings,
        manifest_path: Path,
        state_path: Optional[Path] = None,
        enable_temporal_filter: bool = True,
    ) -> None:
        self.settings = settings
        self.examples = load_examples(manifest_path)
        self.client = OpenAICompatibleVLM(
            api_url=settings.api_url,
            model=settings.model,
            api_key=settings.api_key,
            timeout_seconds=settings.timeout_seconds,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            use_json_object=settings.use_json_object,
            max_retries=settings.max_retries,
            enable_thinking=settings.enable_thinking,
        )
        self.stabilizer = (
            TemporalStabilizer(settings.temporal_confirmations, state_path)
            if enable_temporal_filter
            else None
        )

    @staticmethod
    def _validate_image(path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError("输入图片不存在：%s" % resolved)
        if resolved.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise ValueError("输入图片只支持 jpg/jpeg/png：%s" % resolved)
        return resolved

    def _inputs(
        self,
        image: Optional[Path],
        door_image: Optional[Path],
        button_image: Optional[Path],
    ) -> Tuple[List[dict], Sequence[str]]:
        current_images: List[dict] = []
        fields: List[str] = []
        if self.settings.door_only and button_image:
            raise ValueError("当前配置为 door_only，不能使用 --button-image")
        if image:
            path = self._validate_image(image)
            if self.settings.door_only:
                current_images.append({"label": "当前电梯门图：", "data_url": image_to_data_url(path)})
                fields.append("door_closed")
            else:
                current_images.append({"label": "当前全景图（检查门和按钮）：", "data_url": image_to_data_url(path)})
                fields.extend(("door_closed", "up_pressed", "down_pressed"))
        if door_image:
            path = self._validate_image(door_image)
            current_images.append({"label": "当前电梯门图：", "data_url": image_to_data_url(path)})
            fields.append("door_closed")
        if button_image:
            path = self._validate_image(button_image)
            current_images.append({"label": "当前上/下呼梯按钮图：", "data_url": image_to_data_url(path)})
            fields.extend(("up_pressed", "down_pressed"))
        if not current_images:
            raise ValueError("至少提供 --image，或提供 --door-image/--button-image")
        return current_images, tuple(dict.fromkeys(fields))

    def selected_example_names(self, required_fields: Sequence[str]) -> List[str]:
        maximum = self._shot_budget(required_fields)
        return [
            item.image_path.name
            for item in select_examples(
                self.examples, maximum, required_fields=required_fields
            )
        ]

    def _shot_budget(self, required_fields: Sequence[str]) -> int:
        fields = set(required_fields)
        if fields == {"door_closed"}:
            return min(self.settings.few_shot_count, 2)
        if fields and fields.issubset({"up_pressed", "down_pressed"}):
            return min(self.settings.few_shot_count, 4)
        return min(self.settings.few_shot_count, 6)

    def infer_data_urls(
        self,
        sequence: int,
        current_images: Sequence[dict],
        required_fields: Sequence[str],
        image_stamp: Optional[float] = None,
        mode: str = "",
    ) -> PlannerPayload:
        if not current_images:
            raise ValueError("当前图像不能为空")
        if self.settings.door_only and set(required_fields) != {"door_closed"}:
            raise ValueError("当前配置为 door_only，只允许判断 door_closed")
        maximum = self._shot_budget(required_fields)
        selected = select_examples(
            self.examples,
            maximum,
            required_fields=required_fields,
        )
        is_compact_door_request = (
            set(required_fields) == {"door_closed"} and not selected
        )
        system_prompt = (
            DOOR_ONLY_SYSTEM_PROMPT if is_compact_door_request else SYSTEM_PROMPT
        )
        question = DOOR_ONLY_QUESTION if is_compact_door_request else QUESTION
        messages = build_messages(system_prompt, selected, current_images, question)
        start = time.perf_counter()
        try:
            response = self.client.complete(messages)
            decision = ModelDecision.from_text(response, required_fields=required_fields)
        except (ValueError, VLMOutputError) as first_error:
            # Retry the same image once with a concise format reminder and room
            # to finish the JSON. Never repair or use an incomplete answer.
            retry_messages = [
                *messages[:-1],
                {
                    **messages[-1],
                    "content": [
                        *messages[-1]["content"],
                        {
                            "type": "text",
                            "text": (
                                "上次输出格式不完整。本次只输出规定字段的一个完整 JSON 对象，"
                                "禁止新增字段、翻译、分析或解释；evidence 中每项证据不超过16个汉字。"
                                "仅根据图像判断，无法确认时保持 null，不得猜测。"
                            ),
                        },
                    ],
                },
            ]
            try:
                response = self.client.complete(
                    retry_messages, max_tokens=max(self.settings.max_tokens, 384)
                )
                decision = ModelDecision.from_text(response, required_fields=required_fields)
            except (ValueError, VLMOutputError) as second_error:
                raise ValueError(
                    "模型连续两次返回无效结果：首次 %s；再次 %s"
                    % (first_error, second_error)
                ) from second_error
        latency_ms = (time.perf_counter() - start) * 1000.0
        decision = decision.apply_threshold(self.settings.confidence_threshold)
        required = set(required_fields)
        decision = ModelDecision(
            door_closed=decision.door_closed if "door_closed" in required else None,
            up_pressed=decision.up_pressed if "up_pressed" in required else None,
            down_pressed=decision.down_pressed if "down_pressed" in required else None,
            confidence=dict(decision.confidence),
            evidence=dict(decision.evidence),
        )
        if self.stabilizer:
            decision = self.stabilizer.update(decision)
        expected_values = [getattr(decision, name) for name in required_fields]
        status = "ok" if expected_values and all(value is not None for value in expected_values) else "uncertain"
        return PlannerPayload(
            sequence=sequence,
            status=status,
            door_closed=decision.door_closed,
            up_pressed=decision.up_pressed,
            down_pressed=decision.down_pressed,
            confidence=decision.confidence,
            evidence=decision.evidence,
            latency_ms=round(latency_ms, 2),
            model=self.settings.model,
            few_shot_count=len(selected),
            image_stamp=image_stamp,
            mode=mode,
        )

    def infer(
        self,
        sequence: int,
        image: Optional[Path] = None,
        door_image: Optional[Path] = None,
        button_image: Optional[Path] = None,
        image_stamp: Optional[float] = None,
    ) -> PlannerPayload:
        current_images, required_fields = self._inputs(image, door_image, button_image)
        return self.infer_data_urls(
            sequence=sequence,
            current_images=current_images,
            required_fields=required_fields,
            image_stamp=image_stamp,
            mode="single_shot",
        )
