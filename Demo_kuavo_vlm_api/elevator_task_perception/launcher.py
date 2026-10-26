#!/usr/bin/python3
"""Isolated launcher adapter; original launcher and lower files are never overwritten."""
import base64
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import shlex
import sys
import zlib

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('original_elevator_launcher', ROOT/'upstream/elevator_demo_launcher.py')
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
from button_contract import BACKEND, SCHEMA
from floor_filter import BACKEND as FLOOR_BACKEND, SCHEMA as FLOOR_SCHEMA, config_digest, validate_config
from telemetry_transport import framed_child, preserve_failure
from task_contract import trip

original.Child = framed_child(original.Child)

FLOOR_CONFIG=validate_config(json.loads((ROOT/'floor_filter_config.json').read_text()))


def load_button_pose(path):
    pose = json.loads(Path(path).read_text(encoding='utf-8'))
    if (not isinstance(pose, dict) or set(pose) != {'x', 'y', 'theta'} or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in pose.values()) or
            not -180 <= pose['theta'] <= 180):
        raise ValueError('button_pose.json 必须包含有限数值 x/y/theta，单位米/度')
    return pose


BUTTON_POSE = load_button_pose(ROOT/'button_pose.json')
base_metadata_code = original.remote_metadata_code


def remote_metadata_code():
    # Preserve the original read-only process/config probe, then apply this scheme's point.
    return ("import contextlib,io,json\n"
            "_site_buffer=io.StringIO()\n"
            "with contextlib.redirect_stdout(_site_buffer):\n"
            " exec(%r)\n" % base_metadata_code() +
            "_site_metadata=json.loads(_site_buffer.getvalue())\n"
            "_site_metadata['config']['points']['button']=%r\n" % BUTTON_POSE +
            "print(json.dumps(_site_metadata,ensure_ascii=False))\n")


original.remote_metadata_code = remote_metadata_code
original.DEFAULT_ROOT = ROOT
original.EXTRA_START = ROOT/'run_extra_perception.sh'
original.DOOR_START = ROOT/'run_door_perception.sh'
# Quantification code and door node stay on their original, verified paths.
base_readiness = original.readiness_reason


def readiness(values, now, camera_age=2.0):
    reason = base_readiness(values, now, camera_age)
    if reason:
        return reason
    ready = values['ready'][0]
    if values['door'][0].get('recognition_contract') != 'compact_state_v1':
        return '门节点不是本方案的状态短输出接口'
    if (ready.get('button_backend') != BACKEND or ready.get('button_schema_version') != SCHEMA
            or ready.get('button_confirmations') != 1):
        return '旧按钮感知不兼容，请先退出旧附加感知节点；不能混用'
    if (ready.get('floor_backend')!=FLOOR_BACKEND or ready.get('floor_schema_version')!=FLOOR_SCHEMA
            or ready.get('floor_filter_config_digest')!=config_digest(FLOOR_CONFIG)):
        return '楼层时序过滤接口或参数不兼容，请退出旧附加感知节点'
    return None


original.readiness_reason = readiness
BaseLauncher = original.Launcher

# Load native ROS libraries before running the in-memory lower adapter. Letting
# the old entry load them itself would exec its disk file and discard the adapter.
ROS_ENV_SCRIPT = ('set -e; source "$1"; source "$2"; '
                  'export KUAVO_ELEVATOR_ENV_READY="$3"; '
                  'exec /usr/bin/python3 -u -c "$4"')


