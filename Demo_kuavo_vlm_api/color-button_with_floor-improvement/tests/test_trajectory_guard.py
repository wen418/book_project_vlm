import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'upstream'))
from floor_filter import FloorTracker,DEFAULTS,SCHEMA,BACKEND,valid_floor_proof,adapted_floor_gate
from button_contract import adapted_gate
from elevator_button_floor_main import EvidenceGate


def descending():
    tracker=FloorTracker()
    for raw,stamp in [(21,100),(21,101.5),(20,105.5),(19,109.5),(18,113.5),(17,117.5)]:
        tracker.observe(raw,stamp,.9,.72,99)
    assert tracker.direction==-1 and tracker.anchor==17
    return tracker


def as_packet(value,proof,stamp,sequence=1):
    return {'schema_version':SCHEMA,'floor_backend':BACKEND,'floor':value,'floor_raw':value,
            'floor_filter':proof,'status':'ok' if value is not None else 'uncertain','mode':'floor_monitor',
            'request_id':'test','sequence':sequence,'image_stamp':stamp,'confidence':{'floor':.9}}


class TrajectoryTests(unittest.TestCase):
    def test_direction_requires_multiple_distinct_floor_changes(self):
        tracker=FloorTracker()
        for raw,stamp in [(21,100),(21,101.5),(20,105.5)]:tracker.observe(raw,stamp,.9,.72,99)
        self.assertEqual(tracker.direction,0)
        tracker.observe(19,109.5,.9,.72,99)
        self.assertEqual(tracker.direction,-1)

    def test_repeated_reverse_misread_does_not_poison_baseline(self):
        tracker=descending()
        for stamp in (119,120.5):
            value,proof=tracker.observe(18,stamp,.99,.72,99)
            self.assertIsNone(value);self.assertEqual(proof['reason'],'opposite_to_confirmed_direction')
            self.assertEqual(tracker.anchor,17)
        self.assertEqual(tracker.observe(16,121.5,.9,.72,99)[0],16)
        self.assertEqual(tracker.observe(15,125.5,.9,.72,99)[0],15)

    def test_global_history_catches_chained_adjacent_fast_jumps(self):
        tracker=FloorTracker()
        for raw,stamp in [(19,100),(19,101.5),(18,103),(17,105.5)]:
            tracker.observe(raw,stamp,.9,.72,99)
        value,proof=tracker.observe(16,107,.9,.72,99)
        self.assertIsNone(value);self.assertEqual(proof['reason'],'history_inconsistent')

    def test_history_does_not_limit_total_observed_trip_to_three_floors(self):
        tracker=descending()
        self.assertEqual(tracker.observe(16,121.5,.9,.72,99)[0],16)
        self.assertEqual(tracker.observe(15,125.5,.9,.72,99)[0],15)
        self.assertEqual(tracker.observe(14,129.5,.9,.72,99)[0],14)

    def test_real_reversal_requires_new_coherent_trajectory(self):
        tracker=descending()
        for raw,stamp in [(18,119),(18,120.5),(19,123)]:
            self.assertIsNone(tracker.observe(raw,stamp,.9,.72,99)[0])
        value,proof=tracker.observe(19,124.5,.9,.72,99)
        self.assertEqual(value,19);self.assertEqual(proof['reason'],'recovered')
        self.assertEqual(tracker.direction,1)
        self.assertTrue(valid_floor_proof(as_packet(value,proof,124.5),DEFAULTS,.72,99))

    def test_repeating_one_reverse_number_does_not_unlock_after_timeout(self):
        tracker=descending()
        for stamp in (119,121,123,125,127,129,131,133):
            self.assertIsNone(tracker.observe(18,stamp,.99,.72,99)[0])

    def test_bad_initial_baseline_can_recover_without_rewriting_numbers(self):
        tracker=FloorTracker()
        tracker.observe(18,100,.9,.72,99);tracker.observe(18,101.5,.9,.72,99)
        for raw,stamp in [(15,103),(15,104.5),(14,107)]:
            self.assertIsNone(tracker.observe(raw,stamp,.9,.72,99)[0])
        value,proof=tracker.observe(14,108.5,.9,.72,99)
        self.assertEqual(value,14);self.assertEqual(proof['reason'],'recovered')
        self.assertEqual(proof['raw_floor'],14)

    def test_low_confidence_breaks_quarantined_track(self):
        tracker=descending()
        tracker.observe(18,119,.9,.72,99);tracker.observe(18,120.5,.9,.72,99)
        tracker.observe(19,123,.2,.72,99)
        self.assertEqual(tracker.quarantine,[])
        self.assertIsNone(tracker.observe(19,124.5,.9,.72,99)[0])

    def test_stationary_floor_does_not_imply_direction(self):
        tracker=FloorTracker()
        for stamp in range(100,140):tracker.observe(19,stamp,.9,.72,99)
        self.assertEqual(tracker.direction,0)
        self.assertEqual(tracker.anchor,19)

    def test_ascending_direction_is_symmetric(self):
        tracker=FloorTracker()
        for raw,stamp in [(15,100),(15,101.5),(16,105.5),(17,109.5)]:tracker.observe(raw,stamp,.9,.72,99)
        self.assertEqual(tracker.direction,1)
        self.assertIsNone(tracker.observe(16,111,.9,.72,99)[0])

    def test_new_request_clears_direction_and_recovery_candidates(self):
        tracker=descending();tracker.observe(18,119,.9,.72,99);tracker.reset()
        self.assertEqual(tracker.direction,0);self.assertEqual(tracker.quarantine,[])
        self.assertIsNone(tracker.observe(20,200,.9,.72,199)[0])
        self.assertEqual(tracker.observe(20,201.5,.9,.72,199)[0],20)

    def test_recovery_proof_cannot_bypass_target_double_confirmation(self):
        tracker=descending()
        for raw,stamp in [(18,119),(18,120.5),(19,123)]:tracker.observe(raw,stamp,.9,.72,99)
        value,proof=tracker.observe(19,124.5,.9,.72,99)
        gate=adapted_floor_gate(adapted_gate(EvidenceGate))(15,.72,1,0)
        gate.arm('floor_monitor','floor',19,99,request_id='test')
        self.assertFalse(gate.feed(json.dumps(as_packet(value,proof,124.5)),124.6))
        value,proof=tracker.observe(19,126,.9,.72,99)
        self.assertTrue(gate.feed(json.dumps(as_packet(value,proof,126,2)),126.1))

    def test_malformed_recovery_and_reference_history_rejected(self):
        tracker=descending()
        value,proof=tracker.observe(16,121.5,.9,.72,99)
        p=as_packet(value,proof,121.5);p['floor_filter']['reference_history']=[None]
        self.assertFalse(valid_floor_proof(p,DEFAULTS,.72,99))
        tracker=descending()
        for raw,stamp in [(18,119),(18,120.5),(19,123)]:tracker.observe(raw,stamp,.9,.72,99)
        value,proof=tracker.observe(19,124.5,.9,.72,99)
        p=as_packet(value,proof,124.5);p['floor_filter']['recovery_history'][1]['floor']=10
        self.assertFalse(valid_floor_proof(p,DEFAULTS,.72,99))


if __name__=='__main__':unittest.main()
