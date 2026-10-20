"""Timing provenance, stage isolation and annotation validity; no robot or API."""
import csv
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT.parent/'Demo_vlm2_kuavo_api'))
from stage_timing import StageTiming
from stage_timeline import build_timeline, annotation_alignment
import elevator_quantification as quant
import test_elevator_demo as demo_tests
import test_elevator_quantification as quant_tests
from ros_stream_node import ContinuousRosNode


class TimingTests(unittest.TestCase):
    def timing(self):
        self.now = 1000.
        self.rows = []
        return StageTiming('extra', self.rows.append, lambda:self.now)

    def test_fresh_frame_anchor_uses_actual_receipt_and_capture_times(self):
        t=self.timing()
        t.frame(999.)
        t.begin('button_verify','a',1000)
        self.now=1000.2
        t.frame(999.9)
        self.assertFalse(any(r['event']=='first_fresh_frame_received' for r in self.rows))
        t.frame(1000.1)
        self.now=1000.3
        t.frame(1000.25)
        frames=[r for r in self.rows if r['event']=='first_fresh_frame_received']
        self.assertEqual(len(frames),1)
        self.assertEqual((frames[0]['ros_stamp'],frames[0]['image_stamp']),(1000.2,1000.1))

    def test_cached_frame_is_not_assigned_a_fabricated_later_receipt_time(self):
        t=self.timing()
        self.now=1000.2
        t.frame(1000.1)
        self.now=1000.4
        t.begin('floor_monitor','a',1000)
        self.assertEqual(self.rows[-1]['ros_stamp'],1000.2)
        self.assertEqual(self.rows[-2]['ros_stamp'],1000.4)

    def test_retries_keep_distinct_call_ids_and_old_stage_identity(self):
        t=self.timing();t.begin('button_verify','old',1000)
        context=t.select(1000.1)
        count=[]
        def complete(*args,**kwargs):
            count.append(1)
            if len(count)==1:raise RuntimeError('offline error')
            t.begin('button_verify','new',1000.2)
            return '{}'
        client=t.wrap(NS(complete=complete))
        with self.assertRaises(RuntimeError):client.complete([])
        client.complete([],max_tokens=384)
        t.finish(context,'ok',{'button_lit':True},accepted=False)
        calls=[r for r in self.rows if r['event']=='vlm_request_started']
        self.assertEqual(len({r['api_call_id'] for r in calls}),2)
        self.assertTrue(all(r['request_id']=='old' for r in calls))
        self.assertEqual(self.rows[-1]['request_id'],'old')
        self.assertFalse(self.rows[-1]['accepted'])
        self.assertNotIn('offline error',json.dumps(self.rows))

    def test_extra_real_parse_emits_unlit_after_client_return(self):
        node,published=demo_tests.UpperNodeTests().make_node()
        node.timing=self.timing()
        node.on_request(NS(data=json.dumps(dict(mode='button_verify',request_id='a',not_before_stamp=100))))
        node.frame=(object(),101,1)
        node.client=node.timing.wrap(NS(complete=lambda *a,**k:'{"button_lit":false,"confidence":0.99,"evidence":"unlit"}'))
        with patch('elevator_extra_perception.decode_image',return_value=object()),patch('elevator_extra_perception.encode_image',return_value='image'):
            node.run_once()
        names=[r['event'] for r in self.rows]
        self.assertLess(names.index('vlm_response_received'),names.index('inference_result'))
        self.assertIs(self.rows[-1]['values']['button_lit'],False)
        self.assertIs(published[-1]['button_lit'],False)

    def test_door_aba_request_change_discards_old_result(self):
        for change in (False,True):
            with self.subTest(change=change):
                node=ContinuousRosNode.__new__(ContinuousRosNode)
                node.timing=self.timing()
                node.timing.begin('door_monitor',not_before=999)
                old=node.timing.stage['stage_id']
                node._mode=lambda:'door_monitor'
                node._collect=lambda m:([],['door_closed'],1000,('door',1))
                node._last_signature=None;node._sequence=0;node.max_result_age=15
                node.rospy=NS(Time=NS(now=lambda:NS(to_sec=lambda:1000.5)),loginfo=lambda *a:None,logerr=lambda *a:None)
                def complete(*a,**k):
                    if change:
                        node.timing.begin('idle');node.timing.begin('door_monitor')
                    return '{}'
                client=node.timing.wrap(NS(complete=complete))
                def infer_data_urls(**kwargs):
                    client.complete([])
                    return NS(status='ok',door_closed=False)
                node.pipeline=NS(infer_data_urls=infer_data_urls)
                output=[];node._publish=output.append
                node._run_once()
                self.assertEqual(bool(output),not change)
                self.assertEqual(self.rows[-1]['stage_id'],old)
                self.assertEqual(self.rows[-1]['accepted'],not change)


