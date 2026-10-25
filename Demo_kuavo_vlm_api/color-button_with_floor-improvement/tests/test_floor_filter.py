import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'upstream'))
from floor_filter import FloorTracker,DEFAULTS,SCHEMA,BACKEND,adapted_floor_gate,valid_floor_proof,allowed_delta
from button_contract import adapted_gate,FrameConfirmation
from elevator_button_floor_main import EvidenceGate


def initialized():
    tracker=FloorTracker()
    assert tracker.observe(19,100,.9,.72,99)[0] is None
    assert tracker.observe(19,101.5,.9,.72,99)[0]==19
    return tracker


def packet():
    tracker=FloorTracker();tracker.observe(19,100,.9,.72,99)
    floor,proof=tracker.observe(19,101.5,.9,.72,99)
    return {'schema_version':SCHEMA,'floor_backend':BACKEND,'floor':floor,'floor_raw':19,'floor_filter':proof,
            'status':'ok','mode':'floor_monitor','request_id':'floor_req','sequence':1,'image_stamp':101.5,
            'confidence':{'floor':.9}}


class TrackerTests(unittest.TestCase):
    def test_initial_reading_requires_independent_confirmation(self):
        tracker=FloorTracker()
        self.assertIsNone(tracker.observe(19,100,.9,.72,99)[0])
        self.assertEqual(tracker.observe(19,101.5,.9,.72,99)[0],19)

    def test_same_floor_can_remain_for_arbitrarily_long(self):
        tracker=initialized()
        for stamp in range(103,204,2):
            self.assertEqual(tracker.observe(19,stamp,.9,.72,99)[0],19)

    def test_19_to_13_is_not_accepted_or_corrected(self):
        tracker=initialized()
        value,proof=tracker.observe(13,103,.99,.72,99)
        self.assertIsNone(value);self.assertEqual(proof['reason'],'implausible_jump')
        self.assertEqual(tracker.anchor,19);self.assertEqual(tracker.anchor_stamp,101.5)

    def test_range_expands_from_last_trusted_capture_time(self):
        tracker=initialized()
        self.assertIsNone(tracker.observe(13,103,.99,.72,99)[0])
        self.assertIsNone(tracker.observe(13,104.5,.99,.72,99)[0])
        value,proof=tracker.observe(17,106,.9,.72,99)
        self.assertEqual(value,17);self.assertEqual(proof['allowed_delta'],2)
        self.assertEqual(tracker.observe(17,107.5,.9,.72,99)[0],17)

    def test_seconds_not_number_of_failures_controls_range(self):
        tracker=initialized()
        for i in range(1,20):
            value,proof=tracker.observe(13,101.5+i*.1,.9,.72,99)
            self.assertIsNone(value);self.assertEqual(proof['allowed_delta'],1)

    def test_up_and_down_are_both_supported(self):
        for next_floor in (18,20):
            tracker=initialized()
            self.assertEqual(tracker.observe(next_floor,103,.9,.72,99)[0],next_floor)
            self.assertEqual(tracker.observe(next_floor,104.5,.9,.72,99)[0],next_floor)

    def test_plausible_ordinary_reading_does_not_wait_for_second_frame(self):
        tracker=initialized()
        self.assertEqual(tracker.observe(18,103,.9,.72,99)[0],18)
        self.assertEqual(tracker.observe(19,104.5,.9,.72,99)[0],19)
        self.assertEqual(tracker.observe(19,106,.9,.72,99)[0],19)

    def test_unknown_low_confidence_and_invalid_types_never_update_anchor(self):
        for raw,confidence in ((None,0),(True,.9),('18',.9),(18,.3),(18,float('nan')),(200,.9)):
            tracker=initialized()
            self.assertIsNone(tracker.observe(raw,103,confidence,.72,99)[0])
            self.assertEqual((tracker.anchor,tracker.anchor_stamp),(19,101.5))

    def test_duplicate_and_very_close_images_do_not_supply_confirmation(self):
        tracker=FloorTracker()
        tracker.observe(19,100,.9,.72,99)
        self.assertIsNone(tracker.observe(19,100,.9,.72,99)[0])
        self.assertIsNone(tracker.observe(19,100.01,.9,.72,99)[0])
        self.assertEqual(tracker.observe(19,101,.9,.72,99)[0],19)

    def test_reset_clears_old_stage(self):
        tracker=initialized();tracker.reset()
        self.assertIsNone(tracker.observe(13,200,.9,.72,199)[0])
        self.assertEqual(tracker.observe(13,201.5,.9,.72,199)[0],13)

    def test_long_gap_does_not_instantly_unlock_a_large_jump(self):
        tracker=initialized()
        value,proof=tracker.observe(13,120,.9,.72,99)
        self.assertIsNone(value);self.assertEqual(proof['required_confirmations'],3)
        self.assertIsNone(tracker.observe(13,121.5,.9,.72,99)[0])
        value,proof=tracker.observe(13,123,.9,.72,99)
        self.assertEqual(value,13);self.assertEqual(proof['reason'],'reacquired')

    def test_expansion_is_bounded(self):
        self.assertEqual(allowed_delta(1.5,DEFAULTS),1)
        self.assertEqual(allowed_delta(4.5,DEFAULTS),2)
        self.assertEqual(allowed_delta(8.5,DEFAULTS),3)
        self.assertEqual(allowed_delta(10000,DEFAULTS),3)

    def test_old_stage_or_out_of_order_input_is_ignored(self):
        tracker=initialized()
        self.assertIsNone(tracker.observe(13,100,.9,.72,99)[0])
        self.assertIsNone(tracker.observe(19,98,.9,.72,99)[0])
        self.assertEqual(tracker.anchor_stamp,101.5)

    def test_bad_config_is_rejected(self):
        for change in ({'seconds_per_floor':0},{'bootstrap_confirmations':1},{'max_jump_floors':20}):
            with self.assertRaises(ValueError):FloorTracker({**DEFAULTS,**change})


