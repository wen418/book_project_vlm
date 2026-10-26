import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(1, str(ROOT/'upstream'))
sys.path.append(str(ROOT.parents[1]/'Demo_vlm2_kuavo_api'))
from elevator_button_floor_main import EvidenceGate, LiveRuntime, MockRuntime, execute_demo
from lower_adapter import compact_door_gate, install


class IntegrationTests(unittest.TestCase):
    def test_lower_request_carries_trip(self):
        class Gate:
            def arm(self, *args, **kwargs): self.armed = args, kwargs
            def wait(self, *args): return {'ok': True}
        sent = []
        gate = Gate()
        runtime = types.SimpleNamespace(config={'current_floor': 19, 'target_floor': 20, 'perception': {'button_timeout': 10}},
                    task_started_stamp=98, door_pub=types.SimpleNamespace(publish=lambda m: None),
                    extra_pub=types.SimpleNamespace(publish=lambda m: sent.append(json.loads(m.data))),
                    now=lambda: 100, String=lambda **kwargs: types.SimpleNamespace(**kwargs),
                    gates={'button': gate}, rospy=types.SimpleNamespace(is_shutdown=lambda: False), record=lambda *a, **k: None)
        result = LiveRuntime.wait_extra(runtime, 'button', 'button_verify', 'button_lit', True)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(gate.button_direction, 'up')
        self.assertEqual(sent[0]['current_floor'], 19)
        self.assertEqual(sent[0]['target_floor'], 20)
        self.assertEqual(sent[0]['task_started_stamp'], 98)
        self.assertEqual(sent[0]['button_direction'], 'up')

    def test_mock_up_branch(self):
        r = MockRuntime(20, 'up')
        with patch('builtins.print'): execute_demo(r, 'entry_wait')
        self.assertIn(('wait', 'up_button_lit'), r.events)
        self.assertNotIn(('wait', 'down_button_lit'), r.events)

    def test_service_discovery_cached_but_other_service_not_bypassed(self):
        calls = []
        module = types.SimpleNamespace(require_head_ready=lambda r: None, check_connections=lambda *a: None,
                                       MotionMonitor=type('M', (), {'prepare': lambda *a: None}))
        install(module)
        ros = types.SimpleNamespace(wait_for_service=lambda name, timeout=None: calls.append(name), resolve_name=lambda n: n)
        module.check_connections(ros, {})
        self.assertEqual(len(calls), 3)
        ros.wait_for_service('/move_base/check_arrived', timeout=5)
        self.assertEqual(len(calls), 3)
        ros.wait_for_service('/unrelated_service', timeout=5)
        self.assertEqual(calls[-1], '/unrelated_service')

    def test_motion_baseline_wait_only_first_time(self):
        calls = []
        module = types.SimpleNamespace(require_head_ready=lambda r: None, check_connections=lambda *a: None,
                                       MotionMonitor=type('M', (), {'prepare': lambda *args: calls.append('static check')}))
        install(module)
        m = module.MotionMonitor()
        m.lock = threading.RLock(); m.samples = [{'stamp': 100, 'pose': (0, 0, 0)}]
        m.runtime = types.SimpleNamespace(now=lambda: 100.1)
        m.prepare('startup'); m.prepare('button'); m.prepare('inside')
        self.assertEqual(calls, ['static check'])
        self.assertEqual(m.prepared['point'], 'inside')


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.gate = compact_door_gate(EvidenceGate)(15, .72, 1, 0)
        self.gate.arm('door_monitor', 'door_closed', False, 100, schema_version=2)
        self.packet = {'mode': 'door_monitor', 'schema_version': 2, 'recognition_contract': 'compact_state_v1',
                       'sequence': 1, 'image_stamp': 101, 'door_closed': False, 'status': 'ok',
                       'confidence': {'door_closed': None}}

    def test_open_without_fake_confidence(self):
        self.assertTrue(self.gate.feed(json.dumps(self.packet), 101.1))

    def test_unknown_not_open(self):
        self.assertFalse(self.gate.feed(json.dumps({**self.packet, 'door_closed': None, 'status': 'uncertain'}), 101.1))

    def test_old_json_node_rejected(self):
        self.assertFalse(self.gate.feed(json.dumps({**self.packet, 'recognition_contract': None}), 101.1))

    def test_stale_and_previous_stage(self):
        self.assertFalse(self.gate.feed(json.dumps({**self.packet, 'image_stamp': 99}), 101.1))
        self.assertFalse(self.gate.feed(json.dumps({**self.packet, 'sequence': 2}), 120))


if __name__ == '__main__': unittest.main()
