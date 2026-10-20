from __future__ import annotations

import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from elevator_perception import ElevatorPerception, Settings
from few_shot import answer_for, bytes_to_data_url, load_examples, select_examples
from perception_schema import ModelDecision
from ros_stream_node import LatestFrame
from temporal_filter import TemporalStabilizer
from vlm_client import OpenAICompatibleVLM, VLMRequestError


class FewShotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.examples = load_examples(ROOT / "few-shots" / "manifest.json")

    def test_manifest_and_balanced_six_shot(self) -> None:
        self.assertEqual(len(self.examples), 32)
        selected = select_examples(self.examples, 6)
        self.assertEqual(
            {item.class_key for item in selected},
            {
                ("door", False),
                ("door", True),
                ("buttons", False, False),
                ("buttons", True, False),
                ("buttons", False, True),
                ("buttons", True, True),
            },
        )

    def test_button_only_does_not_select_door_examples(self) -> None:
        selected = select_examples(
            self.examples, 4, required_fields=("up_pressed", "down_pressed")
        )
        self.assertTrue(all(item.door_closed is None for item in selected))
        self.assertEqual(len({item.class_key for item in selected}), 4)

    def test_memory_image_data_url(self) -> None:
        self.assertEqual(bytes_to_data_url(b"abc"), "data:image/jpeg;base64,YWJj")

    def test_reference_answer_uses_unambiguous_probability(self) -> None:
        opened = next(item for item in self.examples if item.class_key == ("door", False))
        answer = answer_for(opened)
        self.assertIn('"door_closed":0.0', answer)
        self.assertNotIn('"confidence"', answer)


class SchemaTests(unittest.TestCase):
    def test_probability_true_false_becomes_high_decision_confidence(self) -> None:
        text = """{
        "door_closed":false,"up_pressed":null,"down_pressed":null,
        "probability_true":{"door_closed":0.0,"up_pressed":null,"down_pressed":null},
        "evidence":{"door_closed":"门打开状态","up_pressed":null,"down_pressed":null}
        }"""
        decision = ModelDecision.from_text(text).apply_threshold(0.72)
        self.assertFalse(decision.door_closed)
        self.assertEqual(decision.confidence["door_closed"], 1.0)
        self.assertEqual(decision.evidence["up_pressed"], "")
        self.assertEqual(decision.evidence["down_pressed"], "")

    def test_probability_true_inconsistent_value_is_rejected_by_threshold(self) -> None:
        text = """{
        "door_closed":false,"up_pressed":null,"down_pressed":null,
        "probability_true":{"door_closed":0.9,"up_pressed":null,"down_pressed":null},
        "evidence":{}
        }"""
        decision = ModelDecision.from_text(text).apply_threshold(0.72)
        self.assertIsNone(decision.door_closed)
        self.assertAlmostEqual(decision.confidence["door_closed"], 0.1)

    def test_parse_and_threshold(self) -> None:
        text = """```json
        {"door_closed":true,"up_pressed":false,"down_pressed":null,
         "confidence":{"door_closed":0.91,"up_pressed":0.5,"down_pressed":0.1},
         "evidence":{"door_closed":"门缝消失"}}
        ```"""
        decision = ModelDecision.from_text(text).apply_threshold(0.72)
        self.assertTrue(decision.door_closed)
        self.assertIsNone(decision.up_pressed)
        self.assertIsNone(decision.down_pressed)


class TemporalTests(unittest.TestCase):
    @staticmethod
    def decision(door=None, up=None, down=None) -> ModelDecision:
        return ModelDecision(
            door_closed=door,
            up_pressed=up,
            down_pressed=down,
            confidence={"door_closed": 0.9, "up_pressed": 0.9, "down_pressed": 0.9},
        )

    def test_safety_asymmetric_door_filter(self) -> None:
        stabilizer = TemporalStabilizer(confirmations=2)
        first_closed = stabilizer.update(self.decision(door=True))
        self.assertIsNone(first_closed.door_closed)
        second_closed = stabilizer.update(self.decision(door=True))
        self.assertTrue(second_closed.door_closed)
        opened = stabilizer.update(self.decision(door=False))
        self.assertFalse(opened.door_closed)

    def test_state_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            one = TemporalStabilizer(confirmations=2, state_path=state)
            one.update(self.decision(up=True))
            two = TemporalStabilizer(confirmations=2, state_path=state)
            result = two.update(self.decision(up=True))
            self.assertTrue(result.up_pressed)


