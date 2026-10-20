"""Observation telemetry, independent of control messages and model inputs."""
from collections import deque
import json
import math
import threading
import time
import uuid

TIMING_TOPIC = "/elevator/perception/stage_timing"


class StageTiming:
    def __init__(self, source, publish, now):
        self.source, self.publish, self.now = source, publish, now
        self.instance = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.local = threading.local()
        self.sequence = 0
        self.stage = None
        self.frames = deque(maxlen=180)
        self.first_frame = False

    def emit(self, event, context=None, **values):
        with self.lock:
            self.sequence += 1
            row = dict(schema_version=1, source=self.source, instance_id=self.instance,
                       event_sequence=self.sequence, event=event, ros_stamp=self.now(),
                       monotonic=time.monotonic(), **(context or self.stage or {}))
            row.update(values)
            self.publish(row)

    def begin(self, mode, request_id=None, not_before=None):
        with self.lock:
            self.stage = None if mode == "idle" else dict(
                stage_id=uuid.uuid4().hex, mode=mode, request_id=request_id,
                not_before_stamp=self.now() if not_before is None else not_before)
            self.first_frame = False
            if self.stage:
                self.emit("stage_request_received")
                for frame in self.frames:
                    self._frame_ready(*frame)

    def frame(self, stamp):
        with self.lock:
            frame = (stamp, self.now(), time.monotonic())
            self.frames.append(frame)
            self._frame_ready(*frame)

    def _frame_ready(self, stamp, received, mono):
        if (self.stage and not self.first_frame and isinstance(stamp, (int, float))
                and math.isfinite(stamp) and stamp > 0
                and stamp >= self.stage["not_before_stamp"]
                and -.1 <= self.now() - stamp <= 2):
            self.first_frame = True
            self.emit("first_fresh_frame_received", image_stamp=stamp,
                      ros_stamp=received, monotonic=mono)

    def select(self, image_stamp, stage=None):
        with self.lock:
            context = dict((self.stage if stage is None else stage) or {}, inference_id=uuid.uuid4().hex, image_stamp=image_stamp)
            self.local.context = context
            self.emit("frame_selected", context)
            return context

    def finish(self, context, status, values=None, accepted=True):
        self.emit("inference_result", context, status=status, values=values or {}, accepted=accepted)
        self.local.context = None

    def wrap(self, client):
        timing = self

        class TimedClient:
            def __getattr__(self, key):
                return getattr(client, key)

            def complete(self, *args, **kwargs):
                context = getattr(timing.local, "context", None)
                if context is None:
                    return client.complete(*args, **kwargs)
                call = dict(context, api_call_id=uuid.uuid4().hex)
                timing.emit("vlm_request_started", call)
                started = time.monotonic()
                try:
                    result = client.complete(*args, **kwargs)
                except Exception:
                    timing.emit("vlm_response_received", call, status="error",
                                duration_s=time.monotonic()-started)
                    raise
                timing.emit("vlm_response_received", call, status="ok",
                            duration_s=time.monotonic()-started)
                return result

        return TimedClient()


def ros_timing(rospy, String, source):
    publisher = rospy.Publisher(TIMING_TOPIC, String, queue_size=100)
    return StageTiming(source, lambda row: publisher.publish(String(data=json.dumps(row, allow_nan=False))),
                       lambda: rospy.Time.now().to_sec())
