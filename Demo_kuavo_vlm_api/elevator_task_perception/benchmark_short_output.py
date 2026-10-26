"""Small paired API smoke on consistently human-labelled historical images, no ROS."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/media/data/vlm/Demo_vlm2_kuavo_api')
DATA = Path('/media/data/vlm/elevator_button_floor_demo/result')
sys.path.insert(0, str(ROOT/'upstream'))
sys.path.insert(0, str(PROJECT))
import cv2
from compact_vlm import configure, infer_value
from elevator_extra_perception import make_client, encode_image
from elevator_demo_core import load_config
from elevator_perception import Settings, ElevatorPerception
from compact_door_node import CompactPipeline


def truth(text, mode):
    if mode == 'floor_monitor':
        if isinstance(text, bool): return None
        try: return int(text)
        except (TypeError, ValueError): return None
    if str(text).lower() == 'true': return True
    if str(text).lower() == 'false': return False
    return None


class Calls:
    def __init__(self, client): self.client, self.rows = client, []
    def __getattr__(self, key): return getattr(self.client, key)
    def complete(self, *args, **kwargs):
        started = time.perf_counter()
        result = self.client.complete(*args, **kwargs)
        self.rows.append({'seconds': time.perf_counter()-started, 'characters': len(result), 'answer': result})
        return result


def main():
    # Import the frozen previous version to measure its real JSON prompt rather
    # than the new module that replaces its infer function in the ROS node.
    old = ROOT.parent/'color-button_with_floor-improvement/upstream/elevator_extra_perception.py'
    if not old.is_file(): old = Path('/media/data/vlm/Demo_kuavo_vlm_api/color-button_with_floor-improvement/upstream/elevator_extra_perception.py')
    spec = importlib.util.spec_from_file_location('baseline_extra', old)
    baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
    config = load_config(ROOT/'upstream/perception.json')
    old_floor = Calls(make_client(config)); new_floor = Calls(configure(make_client(config)))
    settings = Settings.from_json(PROJECT/'config.door_fast.json')
    old_door = ElevatorPerception(settings, PROJECT/'few-shots/manifest.json', enable_temporal_filter=False)
    old_door.client = Calls(old_door.client)
    new_door = CompactPipeline(settings); new_door.client = Calls(new_door.client)
    global_rows = {(r['run_id'], r['id']): r for r in csv.DictReader((DATA/'reviewed_observations.csv').open(encoding='utf-8-sig'))}
    chosen = []; seen = set()
    # Distinct floor values and open/closed door examples from different runs.
    for folder in sorted(DATA.iterdir()):
        if not all((folder/n).is_file() for n in ('labels.csv', 'reviewed_examples.json')): continue
        reviews = json.loads((folder/'reviewed_examples.json').read_text())
        for row in csv.DictReader((folder/'labels.csv').open(encoding='utf-8-sig')):
            mode = row['mode']
            if mode not in ('floor_monitor', 'door_monitor'): continue
            values = [truth(row.get('ground_truth'), mode), truth(reviews.get(row['id'], {}).get('ground_truth'), mode),
                      truth(global_rows.get((folder.name, row['id']), {}).get('ground_truth'), mode)]
            if any(v is None for v in values) or len(set(values)) != 1: continue
            key = (mode, values[0]) if mode == 'floor_monitor' else (mode, values[0], folder.name)
            count = sum(r['mode'] == mode for r in chosen)
            if key in seen or count >= 4: continue
            if mode == 'door_monitor' and sum(r['mode'] == mode and r['truth'] is values[0] for r in chosen) >= 2: continue
            image = folder/row['image']
            if not image.is_file(): continue
            seen.add(key); chosen.append({'run_id': folder.name, 'id': row['id'], 'image': str(image),
                                        'sha256': hashlib.sha256(image.read_bytes()).hexdigest(), 'mode': mode, 'truth': values[0]})
        if len(chosen) >= 8: break
    results = []
    for i, sample in enumerate(chosen):
        image = cv2.imread(sample['image']); mode = sample['mode']
        camera = {**config['camera'], 'jpeg_quality': 80 if mode == 'door_monitor' else 85}
        url = encode_image(image, camera, 'floor_monitor', cv2)
        for version in (('json', 'short') if i % 2 == 0 else ('short', 'json')):
            result = {**sample, 'version': version}
            start = time.perf_counter()
            try:
                if mode == 'floor_monitor':
                    client = old_floor if version == 'json' else new_floor
                    client.rows = []
                    value = baseline.infer(client, url, mode, .72)['floor'] if version == 'json' else infer_value(client, url, mode)
                else:
                    pipeline = old_door if version == 'json' else new_door
                    client = pipeline.client; client.rows = []
                    value = pipeline.infer_data_urls(i, [{'label': '当前电梯门图：', 'data_url': url}],
                                                      ('door_closed',), mode=mode).door_closed
                result.update(prediction=value, correct=type(value) is type(sample['truth']) and value == sample['truth'],
                              seconds=time.perf_counter()-start, calls=client.rows)
            except Exception as exc:
                result.update(prediction=None, correct=False, seconds=time.perf_counter()-start, error_type=type(exc).__name__)
            results.append(result)
            print(mode, sample['id'], version, 'value', result['prediction'], 'seconds', round(result['seconds'], 3), flush=True)
    summary = {}
    for mode in ('door_monitor', 'floor_monitor'):
        summary[mode] = {}
        for version in ('json', 'short'):
            rows = [r for r in results if r['mode'] == mode and r['version'] == version]
            summary[mode][version] = {'n': len(rows), 'correct': sum(r['correct'] for r in rows),
                                      'errors': sum('error_type' in r for r in rows),
                                      'mean_seconds': statistics.mean(r['seconds'] for r in rows) if rows else None,
                                      'median_seconds': statistics.median(r['seconds'] for r in rows) if rows else None}
    out = ROOT/'verification'; out.mkdir(exist_ok=True)
    report = {'summary': summary, 'results': results,
              'compact_vlm_sha256': hashlib.sha256((ROOT/'compact_vlm.py').read_bytes()).hexdigest(),
              'baseline_floor_sha256': hashlib.sha256(old.read_bytes()).hexdigest(),
              'label_rule': 'Exclude unclear, absent, or conflicting labels in any of three human review sources.',
              'limitations': 'Small paired static API smoke; not dynamic accuracy, not a timing guarantee; network/prompt cache can vary.'}
    (out/'short_output_api_smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
