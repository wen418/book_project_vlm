#!/usr/bin/python3
"""Isolated launcher adapter; original launcher and lower files are never overwritten."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import zlib

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('original_elevator_launcher', ROOT/'upstream/elevator_demo_launcher.py')
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
from button_contract import BACKEND, SCHEMA
from floor_filter import BACKEND as FLOOR_BACKEND, SCHEMA as FLOOR_SCHEMA, config_digest, validate_config

FLOOR_CONFIG=validate_config(json.loads((ROOT/'floor_filter_config.json').read_text()))

original.DEFAULT_ROOT = ROOT
original.EXTRA_START = ROOT/'run_extra_perception.sh'
# Quantification code and door node stay on their original, verified paths.
base_readiness = original.readiness_reason


def readiness(values, now, camera_age=2.0):
    reason = base_readiness(values, now, camera_age)
    if reason:
        return reason
    ready = values['ready'][0]
    if (ready.get('button_backend') != BACKEND or ready.get('button_schema_version') != SCHEMA
            or ready.get('button_confirmations') != 1):
        return '旧按钮感知不兼容，请先退出旧附加感知节点；不能混用'
    if (ready.get('floor_backend')!=FLOOR_BACKEND or ready.get('floor_schema_version')!=FLOOR_SCHEMA
            or ready.get('floor_filter_config_digest')!=config_digest(FLOOR_CONFIG)):
        return '楼层时序过滤接口或参数不兼容，请退出旧附加感知节点'
    return None


original.readiness_reason = readiness
BaseLauncher = original.Launcher


def lower_bootstrap(arguments):
    files = {'entry': (ROOT/'upstream/elevator_button_floor_main.py').read_text(),
             'contract': (ROOT/'button_contract.py').read_text(), 'arguments': arguments,
             'floor_contract': (ROOT/'floor_filter.py').read_text(), 'floor_config': FLOOR_CONFIG,
             'entry_path': original.ENTRY}
    files['original_sha256'] = hashlib.sha256((ROOT/'upstream/elevator_button_floor_main.py').read_bytes()).hexdigest()
    encoded = base64.b64encode(zlib.compress(json.dumps(files).encode())).decode()
    return ("import base64,zlib,json,types,sys,hashlib;from pathlib import Path;"
            "p=json.loads(zlib.decompress(base64.b64decode(%r)));" % encoded +
            "assert hashlib.sha256(Path(p['entry_path']).read_bytes()).hexdigest()==p['original_sha256'],"
            "'下位机入口已变化；请先更新本方案快照，不使用旧点位';"
            "sys.path.insert(0,str(Path(p['entry_path']).parent));"
            "c=types.ModuleType('color_button_contract');exec(compile(p['contract'],'<button_contract>','exec'),c.__dict__);"
            "f=types.ModuleType('floor_guard_contract');exec(compile(p['floor_contract'],'<floor_filter>','exec'),f.__dict__);"
            "m=types.ModuleType('elevator_color_lower');m.__file__=p['entry_path'];"
            "sys.modules[m.__name__]=m;exec(compile(p['entry'],p['entry_path'],'exec'),m.__dict__);"
            "m.EvidenceGate=f.adapted_floor_gate(c.adapted_gate(m.EvidenceGate),p['floor_config']);"
            "sys.argv=[p['entry_path']]+p['arguments'];sys.exit(m.main())")


class Launcher(BaseLauncher):
    def valid_image(self, message):
        return super().valid_image(message) and (message.width, message.height) == (848, 480)

    def start_perception(self):
        publishers = dict(self.rosgraph.Master(self.rospy.get_name()).getSystemState()[0])
        topic = self.config['perception']['extra_state_topic']
        if publishers.get(topic):
            raise RuntimeError('附加感知话题已有发布者，请先退出旧节点再运行新方案：'+str(publishers[topic]))
        return super().start_perception()

    def lower_command(self, arguments, locked=False):
        command = ['env', 'ROS_MASTER_URI='+os.environ['ROS_MASTER_URI'], 'ROS_IP=192.168.26.1',
                   '/usr/bin/python3','-u','-c',lower_bootstrap(arguments)]
        if locked:
            command = ['flock','-n','-E','73','/tmp/kuavo_elevator_demo.lock',*command]
        return 'cd '+shlex.quote(original.BOOK)+' && exec '+shlex.join(command)


original.Launcher = Launcher


def main():
    manifest = json.loads((ROOT/'manifest.json').read_text())
    for name, digest in manifest['release_files_sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('方案文件校验失败：'+name)
    arguments = sys.argv[1:]
    if '--result-dir' not in arguments and not any(a.startswith('--result-dir=') for a in arguments):
        arguments += ['--result-dir', str(ROOT/'result')]
    return original.main(arguments)


if __name__ == '__main__':
    sys.exit(main())
