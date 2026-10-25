#!/usr/bin/python3
"""Single-frame colour button and post-VLM floor time guard; no motion changes."""
import collections
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'upstream'))
from elevator_demo_core import load_config
from elevator_extra_perception import ExtraPerceptionNode, decode_image, make_client
from button_contract import BACKEND, SCHEMA, FrameConfirmation, MAX_FRAME_AGE
from button_color_rule import analyze
from floor_filter import FloorTracker, BACKEND as FLOOR_BACKEND, SCHEMA as FLOOR_SCHEMA


class ColorExtraNode(ExtraPerceptionNode):
    def __init__(self, config, client=None, client_factory=None):
        self.frames = collections.deque(maxlen=4)
        self.confirmation = FrameConfirmation()
        self.floor_tracker = FloorTracker(json.loads((ROOT/'floor_filter_config.json').read_text()))
        self.request_epoch = 0
        self.floor_call_epoch = None
        super().__init__(config, client, client_factory)
        self.ready.publish(self.String(data=json.dumps({
            'schema_version': 1, 'stage_timing_version': 1, 'instance_id': self.instance_id,
            'capabilities': ['button_verify','floor_monitor'], 'inference_hz': 1.0,
            'button_backend': BACKEND, 'button_schema_version': SCHEMA,
            'button_confirmations': 1, 'button_processing': 'camera-driven; single fresh frame',
            'floor_backend': FLOOR_BACKEND, 'floor_schema_version': FLOOR_SCHEMA,
            'floor_target_confirmations': self.floor_tracker.config['target_confirmations'],
            'floor_filter_config_digest': self.floor_tracker.digest,
            'status': 'error' if self.client_error else 'ready', 'reason': self.client_error or '颜色按钮＋楼层时序过滤已就绪'})))

    def on_frame(self, message):
        with self.lock:
            self.revision += 1
            self.frame = (message, message.header.stamp.to_sec(), self.revision)
            if self.request['mode'] == 'button_verify':
                self.frames.append(self.frame)
        if getattr(self, 'timing', None):
            self.timing.frame(message.header.stamp.to_sec())

    def on_request(self, message):
        with self.lock:
            self.request_epoch += 1
            self.frames.clear()
            self.confirmation = FrameConfirmation()
            self.floor_tracker.reset()
        return super().on_request(message)

    def run_once(self):
        with self.lock:
            request = dict(self.request)
            epoch = self.request_epoch
            frame = self.frames.popleft() if request['mode'] == 'button_verify' and self.frames else None
            stage = dict(self.timing.stage or {}) if getattr(self, 'timing', None) else None
        if request['mode'] != 'button_verify':
            self.floor_call_epoch = epoch
            return super().run_once()
        if frame is None:
            return
        message, stamp, revision = frame
        context = self.timing.select(stamp, stage) if getattr(self, 'timing', None) else None
        start = time.monotonic()
        try:
            now = self.rospy.Time.now().to_sec()
            if not -.1 <= now-stamp <= MAX_FRAME_AGE or stamp <= request['not_before_stamp']:
                raise ValueError('按钮相机帧过期或来自旧阶段')
            image = decode_image(message, self.config['camera']['yuv422_layout'], self.cv2, self.np, self.bridge)
            feature, _, _ = analyze(image)
            raw = feature['prediction']
            error = ''
        except Exception as exc:
            feature, raw, error = {}, None, str(exc)[:200]
        with self.lock:
            if (epoch != self.request_epoch or request['request_id'] != self.request['request_id']
                    or self.request['mode'] != 'button_verify'):
                return
            value, proof = self.confirmation.observe(raw, stamp, revision, request['not_before_stamp'], self.rospy.Time.now().to_sec())
        result = {'schema_version': SCHEMA, 'button_backend': BACKEND,
                  'button_lit': value, 'button_raw': raw, 'button_confirmation': proof,
                  'confidence': {'button_lit': None, 'floor': 0.0},
                  'evidence': {'button_lit': feature.get('reason', error), 'floor': ''},
                  'status': 'error' if error else ('ok' if value is not None else 'uncertain'),
                  'reason': error or ('当前帧确认已亮' if value is True else '按钮未亮' if value is False else '当前画面不确定，等待新图像'),
                  'latency_ms': round((time.monotonic()-start)*1000, 3),
                  'button_confirmation_span_ms': None if proof is None else 0.0,
                  'button_features': feature, 'camera_revision': revision, 'error': error}
        if context:
            self.timing.finish(context, result['status'], {'button_lit': value})
        self.publish(request, stamp, result)

    def publish(self, request, stamp, result, message_type='result'):
        if message_type=='result' and request['mode']=='floor_monitor':
            started=time.perf_counter()
            with self.lock:
                if (request['request_id']!=self.request['request_id'] or self.request['mode']!='floor_monitor'
                        or self.floor_call_epoch!=self.request_epoch):
                    return
                raw=result.get('floor')
                value,decision=self.floor_tracker.observe(
                    raw,stamp,result.get('confidence',{}).get('floor'),
                    self.config['perception']['min_confidence'],request['not_before_stamp'])
            result=dict(result)
            result.update(schema_version=FLOOR_SCHEMA,floor_backend=FLOOR_BACKEND,
                          floor_raw=raw,floor=value,floor_filter=decision,
                          floor_filter_ms=round((time.perf_counter()-started)*1000,4))
            if result.get('status')!='error':
                result['status']='ok' if value is not None else 'uncertain'
                result['reason']='当前楼层已通过时序检查：%s'%value if value is not None else '楼层暂不可信：'+decision['reason']
        return super().publish(request,stamp,result,message_type)

    def run(self):
        next_floor = 0.0
        while not self.rospy.is_shutdown() and not self.stop.is_set():
            with self.lock:
                mode = self.request['mode']
            now = time.monotonic()
            if mode == 'button_verify':
                self.run_once()
            elif now >= next_floor:
                self.run_once()
                next_floor = max(now+1.0, time.monotonic()+.05)
            self.stop.wait(.005 if mode == 'button_verify' else .01)


def main():
    import rospy
    config = load_config(ROOT/'upstream/perception.json')
    rospy.init_node('elevator_extra_perception_demo', anonymous=False)
    ColorExtraNode(config, client_factory=lambda: make_client(config)).run()


if __name__ == '__main__':
    main()
