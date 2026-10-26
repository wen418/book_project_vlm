import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT/'upstream'))
from task_contract import trip, validate_request
from compact_vlm import parse, configure, infer_value
from floor_filter import FloorTracker, adapted_floor_gate, valid_floor_proof, DEFAULTS, SCHEMA, BACKEND
from button_contract import adapted_gate, FrameConfirmation, SCHEMA as BUTTON_SCHEMA, BACKEND as BUTTON_BACKEND
from lower_adapter import compact_door_gate, install
from elevator_button_floor_main import EvidenceGate


class TripTests(unittest.TestCase):
    def test_up_down(self):
        self.assertEqual(trip(21, 20)['button_direction'], 'down')
        self.assertEqual(trip(19, 20)['button_direction'], 'up')

    def test_invalid_or_same_floor(self):
        for a, b in [(20, 20), (True, 20), (151, 20), ('21', 20), (None, 20)]:
            with self.assertRaises(ValueError): trip(a, b)

    def test_request_intent(self):
        r = {**trip(21, 20), 'task_started_stamp': 100, 'not_before_stamp': 101}
        self.assertEqual(validate_request(r)['button_direction'], 'down')
        for update in [{'button_direction': 'up'}, {'task_started_stamp': 102}]:
            with self.assertRaises(ValueError): validate_request({**r, **update})


class CompactTests(unittest.TestCase):
    def test_states(self):
        self.assertIs(parse('C', 'door_monitor'), True)
        self.assertIs(parse('O', 'door_monitor'), False)
        self.assertIsNone(parse('U', 'door_monitor'))
        self.assertEqual(parse('20\n', 'floor_monitor'), 20)
        self.assertEqual(parse('-1', 'floor_monitor'), -1)

    def test_no_salvage(self):
        for text in ['{"floor":20}', '20 because', '```20```', '020', 'true', '151', '-11']:
            with self.assertRaises(ValueError): parse(text, 'floor_monitor')
        with self.assertRaises(ValueError): parse('OPEN', 'door_monitor')

    def test_short_request(self):
        class Client:
            def complete(self, messages, **kwargs):
                self.messages, self.kwargs = messages, kwargs
                return '20'
        c = configure(Client())
        self.assertEqual(infer_value(c, 'data:image/jpeg;base64,AAA', 'floor_monitor'), 20)
        self.assertFalse(c.use_json_object); self.assertFalse(c.enable_thinking)
        self.assertEqual(c.kwargs, {'max_tokens': 8})
        self.assertEqual(len(c.messages), 2)


