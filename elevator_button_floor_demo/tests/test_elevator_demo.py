"""Behavioral regression checks; no ROS, hardware or API calls."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import elevator_demo_core as core
import elevator_button_floor_main as controller
import elevator_extra_perception as perception


def payload(seq=1, stamp=101, value=20, **overrides):
    result = {"schema_version": 1, "mode": "floor_monitor", "request_id": "current",
              "sequence": seq, "image_stamp": stamp, "status": "ok", "floor": value,
              "confidence": {"floor": 0.95}, "evidence": {"floor": "屏幕显示20"}}
    result.update(overrides)
    return json.dumps(result)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = core.load_config(ROOT / "params/elevator_button_floor_demo.json")

    def test_candidate_coordinates_are_metres_and_live_is_disabled(self):
        self.assertEqual(self.config["points"]["button"]["x"], 0.110)
        self.assertEqual(self.config["points"]["button"]["y"], 0.976)
        self.assertFalse(self.config["calibrated"])
        args = type("Args", (), dict(preflight_confirmed=True, perception_confirmed=True, estop_ready=True))()
        with self.assertRaises(ValueError):
            controller.validate_live(self.config, args)

    def test_original_confirmation_flags_remain_required(self):
        self.config["calibrated"] = True
        for missing in ("preflight_confirmed", "perception_confirmed", "estop_ready"):
            args = type("Args", (), dict(preflight_confirmed=True, perception_confirmed=True, estop_ready=True))()
            setattr(args, missing, False)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                controller.validate_live(self.config, args)

    def test_config_accepts_other_integer_floors_and_rejects_invalid_values(self):
        for floor in (-10, -1, 0, 19, 21, 150):
            self.config["target_floor"] = floor
            controller.validate_config(self.config)
        for floor in (True, 19.5, "19", None, -11, 151):
            self.config["target_floor"] = floor
            with self.subTest(floor=floor), self.assertRaises(ValueError):
                controller.validate_config(self.config)

    def test_head_limits_respect_document_and_controller_intersection(self):
        cases = [(60, -10, True), (-20, -10, True), (-88, -20, True),
                 (88, 25, True), (89, 0, False), (0, 26, False), (0, -21, False)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for yaw, pitch, allowed in cases:
                config = copy.deepcopy(self.config)
                config["head"]["floor"] = dict(yaw=yaw, pitch=pitch)
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(yaw=yaw, pitch=pitch):
                    if allowed:
                        core.load_config(path)
                    else:
                        with self.assertRaises(ValueError):
                            core.load_config(path)


@unittest.skipUnless(shutil.which("git"), "Git required")
class GitVersionTests(unittest.TestCase):
    def repository(self, directory):
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        target = root / "entry.py"
        target.write_text("original\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "entry.py"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
        return target

    def test_committed_file_uses_git_without_creating_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self.repository(directory)
            self.assertIsNone(controller.backup_before_update(target, ".backup_test"))
            self.assertFalse(target.with_name("entry.py.backup_test").exists())
            original = subprocess.check_output(["git", "-C", directory, "show", "HEAD:entry.py"])
            self.assertEqual(original, b"original\n")

    def test_unsaved_work_or_index_prevents_overwrite(self):
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as directory:
                target = self.repository(directory)
                target.write_text("unsaved\n", encoding="utf-8")
                if staged:
                    subprocess.run(["git", "-C", directory, "add", "entry.py"], check=True)
                with self.assertRaisesRegex(RuntimeError, "Git"):
                    controller.backup_before_update(target, ".backup_test")
                self.assertEqual(target.read_text(encoding="utf-8"), "unsaved\n")
                self.assertFalse(target.with_name("entry.py.backup_test").exists())

    def test_untracked_file_keeps_fallback_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.repository(directory)
            target = Path(directory) / "untracked.py"
            target.write_text("not in Git\n", encoding="utf-8")
            backup = controller.backup_before_update(target, ".backup_test")
            self.assertEqual(backup.read_bytes(), target.read_bytes())


class SingleFileDeploymentTests(unittest.TestCase):
    def run_isolated(self, script, arguments):
        return subprocess.run([sys.executable, "-I", "-X", "utf8", str(script), *arguments],
                              cwd=str(script.parent), capture_output=True, encoding="utf-8", timeout=15)

    def test_controller_runs_with_no_shared_module_or_config(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "elevator_button_floor_main.py"
            shutil.copyfile(ROOT / script.name, script)
            self.assertEqual(list(script.parent.iterdir()), [script])
            for arguments in ([], ["--mock"]):
                result = self.run_isolated(script, arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("DEFAULT_CONFIG", result.stdout)
            # Full execution must still reject uncalibrated embedded defaults before importing ROS.
            result = self.run_isolated(script, ["--run", "--preflight-confirmed",
                                               "--perception-confirmed", "--estop-ready"])
            self.assertEqual(result.returncode, 1)
            self.assertIn("calibrated", result.stderr)
            self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_target_floor_override_reaches_preview_and_mock_without_ros(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "elevator_button_floor_main.py"
            shutil.copyfile(ROOT / script.name, script)
            for mode in ([], ["--mock"]):
                result = self.run_isolated(script, ["--target-floor", "19", *mode])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("19", result.stdout)
                self.assertNotIn("确认 20 楼", result.stdout)
                if mode:
                    self.assertIn("floor_19", result.stdout)
                else:
                    self.assertIn("目标楼层：19", result.stdout)
            result = self.run_isolated(script, ["--target-floor", "151"])
            self.assertEqual(result.returncode, 1)
            self.assertIn("target_floor", result.stderr)

    def test_target_floor_override_takes_priority_over_external_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(controller.DEFAULT_CONFIG)
            config["target_floor"] = 18
            path = Path(directory) / "site.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            result = self.run_isolated(ROOT / "elevator_button_floor_main.py",
                                       ["--config", str(path), "--target-floor", "19"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("目标楼层：19", result.stdout)

    def test_external_config_is_optional_and_only_used_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "elevator_button_floor_main.py"
            shutil.copyfile(ROOT / script.name, script)
            external = Path(directory) / "custom.json"
            config = copy.deepcopy(controller.DEFAULT_CONFIG)
            config["points"]["button"]["x"] = 0.225
            external.write_text(json.dumps(config), encoding="utf-8")
            result = self.run_isolated(script, ["--config", str(external)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0.225", result.stdout)
            result = self.run_isolated(script, [])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0.11", result.stdout)
            self.assertNotIn("0.225", result.stdout)

    def test_update_preserves_site_config_and_backs_up_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "elevator_button_floor_main.updated.py"
            target = Path(directory) / "elevator_button_floor_main.py"
            text = (ROOT / target.name).read_text(encoding="utf-8")
            candidate.write_text(text, encoding="utf-8")
            previous = text.replace("'calibrated': False", "'calibrated': True", 1)
            target.write_text(previous, encoding="utf-8")
            result = self.run_isolated(candidate, ["--install-to", str(target)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("'calibrated': True", target.read_text(encoding="utf-8"))
            backups = list(Path(directory).glob("*.backup_*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), previous)

    def test_runtime_finds_existing_generated_messages_without_instantiating_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "elevator_button_floor_main.py"
            shutil.copyfile(ROOT / script.name, script)
            book = root / "workspace/src/sdk/sdk/strategy/book"
            book.mkdir(parents=True)
            generated = root / "workspace/devel/lib/python3/dist-packages/leju_mobile_base_msgs"
            (generated / "srv").mkdir(parents=True)
            (generated / "__init__.py").write_text("", encoding="utf-8")
            (generated / "srv/__init__.py").write_text("class MoveToTarget: pass\n", encoding="utf-8")
            (book / "api.py").write_text(
                "from leju_mobile_base_msgs.srv import MoveToTarget\n"
                "class NeverInstantiate:\n def __init__(self): raise AssertionError('unexpected action')\n"
                "NodeNavigationToTarget=NodeWaitForArrival=HeadAPI=NodeHeadMove=NeverInstantiate\n", encoding="utf-8")
            result = self.run_isolated(script, ["--book-dir", str(book), "--check-runtime"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("运行依赖检查通过", result.stdout)

    def test_runtime_failure_prints_import_chain_without_running_ros(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "elevator_button_floor_main.py"
            shutil.copyfile(ROOT / script.name, script)
            book = Path(directory) / "book"
            book.mkdir()
            (book / "api.py").write_text(
                "raise ModuleNotFoundError('missing messages',name='leju_mobile_base_msgs')\n", encoding="utf-8")
            result = self.run_isolated(script, ["--book-dir", str(book), "--check-runtime"])
            self.assertEqual(result.returncode, 1)
            self.assertIn("Traceback", result.stderr)
            self.assertIn("leju_mobile_base_msgs", result.stderr)
            self.assertIn("/usr/bin/python3", result.stderr)


class HeadFeedbackTests(unittest.TestCase):
    def message(self, yaw=0, pitch=0, stamp=100, count=20):
        q = [0.0] * count
        if count >= 20:
            q[18:20] = [controller.math.radians(yaw), controller.math.radians(pitch)]
        return SimpleNamespace(joint_data=SimpleNamespace(joint_q=q),
                               header=SimpleNamespace(stamp=SimpleNamespace(to_sec=lambda: stamp)))

    def test_measured_radians_are_converted_to_degrees(self):
        feedback = controller.HeadFeedback()
        feedback.feed(self.message(-20, -10), 100.1, 5)
        measured = feedback.snapshot(100.2, 5.1)
        self.assertAlmostEqual(measured["yaw"], -20)
        self.assertAlmostEqual(measured["pitch"], -10)

    def test_invalid_layout_stale_stamp_and_nan_cannot_confirm_motion(self):
        for message in (self.message(count=28), self.message(stamp=98),
                        self.message(stamp=102), self.message(yaw=float("nan"))):
            feedback = controller.HeadFeedback()
            feedback.feed(message, 100, 5)
            with self.assertRaises(RuntimeError):
                feedback.snapshot(100, 5)

    def test_paused_or_repeated_feedback_expires_even_if_ros_time_freezes(self):
        feedback = controller.HeadFeedback()
        feedback.feed(self.message(), 100, 5)
        feedback.feed(self.message(), 100, 5.4)
        with self.assertRaises(RuntimeError):
            feedback.snapshot(100, 5.6)

    def test_feedback_does_not_remain_valid_after_invalid_message(self):
        feedback = controller.HeadFeedback()
        feedback.feed(self.message(), 100, 5)
        feedback.feed(self.message(count=19), 100, 5.1)
        with self.assertRaises(RuntimeError):
            feedback.snapshot(100, 5.2)


class HeadMoveTests(unittest.TestCase):
    def make_fakes(self, measured=None, ready=1, stopped=False):
        clock = SimpleNamespace(now=0.0)
        sent = []
        ros = SimpleNamespace(is_shutdown=lambda: stopped,
                              get_param=lambda key, default=None: ready,
                              Time=SimpleNamespace(now=lambda: SimpleNamespace(to_sec=lambda: 100 + clock.now)))
        head = SimpleNamespace(head_pub=SimpleNamespace(get_num_connections=lambda: 1),
                               control_head=lambda yaw_deg, pitch_deg: sent.append(dict(yaw=yaw_deg, pitch=pitch_deg)) or True)
        def snapshot():
            pose = measured(clock.now) if measured else dict(yaw=-20, pitch=-10)
            return dict(stamp=100 + clock.now, received_at=clock.now, **pose)
        monitor = SimpleNamespace(snapshot=snapshot, wait_sample=snapshot)
        return clock, sent, ros, head, monitor

    def run_move(self, clock, ros, head, monitor, timeout=1):
        def advance(seconds):
            clock.now += seconds
        with patch.object(controller.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(controller.time, "sleep", side_effect=advance):
            return controller.move_head(head, dict(yaw=-20, pitch=-10), 0.2, ros,
                                        monitor, "LookRightUp", timeout=timeout)

    def test_success_requires_new_measured_frames_and_settling(self):
        clock, sent, ros, head, monitor = self.make_fakes()
        result = self.run_move(clock, ros, head, monitor)
        self.assertEqual(sent, [dict(yaw=-20, pitch=-10)])
        self.assertGreaterEqual(clock.now, 0.25)
        self.assertEqual(result["yaw"], -20)

    def test_published_command_with_unmoved_head_fails(self):
        clock, sent, ros, head, monitor = self.make_fakes(lambda now: dict(yaw=0, pitch=0))
        with self.assertRaisesRegex(RuntimeError, "头部未到位.*实测 yaw=0.00"):
            self.run_move(clock, ros, head, monitor)
        self.assertEqual(len(sent), 1)

    def test_hardware_unready_stops_before_publishing(self):
        for ready in (0, -1, None, True):
            clock, sent, ros, head, monitor = self.make_fakes(ready=ready)
            with self.subTest(ready=ready), self.assertRaisesRegex(RuntimeError, "硬件尚未就绪"):
                self.run_move(clock, ros, head, monitor)
            self.assertEqual(sent, [])

    def test_ros_shutdown_does_not_send(self):
        clock, sent, ros, head, monitor = self.make_fakes(stopped=True)
        with self.assertRaisesRegex(RuntimeError, "ROS 已关闭"):
            self.run_move(clock, ros, head, monitor)
        self.assertEqual(sent, [])

    def test_no_feedback_does_not_send(self):
        clock, sent, ros, head, monitor = self.make_fakes()
        monitor.wait_sample = lambda: (_ for _ in ()).throw(RuntimeError("反馈已过期"))
        with self.assertRaisesRegex(RuntimeError, "反馈已过期"):
            self.run_move(clock, ros, head, monitor)
        self.assertEqual(sent, [])

    def test_old_or_repeated_target_sample_cannot_complete(self):
        clock, sent, ros, head, monitor = self.make_fakes()
        monitor.snapshot = lambda: dict(stamp=100, received_at=0, yaw=-20, pitch=-10)
        with self.assertRaisesRegex(RuntimeError, "未到位"):
            self.run_move(clock, ros, head, monitor)

    def test_head_leaving_tolerance_resets_settling(self):
        def measured(now):
            return dict(yaw=0 if 0.15 < now < 0.3 else -20, pitch=-10)
        clock, sent, ros, head, monitor = self.make_fakes(measured)
        self.run_move(clock, ros, head, monitor)
        self.assertGreaterEqual(clock.now, 0.5 - 1e-9)

    def test_hardware_losing_ready_after_send_aborts(self):
        clock, sent, ros, head, monitor = self.make_fakes()
        ros.get_param = lambda key, default=None: 0 if sent else 1
        with self.assertRaisesRegex(RuntimeError, "硬件尚未就绪"):
            self.run_move(clock, ros, head, monitor)
        self.assertEqual(len(sent), 1)

    def test_api_rejection_aborts(self):
        clock, sent, ros, head, monitor = self.make_fakes()
        head.control_head = lambda yaw_deg, pitch_deg: False
        with self.assertRaisesRegex(RuntimeError, "HeadAPI 拒绝"):
            self.run_move(clock, ros, head, monitor)


class ParsingTests(unittest.TestCase):
    def test_false_button_is_known_not_unknown(self):
        value = core.parse_observation('{"button_lit":false,"confidence":0.95,"evidence":"按钮未发光"}', "button_verify")
        self.assertIs(value["button_lit"], False)

    def test_unknown_and_floor_5_are_preserved(self):
        for floor in (None, 5, 20):
            text = json.dumps({"floor": floor, "confidence": 0.9, "evidence": "读数"})
            self.assertEqual(core.parse_observation(text, "floor_monitor")["floor"], floor)

    def test_invalid_outputs_are_rejected(self):
        cases = [dict(floor="20", confidence=0.9, evidence="20"),
                 dict(floor=True, confidence=0.9, evidence="20"),
                 dict(floor=20, confidence=float("nan"), evidence="20"),
                 dict(floor=20, confidence=True, evidence="20"),
                 dict(floor=20, confidence=1.1, evidence="20"),
                 dict(floor=20, confidence=0.9, evidence=""),
                 dict(floor=20, confidence=0.9, evidence="20", reached=True)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                core.parse_observation(json.dumps(case), "floor_monitor")

    def test_truncated_json_is_rejected(self):
        with self.assertRaises(ValueError):
            core.parse_observation('{"floor":20,"confidence":0.9,"evidence":"', "floor_monitor")

    def test_target_floor_is_not_injected_into_recognition(self):
        class Client:
            def complete(self, messages):
                self.messages = messages
                return '{"floor":5,"confidence":0.9,"evidence":"屏幕显示5"}'
        client = Client()
        answer = perception.infer(client, "data:image/jpeg;base64,AAA", "floor_monitor", 0.72)
        self.assertEqual(answer["floor"], 5)
        self.assertNotIn("20", json.dumps(client.messages, ensure_ascii=False))

    def test_low_confidence_cannot_become_known(self):
        class Client:
            def complete(self, messages):
                return '{"floor":20,"confidence":0.5,"evidence":"疑似20"}'
        answer = perception.infer(Client(), "data:image/jpeg;base64,AAA", "floor_monitor", 0.72)
        self.assertIsNone(answer["floor"])
        self.assertEqual(answer["status"], "uncertain")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.gate = core.EvidenceGate(15, 0.72, 2, 1)
        self.gate.arm("floor_monitor", "floor", 20, 100, request_id="current")

    def test_two_distinct_target_frames_confirm(self):
        self.assertFalse(self.gate.feed(payload(), 101))
        self.assertTrue(self.gate.feed(payload(seq=2, stamp=103), 103))

    def test_processing_and_heartbeats_neither_confirm_nor_reset_evidence(self):
        self.gate.feed(payload(), 101)
        self.assertFalse(self.gate.feed(payload(seq=2, stamp=102, status="processing", message_type="heartbeat"), 102))
        self.assertFalse(self.gate.feed(payload(seq=3, stamp=103, message_type="heartbeat"), 103))
        self.assertEqual(self.gate.count, 1)
        self.assertTrue(self.gate.feed(payload(seq=4, stamp=104), 104))

    def test_other_floor_does_not_confirm(self):
        for i in range(1, 5):
            self.assertFalse(self.gate.feed(payload(seq=i, stamp=100+i, value=19), 100+i))

    def test_wrong_request_and_mode_are_ignored(self):
        self.assertFalse(self.gate.feed(payload(request_id="previous"), 101))
        self.assertFalse(self.gate.feed(payload(mode="button_verify"), 101))
        self.assertEqual(self.gate.count, 0)

    def test_unknown_breaks_confirmation(self):
        self.gate.feed(payload(), 101)
        self.gate.feed(payload(seq=2, stamp=102, value=None, status="uncertain"), 102)
        self.assertFalse(self.gate.feed(payload(seq=3, stamp=103), 103))
        self.assertTrue(self.gate.feed(payload(seq=4, stamp=104), 104))

    def test_duplicate_sequence_and_duplicate_frame_do_not_count(self):
        self.gate.feed(payload(), 101)
        self.assertFalse(self.gate.feed(payload(stamp=103), 103))
        self.assertFalse(self.gate.feed(payload(seq=2), 103))
        self.assertEqual(self.gate.count, 1)
        self.assertTrue(self.gate.feed(payload(seq=3, stamp=104), 104))

    def test_stale_previous_stage_future_and_nonfinite_stamps_fail(self):
        for stamp in (99, 70, 105, float("nan"), float("inf")):
            with self.subTest(stamp=stamp):
                self.gate.arm("floor_monitor", "floor", 20, 100, request_id="current")
                self.assertFalse(self.gate.feed(payload(stamp=stamp), 101))

    def test_boolean_confidence_wrong_schema_and_string_floor_fail(self):
        for fields in ({"confidence": {"floor": True}}, {"schema_version": 2}, {"floor": "20"}, {"floor": True}):
            with self.subTest(fields=fields):
                self.gate.arm("floor_monitor", "floor", 20, 100, request_id="current")
                self.assertFalse(self.gate.feed(payload(**fields), 101))

    def test_a_long_gap_resets_old_confirmation(self):
        self.gate.feed(payload(), 101)
        self.assertFalse(self.gate.feed(payload(seq=2, stamp=120), 120))
        self.assertEqual(self.gate.count, 1)

    def test_ready_result_expires_before_wait_returns(self):
        self.gate.feed(payload(), 101)
        self.gate.feed(payload(seq=2, stamp=103), 103)
        with self.assertRaises(TimeoutError):
            self.gate.wait(0.01, lambda: 150)
        self.assertFalse(self.gate.ready)

    def test_rearm_forgets_floor_from_previous_stage(self):
        self.gate.feed(payload(), 101)
        self.gate.feed(payload(seq=2, stamp=103), 103)
        self.gate.arm("floor_monitor", "floor", 20, 110, request_id="new")
        self.assertFalse(self.gate.ready)
        self.assertFalse(self.gate.feed(payload(seq=3, stamp=111), 111))

    def test_door_adapter_accepts_original_schema_only_after_head_settle(self):
        gate = core.EvidenceGate(15, 0.72, 1, 0)
        gate.arm("door_monitor", "door_closed", False, 200, schema_version=2)
        base = dict(sequence=3, image_stamp=199, door_closed=False, confidence={"door_closed": 0.9},
                    mode="door_monitor", status="ok", schema_version=2)
        self.assertFalse(gate.feed(json.dumps(base), 201))
        base.update(sequence=4, image_stamp=201)
        self.assertTrue(gate.feed(json.dumps(base), 201))

    def test_unlit_button_does_not_release_gate(self):
        gate = core.EvidenceGate(15, 0.72, 2, 1)
        gate.arm("button_verify", "button_lit", True, 100, request_id="current")
        for seq in (1, 2, 3):
            item = json.loads(payload(seq=seq, stamp=100+seq, mode="button_verify"))
            item.update(button_lit=False, confidence={"button_lit": 0.99})
            self.assertFalse(gate.feed(json.dumps(item), 100+seq))


class WorkflowTests(unittest.TestCase):
    def test_completion_marker_requires_last_navigation_success(self):
        for entry_only, fail in ((True, False), (False, False), (False, True)):
            runtime = controller.MockRuntime()
            runtime.record = Mock()
            if fail:
                navigate = runtime.navigate
                runtime.navigate = lambda point: False if point == "exit" else navigate(point)
                with self.assertRaises(RuntimeError):
                    controller.execute_demo(runtime)
                runtime.record.assert_not_called()
            else:
                controller.execute_demo(runtime, entry_only=entry_only)
                runtime.record.assert_called_once_with("workflow_completed", scope="entry_only" if entry_only else "full")

    def test_first_button_positive_is_fresh_and_recorded_once_per_request(self):
        runtime = controller.LiveRuntime.__new__(controller.LiveRuntime)
        gate = core.EvidenceGate(15, .72, 2, 1)
        gate.arm("button_verify", "button_lit", True, 100, request_id="current")
        runtime.active_observation = "button"
        runtime.gates = dict(button=gate)
        runtime.now = lambda: 105
        runtime.rospy = SimpleNamespace(loginfo_throttle=Mock())
        runtime.record = Mock()
        base = dict(schema_version=1, mode="button_verify", sequence=1, request_id="old", image_stamp=101,
                    status="ok", button_lit=True, confidence=dict(button_lit=.9))
        for updates in ({}, dict(request_id="current", image_stamp=99),
                        dict(request_id="current", image_stamp=102, sequence=2, button_lit=False)):
            runtime.on_state("button", SimpleNamespace(data=json.dumps(dict(base, **updates))))
        runtime.record.assert_not_called()
        base.update(request_id="current", image_stamp=103, sequence=3)
        runtime.on_state("button", SimpleNamespace(data=json.dumps(base)))
        base.update(image_stamp=104, sequence=4)
        runtime.on_state("button", SimpleNamespace(data=json.dumps(base)))
        runtime.record.assert_called_once()
        self.assertEqual(runtime.record.call_args.args[0], "button_first_positive")
        self.assertEqual(runtime.record.call_args.kwargs["result"]["image_stamp"], 103)
    def test_live_floor_gate_uses_configured_target(self):
        runtime = controller.LiveRuntime.__new__(controller.LiveRuntime)
        runtime.config = dict(target_floor=19)
        runtime.wait_extra = lambda *args: args
        self.assertEqual(runtime.wait_floor(), ("floor", "floor_monitor", "floor", 19))

    def test_complete_sequence(self):
        runtime = controller.MockRuntime()
        core.execute_demo(runtime)
        relevant = [event for event in runtime.events if event[0] != "mode"]
        self.assertEqual(relevant, [
            ("navigate", "button"), ("head", "button"), ("wait", "down_button_lit"),
            ("head", "door"), ("navigate", "origin"), ("wait", "door_fully_open"),
            ("navigate", "inside"), ("head", "floor"), ("wait", "floor_20"),
            ("head", "door"), ("wait", "door_fully_open"), ("navigate", "exit"), ("close",)])

    def test_button_wait_failure_never_returns_to_origin_or_enters(self):
        runtime = controller.MockRuntime()
        runtime.wait_button = lambda: (_ for _ in ()).throw(TimeoutError("未亮"))
        with self.assertRaises(TimeoutError):
            core.execute_demo(runtime)
        self.assertNotIn(("navigate", "origin"), runtime.events)
        self.assertNotIn(("navigate", "inside"), runtime.events)
        self.assertEqual(runtime.events[-1], ("close",))

    def test_floor_failure_never_turns_back_to_door_or_exits(self):
        runtime = controller.MockRuntime()
        runtime.wait_floor = lambda: (_ for _ in ()).throw(TimeoutError("未到20"))
        with self.assertRaises(TimeoutError):
            core.execute_demo(runtime)
        floor_index = runtime.events.index(("head", "floor"))
        self.assertNotIn(("head", "door"), runtime.events[floor_index:])
        self.assertNotIn(("navigate", "exit"), runtime.events)

    def test_navigation_failure_cancels_later_steps(self):
        for point in ("button", "origin", "inside", "exit"):
            runtime = controller.MockRuntime()
            original = runtime.navigate
            runtime.navigate = lambda name, point=point: original(name) and name != point
            with self.subTest(point=point), self.assertRaises(RuntimeError):
                core.execute_demo(runtime)
            self.assertEqual(runtime.events[-1], ("close",))
            if point == "inside":
                self.assertNotIn(("head", "floor"), runtime.events)


class UpperNodeTests(unittest.TestCase):
    def make_node(self):
        node = perception.ExtraPerceptionNode.__new__(perception.ExtraPerceptionNode)
        node.lock, node.publish_lock = threading.Lock(), threading.Lock()
        node.stop = threading.Event()
        node.request = dict(mode="idle", request_id="", not_before_stamp=0)
        node.frame = None
        node.last_signature = None
        node.sequence, node.instance_id = 0, "test-instance"
        node.config = core.load_config(ROOT / "params/elevator_button_floor_demo.json")
        node.state = dict(status="idle", reason="等待下位机观察请求")
        node.last_result, node.client_error = None, ""
        node.client = object()
        node.rospy = SimpleNamespace(
            Time=SimpleNamespace(now=lambda: SimpleNamespace(to_sec=lambda: 102)),
            is_shutdown=lambda: False, logwarn=lambda *args: None)
        node.cv2 = node.np = node.bridge = object()
        node.String = lambda data: SimpleNamespace(data=data)
        published = []
        node.publisher = SimpleNamespace(publish=lambda message: published.append(json.loads(message.data)))
        return node, published

    def test_inflight_answer_is_discarded_after_request_change(self):
        node, published = self.make_node()
        node.request = dict(mode="floor_monitor", request_id="old", not_before_stamp=100)
        node.frame = (object(), 101, 1)
        def inference(*args):
            node.request = dict(mode="floor_monitor", request_id="new", not_before_stamp=102)
            return dict(floor=20, confidence={"floor": 0.9}, status="ok")
        with patch.object(perception, "decode_image", return_value=object()), \
                patch.object(perception, "encode_image", return_value="image"), \
                patch.object(perception, "infer", side_effect=inference):
            node.run_once()
        self.assertFalse(any(message["message_type"] == "result" for message in published))

    def test_idle_has_basic_heartbeat_without_model_calls(self):
        node, published = self.make_node()
        with patch.object(perception, "infer", side_effect=AssertionError("unexpected model call")):
            node.run_once()
            node.on_heartbeat(None)
        self.assertEqual(published[-1]["status"], "idle")
        self.assertEqual(published[-1]["message_type"], "heartbeat")
        self.assertFalse(published[-1]["image_received"])
        self.assertIn("reason", published[-1])

    def test_waiting_image_has_explanation(self):
        node, published = self.make_node()
        node.request = dict(mode="floor_monitor", request_id="current", not_before_stamp=100)
        node.run_once()
        node.emit_heartbeat()
        self.assertEqual(published[-1]["status"], "waiting_image")
        self.assertIn("相机", published[-1]["reason"])

    def test_plain_mode_request_and_invalid_request_are_visible(self):
        node, published = self.make_node()
        node.on_request(SimpleNamespace(data="button_verify"))
        self.assertEqual(node.request["mode"], "button_verify")
        self.assertTrue(node.request["request_id"])
        self.assertEqual(published[-1]["status"], "waiting_image")
        node.on_request(SimpleNamespace(data="invalid-mode"))
        self.assertEqual(published[-1]["status"], "error")
        self.assertIn("格式", published[-1]["reason"])

    def test_processing_heartbeat_and_unlit_button_result(self):
        node, published = self.make_node()
        node.request = dict(mode="button_verify", request_id="current", not_before_stamp=100)
        node.frame = (object(), 101, 1)
        def inference(*args):
            node.on_heartbeat(None)
            return dict(button_lit=False, confidence={"button_lit": 0.95},
                        evidence={"button_lit": "按钮未发光"}, status="ok")
        with patch.object(perception, "decode_image", return_value=object()), \
                patch.object(perception, "encode_image", return_value="image"), \
                patch.object(perception, "infer", side_effect=inference):
            node.run_once()
        self.assertTrue(any(message["status"] == "processing" for message in published))
        self.assertEqual(published[-1]["message_type"], "result")
        self.assertIs(published[-1]["button_lit"], False)
        self.assertIn("未点亮", published[-1]["reason"])
        node.emit_heartbeat()
        self.assertTrue(published[-1]["result_fresh"])
        self.assertIs(published[-1]["button_lit"], False)

    def test_client_initialization_error_is_visible_while_idle(self):
        node, published = self.make_node()
        node.client_error = "缺少 API 配置"
        node.run_once()
        node.emit_heartbeat()
        self.assertEqual(published[-1]["status"], "error")
        self.assertIn("初始化失败", published[-1]["reason"])

    def test_constructor_keeps_feedback_alive_when_client_initialization_fails(self):
        topics, options, callbacks = {}, {}, []
        def publisher(name, _message_type, **kwargs):
            topics[name], options[name] = [], kwargs
            return SimpleNamespace(publish=lambda message: topics[name].append(json.loads(message.data)))
        ros = SimpleNamespace(
            Time=SimpleNamespace(now=lambda: SimpleNamespace(to_sec=lambda: 102)),
            Publisher=publisher, Subscriber=lambda *args, **kwargs: object(),
            on_shutdown=lambda callback: None, Duration=lambda value: value,
            Timer=lambda duration, callback: callbacks.append(callback) or SimpleNamespace(shutdown=lambda: None),
            is_shutdown=lambda: False)
        modules = {"rospy": ros, "cv2": SimpleNamespace(), "numpy": SimpleNamespace(),
                   "cv_bridge": SimpleNamespace(CvBridge=lambda: object()),
                   "sensor_msgs.msg": SimpleNamespace(Image=object),
                   "std_msgs.msg": SimpleNamespace(String=lambda data: SimpleNamespace(data=data))}
        config = core.load_config(ROOT / "params/elevator_button_floor_demo.json")
        with patch.dict(sys.modules, modules):
            node = perception.ExtraPerceptionNode(config, client_factory=lambda: (_ for _ in ()).throw(ValueError("缺少API配置")))
        state_topic, ready_topic = config["perception"]["extra_state_topic"], config["perception"]["extra_ready_topic"]
        self.assertTrue(options[state_topic]["latch"])
        self.assertEqual(topics[state_topic][-1]["status"], "error")
        self.assertEqual(topics[ready_topic][-1]["status"], "error")
        self.assertEqual(topics[ready_topic][-1]["inference_hz"], 1.0)
        callbacks[0](None)
        self.assertEqual(topics[state_topic][-1]["message_type"], "heartbeat")

    def test_model_error_is_published_as_result_with_reason(self):
        node, published = self.make_node()
        node.request = dict(mode="floor_monitor", request_id="current", not_before_stamp=100)
        node.frame = (object(), 101, 1)
        with patch.object(perception, "decode_image", return_value=object()), \
                patch.object(perception, "encode_image", return_value="image"), \
                patch.object(perception, "infer", side_effect=RuntimeError("API连接超时")):
            node.run_once()
        self.assertEqual(published[-1]["status"], "error")
        self.assertEqual(published[-1]["message_type"], "result")
        self.assertIn("超时", published[-1]["reason"])

    def test_expired_results_are_marked_stale_in_heartbeat(self):
        node, published = self.make_node()
        node.request = dict(mode="floor_monitor", request_id="current", not_before_stamp=1)
        node.state = dict(status="ok", reason="上次读数")
        node.last_result = dict(image_stamp=70, floor=20)
        node.emit_heartbeat()
        self.assertEqual(published[-1]["status"], "stale_result")
        self.assertFalse(published[-1]["result_fresh"])
        self.assertIsNone(published[-1]["floor"])

    def test_zero_and_stale_camera_timestamps_are_rejected(self):
        self.assertFalse(perception.math_is_fresh(0, 101, 2, 100))
        self.assertFalse(perception.math_is_fresh(99, 101, 2, 100))
        self.assertFalse(perception.math_is_fresh(101, 120, 15, 100))
        self.assertTrue(perception.math_is_fresh(101, 102, 2, 100))


if __name__ == "__main__":
    unittest.main()
