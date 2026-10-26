"""Direction-free floor tracking. Planner intent never substitutes for a camera vote."""
import collections
import hashlib
import json
import math

SCHEMA = 7
BACKEND = 'floor_time_guard_no_direction_v3'
DEFAULTS = {'seconds_per_floor': 4.0, 'base_slack_floors': 1, 'max_anchor_age': 12.0,
            'bootstrap_confirmations': 2, 'reacquire_confirmations': 3, 'target_confirmations': 2,
            'min_confirmation_span': .5, 'max_confirmation_span': 12.0,
            'target_window_seconds': 5.0, 'target_window_results': 3}


def number(v):
    return type(v) in (int, float) and math.isfinite(v)


def floor_number(v):
    return type(v) is int and -10 <= v <= 150


def validate_config(config):
    if set(config) != set(DEFAULTS): raise ValueError('floor config fields differ')
    for k, default in DEFAULTS.items():
        if not number(config[k]) or config[k] <= 0 or (type(default) is int and type(config[k]) is not int):
            raise ValueError('invalid '+k)
    if not (config['bootstrap_confirmations'] >= 2 and config['reacquire_confirmations'] >= 3
            and config['target_confirmations'] == 2 and config['target_window_results'] == 3
            and config['min_confirmation_span'] < config['target_window_seconds'] <= config['max_confirmation_span'] <= config['max_anchor_age']):
        raise ValueError('inconsistent floor config')
    return dict(config)


def config_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def allowed_delta(elapsed, config):
    return config['base_slack_floors']+int(max(0, elapsed)/config['seconds_per_floor'])


class FloorTracker:
    def __init__(self, config=None):
        self.config = validate_config(DEFAULTS if config is None else config)
        self.digest = config_digest(self.config)
        self.reset()

    def reset(self, current_floor=None, task_started_stamp=None):
        self.anchor = current_floor if floor_number(current_floor) and number(task_started_stamp) else None
        self.anchor_stamp = task_started_stamp if self.anchor is not None else None
        self.seed = self.anchor is not None
        self.last_seen = None
        self.ever_trusted = False
        self.pending = []
        self.history = []
        self.unknown_count = 0

    def observe(self, raw, stamp, confidence=None, min_confidence=None, not_before=0):
        cfg = self.config
        d = {'accepted': False, 'raw_floor': raw, 'reason': 'unknown_reading', 'config_digest': self.digest}
        if not number(stamp) or stamp <= not_before or (self.last_seen is not None and stamp <= self.last_seen):
            d['reason'] = 'old_or_duplicate_image'; return None, d
        self.last_seen = stamp
        if self.anchor_stamp is not None and stamp-self.anchor_stamp > cfg['max_anchor_age']:
            self.anchor = self.anchor_stamp = None
            self.seed = False
            self.history = []; self.pending = []
        self.history = [p for p in self.history if stamp-p['stamp'] <= cfg['max_anchor_age']]
        d.update(anchor_floor=self.anchor, anchor_stamp=self.anchor_stamp, planner_seed=self.seed,
                 reference_history=[dict(p) for p in self.history])
        if not floor_number(raw):
            self.unknown_count += 1
            if self.unknown_count > 1: self.pending = []
            return None, d
        self.unknown_count = 0
        if self.anchor is not None:
            elapsed = stamp-self.anchor_stamp
            d.update(elapsed=elapsed, allowed_delta=allowed_delta(elapsed, cfg))
            if elapsed <= 0 or abs(raw-self.anchor) > allowed_delta(elapsed, cfg):
                self.pending = []; d['reason'] = 'implausible_jump'; return None, d
            if any(abs(raw-p['floor']) > allowed_delta(stamp-p['stamp'], cfg) for p in self.history):
                self.pending = []; d['reason'] = 'history_inconsistent'; return None, d
        required = (cfg['reacquire_confirmations'] if self.ever_trusted else cfg['bootstrap_confirmations']) if self.anchor is None else 1
        if self.seed: required = cfg['bootstrap_confirmations']
        self.pending = [p for p in self.pending if stamp-p[1] <= cfg['max_confirmation_span']]
        if self.pending and self.pending[-1][0] != raw: self.pending = []
        self.pending.append((raw, stamp)); self.pending = self.pending[-required:]
        d.update(required_confirmations=required, confirmation_stamps=[p[1] for p in self.pending],
                 observed_floors=[p[0] for p in self.pending])
        if len(self.pending) < required or (required > 1 and stamp-self.pending[0][1] < cfg['min_confirmation_span']):
            d['reason'] = 'confirming_floor'; return None, d
        phase = 'initialized' if self.seed or (self.anchor is None and not self.ever_trusted) else 'reacquired' if self.anchor is None else 'tracked'
        d.update(accepted=True, reason=phase, confirmed_floor=raw)
        if phase != 'tracked': self.history = [{'floor': p[0], 'stamp': p[1]} for p in self.pending]
        else: self.history.append({'floor': raw, 'stamp': stamp})
        self.history = self.history[-128:]
        self.anchor, self.anchor_stamp, self.seed, self.ever_trusted = raw, stamp, False, True
        return raw, d