class FloorTests(unittest.TestCase):
    def setUp(self):
        self.tracker = FloorTracker()
        self.gate = adapted_floor_gate(EvidenceGate)(15, .72, 1, 0)
        self.gate.arm('floor_monitor', 'floor', 20, 100, request_id='trip1')
        self.seq = 0

    def packet(self, raw, stamp):
        floor, proof = self.tracker.observe(raw, stamp, not_before=100)
        p = {'schema_version': SCHEMA, 'floor_backend': BACKEND, 'floor': floor, 'floor_raw': raw,
             'floor_filter': proof, 'image_stamp': stamp, 'sequence': self.seq,
             'mode': 'floor_monitor', 'message_type': 'result', 'request_id': 'trip1',
             'status': 'ok' if floor is not None else 'uncertain', 'confidence': {'floor': None}}
        self.seq += 1
        return p

    def feed(self, raw, stamp):
        p = self.packet(raw, stamp)
        return self.gate.feed(json.dumps(p), stamp+.1), p

    def test_adjacent_stop_without_direction(self):
        for raw, stamp in [(21, 101), (21, 102), (20, 105)]: self.assertFalse(self.feed(raw, stamp)[0])
        self.assertTrue(self.feed(20, 106.5)[0])

    def test_reversal_accepted(self):
        for raw, stamp in [(19, 101), (19, 102), (18, 106), (17, 110), (18, 114), (19, 118), (20, 122)]:
            _, p = self.feed(raw, stamp)
            if stamp > 101: self.assertEqual(p['floor'], raw)
        self.assertTrue(self.feed(20, 123.5)[0])

    def test_one_unknown_tolerated(self):
        for raw, stamp in [(21, 101), (21, 102), (20, 105), (None, 106)]: self.assertFalse(self.feed(raw, stamp)[0])
        self.assertTrue(self.feed(20, 108)[0])

    def test_two_unknown_reset(self):
        for raw, stamp in [(21, 101), (21, 102), (20, 105), (None, 106), (None, 107)]: self.feed(raw, stamp)
        self.assertFalse(self.feed(20, 108)[0])

    def test_window_expiry(self):
        for raw, stamp in [(21, 101), (21, 102), (20, 105)]: self.feed(raw, stamp)
        self.assertFalse(self.feed(20, 111)[0])

    def test_other_floor_clears(self):
        for raw, stamp in [(21, 101), (21, 102), (20, 105), (21, 106)]: self.feed(raw, stamp)
        self.assertFalse(self.feed(20, 108)[0])

    def test_jump_rejected(self):
        for raw, stamp in [(19, 101), (19, 102)]: self.feed(raw, stamp)
        _, p = self.feed(13, 104)
        self.assertIsNone(p['floor']); self.assertEqual(p['floor_filter']['reason'], 'implausible_jump')

    def test_elapsed_expands_range(self):
        self.feed(19, 101); self.feed(19, 102)
        _, p = self.feed(16, 110.1)
        self.assertEqual(p['floor'], 16)

    def test_reacquire_stationary_after_bad_anchor(self):
        self.feed(25, 101); self.feed(25, 102)
        for stamp in [115, 116.2]: self.assertIsNone(self.feed(20, stamp)[1]['floor'])
        self.assertTrue(self.feed(20, 117.5)[0])

    def test_slow_api_reacquisition_not_stuck(self):
        self.feed(25, 101); self.feed(25, 102)
        self.feed(20, 115); self.feed(20, 118)
        self.assertTrue(self.feed(20, 121)[0])

    def test_planner_seed_is_not_vote(self):
        self.tracker.reset(20, 99)
        self.assertFalse(self.feed(20, 101)[0])
        p = self.packet(20, 102)
        p.update(current_floor=20, task_started_stamp=99)
        self.assertTrue(valid_floor_proof(p, DEFAULTS, .72, 100))
        self.assertTrue(self.gate.feed(json.dumps(p), 102.1))

    def test_seed_expires_instead_of_blocking(self):
        self.tracker.reset(21, 80)
        self.assertFalse(self.feed(15, 101)[0])
        self.assertEqual(self.feed(15, 102)[1]['floor'], 15)

    def test_old_duplicate_heartbeat_no_credit(self):
        self.feed(21, 101); self.feed(21, 102)
        _, p = self.feed(20, 105)
        self.assertFalse(self.gate.feed(json.dumps(p), 105.1))
        self.assertFalse(self.gate.feed(json.dumps({**p, 'sequence': 100, 'message_type': 'heartbeat'}), 105.1))
        self.assertEqual(self.gate.count, 1)

    def test_wrong_request_ignored(self):
        p = self.packet(20, 101); p['request_id'] = 'previous'
        self.assertFalse(self.gate.feed(json.dumps(p), 101.1))
        self.assertEqual(self.gate.count, 0)

    def test_stale_result_clears(self):
        self.feed(21, 101); self.feed(21, 102); _, p = self.feed(20, 105)
        p['sequence'] += 100; p['image_stamp'] += 1
        self.assertFalse(self.gate.feed(json.dumps(p), 120))
        self.assertEqual(self.gate.count, 0)


class ButtonTests(unittest.TestCase):
    def packet(self):
        _, proof = FrameConfirmation().observe(True, 101, 1, 100, 101.1)
        return {'schema_version': BUTTON_SCHEMA, 'button_backend': BUTTON_BACKEND, 'status': 'ok',
                'button_lit': True, 'up_lit': False, 'down_lit': True, 'selected_direction': 'down',
                'button_confirmation': proof, 'mode': 'button_verify', 'request_id': 'b',
                'image_stamp': 101, 'sequence': 1}

    def gate(self, direction):
        g = adapted_gate(EvidenceGate)(15, .72, 1, 0)
        g.arm('button_verify', 'button_lit', True, 100, request_id='b'); g.button_direction = direction
        return g

    def test_single_fresh_selected_button(self):
        self.assertTrue(self.gate('down').feed(json.dumps(self.packet()), 101.1))

    def test_opposite_lit_not_accepted(self):
        self.assertFalse(self.gate('up').feed(json.dumps(self.packet()), 101.1))

    def test_selected_state_consistency(self):
        self.assertFalse(self.gate('down').feed(json.dumps({**self.packet(), 'down_lit': False}), 101.1))


class RuntimeChecks(unittest.TestCase):
    def test_hardware_check_cached(self):
        calls = []
        module = types.SimpleNamespace(require_head_ready=lambda r: calls.append(r),
                                       check_connections=lambda *a: None,
                                       MotionMonitor=type('MM', (), {'prepare': lambda *a: None}))
        install(module)
        module.require_head_ready('ros'); module.require_head_ready('ros')
        self.assertEqual(calls, ['ros'])

    def test_head_does_not_poll_feedback(self):
        module = types.SimpleNamespace(require_head_ready=lambda r: self.fail('runtime repeated check'),
                                       check_connections=lambda *a: None,
                                       MotionMonitor=type('MM', (), {'prepare': lambda *a: None}))
        install(module)
        head = types.SimpleNamespace(control_head=lambda y, p: True)
        r = module.move_head(head, {'yaw': -20, 'pitch': -20}, 0, types.SimpleNamespace(is_shutdown=lambda: False), None, 'test')
        self.assertFalse(r['arrival_verified'])


if __name__ == '__main__': unittest.main()