class PipelineTests(unittest.TestCase):
    def test_door_only_zero_shot_uses_compact_contract(self) -> None:
        settings = Settings(
            api_url="http://unused",
            model="test-model",
            confidence_threshold=0.7,
            few_shot_count=0,
            door_only=True,
        )
        pipeline = ElevatorPerception(
            settings,
            ROOT / "few-shots" / "manifest.json",
            enable_temporal_filter=False,
        )

        class FakeClient:
            def complete(self, messages):
                self.messages = messages
                return (
                    '{"door_closed":false,'
                    '"probability_true":{"door_closed":0.02},'
                    '"evidence":{"door_closed":"入口敞开"}}'
                )

        fake = FakeClient()
        pipeline.client = fake
        result = pipeline.infer(
            sequence=1,
            door_image=ROOT / "few-shots" / "outopen.jpg",
        )
        self.assertFalse(result.door_closed)
        self.assertIsNone(result.up_pressed)
        self.assertIsNone(result.down_pressed)
        self.assertEqual(result.few_shot_count, 0)
        self.assertEqual(len(fake.messages), 2)  # system + current image
        self.assertNotIn("up_pressed", fake.messages[0]["content"])

    def test_door_only_prompt_reserves_false_for_fully_open_passage(self) -> None:
        from prompts import DOOR_ONLY_SYSTEM_PROMPT

        self.assertIn("入口完整敞开", DOOR_ONLY_SYSTEM_PROMPT)
        self.assertIn("正在开关", DOOR_ONLY_SYSTEM_PROMPT)
        self.assertIn("door_closed=null", DOOR_ONLY_SYSTEM_PROMPT)

    def test_door_only_rejects_button_input(self) -> None:
        pipeline = ElevatorPerception(
            Settings(
                api_url="http://unused",
                model="test-model",
                few_shot_count=0,
                door_only=True,
            ),
            ROOT / "few-shots" / "manifest.json",
            enable_temporal_filter=False,
        )
        with self.assertRaisesRegex(ValueError, "door_only"):
            pipeline.infer(
                sequence=1,
                button_image=ROOT / "few-shots" / "up_off&down_off.jpg",
            )

    def test_inference_contract_without_network(self) -> None:
        settings = Settings(
            api_url="http://unused",
            model="test-model",
            confidence_threshold=0.7,
            few_shot_count=6,
        )
        pipeline = ElevatorPerception(
            settings,
            ROOT / "few-shots" / "manifest.json",
            enable_temporal_filter=False,
        )

        class FakeClient:
            def complete(self, messages):
                self.messages = messages
                return (
                    '{"door_closed":false,"up_pressed":true,"down_pressed":false,'
                    '"probability_true":{"door_closed":0.1,"up_pressed":0.92,"down_pressed":0.12},'
                    '"evidence":{"door_closed":"门打开","up_pressed":"上键红灯",'
                    '"down_pressed":"下键未亮"}}'
                )

        fake = FakeClient()
        pipeline.client = fake
        result = pipeline.infer(
            sequence=7,
            door_image=ROOT / "few-shots" / "outopen.jpg",
            button_image=ROOT / "few-shots" / "up_on&down_off.jpg",
            image_stamp=123.0,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.sequence, 7)
        self.assertFalse(result.door_closed)
        self.assertTrue(result.up_pressed)
        self.assertFalse(result.down_pressed)
        self.assertEqual(result.few_shot_count, 6)
        self.assertEqual(len(fake.messages), 14)  # system + 6 pairs + current request

    def test_task_specific_shot_budget_and_mask(self) -> None:
        settings = Settings(
            api_url="http://unused",
            model="test-model",
            confidence_threshold=0.7,
            few_shot_count=6,
        )
        pipeline = ElevatorPerception(
            settings,
            ROOT / "few-shots" / "manifest.json",
            enable_temporal_filter=False,
        )

        class FakeClient:
            def complete(self, messages):
                self.messages = messages
                return (
                    '{"door_closed":true,"up_pressed":true,"down_pressed":false,'
                    '"probability_true":{"door_closed":0.99,"up_pressed":0.9,"down_pressed":0.1},'
                    '"evidence":{}}'
                )

        fake = FakeClient()
        pipeline.client = fake
        result = pipeline.infer_data_urls(
            sequence=1,
            current_images=[{"label": "button", "data_url": "data:image/jpeg;base64,YWJj"}],
            required_fields=("up_pressed", "down_pressed"),
            mode="button_verify",
        )
        self.assertIsNone(result.door_closed)
        self.assertEqual(result.few_shot_count, 4)
        self.assertEqual(len(fake.messages), 10)  # system + 4 pairs + current request


class StreamBufferTests(unittest.TestCase):
    def test_latest_frame_replaces_old_frame(self) -> None:
        slot = LatestFrame()
        self.assertIsNone(slot.snapshot())
        slot.update("old", 1.0)
        slot.update("new", 2.0)
        record = slot.snapshot()
        self.assertIsNotNone(record)
        self.assertEqual(record.message, "new")
        self.assertEqual(record.stamp, 2.0)
        self.assertEqual(record.revision, 2)


class VLMClientTests(unittest.TestCase):
    class Response:
        def __init__(self, payload) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def test_request_uses_generic_json_object_and_bearer_key(self) -> None:
        response = self.Response(
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        )
        client = OpenAICompatibleVLM("https://api.example/v1/chat/completions", "test", "key")
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(client.complete([]), "{}")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["response_format"]["type"], "json_object")
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("chat_template_kwargs", payload)
        self.assertEqual(request.headers["Authorization"], "Bearer key")

    def test_remote_api_requires_key(self) -> None:
        client = OpenAICompatibleVLM(
            "https://api.example/v1/chat/completions", "test"
        )
        with self.assertRaisesRegex(VLMRequestError, "VLM_API_KEY"):
            client.complete([])

    def test_empty_content_has_actionable_error(self) -> None:
        response = self.Response(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "thinking"},
                    }
                ]
            }
        )
        client = OpenAICompatibleVLM(
            "https://api.example/v1/chat/completions", "test", "key"
        )
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(VLMRequestError, "finish_reason=length"):
                client.complete([])


if __name__ == "__main__":
    unittest.main()
