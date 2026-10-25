"""Time-aware floor rejection, not number correction. Stdlib-only shared contract."""
import hashlib
import json
import math

SCHEMA = 5
BACKEND = 'floor_time_guard_v1'
DEFAULTS = {'seconds_per_floor':4.0, 'base_slack_floors':1, 'max_jump_floors':3,
            'max_anchor_age':12.0, 'confirmations':1, 'bootstrap_confirmations':2,
            'target_confirmations':2, 'reacquire_confirmations':3,
            'min_confirmation_span':.5, 'max_confirmation_span':12.0}


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


def validate_config(config):
    if set(config) != set(DEFAULTS):
        raise ValueError('floor filter config fields differ')
    for key in ('seconds_per_floor','max_anchor_age','min_confirmation_span','max_confirmation_span'):
        if not number(config[key]) or config[key] <= 0:
            raise ValueError('Invalid floor filter '+key)
    for key in ('base_slack_floors','max_jump_floors','confirmations','bootstrap_confirmations','target_confirmations','reacquire_confirmations'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError('Invalid floor filter '+key)
    if not (1 <= config['confirmations'] <= config['bootstrap_confirmations'] <= config['reacquire_confirmations'] <= 6
            and 2 <= config['bootstrap_confirmations'] and 2 <= config['target_confirmations'] <= 6
            and config['base_slack_floors'] <= config['max_jump_floors'] <= 5
            and config['min_confirmation_span'] <= config['max_confirmation_span'] <= config['max_anchor_age']):
        raise ValueError('Inconsistent floor filter limits')
    return dict(config)


def config_digest(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def allowed_delta(elapsed, config):
    return min(config['max_jump_floors'],config['base_slack_floors']+int(max(0,elapsed)/config['seconds_per_floor']))


class FloorTracker:
    def __init__(self, config=None):
        self.config=validate_config(DEFAULTS if config is None else config)
        self.digest=config_digest(self.config)
        self.reset()

    def reset(self):
        self.anchor=None
        self.anchor_stamp=None
        self.last_seen=None
        self.ever_trusted=False
        self.reacquiring=False
        self.pending=[]

    def observe(self, raw, stamp, confidence, min_confidence, not_before=0):
        cfg=self.config
        decision={'accepted':False,'raw_floor':raw,'reason':'unknown_reading','config_digest':self.digest,
                  'anchor_floor':self.anchor,'anchor_stamp':self.anchor_stamp,'elapsed':None,'allowed_delta':None,
                  'required_confirmations':None}
        if not number(stamp) or stamp <= not_before or (self.last_seen is not None and stamp <= self.last_seen):
            decision['reason']='old_or_duplicate_image'
            return None,decision
        self.last_seen=stamp
        if self.anchor_stamp is not None and stamp-self.anchor_stamp > cfg['max_anchor_age']:
            self.anchor=self.anchor_stamp=None
            self.pending=[]
            self.reacquiring=True
        decision.update(anchor_floor=self.anchor,anchor_stamp=self.anchor_stamp)
        if (type(raw) is not int or not -10 <= raw <= 150 or not number(confidence)
                or not number(min_confidence) or not min_confidence <= confidence <= 1):
            self.pending=[]
            decision['reason']='unknown_or_low_confidence'
            return None,decision
        required=(cfg['reacquire_confirmations'] if self.reacquiring else
                  cfg['bootstrap_confirmations'] if self.anchor is None else cfg['confirmations'])
        decision['required_confirmations']=required
        if self.anchor is not None:
            elapsed=stamp-self.anchor_stamp
            limit=allowed_delta(elapsed,cfg)
            decision.update(elapsed=elapsed,allowed_delta=limit)
            if abs(raw-self.anchor)>limit:
                self.pending=[]
                decision['reason']='implausible_jump'
                return None,decision
        if (self.pending and (self.pending[-1][0]!=raw or stamp-self.pending[0][1]>cfg['max_confirmation_span'])):
            self.pending=[]
        self.pending.append((raw,stamp,confidence))
        self.pending=self.pending[-required:]
        decision['confirmation_stamps']=[p[1] for p in self.pending]
        decision['observed_floors']=[p[0] for p in self.pending]
        decision['confirmation_confidences']=[p[2] for p in self.pending]
        if len(self.pending)<required or (required>1 and stamp-self.pending[0][1]<cfg['min_confirmation_span']):
            decision['reason']='confirming_floor'
            return None,decision
        phase='reacquired' if self.reacquiring else 'tracked' if self.anchor is not None else 'initialized'
        decision.update(accepted=True,reason=phase,confirmed_floor=raw)
        self.anchor,self.anchor_stamp=raw,stamp
        self.ever_trusted=True
        self.reacquiring=False
        return raw,decision


def valid_floor_proof(payload,config,min_confidence,not_before):
    if (payload.get('schema_version')!=SCHEMA or payload.get('floor_backend')!=BACKEND
            or payload.get('status')!='ok' or type(payload.get('floor')) is not int):
        return False
    proof=payload.get('floor_filter')
    if not isinstance(proof,dict) or proof.get('accepted') is not True or proof.get('config_digest')!=config_digest(config):
        return False
    floor,stamp=payload['floor'],payload.get('image_stamp')
    phase=proof.get('reason')
    if phase not in ('initialized','tracked','reacquired'):
        return False
    count=(config['reacquire_confirmations'] if phase=='reacquired' else
           config['bootstrap_confirmations'] if phase=='initialized' else config['confirmations'])
    stamps,values,confidences=(proof.get(key) for key in ('confirmation_stamps','observed_floors','confirmation_confidences'))
    if (proof.get('required_confirmations')!=count or not all(isinstance(a,list) and len(a)==count for a in (stamps,values,confidences))
            or not all(number(s) and s>not_before for s in stamps)
            or not all(stamps[i]>stamps[i-1] for i in range(1,count))
            or not all(type(v) is int and v==floor for v in values)
            or not all(number(c) and min_confidence<=c<=1 for c in confidences)
            or stamp!=stamps[-1] or proof.get('confirmed_floor')!=floor
            or not (config['min_confirmation_span'] if count>1 else 0)<=stamps[-1]-stamps[0]<=config['max_confirmation_span']):
        return False
    anchor,anchor_stamp=proof.get('anchor_floor'),proof.get('anchor_stamp')
    if phase=='tracked':
        if type(anchor) is not int or not number(anchor_stamp) or not 0<stamp-anchor_stamp<=config['max_anchor_age']:
            return False
        if abs(floor-anchor)>allowed_delta(stamp-anchor_stamp,config):
            return False
    elif anchor is not None or anchor_stamp is not None:
        return False
    return True


def adapted_floor_gate(base,config=None):
    cfg=validate_config(DEFAULTS if config is None else config)
    class FloorGuardGate(base):
        def arm(self,mode,field,expected,not_before,request_id=None,schema_version=1):
            if not hasattr(self,'_original_confirmation_rule'):
                self._original_confirmation_rule=(self.confirmations,self.min_span)
            self.confirmations,self.min_span=self._original_confirmation_rule
            if mode=='floor_monitor':
                self.confirmations=max(self.confirmations,cfg['target_confirmations'])
                self.min_span=max(self.min_span,cfg['min_confirmation_span'])
            return super().arm(mode,field,expected,not_before,request_id,
                               SCHEMA if mode=='floor_monitor' else schema_version)

        def feed(self,text,now):
            can_credit=False
            previous_sequence=self.sequence if hasattr(self,'sequence') else None
            if self.mode=='floor_monitor':
                try:payload=json.loads(text)
                except (ValueError,TypeError):return super().feed(text,now)
                if (isinstance(payload,dict) and payload.get('mode')==self.mode
                        and payload.get('request_id')==self.request_id and payload.get('message_type','result')=='result'):
                    if not valid_floor_proof(payload,cfg,self.min_confidence,self.not_before):
                        with self.condition:
                            self.reset('楼层未知、跳变或时序确认不足')
                            self.condition.notify_all()
                        return False
                    can_credit=(type(payload.get('sequence')) is int and
                                (previous_sequence is None or payload['sequence']>previous_sequence))
            ready=super().feed(text,now)
            if can_credit:
                # A bootstrap/reacquisition packet can already prove several distinct
                # target images. Credit those, rather than requiring redundant API calls.
                with self.condition:
                    if (self.last_payload is not None and self.last_payload.get('sequence')==payload.get('sequence')
                            and self.last_payload.get('image_stamp')==payload.get('image_stamp')
                            and self.count>0 and payload.get('floor')==self.expected):
                        stamps=payload['floor_filter']['confirmation_stamps']
                        if len(stamps)>1:
                            self.count=max(self.count,len(stamps))
                            self.first_stamp=min(self.first_stamp,stamps[0])
                            self.ready=self.count>=self.confirmations and stamps[-1]-self.first_stamp>=self.min_span
                            self.condition.notify_all()
                            ready=self.ready
            return ready
    return FloorGuardGate
