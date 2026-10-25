"""Time-aware floor rejection, not number correction. Stdlib-only shared contract."""
import hashlib
import json
import math

SCHEMA = 6
BACKEND = 'floor_track_guard_v2'
DEFAULTS = {'seconds_per_floor':4.0, 'base_slack_floors':1, 'max_jump_floors':3,
            'max_anchor_age':12.0, 'confirmations':1, 'bootstrap_confirmations':2,
            'target_confirmations':2, 'reacquire_confirmations':3,
            'min_confirmation_span':.5, 'max_confirmation_span':12.0,
            'direction_confirmations':2,'recovery_confirmations':4,
            'recovery_min_span':4.0,'recovery_max_gap':5.0}


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


def validate_config(config):
    if set(config) != set(DEFAULTS):
        raise ValueError('floor filter config fields differ')
    for key in ('seconds_per_floor','max_anchor_age','min_confirmation_span','max_confirmation_span','recovery_min_span','recovery_max_gap'):
        if not number(config[key]) or config[key] <= 0:
            raise ValueError('Invalid floor filter '+key)
    for key in ('base_slack_floors','max_jump_floors','confirmations','bootstrap_confirmations','target_confirmations','reacquire_confirmations','direction_confirmations','recovery_confirmations'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError('Invalid floor filter '+key)
    if not (1 <= config['confirmations'] <= config['bootstrap_confirmations'] <= config['reacquire_confirmations'] <= 6
            and 2 <= config['bootstrap_confirmations'] and 2 <= config['target_confirmations'] <= 6
            and 2 <= config['direction_confirmations'] <= 5 and 4 <= config['recovery_confirmations'] <= 12
            and config['min_confirmation_span'] <= config['recovery_min_span'] <= config['max_confirmation_span']
            and config['base_slack_floors'] <= config['max_jump_floors'] <= 5
            and config['min_confirmation_span'] <= config['max_confirmation_span'] <= config['max_anchor_age']):
        raise ValueError('Inconsistent floor filter limits')
    return dict(config)


def config_digest(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def allowed_delta(elapsed, config):
    return min(config['max_jump_floors'],config['base_slack_floors']+int(max(0,elapsed)/config['seconds_per_floor']))


def sign(value):
    return (value>0)-(value<0)


def history_delta(elapsed,config):
    # The single-step cap must not cap the total travel of a longer observed trace.
    return config['base_slack_floors']+int(max(0,elapsed)/config['seconds_per_floor'])


def coherent_trace(trace,config,min_confidence):
    if not isinstance(trace,list) or not config['recovery_confirmations']<=len(trace)<=128:
        return False
    for item in trace:
        if (not isinstance(item,dict) or type(item.get('floor')) is not int or not -10<=item['floor']<=150
                or not number(item.get('stamp')) or not number(item.get('confidence'))
                or not min_confidence<=item['confidence']<=1):return False
    span=trace[-1]['stamp']-trace[0]['stamp']
    if not config['recovery_min_span']<=span<=config['max_confirmation_span']:return False
    direction=sign(trace[-1]['floor']-trace[0]['floor'])
    if direction==0:return False  # Repeating one questionable number is not a new trajectory.
    for i,item in enumerate(trace):
        if i and not 0<item['stamp']-trace[i-1]['stamp']<=config['recovery_max_gap']:return False
        if i and direction*(item['floor']-trace[i-1]['floor'])<0:return False
        for earlier in trace[:i]:
            if abs(item['floor']-earlier['floor'])>history_delta(item['stamp']-earlier['stamp'],config):return False
    return True


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
        self.history=[]
        self.direction=0
        self.direction_vote=0
        self.direction_count=0
        self.quarantine=[]
        self.last_trusted=None

    def _reject(self,raw,stamp,confidence,minimum,decision):
        cfg=self.config
        item={'floor':raw,'stamp':stamp,'confidence':confidence}
        trace=[p for p in self.quarantine if stamp-p['stamp']<=cfg['max_confirmation_span']]
        if trace:
            step=sign(raw-trace[-1]['floor'])
            trend=sign(trace[-1]['floor']-trace[0]['floor'])
            if (stamp-trace[-1]['stamp']>cfg['recovery_max_gap'] or (step and trend and step!=trend)
                    or any(abs(raw-p['floor'])>history_delta(stamp-p['stamp'],cfg) for p in trace)):
                trace=[]
        self.quarantine=(trace+[item])[-128:]
        decision['recovery_observations']=len(self.quarantine)
        if not coherent_trace(self.quarantine,cfg,minimum):return None,decision
        recovery=[dict(p) for p in self.quarantine]
        self.direction=sign(raw-recovery[0]['floor'])
        decision.update(accepted=True,reason='recovered',confirmed_floor=raw,
                        required_confirmations=1,confirmation_stamps=[stamp],observed_floors=[raw],
                        confirmation_confidences=[confidence],recovery_history=recovery,direction=self.direction)
        self.anchor,self.anchor_stamp=raw,stamp
        self.last_trusted=item
        self.history=recovery
        self.pending=[(raw,stamp,confidence)]
        self.ever_trusted=True;self.reacquiring=False;self.quarantine=[]
        return raw,decision

    def observe(self, raw, stamp, confidence, min_confidence, not_before=0):
        cfg=self.config
        decision={'accepted':False,'raw_floor':raw,'reason':'unknown_reading','config_digest':self.digest,
                  'anchor_floor':self.anchor,'anchor_stamp':self.anchor_stamp,'elapsed':None,'allowed_delta':None,
                  'required_confirmations':None}
        if not number(stamp) or stamp <= not_before or (self.last_seen is not None and stamp <= self.last_seen):
            decision['reason']='old_or_duplicate_image'
            return None,decision
        self.last_seen=stamp
        self.history=[p for p in self.history if stamp-p['stamp']<=cfg['max_anchor_age']]
        if self.anchor_stamp is not None and stamp-self.anchor_stamp > cfg['max_anchor_age']:
            self.anchor=self.anchor_stamp=None
            self.pending=[]
            self.reacquiring=True
            self.history=[]
            if not self.direction:self.direction_vote=self.direction_count=0
        decision.update(anchor_floor=self.anchor,anchor_stamp=self.anchor_stamp,direction=self.direction,
                        reference_history=[dict(p) for p in self.history],
                        previous_trusted_floor=None if self.last_trusted is None else self.last_trusted['floor'])
        if (type(raw) is not int or not -10 <= raw <= 150 or not number(confidence)
                or not number(min_confidence) or not min_confidence <= confidence <= 1):
            self.pending=[]
            self.quarantine=[]
            decision['reason']='unknown_or_low_confidence'
            return None,decision
        required=(cfg['reacquire_confirmations'] if self.reacquiring else
                  cfg['bootstrap_confirmations'] if self.anchor is None else cfg['confirmations'])
        decision['required_confirmations']=required
        reference=self.anchor if self.anchor is not None else None if self.last_trusted is None else self.last_trusted['floor']
        if self.direction and reference is not None and self.direction*(raw-reference)<0:
            self.pending=[];decision['reason']='opposite_to_confirmed_direction'
            return self._reject(raw,stamp,confidence,min_confidence,decision)
        if self.anchor is not None:
            elapsed=stamp-self.anchor_stamp
            limit=allowed_delta(elapsed,cfg)
            decision.update(elapsed=elapsed,allowed_delta=limit)
            if abs(raw-self.anchor)>limit:
                self.pending=[]
                decision['reason']='implausible_jump'
                return self._reject(raw,stamp,confidence,min_confidence,decision)
            if any(abs(raw-p['floor'])>history_delta(stamp-p['stamp'],cfg) for p in self.history):
                self.pending=[];decision['reason']='history_inconsistent'
                return self._reject(raw,stamp,confidence,min_confidence,decision)
        if (self.pending and (self.pending[-1][0]!=raw or stamp-self.pending[0][1]>cfg['max_confirmation_span'])):
            self.pending=[]
        self.pending.append((raw,stamp,confidence))
        if len(self.pending)>required:
            self.pending=([self.pending[0]]+self.pending[-(required-1):]) if required>1 else self.pending[-1:]
        decision['confirmation_stamps']=[p[1] for p in self.pending]
        decision['observed_floors']=[p[0] for p in self.pending]
        decision['confirmation_confidences']=[p[2] for p in self.pending]
        if len(self.pending)<required or (required>1 and stamp-self.pending[0][1]<cfg['min_confirmation_span']):
            decision['reason']='confirming_floor'
            return None,decision
        phase='reacquired' if self.reacquiring else 'tracked' if self.anchor is not None else 'initialized'
        decision.update(accepted=True,reason=phase,confirmed_floor=raw)
        if self.anchor is not None and raw!=self.anchor and not self.direction:
            vote=sign(raw-self.anchor)
            self.direction_count=self.direction_count+1 if vote==self.direction_vote else 1
            self.direction_vote=vote
            if self.direction_count>=cfg['direction_confirmations']:self.direction=vote
        # Proof direction describes the check made before this observation.
        decision['direction_after']=self.direction
        if phase in ('initialized','reacquired'):
            self.history=[{'floor':p[0],'stamp':p[1],'confidence':p[2]} for p in self.pending]
        else:self.history.append({'floor':raw,'stamp':stamp,'confidence':confidence})
        self.history=self.history[-128:]
        self.last_trusted={'floor':raw,'stamp':stamp,'confidence':confidence}
        self.quarantine=[]
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
    if phase not in ('initialized','tracked','reacquired','recovered'):
        return False
    count=(1 if phase=='recovered' else config['reacquire_confirmations'] if phase=='reacquired' else
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
    direction=proof.get('direction')
    if type(direction) is not int or direction not in (-1,0,1):return False
    if phase=='recovered':
        trace=proof.get('recovery_history')
        if not coherent_trace(trace,config,min_confidence):return False
        if (trace[-1]['floor']!=floor or trace[-1]['stamp']!=stamp or trace[0]['stamp']<=not_before
                or sign(trace[-1]['floor']-trace[0]['floor'])!=direction):return False
        return True
    if phase=='tracked':
        if type(anchor) is not int or not number(anchor_stamp) or not 0<stamp-anchor_stamp<=config['max_anchor_age']:
            return False
        if abs(floor-anchor)>allowed_delta(stamp-anchor_stamp,config):
            return False
        if direction and direction*(floor-anchor)<0:return False
        refs=proof.get('reference_history')
        if not isinstance(refs,list) or not 1<=len(refs)<=128 or not all(isinstance(p,dict) for p in refs):return False
        if refs[-1].get('floor')!=anchor or refs[-1].get('stamp')!=anchor_stamp:return False
        for p in refs:
            if (type(p.get('floor')) is not int or not -10<=p['floor']<=150 or not number(p.get('stamp'))
                    or not 0<stamp-p['stamp']<=config['max_anchor_age'] or p['stamp']<=not_before
                    or not number(p.get('confidence')) or not min_confidence<=p['confidence']<=1
                    or abs(floor-p['floor'])>history_delta(stamp-p['stamp'],config)):return False
    elif anchor is not None or anchor_stamp is not None:
        return False
    elif direction and type(proof.get('previous_trusted_floor')) is int and direction*(floor-proof['previous_trusted_floor'])<0:
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
