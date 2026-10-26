"""Build hashes for this isolated release. No robot or network operations."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    runtime = ['launcher.py', 'task_contract.py', 'button_color_rule.py', 'button_contract.py',
               'button_layout.json', 'button_pose.json', 'dual_button.py', 'extra_color_node.py',
               'floor_filter.py', 'floor_filter_config.json', 'lower_adapter.py', 'compact_vlm.py',
               'compact_door_node.py', 'door_ros.yaml', 'telemetry_transport.py',
               'start_elevator_demo.sh', 'run_extra_perception.sh', 'run_door_perception.sh',
               'build_manifest.py']
    runtime += ['smoke_private_ros.py', 'benchmark_short_output.py']
    runtime += [p.relative_to(ROOT).as_posix() for p in sorted((ROOT/'upstream').glob('*')) if p.is_file()]
    manifest = {'release': 'elevator_task_perception_v1', 'button_confirmations': 1,
                'floor_direction_constraint': False, 'floor_target_rule': '2 of last 3 in 5 seconds',
                'vlm_response': 'door C/O/U; floor integer/U; max_tokens=8; no few-shot',
                'checks': 'startup readiness; runtime command/settle; retain arrival and motion telemetry',
                'lower_original_sha256': 'f371a7e12f49bb023e726499dd14691e232841b11ced6b41c3f78a53c2b8a525',
                'external_dependencies_sha256': {
                    '/media/data/vlm/Demo_vlm2_kuavo_api/vlm_client.py': 'ef1c9d304c16ad92e288ec8c24acdeba7ba7a35cb539f495d7ae9d3e424f3700',
                    '/media/data/vlm/Demo_vlm2_kuavo_api/ros_stream_node.py': '28815d5355fe02bec9581e1597b13814ee7a2fb3de7678e9f5003284da95fecd',
                    '/media/data/vlm/Demo_vlm2_kuavo_api/perception_schema.py': '0803e60a04b59952c1772fe77fc6a82fdb06b8397f29d5ffffcac9592436891a',
                    '/media/data/vlm/Demo_vlm2_kuavo_api/stage_timing.py': '84f124d070e565431be48c87ee360a05dd62c50b55629547d1caa8dd7d57cddf',
                    '/media/data/vlm/Demo_vlm2_kuavo_api/elevator_perception.py': '8466694f4559557eeb7aa173e1945290663fe27fb037a5e701f27db2224f1046'},
                'release_files_sha256': {n: hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in runtime}}
    (ROOT/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('MANIFEST', len(runtime), 'runtime files')


if __name__ == '__main__': main()
