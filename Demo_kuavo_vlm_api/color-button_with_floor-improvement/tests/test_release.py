import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import types
import unittest
import tempfile
import os
import shlex
import subprocess
from unittest.mock import patch
from collections import deque

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'upstream'))
from button_contract import FrameConfirmation, adapted_gate, valid_proof, BACKEND, SCHEMA
from elevator_button_floor_main import EvidenceGate
from extra_color_node import ColorExtraNode
from launcher import readiness, lower_bootstrap, original
import launcher
from floor_filter import BACKEND as FLOOR_BACKEND, SCHEMA as FLOOR_SCHEMA, config_digest, DEFAULTS


def payload():
    frame = FrameConfirmation()
    value, proof = frame.observe(True, 100.10, 2, 100., 100.11)
    return {'schema_version': SCHEMA, 'mode': 'button_verify', 'button_backend': BACKEND,
            'message_type': 'result', 'request_id': 'req', 'sequence': 1, 'image_stamp': 100.10,
            'status': 'ok', 'button_lit': value, 'button_confirmation': proof,
            'confidence': {'button_lit': None}}


class FrameTests(unittest.TestCase):
    def test_first_fresh_positive_is_immediately_confirmed(self):
        frame = FrameConfirmation()
        value, proof = frame.observe(True,100.05,1,100,100.06)
        self.assertIs(value,True)
        self.assertEqual(proof['frame_revisions'],[1])
        self.assertEqual(proof['confirmed_frames'],1)

    def test_each_frame_uses_its_own_state(self):
        for middle in (None, False):
            frame = FrameConfirmation()
            self.assertIs(frame.observe(True,100.05,1,100,100.06)[0],True)
            self.assertIs(frame.observe(middle,100.10,2,100,100.11)[0],middle)
            self.assertIs(frame.observe(True,100.15,3,100,100.16)[0],True)

    def test_duplicate_timestamp_cannot_confirm(self):
        frame = FrameConfirmation()
        frame.observe(True,100.05,1,100,100.06)
        self.assertIsNone(frame.observe(True,100.05,2,100,100.07)[0])
        self.assertIs(frame.observe(True,100.10,3,100,100.11)[0],True)

    def test_single_frame_does_not_require_adjacent_callbacks(self):
        frame = FrameConfirmation()
        frame.observe(True,100.05,1,100,100.06)
        self.assertIs(frame.observe(True,100.10,3,100,100.11)[0],True)

    def test_no_confirmation_delay_after_a_gap(self):
        frame = FrameConfirmation()
        frame.observe(True,100.05,1,100,100.06)
        self.assertIs(frame.observe(True,100.50,2,100,100.51)[0],True)

    def test_old_stale_and_future_frames_rejected(self):
        for stamp, now in ((99.9,100.1),(100.05,101),(102,100.1),(float('nan'),100.1)):
            self.assertIsNone(FrameConfirmation().observe(True,stamp,1,100,now)[0])

    def test_new_request_needs_its_own_new_image(self):
        frame = FrameConfirmation()
        self.assertIsNone(frame.observe(True,100.05,1,100.07,100.11)[0])
        self.assertIs(frame.observe(True,100.10,2,100.07,100.11)[0],True)


class GateTests(unittest.TestCase):
    def gate(self, mode='button_verify', field='button_lit', expected=True):
        gate = adapted_gate(EvidenceGate)(15,.72,1,0)
        gate.arm(mode,field,expected,100,request_id='req')
        return gate

    def test_new_contract_accepts_real_proof_without_confidence(self):
        self.assertTrue(self.gate().feed(json.dumps(payload()),100.12))

    def test_old_api_packet_never_bypasses_new_gate(self):
        p = payload();p.update(schema_version=1,confidence={'button_lit':1.0})
        self.assertFalse(self.gate().feed(json.dumps(p),100.12))

    def test_bad_proofs_are_rejected(self):
        mutations = [lambda p:p['button_confirmation'].update(frame_stamps=[100.1,100.1]),
                     lambda p:p['button_confirmation'].update(frame_revisions=[]),
                     lambda p:p['button_confirmation'].update(raw_states=[False]),
                     lambda p:p['button_confirmation'].update(confirmed_frames=2),
                     lambda p:p.update(schema_version=3,button_backend='roi_color_two_frame_v1'),
                     lambda p:p.update(button_backend='other'),
                     lambda p:p.update(image_stamp=100.11),
                     lambda p:p.update(button_lit=None)]
        for mutate in mutations:
            p=payload();mutate(p)
            self.assertFalse(self.gate().feed(json.dumps(p),100.12))

    def test_old_request_heartbeat_and_repeated_seq_are_not_new_evidence(self):
        p=payload();p['request_id']='old'
        self.assertFalse(self.gate().feed(json.dumps(p),100.12))
        p=payload();p['message_type']='heartbeat'
        self.assertFalse(self.gate().feed(json.dumps(p),100.12))
        gate=self.gate();self.assertTrue(gate.feed(json.dumps(payload()),100.12))
        self.assertFalse(gate.feed(json.dumps(payload()),100.13))

    def test_stale_and_old_stage_proof_rejected(self):
        self.assertFalse(self.gate().feed(json.dumps(payload()),101))
        gate=self.gate();gate.arm('button_verify','button_lit',True,100.11,request_id='req')
        self.assertFalse(gate.feed(json.dumps(payload()),100.12))

    def test_floor_and_door_retain_original_confidence_gate(self):
        for mode,field,value,schema in [('floor_monitor','floor',15,1),('door_monitor','door_closed',False,2)]:
            gate=self.gate(mode,field,value)
            gate.arm(mode,field,value,100,request_id='req',schema_version=schema)
            p={'mode':mode,'request_id':'req','schema_version':schema,'status':'ok','sequence':1,
               'image_stamp':100.1,field:value,'confidence':{field:.5}}
            self.assertFalse(gate.feed(json.dumps(p),100.12))
            p['sequence']=2;p['image_stamp']=100.15;p['confidence'][field]=.9
            self.assertTrue(gate.feed(json.dumps(p),100.16))


