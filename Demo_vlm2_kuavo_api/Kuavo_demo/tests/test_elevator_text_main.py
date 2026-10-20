import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("elevator_text_main", ROOT / "elevator_text_main.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def payload(
    sequence,
    stamp,
    door_closed,
    status="ok",
    mode="door_monitor",
    confidence=0.95,
    schema_version=2,
):
    return json.dumps(
        {
            "sequence": sequence,
            "image_stamp": stamp,
            "door_closed": door_closed,
            "status": status,
            "mode": mode,
            "confidence": {"door_closed": confidence},
            "schema_version": schema_version,
        }
    )


class BuildDemoTests(unittest.TestCase):
    def load_config(self):
        return MODULE.load_config(ROOT / "params" / "elevator_demo.yaml")

    def test_targets_enter_then_exit(self):
        start, targets, perception, settle = MODULE.build_demo(self.load_config())
        self.assertEqual([item["name"] for item in targets], ["EnterInsideAndFaceDoor", "ExitToOutsideOrigin"])
        self.assertEqual((targets[1]["x"], targets[1]["y"]), (start["x"], start["y"]))
        self.assertEqual(perception["required_mode"], "door_monitor")
        self.assertEqual(settle, 2.0)

    def test_uncalibrated_is_rejected_for_live(self):
        config = self.load_config()
        config["calibrated"] = False
        args = type("Args", (), {"preflight_confirmed": True, "perception_confirmed": True, "estop_ready": True})()
        with self.assertRaises(ValueError):
            MODULE.validate_live(config, args)


class DoorGateTests(unittest.TestCase):
    def gate(self):
        gate = MODULE.DoorGate(
            "door_monitor",
            max_result_age=10,
            min_confidence=0.72,
            confirmations=2,
            min_confirmation_span=2,
        )
        gate.arm(False, 100.0)
        return gate

    def test_two_fresh_open_results_release_gate(self):
        gate = self.gate()
        self.assertFalse(gate.feed(payload(1, 101, False), now_ros=101, now_monotonic=10))
        self.assertTrue(gate.feed(payload(2, 103, False), now_ros=103, now_monotonic=12))

    def test_closed_result_keeps_gate_closed(self):
        gate = self.gate()
        self.assertFalse(gate.feed(payload(1, 101, True), now_ros=101, now_monotonic=10))

    def test_uncertain_resets_confirmation_count(self):
        gate = self.gate()
        gate.feed(payload(1, 101, False), now_ros=101, now_monotonic=10)
        gate.feed(payload(2, 102, None, status="uncertain"), now_ros=102, now_monotonic=11)
        self.assertFalse(gate.feed(payload(3, 104, False), now_ros=104, now_monotonic=13))

    def test_old_image_is_rejected(self):
        gate = self.gate()
        self.assertFalse(gate.feed(payload(1, 99, False), now_ros=101, now_monotonic=10))

    def test_stale_image_is_rejected(self):
        gate = self.gate()
        self.assertFalse(gate.feed(payload(1, 101, False), now_ros=120, now_monotonic=10))

    def test_wrong_mode_is_rejected(self):
        gate = self.gate()
        self.assertFalse(gate.feed(payload(1, 101, False, mode="idle"), now_ros=101, now_monotonic=10))

    def test_low_confidence_is_rejected(self):
        gate = self.gate()
        self.assertFalse(
            gate.feed(payload(1, 101, False, confidence=0.71), now_ros=101, now_monotonic=10)
        )

    def test_wrong_schema_is_rejected(self):
        gate = self.gate()
        self.assertFalse(
            gate.feed(payload(1, 101, False, schema_version=1), now_ros=101, now_monotonic=10)
        )

    def test_duplicate_sequence_does_not_add_confirmation(self):
        gate = self.gate()
        gate.feed(payload(1, 101, False), now_ros=101, now_monotonic=10)
        self.assertFalse(gate.feed(payload(1, 103, False), now_ros=103, now_monotonic=12))

    def test_invalid_json_fails_closed(self):
        gate = self.gate()
        self.assertFalse(gate.feed("not-json", now_ros=101, now_monotonic=10))


class StateMachineTests(unittest.TestCase):
    def test_full_sequence_and_idle_cleanup(self):
        events = []
        MODULE.execute_demo(
            lambda state, stage: events.append(("door", stage, state)),
            lambda target: events.append(("nav", target)) or True,
            lambda seconds: events.append(("settle", seconds)),
            2.0,
            lambda mode: events.append(("mode", mode)),
        )
        self.assertEqual(
            events,
            [
                ("mode", "door_monitor"),
                ("door", "outside_open", False),
                ("nav", "inside"),
                ("settle", 2.0),
                ("door", "inside_closed", True),
                ("door", "inside_open", False),
                ("nav", "outside"),
                ("settle", 2.0),
                ("mode", "idle"),
            ],
        )

    def test_failure_still_switches_perception_to_idle(self):
        modes = []
        with self.assertRaises(RuntimeError):
            MODULE.execute_demo(lambda *_: None, lambda _: False, lambda _: None, 2, modes.append)
        self.assertEqual(modes, ["door_monitor", "idle"])


if __name__ == "__main__":
    unittest.main()