def valid_floor_proof(payload, config, min_confidence, not_before):
    if (payload.get('schema_version') != SCHEMA or payload.get('floor_backend') != BACKEND
            or payload.get('status') != 'ok' or not floor_number(payload.get('floor'))): return False
    p = payload.get('floor_filter')
    if not isinstance(p, dict) or p.get('accepted') is not True or p.get('config_digest') != config_digest(config): return False
    phase = p.get('reason')
    n = config['bootstrap_confirmations'] if phase == 'initialized' else config['reacquire_confirmations'] if phase == 'reacquired' else 1 if phase == 'tracked' else 0
    stamps, values = p.get('confirmation_stamps'), p.get('observed_floors')
    if (not n or p.get('required_confirmations') != n or not isinstance(stamps, list) or len(stamps) != n
            or not isinstance(values, list) or len(values) != n
            or not all(number(s) and s > not_before for s in stamps)
            or not all(stamps[i] > stamps[i-1] for i in range(1, n))
            or not all(type(v) is int and v == payload['floor'] for v in values)
            or p.get('confirmed_floor') != payload['floor'] or payload.get('image_stamp') != stamps[-1]
            or not (config['min_confirmation_span'] if n > 1 else 0) <= stamps[-1]-stamps[0] <= config['max_confirmation_span']): return False
    anchor, astamp = p.get('anchor_floor'), p.get('anchor_stamp')
    if anchor is not None:
        if (not floor_number(anchor) or not number(astamp) or not 0 < stamps[-1]-astamp <= config['max_anchor_age']
                or abs(payload['floor']-anchor) > allowed_delta(stamps[-1]-astamp, config)): return False
        if p.get('planner_seed'):
            if phase != 'initialized' or anchor != payload.get('current_floor') or astamp != payload.get('task_started_stamp'): return False
        elif astamp <= not_before: return False
    elif astamp is not None or phase == 'tracked': return False
    refs = p.get('reference_history')
    if not isinstance(refs, list) or len(refs) > 128: return False
    if phase == 'tracked' and (not refs or refs[-1].get('floor') != anchor or refs[-1].get('stamp') != astamp): return False
    for item in refs:
        if (not isinstance(item, dict) or not floor_number(item.get('floor')) or not number(item.get('stamp'))
                or not not_before < item['stamp'] < stamps[-1]
                or abs(payload['floor']-item['floor']) > allowed_delta(stamps[-1]-item['stamp'], config)): return False
    return True


def adapted_floor_gate(base, config=None):
    cfg = validate_config(DEFAULTS if config is None else config)
    class FloorGate(base):
        def wait(self, timeout, now_fn, stopped=lambda: False):
            saved = self.max_age
            self.max_age = min(saved, cfg['target_window_seconds'])
            try: return super().wait(timeout, now_fn, stopped)
            finally: self.max_age = saved

        def arm(self, mode, field, expected, not_before, request_id=None, schema_version=1):
            self.target_window = collections.deque(maxlen=cfg['target_window_results'])
            return super().arm(mode, field, expected, not_before, request_id, SCHEMA if mode == 'floor_monitor' else schema_version)

        def feed(self, text, now):
            if self.mode != 'floor_monitor': return super().feed(text, now)
            with self.condition:
                try:
                    p = json.loads(text)
                    if not isinstance(p, dict): raise ValueError('楼层消息不是对象')
                    if p.get('mode') != self.mode or p.get('request_id') != self.request_id or p.get('message_type', 'result') != 'result': return False
                    seq, stamp = p.get('sequence'), p.get('image_stamp')
                    if type(seq) is not int: raise ValueError('楼层 sequence 无效')
                    if self.sequence is not None and seq <= self.sequence: return False
                    self.sequence = seq
                    if not number(stamp) or stamp <= self.not_before or not -.1 <= now-stamp <= min(self.max_age, cfg['target_window_seconds']):
                        raise ValueError('楼层画面过期或来自旧阶段')
                    if self.frame_stamp is not None and stamp <= self.frame_stamp: return False
                    self.frame_stamp = stamp
                    valid = valid_floor_proof(p, cfg, self.min_confidence, self.not_before)
                    self.ready = False
                    while self.target_window and stamp-self.target_window[0][0] > cfg['target_window_seconds']: self.target_window.popleft()
                    if not valid:
                        self.target_window.append((stamp, None))
                        self.count = sum(v == self.expected for _, v in self.target_window)
                        self.reason = '楼层待确认：'+str(p.get('floor_filter', {}).get('reason', p.get('error') or 'invalid_proof'))
                    elif p['floor'] != self.expected:
                        self.target_window.clear(); self.count = 0
                        self.reason = '当前楼层 %s，等待目标 %s' % (p['floor'], self.expected)
                    else:
                        votes = dict(self.target_window)
                        for s in p['floor_filter']['confirmation_stamps']:
                            if stamp-s <= cfg['target_window_seconds']:
                                votes[s] = self.expected
                        self.target_window = collections.deque(sorted(votes.items())[-cfg['target_window_results']:],
                                                               maxlen=cfg['target_window_results'])
                        hits = [s for s, v in self.target_window if v == self.expected]
                        self.count = len(hits)
                        self.first_stamp = hits[0] if hits else None
                        self.ready = len(hits) >= cfg['target_confirmations'] and hits[-1]-hits[0] >= cfg['min_confirmation_span']
                        self.last_payload = p
                        self.reason = '目标楼层确认 %s/2（最近3个结果，5秒窗口）' % self.count
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    self.target_window.clear(); self.reset(str(exc))
                self.condition.notify_all()
                return self.ready
    return FloorGate
