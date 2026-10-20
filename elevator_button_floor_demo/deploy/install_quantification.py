#!/usr/bin/env python3
"""Install over SSH; preserve site config and prefer committed Git versions to backups."""
import argparse
import ast
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import uuid

SOURCE = Path(__file__).resolve().parents[1]
UPPER_ROOT = "/media/data/vlm/elevator_button_floor_demo"
LAUNCH_ROOT = "/media/data/vlm/Demo_kuavo_vlm_api"
LOWER_ENTRY = "/home/lab/kuavo-ros-control/src/kuavo_humanoid_sdk/kuavo_humanoid_sdk/kuavo_strategy_pytree/book/elevator_button_floor_main.py"
SSH_OPTIONS = {}


def backup_source():
    source = (SOURCE / "elevator_button_floor_main.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == "backup_before_update")
    return ast.get_source_segment(source, node) + "\n"


def run(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8", errors="replace")


def remote(host, code):
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    command = "/usr/bin/python3 -c " + shlex.quote("import base64;exec(base64.b64decode(" + repr(encoded) + "))")
    return run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=6",
                *SSH_OPTIONS.get(host, []), host, command])


def upload(host, local, destination):
    run(["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", *SSH_OPTIONS.get(host, []), str(local), host + ":" + destination])


READ_CONFIG = """import ast,json,hashlib
from pathlib import Path
p=Path(%r)
s=p.read_text(encoding='utf-8-sig')
t=ast.parse(s)
n=next(n for n in t.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='DEFAULT_CONFIG' for x in n.targets))
config=ast.literal_eval(n.value)
lines=s.splitlines(keepends=True)
normalized=''.join(lines[:n.lineno-1])+'DEFAULT_CONFIG = None\\n'+''.join(lines[n.end_lineno:])
print(json.dumps(dict(config=config,code_sha256=hashlib.sha256(normalized.encode()).hexdigest()),ensure_ascii=False))
"""


def active_check(host):
    result = remote(host, """from pathlib import Path
import json
names={'elevator_demo_launcher.py','elevator_quantification.py','elevator_button_floor_main.py'}
active=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  a=[v.decode(errors='replace') for v in (p/'cmdline').read_bytes().split(b'\\0') if v]
  if any(Path(x).name in names for x in a[:3]):active.append(p.name)
 except OSError:pass
print(json.dumps(active))
""")
    if json.loads(result):
        raise RuntimeError(host + " 有测试/调度进程运行，停止部署：" + result)