def lower_bootstrap(arguments):
    files = {'entry': (ROOT/'upstream/elevator_button_floor_main.py').read_text(),
             'contract': (ROOT/'button_contract.py').read_text(), 'arguments': arguments,
             'floor_contract': (ROOT/'floor_filter.py').read_text(), 'floor_config': FLOOR_CONFIG,
             'transport': (ROOT/'telemetry_transport.py').read_text(),
             'task_contract': (ROOT/'task_contract.py').read_text(),
             'adapter': (ROOT/'lower_adapter.py').read_text(),
             'button_pose': BUTTON_POSE,
             'entry_path': original.ENTRY}
    files['original_sha256'] = 'f371a7e12f49bb023e726499dd14691e232841b11ced6b41c3f78a53c2b8a525'
    encoded = base64.b64encode(zlib.compress(json.dumps(files).encode())).decode()
    return ("import base64,zlib,json,types,sys,hashlib,os;from pathlib import Path;"
            "p=json.loads(zlib.decompress(base64.b64decode(%r)));" % encoded +
            "assert hashlib.sha256(Path(p['entry_path']).read_bytes()).hexdigest()==p['original_sha256'],"
            "'下位机入口已变化；请先更新本方案快照，不使用旧点位';"
            "sys.path.insert(0,str(Path(p['entry_path']).parent));"
            "q=types.ModuleType('task_contract');sys.modules[q.__name__]=q;exec(compile(p['task_contract'],'<task_contract>','exec'),q.__dict__);"
            "c=types.ModuleType('color_button_contract');exec(compile(p['contract'],'<button_contract>','exec'),c.__dict__);"
            "f=types.ModuleType('floor_guard_contract');exec(compile(p['floor_contract'],'<floor_filter>','exec'),f.__dict__);"
            "t=types.ModuleType('elevator_transport');exec(compile(p['transport'],'<telemetry_transport>','exec'),t.__dict__);sys.stdout=t.EventBoundaryWriter(sys.stdout);"
            "m=types.ModuleType('elevator_color_lower');m.__file__=p['entry_path'];"
            "sys.modules[m.__name__]=m;exec(compile(p['entry'],p['entry_path'],'exec'),m.__dict__);"
            "m.DEFAULT_CONFIG['points']['button']=dict(p['button_pose']);"
            "a=types.ModuleType('task_lower_adapter');exec(compile(p['adapter'],'<lower_adapter>','exec'),a.__dict__);a.install(m);"
            "m.EvidenceGate=f.adapted_floor_gate(c.adapted_gate(a.compact_door_gate(m.EvidenceGate)),p['floor_config']);"
            "print('[方案] button='+c.BACKEND+'/schema'+str(c.SCHEMA)+'; floor='+f.BACKEND+'/schema'+str(f.SCHEMA)+'; button_pose='+str(m.DEFAULT_CONFIG['points']['button'])+'; ROS_ENV='+str(os.environ.get('KUAVO_ELEVATOR_ENV_READY')),flush=True);"
            "sys.argv=[p['entry_path']]+p['arguments'];sys.exit(m.main())")


class Launcher(BaseLauncher):
    def preflight(self):
        intent = trip(self.args.current_floor, self.args.target_floor)
        from dual_button import validate_layout
        layout = validate_layout(json.loads((ROOT/'button_layout.json').read_text(encoding='utf-8')))
        single = layout['single_button_floors'].get(str(self.args.current_floor))
        if single and single != intent['button_direction']:
            raise ValueError('本层只有 '+single+' 按钮，不支持本次呼梯方向')
        super().preflight()
        self.config.update(intent)
        print('本次任务：'+str(self.config['current_floor'])+' → '+str(self.config['target_floor'])+
              '；检查 '+self.config['button_direction']+' 按钮；楼层无方向限制', flush=True)

    def close(self):
        if self.recorder is not None:
            close_recording = self.recorder.close

            def with_diagnostics(exit_code):
                try:
                    preserve_failure(self.recorder, exit_code, ROOT/'verification/runtime_failures')
                except Exception as exc:
                    print('故障日志保存失败：'+str(exc), file=sys.stderr, flush=True)
                return close_recording(exit_code)

            self.recorder.close = with_diagnostics
        return super().close()

    def valid_image(self, message):
        return super().valid_image(message) and (message.width, message.height) == (848, 480)

    def start_perception(self):
        publishers = dict(self.rosgraph.Master(self.rospy.get_name()).getSystemState()[0])
        topic = self.config['perception']['extra_state_topic']
        if publishers.get(topic):
            raise RuntimeError('附加感知话题已有发布者，请先退出旧节点再运行新方案：'+str(publishers[topic]))
        if publishers.get(self.config['perception']['door_state_topic']):
            raise RuntimeError('门感知话题已有发布者，请先退出旧节点；本方案使用短输出门节点')
        return super().start_perception()

    def lower_command(self, arguments, locked=False):
        arguments = list(arguments)
        if '--current-floor' not in arguments:
            arguments += ['--current-floor', str(self.args.current_floor)]
        if '--target-floor' not in arguments:
            arguments += ['--target-floor', str(self.args.target_floor)]
        workspace = next(path.parent for path in PurePosixPath(original.BOOK).parents if path.name == 'src')
        command = ['env', 'ROS_MASTER_URI='+os.environ['ROS_MASTER_URI'], 'ROS_IP=192.168.26.1',
                   '/bin/bash', '--noprofile', '--norc', '-c', ROS_ENV_SCRIPT, 'elevator-color-ros-env',
                   '/opt/ros/noetic/setup.bash', str(workspace/'devel/setup.bash'),
                   str(workspace), lower_bootstrap(arguments)]
        if locked:
            command = ['flock','-n','-E','73','/tmp/kuavo_elevator_demo.lock',*command]
        return 'cd '+shlex.quote(original.BOOK)+' && exec '+shlex.join(command)


original.Launcher = Launcher


def main():
    manifest = json.loads((ROOT/'manifest.json').read_text())
    for name, digest in manifest['release_files_sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('方案文件校验失败：'+name)
    for name, digest in manifest['external_dependencies_sha256'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('共享依赖已变化，请重新核对兼容性：'+name)
    arguments = sys.argv[1:]
    if '--result-dir' not in arguments and not any(a.startswith('--result-dir=') for a in arguments):
        arguments += ['--result-dir', str(ROOT/'result')]
    return original.main(arguments)


if __name__ == '__main__':
    sys.exit(main())
