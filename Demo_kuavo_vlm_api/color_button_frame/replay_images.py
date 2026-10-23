"""Single-frame regression only. Sparse captures cannot validate adjacent-frame accuracy."""
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from button_color_rule import analyze


def main():
    data=Path('/media/data/vlm/elevator_button_floor_demo/result')
    old=Path('/media/data/vlm/qwen35_elevator/output/button_color_20261023_204409/multichannel_v2/evaluation_results.json')
    samples=[r for r in json.loads(old.read_text()) if r['variant']=='precise_roi']
    results=[]
    for row in samples:
        image=cv2.imread(str(data/row['run_id']/row['image']))
        start=time.perf_counter();result,_,_=analyze(image)
        assert result['prediction'] is row['prediction'],(row['run_id'],row['id'])
        results.append({'run_id':row['run_id'],'id':row['id'],'ground_truth':row['ground_truth'],
                        **result,'elapsed_ms':(time.perf_counter()-start)*1000})
    summary={'n':len(results),'correct':sum(r['prediction'] is not None and r['prediction']==r['ground_truth'] for r in results),
             'incorrect':sum(r['prediction'] is not None and r['prediction']!=r['ground_truth'] for r in results),
             'unknown':sum(r['prediction'] is None for r in results),
             'all_predictions_match_frozen_benchmark':True,
             'note':'Single-frame regression, not two-frame dynamic accuracy. Images are sparse historical captures.'}
    out=ROOT/'verification';out.mkdir(exist_ok=True)
    (out/'single_frame_replay.json').write_text(json.dumps({'summary':summary,'results':results},ensure_ascii=False,indent=2))
    print(json.dumps(summary))


if __name__=='__main__':main()