class TimelineTests(unittest.TestCase):
    def fixture(self):
        events=[dict(event='navigation_start',point='button',ros_stamp=999),
                dict(event='arrival_reported',point='button',ros_stamp=1000),
                dict(event='navigation_arrived',point='button',ros_stamp=1000.1),
                dict(event='head',view='button',ros_stamp=1000.2),
                dict(event='head_reached',view='button',ros_stamp=1001),
                dict(event='observe_start',mode='button_verify',request_id='a',not_before_stamp=1002,ros_stamp=1002.01,expected=True),
                dict(event='observe_confirmed',ros_stamp=1007,result=dict(mode='button_verify',request_id='a'))]
        def e(name,stamp,**kwargs):
            return dict(event=name,source='extra',instance_id='node',stage_id='s',request_id='a',mode='button_verify',ros_stamp=stamp,**kwargs)
        rows=[e('stage_request_received',1002.1),e('first_fresh_frame_received',1002.3,image_stamp=1002.2),
              e('frame_selected',1002.4,image_stamp=1002.2),e('vlm_request_started',1002.5,api_call_id='call'),
              e('vlm_response_received',1003.5,api_call_id='call',status='ok'),
              e('inference_result',1003.6,status='ok',accepted=True,image_stamp=1002.2,values=dict(button_lit=False)),
              e('inference_result',1006,status='ok',accepted=True,image_stamp=1005,values=dict(button_lit=True))]
        return events,rows

    def test_stage_anchors_and_late_or_wrong_requests_are_not_mixed(self):
        events,rows=self.fixture()
        rows.append(dict(rows[-1],request_id='wrong',stage_id='wrong',ros_stamp=1002.1))
        result=build_timeline(events,rows,[],999)
        stage=result['stages'][0];a=stage['anchors']
        self.assertEqual(a['position_arrived']['ros_stamp'],1000)
        self.assertEqual(a['head_settled']['ros_stamp'],1001)
        self.assertEqual(a['first_vlm_response']['ros_stamp'],1003.5)
        self.assertEqual(a['button_unlit_ready']['ros_stamp'],1003.6)
        self.assertEqual(a['first_positive']['ros_stamp'],1006)
        self.assertEqual(a['confirmation']['ros_stamp'],1007)
        self.assertEqual(stage['durations']['first_client_call_s'],1)

    def test_early_press_and_brackets_across_readiness_are_explicit(self):
        events,rows=self.fixture();timeline=build_timeline(events,rows,[])
        for opened,previous,status in [(1001,1000.9,'before_observation'),(1002.2,1002.1,'before_first_fresh_frame'),
                                      (1003,1002.9,'before_ready'),(1004,1003,'straddles_readiness'),
                                      (1004,None,'single_frame_only'),(1004,1003.9,'after_ready')]:
            self.assertEqual(annotation_alignment(timeline,'button',opened,previous)['status'],status)

    def test_absent_telemetry_is_missing_instead_of_estimated(self):
        events,_=self.fixture();stage=build_timeline(events,[],[])['stages'][0]
        self.assertNotIn('first_vlm_request',stage['anchors'])
        self.assertIsNone(stage['durations']['first_client_call_s'])
        self.assertTrue(stage['warnings'])

    def test_unaccepted_or_uncertain_result_cannot_prove_readiness(self):
        events,rows=self.fixture()
        for changed in (dict(accepted=False),dict(status='uncertain')):
            data=[dict(r,**changed) if r['event']=='inference_result' else r for r in rows]
            self.assertNotIn('button_unlit_ready',build_timeline(events,data,[])['stages'][0]['anchors'])

    def test_two_door_stages_have_separate_sessions(self):
        events=[dict(event='door_wait_start',ros_stamp=1000),dict(event='door_confirmed',ros_stamp=1005),
                dict(event='door_wait_start',ros_stamp=1010),dict(event='door_confirmed',ros_stamp=1015)]
        rows=[dict(event='stage_request_received',source='door',instance_id='n',stage_id=str(i),mode='door_monitor',ros_stamp=t)
              for i,t in enumerate((1000.1,1010.1))]
        stages=build_timeline(events,rows,[])['stages']
        self.assertEqual([s['stage_id'] for s in stages],['0','1'])

    def test_final_exit_and_measured_motion_are_in_global_timeline(self):
        events=[dict(event='navigation_start',point='exit',ros_stamp=1010),
                dict(event='motion_started',point='exit',onset_ros_stamp=1010.2,ros_stamp=1010.5),
                dict(event='arrival_reported',point='exit',ros_stamp=1015),
                dict(event='workflow_completed',scope='full',ros_stamp=1016)]
        result=build_timeline(events,[],[],1000)
        self.assertEqual(result['workflow'][1]['ros_stamp'],1010.2)
        self.assertEqual(result['workflow'][1]['reported_ros_stamp'],1010.5)
        self.assertEqual(result['workflow'][-1]['scope'],'full')

    def test_recorder_requires_both_timing_links_before_returning_ready(self):
        for connections in (0,2):
            with self.subTest(connections=connections),tempfile.TemporaryDirectory() as d:
                session=quant_tests.RecorderTests().session(d)
                session.frames[1000]=b'fake'
                ros=NS(Time=NS(now=lambda:NS(to_sec=lambda:1000)),get_param=lambda *a:None,
                       Subscriber=lambda *a,**k:NS(get_num_connections=lambda:connections,unregister=lambda:None))
                modules={'cv2':NS(),'numpy':NS(),'cv_bridge':NS(CvBridge=lambda:object()),
                         'sensor_msgs.msg':NS(Image=object),'std_msgs.msg':NS(String=object)}
                with patch.dict(sys.modules,modules),patch.object(quant.time,'monotonic',side_effect=[0,0,6]),patch.object(quant.time,'sleep'):
                    if connections==0:
                        with self.assertRaisesRegex(RuntimeError,'遥测'):session.start(ros)
                    else:session.start(ros)
                session.close(130)

    def test_first_available_frame_is_not_treated_as_observed_onset(self):
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d)
            quant.write_json(directory/'button_frames.json',dict(frames=[dict(image_stamp=1002)]))
            result=quant.visual_delay(directory,dict(actual_button_lit_ros_stamp=1002),
                                      dict(button_confirmed_ros_stamp=1008,button_first_positive_ros_stamp=1005),'button')
            self.assertIsNone(result['actual_button_lit_to_recognition_s'])
            self.assertIsNone(result['actual_button_lit_to_first_positive_s'])
            self.assertEqual(result['button_visual_onset_status'],'onset_not_observed')

    def test_telemetry_is_deduplicated_and_never_becomes_accuracy_samples(self):
        with tempfile.TemporaryDirectory() as d:
            session=quant_tests.RecorderTests().session(d)
            row=dict(event='inference_result',schema_version=1,source='extra',instance_id='node',
                     event_sequence=1,stage_id='s',mode='button_verify',request_id='a',ros_stamp=1002,
                     image_stamp=1001,not_before_stamp=1000,status='ok',accepted=True,values=dict(button_lit=False))
            with patch('builtins.print') as output:
                session.on_timing(NS(data=json.dumps(row)));session.on_timing(NS(data=json.dumps(row)))
            output.assert_called_once()
            self.assertEqual(len(session.timing_events),1)
            self.assertFalse(session.observations)
            quant_tests.finish(session)
            self.assertTrue((session.directory/'stage_timeline.html').is_file())
            self.assertEqual(len((session.directory/'perception_timing.jsonl').read_text().splitlines()),1)
            with (session.directory/'labels.csv').open(encoding='utf-8-sig',newline='') as f:
                self.assertEqual(list(csv.DictReader(f)),[])


if __name__=='__main__':unittest.main()
