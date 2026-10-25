"""Replay recorded API responses using real capture timestamps, never truth as input."""
import collections
import csv
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from floor_filter import FloorTracker
from floor_filter import adapted_floor_gate,SCHEMA,BACKEND
from button_contract import adapted_gate
sys.path.insert(0,str(ROOT/'upstream'))
from elevator_button_floor_main import EvidenceGate
DATA=Path('/media/data/vlm/elevator_button_floor_demo/result')


def floor_value(value):
    if type(value) is int:return value
    if isinstance(value,str) and value.lstrip('-').isdigit():return int(value)
    return None


def metrics(rows,field):
    answered=[r for r in rows if r[field] is not None]
    correct=sum(type(r[field]) is int and r[field]==r['ground_truth'] for r in answered)
    return {'n':len(rows),'correct':correct,'incorrect':len(answered)-correct,'unknown':len(rows)-len(answered),
            'accuracy_including_unknown':correct/len(rows) if rows else None,
            'answered_accuracy':correct/len(answered) if answered else None,
            'coverage':len(answered)/len(rows) if rows else None}


def main():
    out=ROOT/'verification';out.mkdir(exist_ok=True)
    config=json.loads((ROOT/'floor_filter_config.json').read_text())
    global_file=DATA/'reviewed_observations.csv'
    global_rows={(r['run_id'],r['id']):r for r in csv.DictReader(global_file.open(encoding='utf-8-sig')) if r['mode']=='floor_monitor'}
    results=[];excluded=[];hashes={str(global_file):hashlib.sha256(global_file.read_bytes()).hexdigest()}
    for folder in sorted(DATA.iterdir()):
        if not folder.is_dir() or not (folder/'labels.csv').exists():continue
        paths=[folder/name for name in ('labels.csv','reviewed_examples.json','observations.json')]
        for p in paths:hashes[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
        reviews=json.loads(paths[1].read_text(encoding='utf-8-sig'))
        labels={r['id']:r for r in csv.DictReader(paths[0].open(encoding='utf-8-sig'))}
        observations=[r for r in json.loads(paths[2].read_text(encoding='utf-8-sig')) if r['mode']=='floor_monitor']
        tracker=FloorTracker(config);last_request=object()
        for row in sorted(observations,key=lambda r:r['image_stamp']):
            key=(folder.name,row['id'])
            values=[floor_value(labels[row['id']].get('ground_truth')),floor_value(reviews.get(row['id'],{}).get('ground_truth')),
                    floor_value(global_rows.get(key,{}).get('ground_truth'))]
            if any(v is None for v in values) or len(set(values))!=1:
                excluded.append({'run_id':folder.name,'id':row['id'],'manual_labels':values});continue
            request=row['payload'].get('request_id')
            if request!=last_request:tracker.reset();last_request=request
            raw=row['prediction'] if row['status']=='ok' else None
            confidence=row.get('confidence')
            start=time.perf_counter()
            filtered,decision=tracker.observe(raw,row['image_stamp'],confidence,.72)
            elapsed_ms=(time.perf_counter()-start)*1000
            results.append({'run_id':folder.name,'id':row['id'],'request_id':request,'image':row['image'],
                            'image_stamp':row['image_stamp'],'ground_truth':values[0],
                            'received_ros_stamp':row['received_ros_stamp'],
                            'raw':raw,'filtered':filtered,'confidence':confidence,
                            'decision':decision,'filter_ms':elapsed_ms})
    summary={'raw_api':metrics(results,'raw'),'filtered_api':metrics(results,'filtered'),
             'excluded_manual_unknown_or_conflicting':len(excluded),
             'run_count':len({r['run_id'] for r in results}),
             'filter_mean_ms':sum(r['filter_ms'] for r in results)/len(results),
             'filtered_wrong_raws':sum(r['raw'] is not None and r['raw']!=r['ground_truth'] and r['filtered'] is None for r in results),
             'withheld_correct_raws':sum(r['raw']==r['ground_truth'] and r['filtered'] is None for r in results),
             'reason_counts':dict(collections.Counter(r['decision']['reason'] for r in results)),
             'new_api_calls':0,'parameters_fitted_on_truth':False,'development_regression':True,
             'algorithm_revised_after_initial_replay':True,
             'note':'Historical API outputs only; manual unknowns excluded before sequence processing as well as scoring; actual timestamp gaps retained. Not a dynamic robot test.'}
    targets=[]
    grouped=collections.defaultdict(list)
    for row in results:grouped[(row['run_id'],row['request_id'])].append(row)
    for target in sorted({r['ground_truth'] for r in results}):
        events=[]
        for (run_id,request_id),stream in grouped.items():
            gate=adapted_floor_gate(adapted_gate(EvidenceGate),config)(15,.72,1,0)
            gate.arm('floor_monitor','floor',target,stream[0]['image_stamp']-.001,request_id=request_id)
            raw_trigger=next((r for r in stream if r['raw']==target),None)
            filtered_trigger=None
            for seq,r in enumerate(stream):
                p={'schema_version':SCHEMA,'floor_backend':BACKEND,'mode':'floor_monitor','request_id':request_id,
                   'sequence':seq,'image_stamp':r['image_stamp'],'floor':r['filtered'],'floor_filter':r['decision'],
                   'status':'ok' if r['filtered'] is not None else 'uncertain','confidence':{'floor':r['confidence']}}
                if gate.feed(json.dumps(p),r['received_ros_stamp']):filtered_trigger=r;break
            events.append({'run_id':run_id,'request_id':request_id,**{name:None if row is None else
                {'id':row['id'],'ground_truth':row['ground_truth'],'image_stamp':row['image_stamp'],
                 'received_ros_stamp':row['received_ros_stamp'],'correct_at_trigger':row['ground_truth']==target}
                 for name,row in [('raw_trigger',raw_trigger),('guarded_trigger',filtered_trigger)]}})
        targets.append({'target_floor':target,**{field:{'false_target_match':sum(r[field]==target and r['ground_truth']!=target for r in results),
                                                      'correct_target_frames':sum(r[field]==target and r['ground_truth']==target for r in results)}
                                               for field in ('raw','filtered')},'first_trigger_per_request':events})
    for name,obj in [('floor_history_summary.json',summary),('floor_history_results.json',results),
                     ('floor_history_excluded.json',excluded),('floor_history_hashes.json',hashes),('floor_target_comparison.json',targets)]:
        (out/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'floor_history_results.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['run_id','id','image_stamp','ground_truth','raw','filtered','confidence','reason','allowed_delta','filter_ms'])
        writer.writeheader()
        for r in results:writer.writerow({**{k:r[k] for k in ('run_id','id','image_stamp','ground_truth','raw','filtered','confidence','filter_ms')},
                                        'reason':r['decision']['reason'],'allowed_delta':r['decision']['allowed_delta']})
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
