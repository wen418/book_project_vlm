"""Launcher boundaries: no live ROS, SSH, model calls or robot commands."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "deploy/elevator_demo_launcher.py"
spec = importlib.util.spec_from_file_location("elevator_launcher_under_test", SOURCE)
launcher = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"fcntl": SimpleNamespace()} if os.name == "nt" else {}):
    spec.loader.exec_module(launcher)


class ReadinessTests(unittest.TestCase):
    def test_old_rate_node_cannot_be_silently_reused(self):
        for frequency in (None, .5, 2.0):
            values = self.values()
            values["ready"][0]["inference_hz"] = frequency
            self.assertIn("1 Hz", launcher.readiness_reason(values, 100))
    def values(self):
        return {
            "door": ({"schema_version": 2, "mode": "idle", "status": "idle"}, 100),
            "extra": ({"mode": "idle", "status": "idle", "image_received": True, "image_age_sec": 0.1}, 100),
            "ready": ({"schema_version": 1, "status": "ready", "inference_hz": 1.0, "capabilities": ["button_verify", "floor_monitor"]}, 50),
            "camera": (True, 100),
        }

    def test_requires_all_fresh_feedback_but_latched_ready_can_be_old(self):
        values = self.values()
        self.assertIsNone(launcher.readiness_reason(values, 100))
        for name in values:
            incomplete = dict(values)
            incomplete.pop(name)
            self.assertIsNotNone(launcher.readiness_reason(incomplete, 100))
        self.assertIsNotNone(launcher.readiness_reason(values, 106))

    def test_clients_errors_wrong_schemas_and_missing_capabilities_block_start(self):
        for name, fields in [("ready", {"status": "error", "reason": "missing credentials"}),
                             ("ready", {"schema_version": 2}), ("ready", {"capabilities": ["button_verify"]}),
                             ("door", {"schema_version": 1}), ("door", {"mode": "door_monitor"}),
                             ("extra", {"status": "error"})]:
            values = copy.deepcopy(self.values())
            values[name][0].update(fields)
            with self.subTest(name=name, fields=fields):
                self.assertIsNotNone(launcher.readiness_reason(values, 100))

    def test_stale_unknown_or_invalid_camera_age_never_starts_robot(self):
        for age in (None, True, float("nan"), float("inf"), 10):
            values = self.values()
            values["extra"][0]["image_age_sec"] = age
            self.assertIsNotNone(launcher.readiness_reason(values, 100))
        values = self.values()
        values["camera"] = False, 100
        self.assertIsNotNone(launcher.readiness_reason(values, 100))


class SequenceTests(unittest.TestCase):
    def make_launcher(self, root, **overrides):
        args = dict(check=False, check_startup=False, perception_only=False, target_floor=None,
                    lower_host="kuavo_master", ready_timeout=1)
        args.update(overrides)
        with patch.dict(os.environ, {"ELEVATOR_LAUNCH_DIR": root, "ROS_MASTER_URI": "http://kuavo_master:11311"}):
            result = launcher.Launcher(SimpleNamespace(**args))
        result.preflight = Mock()
        result.connect_ros = Mock()
        result.start_perception = Mock()
        result.remote = Mock(return_value="interfaces ready\n")
        return result

    def test_missing_hardware_state_blocks_before_head_check_or_actions(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root)
            del instance.preflight
            config = dict(target_floor=20, head=dict(floor=dict(yaw=-20, pitch=-20)), calibrated=True)
            instance.remote.side_effect = [json.dumps(dict(config=config, active_pids=[])), "dependencies ready\n"]
            graph = SimpleNamespace(Master=lambda name: SimpleNamespace(getParam=lambda key: None),
                                    MasterError=RuntimeError)
            with patch.dict(os.environ, {"ROS_MASTER_URI": "http://kuavo_master:11311"}), \
                    patch.dict(sys.modules, {"rosgraph": graph}), patch.object(Path, "is_file", return_value=True), \
                    self.assertRaisesRegex(RuntimeError, "主控/电机尚未就绪"):
                instance.preflight()
            self.assertEqual(instance.remote.call_count, 2)

    def test_existing_lower_test_prevents_startup(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root)
            del instance.preflight
            instance.remote.return_value = json.dumps(dict(config={}, active_pids=[123]))
            with patch.object(Path, "is_file", return_value=True), \
                    self.assertRaisesRegex(RuntimeError, "已有测试程序运行"):
                instance.preflight()
            self.assertEqual(instance.remote.call_count, 1)

    def test_check_mode_never_starts_any_perception_or_remote_demo(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root, check=True)
            with patch.object(launcher, "Child") as child:
                self.assertEqual(instance.run(), 0)
                instance.start_perception.assert_not_called()
                instance.remote.assert_not_called()
                child.assert_not_called()

    def test_readiness_failure_never_sends_lower_motion_command(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root)
            instance.start_perception.side_effect = TimeoutError("no camera")
            with patch.object(launcher, "Child") as child, self.assertRaises(TimeoutError):
                instance.run()
            instance.remote.assert_not_called()
            child.assert_not_called()

    def test_interface_check_failure_never_sends_lower_motion_command(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root)
            instance.remote.side_effect = RuntimeError("navigation missing")
            with patch.dict(os.environ, {"ROS_MASTER_URI": "http://kuavo_master:11311"}), \
                    patch.object(launcher, "Child") as child, self.assertRaises(RuntimeError):
                instance.run()
            child.assert_not_called()

    def test_startup_check_stops_after_perception_without_robot_demo(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root, check_startup=True)
            with patch.object(launcher, "Child") as child:
                self.assertEqual(instance.run(), 0)
                instance.remote.assert_not_called()
                child.assert_not_called()

    def test_full_run_checks_services_then_passes_target_and_original_flags(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root, target_floor=19)
            fake = SimpleNamespace(process=SimpleNamespace(poll=lambda: 0, returncode=0))
            with patch.dict(os.environ, {"ROS_MASTER_URI": "http://kuavo_master:11311"}), \
                    patch.object(launcher, "Child", return_value=fake) as child:
                self.assertEqual(instance.run(), 0)
            self.assertIn("--check-services", instance.remote.call_args.args[0])
            command = child.call_args.args[1]
            self.assertIn("-tt", command)
            for flag in ("--run", "--preflight-confirmed", "--perception-confirmed", "--estop-ready", "--target-floor 19"):
                self.assertIn(flag, command[-1])
            self.assertIn("flock", command[-1])

    def test_missing_target_override_preserves_lower_default(self):
        with tempfile.TemporaryDirectory() as root:
            instance = self.make_launcher(root)
            fake = SimpleNamespace(process=SimpleNamespace(poll=lambda: 0, returncode=0))
            with patch.dict(os.environ, {"ROS_MASTER_URI": "http://kuavo_master:11311"}), \
                    patch.object(launcher, "Child", return_value=fake) as child:
                self.assertEqual(instance.run(), 0)
            self.assertNotIn("--target-floor", child.call_args.args[1][-1])


if __name__ == "__main__":
    unittest.main()
