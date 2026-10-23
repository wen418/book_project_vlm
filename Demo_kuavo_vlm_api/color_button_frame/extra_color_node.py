#!/usr/bin/python3
"""Replace only button inference; retain the snapshotted floor API implementation."""
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


class ColorExtraNode(ExtraPerceptionNode):
    def __init__(self, config, client=None, client_factory=None):
        self.frames = collections.deque(maxlen=4)
        self.confirmation = FrameConfirmation()
        self.request_epoch = 0
        super().__init__(config, client, client_factory)
        self.ready.publish(self.String(data=json.dumps({
            'schema_version': 1, 'stage_timing_version': 1, 'instance_id': self.instance_id,
            'capabilities': ['button_verify','floor_monitor'], 'inference_hz': 1.0,
            'button_backend': BACKEND, 'button_schema_version': SCHEMA,
            'button_confirmations': 1, 'button_processing': 'camera-driven; single fresh frame',
            'status': 'error' if self.client_error else 'ready', 'reason': self.client_error or '颜色按钮＋原API楼层已就绪'})))

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
        return super().on_request(message)

    def run_once(self):
        with self.lock:
            request = dict(self.request)
            epoch = self.request_epoch
            frame = self.frames.popleft() if request['mode'] == 'button_verify' and self.frames else None
            stage = dict(self.timing.stage or {}) if getattr(self, 'timing', None) else None
        if request['mode'] != 'button_verify':
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