def install_entry(host, staging, target):
    before = json.loads(remote(host, READ_CONFIG % target))
    source = (SOURCE / "elevator_button_floor_main.py").read_text(encoding="utf-8")
    source_tree = ast.parse(source)
    node = next(n for n in source_tree.body if isinstance(n, ast.Assign) and
                any(isinstance(x, ast.Name) and x.id == "DEFAULT_CONFIG" for x in n.targets))
    lines = source.splitlines(keepends=True)
    normalized = ''.join(lines[:node.lineno-1])+'DEFAULT_CONFIG = None\n'+''.join(lines[node.end_lineno:])
    expected_hash = hashlib.sha256(normalized.encode()).hexdigest()
    controller_updated = before["code_sha256"] != expected_hash
    if controller_updated:
        output = remote(host, """import subprocess
subprocess.run(['/usr/bin/python3',%r,'--install-to',%r],check=True)
""" % (staging, target))
        print(output.strip())
    # Update only the explicitly requested rate and single-frame confirmation rules.
    remote(host, backup_source() + """import ast,os,shutil,tempfile
from pathlib import Path
p=Path(%r)
s=p.read_text(encoding='utf-8-sig')
n=next(n for n in ast.parse(s).body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='DEFAULT_CONFIG' for x in n.targets))
c=ast.literal_eval(n.value)
original=__import__('copy').deepcopy(c)
c['extra_perception']['inference_period']=1.0
for name in ('button','floor'):
 c['perception'][name].update(confirmations=1,min_span=0.0)
if c!=original:
 lines=s.splitlines(keepends=True)
 updated=''.join(lines[:n.lineno-1])+'DEFAULT_CONFIG = '+repr(c)+'\\n'+''.join(lines[n.end_lineno:])
 compile(updated,str(p),'exec')
 # The single-file installer already saved the original when it changed code.
 if not %r:backup_before_update(p,'.backup_rate_'+__import__('uuid').uuid4().hex[:8])
 with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=str(p.parent),delete=False) as f:
  f.write(updated)
  temporary=f.name
 shutil.copymode(p,temporary)
 os.replace(temporary,str(p))
""" % (target, controller_updated))
    after = json.loads(remote(host, READ_CONFIG % target))
    expected_config = before["config"]
    expected_config["extra_perception"]["inference_period"] = 1.0
    for name in ("button", "floor"):
        expected_config["perception"][name].update(confirmations=1, min_span=0.0)
    if after["config"] != expected_config:
        raise RuntimeError("现场配置发生变化：" + target)
    if expected_hash != after["code_sha256"]:
        raise RuntimeError("安装代码校验失败：" + target)
    return dict(host=host, path=target, code_sha256=expected_hash,
                site_config_preserved_except=["extra_perception.inference_period", "perception.button", "perception.floor"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upper", default="leju_kuavo@192.168.5.244")
    parser.add_argument("--upper-host-key-alias", help="上位机地址变更时，沿用已验证的旧主机指纹名称")
    parser.add_argument("--lower", default="lab@192.168.5.133")
    parser.add_argument("--lower-jump", help="通过已验证的上位机 SSH 跳转连接下位机")
    parser.add_argument("--lower-jump-host-key-alias", help="跳板机地址变更时，沿用已验证的旧指纹名称")
    parser.add_argument("--lower-host-key-alias", help="沿用已固定的下位机主机指纹名称")
    args = parser.parse_args()
    SSH_OPTIONS[args.upper] = ["-o", "HostKeyAlias="+args.upper_host_key_alias] if args.upper_host_key_alias else []
    SSH_OPTIONS[args.lower] = []
    if args.lower_jump:
        if args.lower_jump_host_key_alias:
            jump = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=6",
                    "-o", "HostKeyAlias="+args.lower_jump_host_key_alias, "-W", "%h:%p", args.lower_jump]
            SSH_OPTIONS[args.lower] += ["-o", "ProxyCommand="+shlex.join(jump)]
        else:
            SSH_OPTIONS[args.lower] += ["-o", "ProxyJump="+args.lower_jump]
    elif args.lower_jump_host_key_alias:
        parser.error("lower-jump-host-key-alias 需要配合 lower-jump")
    if args.lower_host_key_alias:
        SSH_OPTIONS[args.lower] += ["-o", "HostKeyAlias="+args.lower_host_key_alias]
    active_check(args.upper)
    active_check(args.lower)
    # Refuse before touching either machine if the upper's tracked work is unsaved.
    remote(args.upper, """import subprocess
from pathlib import Path
p=Path('/media/data/vlm')
if (p/'.git').exists():
 result=subprocess.run(['git','-C',str(p),'status','--porcelain','--untracked-files=no'],stdout=subprocess.PIPE,check=True,text=True)
 if result.stdout.strip():raise RuntimeError('上位机有未提交的 Git 修改；先提交再部署。')
""")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    staging = "/tmp/elevator_quant_install_" + stamp
    for host in (args.upper, args.lower):
        remote(host, "from pathlib import Path;Path(%r).mkdir()" % staging)
        upload(host, SOURCE / "elevator_button_floor_main.py", staging + "/controller.py")
    installed = [install_entry(args.lower, staging + "/controller.py", LOWER_ENTRY),
                 install_entry(args.upper, staging + "/controller.py", UPPER_ROOT + "/elevator_button_floor_main.py")]
    files = {
        "stage_timeline.py": UPPER_ROOT + "/stage_timeline.py",
        "../Demo_vlm2_kuavo_api/stage_timing.py": "/media/data/vlm/Demo_vlm2_kuavo_api/stage_timing.py",
        "../Demo_vlm2_kuavo_api/ros_stream_node.py": "/media/data/vlm/Demo_vlm2_kuavo_api/ros_stream_node.py",
        "templates/labels_review.html": UPPER_ROOT + "/templates/labels_review.html",
        "elevator_quantification.py": UPPER_ROOT + "/elevator_quantification.py",
        "elevator_extra_perception.py": UPPER_ROOT + "/elevator_extra_perception.py",
        "README.md": UPPER_ROOT + "/README.md",
        "验证记录.md": UPPER_ROOT + "/验证记录.md",
        "上位机一键启动说明.md": LAUNCH_ROOT + "/README.md",
        "deploy/elevator_demo_launcher.py": LAUNCH_ROOT + "/elevator_demo_launcher.py",
        "deploy/start_elevator_demo.sh": LAUNCH_ROOT + "/start_elevator_demo.sh",
        "deploy/run_extra_perception.sh": UPPER_ROOT + "/deploy/run_extra_perception.sh",
        "../Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception/launch/elevator_vlm.launch":
            "/media/data/vlm/Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception/launch/elevator_vlm.launch",
        "tests/test_elevator_quantification.py": UPPER_ROOT + "/tests/test_elevator_quantification.py",
        "tests/test_elevator_stage_timing.py": UPPER_ROOT + "/tests/test_elevator_stage_timing.py",
        "tests/timing_review_ui.cjs": UPPER_ROOT + "/tests/timing_review_ui.cjs",
        "tests/labels_review_ui.cjs": UPPER_ROOT + "/tests/labels_review_ui.cjs",
        "tests/test_elevator_demo.py": UPPER_ROOT + "/tests/test_elevator_demo.py",
        "tests/test_elevator_launcher.py": UPPER_ROOT + "/tests/test_elevator_launcher.py",
    }
    manifest = []
    for index, (relative, destination) in enumerate(files.items()):
        source = SOURCE / relative
        # Linux scripts must keep LF, regardless of the checkout's Windows setting.
        content = source.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8")
        item = dict(staging=staging + "/file_" + str(index), target=destination,
                    sha256=hashlib.sha256(content).hexdigest(), executable=destination.endswith((".py", ".sh")))
        # A temporary local file also makes SCP content match the verified LF bytes.
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "upload"
            path.write_bytes(content)
            upload(args.upper, path, item["staging"])
        manifest.append(item)
    install_code = backup_source() + """from pathlib import Path
import hashlib,json,os,shutil,tempfile
manifest=%r
for item in manifest:
 p=Path(item['staging'])
 if hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise RuntimeError('staging hash mismatch')
for item in manifest:
 p,t=Path(item['staging']),Path(item['target'])
 t.parent.mkdir(parents=True,exist_ok=True)
 if t.exists():backup_before_update(t,'.backup_'+%r)
 with tempfile.NamedTemporaryFile(dir=str(t.parent),delete=False) as stream:
  stream.write(p.read_bytes())
  temporary=stream.name
 os.replace(temporary,str(t))
 p.unlink()
 t.chmod(0o755 if item['executable'] else 0o644)
 if hashlib.sha256(t.read_bytes()).hexdigest()!=item['sha256']:raise RuntimeError('installed hash mismatch')
Path(%r).mkdir(exist_ok=True)
# Keep a distribution copy so the offline tests use the installed launcher implementation.
shutil.copy2(%r,%r)
print(json.dumps([dict(path=i['target'],sha256=i['sha256']) for i in manifest],ensure_ascii=False))
""" % (manifest, stamp, UPPER_ROOT + "/result", LAUNCH_ROOT + "/elevator_demo_launcher.py", UPPER_ROOT + "/deploy/elevator_demo_launcher.py")
    installed += json.loads(remote(args.upper, install_code))
    print(remote(args.upper, backup_source() + """import json,os,re,shutil,tempfile
from pathlib import Path
items=[(Path(%r), 'json'), (Path('/media/data/vlm/Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception/config/perception.yaml'), 'yaml')]
for p,kind in items:
 before=p.read_text(encoding='utf-8')
 if kind=='json':
  c=json.loads(before)
  c['extra_perception']['inference_period']=1.0
  for name in ('button','floor'):c['perception'][name].update(confirmations=1,min_span=0.0)
  after=json.dumps(c,ensure_ascii=False,indent=2)+'\\n'
 else:
  after,n=re.subn(r'(?m)^inference_hz:.*$', 'inference_hz: 1.0',before)
  if n!=1:raise RuntimeError('Cannot locate unique inference_hz')
  after=re.sub(r'(?m)^#.*每[0-9]+秒最多请求一次.*$', '# 门、按键、楼层统一最多每秒请求一次；慢推理会降低实际输出频率。', after)
 if after!=before:
  backup_before_update(p,'.backup_'+%r)
  with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=str(p.parent),delete=False) as f:
   f.write(after)
   temporary=f.name
  shutil.copymode(p,temporary)
  os.replace(temporary,str(p))
 print(str(p)+': VLM request rate 1 Hz')
""" % (UPPER_ROOT + "/params/elevator_button_floor_demo.json", stamp)).strip())
    for host in (args.upper, args.lower):
        # Delete only our known staging files, never recursively remove a computed path.
        remote(host, "from pathlib import Path\np=Path(%r)\nfor f in p.iterdir():f.unlink()\np.rmdir()" % staging)
    print(json.dumps(dict(installed=installed, untracked_file_backup_suffix=".backup_"+stamp,
                         git_tracked_files="原版本在 Git HEAD，更新后请提交并推送"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