class NodeTests(unittest.TestCase):
    def node(self):
        node=ColorExtraNode.__new__(ColorExtraNode)
        node.lock=threading.Lock();node.confirmation=FrameConfirmation();node.request_epoch=1
        node.request={'mode':'button_verify','request_id':'req','not_before_stamp':100}
        node.frames=deque([(None,100.05,1),(None,100.10,2)])
        node.timing=None;node.config={'camera':{'yuv422_layout':'yuyv'}}
        node.cv2=node.np=node.bridge=None
        node.rospy=types.SimpleNamespace(Time=types.SimpleNamespace(now=lambda:types.SimpleNamespace(to_sec=lambda:100.12)))
        node.output=[];node.publish=lambda req,stamp,result:node.output.append({**result,'image_stamp':stamp})
        return node

    def test_button_never_calls_api_and_first_true_is_released(self):
        node=self.node();node.client=types.SimpleNamespace(complete=lambda *a:(_ for _ in ()).throw(AssertionError('API called')))
        with patch('extra_color_node.decode_image',return_value=object()),patch('extra_color_node.analyze',return_value=({'prediction':True,'reason':'red_emission_area'},None,None)):
            node.run_once();node.run_once()
        self.assertIs(node.output[0]['button_lit'],True)
        self.assertIs(node.output[1]['button_lit'],True)
        self.assertIsNone(node.output[1]['confidence']['button_lit'])
        self.assertTrue(valid_proof(node.output[1],100.12,100))

    def test_floor_delegates_to_original_implementation(self):
        node=self.node();node.request['mode']='floor_monitor'
        with patch('elevator_extra_perception.ExtraPerceptionNode.run_once',return_value='unchanged') as method:
            self.assertEqual(node.run_once(),'unchanged');method.assert_called_once()

    def test_request_switch_during_decode_drops_result(self):
        node=self.node()
        def decode(*args):
            node.request_epoch+=1
            return object()
        with patch('extra_color_node.decode_image',side_effect=decode),patch('extra_color_node.analyze',return_value=({'prediction':True},None,None)):
            node.run_once()
        self.assertEqual(node.output,[])

    def test_decode_error_cannot_reuse_previous_positive(self):
        node=self.node();node.confirmation.observe(True,100.02,0,100,100.03)
        with patch('extra_color_node.decode_image',side_effect=ValueError('bad image')):
            node.run_once()
        self.assertIsNone(node.output[0]['button_lit'])
        self.assertEqual(node.output[0]['status'],'error')
        self.assertIsNone(node.output[0]['button_confirmation'])


