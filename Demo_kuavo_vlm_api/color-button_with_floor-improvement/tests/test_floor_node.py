import json
from pathlib import Path
import sys
import threading
from collections import deque
from unittest.mock import patch
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'upstream'))
from floor_filter import FloorTracker,SCHEMA
from extra_color_node import ColorExtraNode


class FloorNodeTests(unittest.TestCase):
    def node(self):
        node=ColorExtraNode.__new__(ColorExtraNode)
        node.lock=threading.Lock();node.request_epoch=node.floor_call_epoch=1
        node.request={'mode':'floor_monitor','request_id':'floor','not_before_stamp':99}
        node.config={'perception':{'min_confidence':.72}}
        node.floor_tracker=FloorTracker();node.frames=deque()
        return node

    def test_raw_prediction_preserved_but_only_filtered_value_published(self):
        node=self.node()
        request=dict(node.request)
        raw={'floor':19,'status':'ok','confidence':{'floor':.91},'latency_ms':1450}
        with patch('elevator_extra_perception.ExtraPerceptionNode.publish') as publish:
            node.publish(request,100,raw)
            first=publish.call_args[0][2]
            self.assertIsNone(first['floor']);self.assertEqual(first['floor_raw'],19)
            self.assertEqual(first['confidence']['floor'],.91);self.assertEqual(first['latency_ms'],1450)
            node.publish(request,101.5,raw)
            second=publish.call_args[0][2]
            self.assertEqual(second['floor'],19);self.assertEqual(second['schema_version'],SCHEMA)
            node.publish(request,103,{**raw,'floor':13})
            third=publish.call_args[0][2]
            self.assertIsNone(third['floor']);self.assertEqual(third['floor_raw'],13)
            self.assertEqual(third['floor_filter']['reason'],'implausible_jump')
        self.assertEqual(raw['floor'],19)

    def test_old_request_epoch_cannot_update_filter(self):
        node=self.node();node.floor_call_epoch=0
        with patch('elevator_extra_perception.ExtraPerceptionNode.publish') as publish:
            node.publish(dict(node.request),100,{'floor':19,'status':'ok','confidence':{'floor':.9}})
            publish.assert_not_called()
        self.assertIsNone(node.floor_tracker.last_seen)

    def test_request_change_resets_floor_history(self):
        node=self.node();node.floor_tracker.observe(19,100,.9,.72,99)
        with patch('elevator_extra_perception.ExtraPerceptionNode.on_request'):
            node.on_request(object())
        self.assertIsNone(node.floor_tracker.anchor);self.assertEqual(node.floor_tracker.pending,[])

    def test_heartbeat_does_not_count_as_model_observation(self):
        node=self.node()
        with patch('elevator_extra_perception.ExtraPerceptionNode.publish') as publish:
            node.publish(dict(node.request),100,{'floor':19,'status':'ok'},message_type='heartbeat')
            publish.assert_called_once()
        self.assertIsNone(node.floor_tracker.last_seen)


if __name__=='__main__':unittest.main()
