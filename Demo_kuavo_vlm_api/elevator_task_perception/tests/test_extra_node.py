import collections
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(1, str(ROOT/'upstream'))
from extra_color_node import ColorExtraNode
from floor_filter import FloorTracker, SCHEMA
from button_contract import FrameConfirmation
from task_contract import trip


class ExtraNodeTests(unittest.TestCase):
    def node(self):
        n = ColorExtraNode.__new__(ColorExtraNode)
        n.lock = threading.RLock(); n.request_epoch = n.floor_call_epoch = 1
        n.request = {**trip(21, 20), 'mode': 'floor_monitor', 'request_id': 'floor',
                     'not_before_stamp': 99, 'task_started_stamp': 98}
        n.config = {'perception': {'min_confidence': .72}}
        n.floor_tracker = FloorTracker(); n.frames = collections.deque()
        n.rospy = types.SimpleNamespace(logwarn=lambda *a: None)
        return n

    def test_filtered_value_and_trip_preserved_without_fake_confidence(self):
        n = self.node(); req = dict(n.request)
        raw = {'floor': 20, 'status': 'ok', 'confidence': {'floor': None}}
        with patch('elevator_extra_perception.ExtraPerceptionNode.publish') as publish:
            n.publish(req, 100, raw)
            self.assertIsNone(publish.call_args[0][2]['floor'])
            n.publish(req, 101, raw)
            out = publish.call_args[0][2]
            self.assertEqual(out['floor'], 20)
            self.assertEqual(out['schema_version'], SCHEMA)
            self.assertIsNone(out['confidence']['floor'])
            self.assertEqual(out['current_floor'], 21)
            self.assertEqual(out['target_floor'], 20)

    def test_stale_epoch_not_published(self):
        n = self.node(); n.floor_call_epoch = 0
        with patch('elevator_extra_perception.ExtraPerceptionNode.publish') as publish:
            n.publish(dict(n.request), 100, {'floor': 20})
            publish.assert_not_called()

    def test_new_trip_resets_and_seeds(self):
        n = self.node(); n.floor_tracker.observe(20, 100)
        r = {**trip(19, 20), 'request_id': 'new', 'mode': 'floor_monitor',
             'task_started_stamp': 101, 'not_before_stamp': 102}
        with patch('elevator_extra_perception.ExtraPerceptionNode.on_request') as parent:
            n.on_request(types.SimpleNamespace(data=json.dumps(r)))
            parent.assert_called_once()
        self.assertEqual(n.floor_tracker.anchor, 19)
        self.assertEqual(n.floor_tracker.pending, [])
        self.assertEqual(n.request_epoch, 2)

    def test_invalid_trip_does_not_reset_active_request(self):
        n = self.node()
        n.on_request(types.SimpleNamespace(data=json.dumps({**n.request, 'button_direction': 'up'})))
        self.assertEqual(n.request_epoch, 1)

    def test_heartbeat_not_floor_vote(self):
        n = self.node()
        with patch('elevator_extra_perception.ExtraPerceptionNode.publish'):
            n.publish(dict(n.request), 100, {}, message_type='heartbeat')
        self.assertIsNone(n.floor_tracker.last_seen)


if __name__ == '__main__': unittest.main()