class LaunchTests(unittest.TestCase):
    def test_ros_setup_keeps_memory_entry_and_both_loaded_environments(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)/'robot workspace'
            workspace.mkdir()
            distro, setup = workspace/'ros setup.bash', workspace/'workspace setup.bash'
            distro.write_text('export TEST_ROS_LOADED=yes\n')
            setup.write_text('export TEST_WORKSPACE_LOADED=yes\n')
            code = ("import os,json; print(json.dumps({k:os.environ.get(k) for k in "
                    "['TEST_ROS_LOADED','TEST_WORKSPACE_LOADED','KUAVO_ELEVATOR_ENV_READY']}))")
            completed = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                launcher.ROS_ENV_SCRIPT, 'test-elevator-env', str(distro), str(setup), str(workspace), code],
                capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(completed.stdout), {'TEST_ROS_LOADED':'yes',
                'TEST_WORKSPACE_LOADED':'yes', 'KUAVO_ELEVATOR_ENV_READY':str(workspace)})

    def test_failed_workspace_setup_never_executes_lower_code(self):
        with tempfile.TemporaryDirectory() as directory:
            setup = Path(directory)/'setup.bash'
            setup.write_text('return 9\n')
            completed = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                launcher.ROS_ENV_SCRIPT, 'test-elevator-env', str(setup), str(setup), directory,
                "raise RuntimeError('must never execute')"], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 9)
            self.assertNotIn('must never execute', completed.stderr)

    def test_lower_command_preserves_adapter_when_ros_environment_is_not_loaded(self):
        instance = launcher.Launcher(types.SimpleNamespace(lower_host='kuavo_master'))
        with patch.dict(os.environ, {'ROS_MASTER_URI':'http://kuavo_master:11311'}):
            command = shlex.split(instance.lower_command(['--check-runtime'], locked=True))
        self.assertEqual(command[:5], ['cd', original.BOOK, '&&', 'exec', 'flock'])
        self.assertIn(launcher.ROS_ENV_SCRIPT, command)
        script_index = command.index(launcher.ROS_ENV_SCRIPT)
        self.assertEqual(command[script_index+1:script_index+5], ['elevator-color-ros-env',
            '/opt/ros/noetic/setup.bash','/home/lab/kuavo-ros-control/devel/setup.bash','/home/lab/kuavo-ros-control'])
        self.assertIn('elevator_color_lower', command[script_index+5])

    def test_site_metadata_changes_only_button_point_and_retains_process_check(self):
        metadata = {'config': {'points': {'button': {'x': .11, 'y': .976, 'theta': 2.0},
                                         'origin': {'x': -3.465}}, 'head': {'floor': {'yaw': -20}}},
                    'active_pids': [12345]}
        output = io.StringIO()
        with patch('launcher.base_metadata_code', return_value='print(%r)' % json.dumps(metadata)):
            with contextlib.redirect_stdout(output):
                exec(launcher.remote_metadata_code(), {})
        expected = copy.deepcopy(metadata)
        expected['config']['points']['button'] = launcher.BUTTON_POSE
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertEqual(metadata['config']['points']['button']['x'], .11)

    def test_lower_execution_uses_site_point_without_modifying_snapshot(self):
        from elevator_button_floor_main import DEFAULT_CONFIG
        before = copy.deepcopy(DEFAULT_CONFIG)
        with patch.object(original, 'ENTRY', str(ROOT/'upstream/elevator_button_floor_main.py')):
            code = lower_bootstrap([])
        output = io.StringIO()
        with patch.object(sys, 'argv', sys.argv[:]), patch.dict(sys.modules):
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                exec(code, {})
            self.assertEqual(stopped.exception.code, 0)
            active_config = sys.modules['elevator_color_lower'].DEFAULT_CONFIG
            expected = copy.deepcopy(before)
            expected['points']['button'] = launcher.BUTTON_POSE
            self.assertEqual(active_config, expected)
        self.assertEqual(DEFAULT_CONFIG, before)
        self.assertIn("'x': -0.039", output.getvalue())

    def test_button_pose_rejects_invalid_coordinates(self):
        invalid = [{'x': True, 'y': .914, 'theta': 1},
                   {'x': float('nan'), 'y': .914, 'theta': 1},
                   {'x': -.039, 'y': .914, 'theta': 181},
                   {'x': -.039, 'y': .914}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'pose.json'
            for pose in invalid:
                path.write_text(json.dumps(pose), encoding='utf-8')
                with self.assertRaises(ValueError):
                    launcher.load_button_pose(path)

    def test_old_extra_readiness_is_rejected(self):
        values={'door':({'schema_version':2,'mode':'idle','status':'idle','stage_timing_version':1},10),
                'extra':({'mode':'idle','status':'idle','image_received':True,'image_age_sec':.1},10),
                'ready':({'schema_version':1,'status':'ready','capabilities':['button_verify','floor_monitor'],
                          'stage_timing_version':1,'inference_hz':1.0},10),'camera':(True,10)}
        self.assertIn('旧按钮',readiness(values,10))
        values['ready'][0].update(button_backend=BACKEND,button_schema_version=SCHEMA,button_confirmations=1)
        self.assertIn('楼层',readiness(values,10))
        values['ready'][0].update(floor_backend=FLOOR_BACKEND,floor_schema_version=FLOOR_SCHEMA,
                                  floor_filter_config_digest=config_digest(DEFAULTS))
        self.assertIsNone(readiness(values,10))

    def test_old_runtime_snapshot_and_door_paths_unchanged(self):
        self.assertEqual(str(original.DOOR_START),'/media/data/vlm/Demo_vlm2_kuavo_api/deploy/run_ros_api.sh')
        self.assertEqual(str(original.QUANT_ROOT),'/media/data/vlm/elevator_button_floor_demo')
        code=lower_bootstrap(['--mock','--target-floor','15'])
        compile(code,'<lower_bootstrap>','exec')
        self.assertLess(len(code.encode()),120000)


if __name__=='__main__':
    unittest.main()