class FloorGateTests(unittest.TestCase):
    def gate(self):
        gate=adapted_floor_gate(adapted_gate(EvidenceGate))(15,.72,1,0)
        gate.arm('floor_monitor','floor',19,99,request_id='floor_req')
        return gate

    def test_filtered_confirmed_packet_passes(self):
        self.assertTrue(self.gate().feed(json.dumps(packet()),103.2))

    def test_two_target_readings_required_after_baseline(self):
        tracker=initialized();gate=self.gate()
        gate.arm('floor_monitor','floor',18,99,request_id='floor_req')
        for seq,stamp in [(1,103),(2,104.5)]:
            value,proof=tracker.observe(18,stamp,.9,.72,99)
            p={**packet(),'sequence':seq,'image_stamp':stamp,'floor':value,'floor_filter':proof}
            self.assertEqual(gate.feed(json.dumps(p),stamp+.1),seq==2)

    def test_one_adjacent_wrong_target_then_other_floor_cannot_pass(self):
        tracker=initialized();gate=self.gate()
        gate.arm('floor_monitor','floor',18,99,request_id='floor_req')
        for seq,stamp,raw in [(1,103,18),(2,104.5,19)]:
            value,proof=tracker.observe(raw,stamp,.9,.72,99)
            p={**packet(),'sequence':seq,'image_stamp':stamp,'floor':value,'floor_filter':proof}
            self.assertFalse(gate.feed(json.dumps(p),stamp+.1))

    def test_legacy_raw_api_packet_cannot_pass(self):
        p=packet();p['schema_version']=1;p.pop('floor_filter')
        self.assertFalse(self.gate().feed(json.dumps(p),103.2))

    def test_malformed_confidence_history_and_jump_proofs_rejected(self):
        changes=[lambda p:p['floor_filter'].update(confirmation_stamps=[103,103]),
                 lambda p:p['floor_filter'].update(confirmation_confidences=[.2,.9]),
                 lambda p:p['floor_filter'].update(observed_floors=[18,19]),
                 lambda p:p['floor_filter'].update(anchor_floor=25),
                 lambda p:p['floor_filter'].update(config_digest='wrong'),
                 lambda p:p.update(floor_backend='old')]
        for change in changes:
            p=packet();change(p)
            self.assertFalse(self.gate().feed(json.dumps(p),103.2))

    def test_duplicate_heartbeat_wrong_request_and_old_frame_rejected(self):
        for change in (lambda p:p.update(message_type='heartbeat'),lambda p:p.update(request_id='old')):
            p=packet();change(p);self.assertFalse(self.gate().feed(json.dumps(p),103.2))
        self.assertFalse(self.gate().feed(json.dumps(packet()),120))
        gate=self.gate();self.assertTrue(gate.feed(json.dumps(packet()),103.2))
        self.assertFalse(gate.feed(json.dumps(packet()),103.3))

    def test_original_door_confidence_and_single_frame_button_are_retained(self):
        Gate=adapted_floor_gate(adapted_gate(EvidenceGate))
        gate=Gate(15,.72,1,0);gate.arm('door_monitor','door_closed',False,99,schema_version=2)
        door={'schema_version':2,'mode':'door_monitor','status':'ok','sequence':1,'image_stamp':103,
              'door_closed':False,'confidence':{'door_closed':.9}}
        self.assertTrue(gate.feed(json.dumps(door),103.2))
        gate.arm('button_verify','button_lit',True,99,request_id='button')
        value,proof=FrameConfirmation().observe(True,103,1,99,103.1)
        from button_contract import SCHEMA as BUTTON_SCHEMA,BACKEND as BUTTON_BACKEND
        button={'schema_version':BUTTON_SCHEMA,'button_backend':BUTTON_BACKEND,'mode':'button_verify','request_id':'button',
                'status':'ok','sequence':1,'image_stamp':103,'button_lit':value,'button_confirmation':proof}
        self.assertTrue(gate.feed(json.dumps(button),103.2))


if __name__=='__main__':unittest.main()
