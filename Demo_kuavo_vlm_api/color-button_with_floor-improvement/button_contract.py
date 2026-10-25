"""State-only button protocol; shared by upper node and lower gate (stdlib only)."""
import json
import math

SCHEMA = 4
BACKEND = 'roi_color_single_frame_v2'
MAX_FRAME_AGE = .5


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


class FrameConfirmation:
    def __init__(self):
        self.last_stamp = None

    def observe(self, raw, stamp, revision, not_before, now):
        if (not number(stamp) or not number(now) or type(revision) is not int
                or stamp <= not_before or not -.1 <= now-stamp <= MAX_FRAME_AGE):
            return None, None
        if self.last_stamp is not None and stamp <= self.last_stamp:
            return None, None
        self.last_stamp = stamp
        if raw is not True:
            return False if raw is False else None, None
        return True, {'frame_stamps': [stamp], 'frame_revisions': [revision],
                      'raw_states': [True], 'confirmed_frames': 1}


def valid_proof(payload, now, not_before):
    if (payload.get('schema_version') != SCHEMA or payload.get('button_backend') != BACKEND
            or payload.get('status') != 'ok' or payload.get('button_lit') is not True):
        return False
    proof = payload.get('button_confirmation')
    if not isinstance(proof, dict) or type(proof.get('confirmed_frames')) is not int or proof['confirmed_frames'] != 1:
        return False
    stamps, revisions, states = (proof.get(key) for key in ('frame_stamps','frame_revisions','raw_states'))
    if not all(isinstance(item, list) and len(item) == 1 for item in (stamps, revisions, states)):
        return False
    return (all(number(s) for s in stamps) and all(type(r) is int for r in revisions)
            and all(s is True for s in states) and number(now) and number(not_before)
            and stamps[0] > not_before and -.1 <= now-stamps[0] <= MAX_FRAME_AGE
            and payload.get('image_stamp') == stamps[0])


def adapted_gate(base):
    class ColorButtonGate(base):
        def arm(self, mode, field, expected, not_before, request_id=None, schema_version=1):
            return super().arm(mode, field, expected, not_before, request_id,
                               SCHEMA if mode == 'button_verify' else schema_version)

        def feed(self, text, now):
            if self.mode != 'button_verify':
                return super().feed(text, now)
            with self.condition:
                try:
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise ValueError('按钮状态不是对象')
                    if (payload.get('message_type', 'result') != 'result' or payload.get('mode') != self.mode
                            or payload.get('request_id') != self.request_id):
                        return False
                    seq = payload.get('sequence')
                    if type(seq) is not int:
                        raise ValueError('按钮sequence无效')
                    if self.sequence is not None and seq <= self.sequence:
                        return False
                    self.sequence = seq
                    if self.field != 'button_lit' or self.expected is not True or not valid_proof(payload, now, self.not_before):
                        raise ValueError('尚未获得当前请求的新画面已亮证据')
                    stamp = payload['image_stamp']
                    if self.frame_stamp is not None and stamp <= self.frame_stamp:
                        return False
                    self.frame_stamp = stamp
                    self.first_stamp = payload['button_confirmation']['frame_stamps'][0]
                    self.count, self.ready = 1, True
                    self.last_payload = payload
                    self.reason = '单张相机新帧确认下行按钮已亮（颜色规则，无模型置信度）'
                except (ValueError, TypeError, AttributeError, KeyError) as exc:
                    self.reset(str(exc))
                self.condition.notify_all()
                return self.ready

        def wait(self, timeout, now_fn, stopped=lambda: False):
            # Base.wait checks age again immediately before the caller moves on.
            if self.mode != 'button_verify':
                return super().wait(timeout, now_fn, stopped)
            saved = self.max_age
            self.max_age = min(saved, MAX_FRAME_AGE)
            try:
                return super().wait(timeout, now_fn, stopped)
            finally:
                self.max_age = saved
    return ColorButtonGate
