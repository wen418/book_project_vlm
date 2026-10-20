#!/usr/bin/python3
"""Upper-computer experiment recorder. Running this file selects --program quantify."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import csv
from datetime import datetime, timezone, timedelta
import html
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import uuid
from stage_timeline import build_timeline, anchor_rows, timeline_html, annotation_alignment

ROOT = Path(__file__).resolve().parent
EVENT_PREFIX = "__ELEVATOR_EVENT__"
DOOR_FRAME_INTERVAL_S = 0.1


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def elapsed(end, start, key="monotonic"):
    if not end or not start or not number(end.get(key)) or not number(start.get(key)):
        return None
    value = end[key] - start[key]
    return round(value, 6) if value >= 0 else None


def return_metrics(events):
    start_index = next((i for i, e in enumerate(events) if e["event"] == "navigation_start" and
                        e.get("point") in ("origin", "entry_wait")), None)
    result = dict(return_navigation_s=None, return_settle_s=None, button_to_door_wait_s=None,
                  return_max_heading_change_deg=None, return_heading_status="not_started")
    if start_index is None:
        return result
    start = events[start_index]
    window = events[start_index+1:]
    end = next((i for i, e in enumerate(window) if e["event"] in ("navigation_start", "door_wait_start")), len(window))
    window = window[:end]
    first = lambda name: next((e for e in window if e["event"] == name), None)
    arrival, settled = first("arrival_reported"), first("navigation_arrived")
    result["return_navigation_s"] = elapsed(arrival, start)
    result["return_settle_s"] = elapsed(settled, arrival)
    button = next((e for e in events[:start_index] if e["event"] == "observe_confirmed" and
                   e.get("result", {}).get("mode") == "button_verify"), None)
    door_wait = next((e for e in events[start_index:] if e["event"] == "door_wait_start"), None)
    result["button_to_door_wait_s"] = elapsed(door_wait, button)
    reference = start.get("heading_reference_deg")
    samples = [e for e in window if e["event"] == "motion_sample" and e.get("point") == start["point"]]
    if samples and number(reference):
        angles = [e["pose"][2]-math.radians(reference) for e in samples]
        result["return_max_heading_change_deg"] = max(abs(math.degrees(math.atan2(math.sin(a), math.cos(a)))) for a in angles)
    if start.get("return_no_turn"):
        result["return_heading_status"] = "verified" if first("return_heading_verified") else "unverified"
        if first("return_heading_failed"):
            result["return_heading_status"] = "failed"
    else:
        result["return_heading_status"] = "rotation_allowed"
    return result


def entry_metrics(events):
    """Use only one lower-computer monotonic clock for the entry chain."""
    start = next((i for i, e in enumerate(events) if e["event"] == "door_wait_start"), None)
    if start is None:
        return {"measurement_status": "entry_not_started", **return_metrics(events)}
    chain = events[start:]
    # Do not borrow exit-door events or later navigation from the full demo.
    finish = next((i for i, e in enumerate(chain) if e["event"] == "motion_measurement_end" and e.get("point") == "inside"), None)
    if finish is None:
        finish = next((i for i, e in enumerate(chain) if e["event"] == "navigation_arrived" and e.get("point") == "inside"), None)
    if finish is not None:
        chain = chain[:finish + 1]
    def event(name, point=None):
        return next((e for e in chain if e["event"] == name and (point is None or e.get("point") == point)), None)
    wait, confirmed = event("door_wait_start"), event("door_confirmed")
    issue, accepted = event("command_issue", "inside"), event("command_accepted", "inside")
    arrival, motion = event("arrival_reported", "inside"), event("motion_started", "inside")
    motion_end = event("motion_measurement_end", "inside")
    if motion_end and motion_end.get("reason") != "detected":
        motion = None
    onset = {"monotonic": motion["onset_monotonic"]} if motion else None
    result = confirmed.get("result", {}) if confirmed else {}
    latency = result.get("latency_ms")
    metrics = {
        "measurement_status": "complete" if all((confirmed, issue, accepted, arrival, motion)) else "incomplete",
        "door_wait_to_recognition_s": elapsed(confirmed, wait),
        "door_inference_s": round(latency / 1000, 6) if number(latency) and latency >= 0 else None,
        "recognition_to_command_s": elapsed(issue, confirmed),
        "command_call_s": elapsed(accepted, issue),
        "command_to_motion_s": elapsed(onset, issue),
        "motion_to_entry_s": elapsed(arrival, onset),
        "recognition_to_entry_s": elapsed(arrival, confirmed),
        "wait_to_entry_s": elapsed(arrival, wait),
        "entry_settle_s": elapsed(event("navigation_arrived", "inside"), arrival),
        "motion_measurement_reason": motion_end.get("reason") if motion_end else "missing",
        "door_image_stamp": result.get("image_stamp"),
        "door_confirmed_ros_stamp": confirmed.get("ros_stamp") if confirmed else None,
        "command_ros_stamp": issue.get("ros_stamp") if issue else None,
        "motion_ros_stamp": motion.get("onset_ros_stamp") if motion else None,
        "entry_ros_stamp": arrival.get("ros_stamp") if arrival else None,
    }
    if not all(number(metrics.get(k)) for k in ("door_inference_s", "command_to_motion_s", "motion_to_entry_s")):
        metrics["measurement_status"] = "incomplete"
    return {**metrics, **return_metrics(events)}


def statistics(values):
    values = sorted(v for v in values if number(v))
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None}
    def percentile(p):
        position = (len(values) - 1) * p
        low = int(position)
        return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)
    return dict(n=len(values), mean=sum(values)/len(values), p50=percentile(.5), p95=percentile(.95))


def remove_pending(directory, root):
    """Delete only a marked immediate child of our known staging root."""
    directory, root = Path(directory), Path(root).resolve()
    if directory.is_symlink() or directory.resolve().parent != root:
        raise ValueError("临时结果路径越界")
    marker = json.loads((directory / ".pending.json").read_text(encoding="utf-8"))
    if marker["run_id"] != directory.name or Path(marker["pending_root"]).resolve() != root:
        raise ValueError("临时结果标记不匹配")
    shutil.rmtree(str(directory.resolve()))


def cleanup_abandoned(root):
    # The deployed recorder is Linux. os.kill(pid, 0) is not a safe liveness probe on Windows.
    if os.name == "nt":
        return
    root = Path(root).resolve()
    if not root.exists():
        return
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not (directory / ".pending.json").is_file():
            continue
        marker = json.loads((directory / ".pending.json").read_text(encoding="utf-8"))
        pid = marker.get("pid")
        if type(pid) is not int or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            remove_pending(directory, root)
        except PermissionError:
            pass


def workflow_complete(events, args):
    scope = "observe_" + args.observe_only if args.observe_only else ("entry_only" if args.entry_only else "full")
    marker = next((i for i, e in enumerate(events) if e.get("event") == "workflow_completed" and e.get("scope") == scope), None)
    if marker is None:
        return False
    preceding = events[:marker]
    if args.observe_only:
        name = "door_confirmed" if args.observe_only == "door" else "observe_confirmed"
        mode = {"button": "button_verify", "floor": "floor_monitor", "door": "door_monitor"}[args.observe_only]
        return any(e.get("event") == name and e.get("result", {}).get("mode") == mode for e in preceding)
    return any(e.get("event") == "navigation_arrived" and e.get("point") ==
               ("inside" if args.entry_only else "exit") for e in preceding)


def button_metrics(events):
    start = next((e for e in events if e["event"] == "observe_start" and e.get("mode") == "button_verify"), None)
    request = start.get("request_id") if start else None
    matches = lambda e: start is not None and e.get("result", {}).get("mode") == "button_verify" and e["result"].get("request_id") == request
    first = next((e for e in events if e["event"] == "button_first_positive" and matches(e)), None)
    confirmed = next((e for e in events if e["event"] == "observe_confirmed" and matches(e)), None)
    result = (confirmed or {}).get("result", {})
    latency = result.get("latency_ms")
    return dict(button_wait_to_recognition_s=elapsed(confirmed, start),
                button_wait_to_first_positive_s=elapsed(first, start),
                button_confirmation_s=elapsed(confirmed, first),
                button_inference_s=latency/1000 if number(latency) and latency >= 0 else None,
                button_first_positive_ros_stamp=(first or {}).get("ros_stamp"),
                button_confirmed_ros_stamp=(confirmed or {}).get("ros_stamp"),
                button_image_stamp=result.get("image_stamp"))


class QuantificationSession:
    def __init__(self, args, config):
        self.args, self.config = args, config
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        self.run_id = stamp + "_" + uuid.uuid4().hex[:8]
        self.result_root = Path(args.result_dir).expanduser().resolve()
        self.pending_root = self.result_root.parent / ("." + self.result_root.name + "_pending")
        cleanup_abandoned(self.pending_root)
        self.directory = self.pending_root / self.run_id
        (self.directory / "images").mkdir(parents=True)
        write_json(self.directory / ".pending.json", dict(run_id=self.run_id, pending_root=str(self.pending_root), pid=os.getpid()))
        self.lock = threading.RLock()
        self.events, self.observations, self.seen = [], [], set()
        self.timing_events, self.timing_seen, self.ready_notified = [], set(), set()
        self.frames, self.frame_bytes = OrderedDict(), 0
        self.views, self.pending_views = [], []
        self.door_frames = []
        self.door_window = dict(start_ros_stamp=None, end_ros_stamp=None, target_interval_s=DOOR_FRAME_INTERVAL_S)
        self.last_door_frame = -math.inf
        self.button_frames = []
        self.button_window = dict(start_ros_stamp=None, end_ros_stamp=None, target_interval_s=DOOR_FRAME_INTERVAL_S)
        self.last_button_frame = -math.inf
        self.current_view = args.view if args.view != "auto" else "unverified"
        self.latest_head = None
        self.last_snapshot = -math.inf
        self.subscribers, self.fatal = [], None
        self.closed = False
        self.diagnostics = dict(camera_frames=0, image_errors=0, event_errors=0, last_error="")
        self.events_stream = (self.directory / "events.jsonl").open("a", encoding="utf-8", buffering=1)
        self.observations_stream = (self.directory / "observations.jsonl").open("a", encoding="utf-8", buffering=1)
        self.door_frames_stream = (self.directory / "door_frames.jsonl").open("a", encoding="utf-8", buffering=1)
        self.button_frames_stream = (self.directory / "button_frames.jsonl").open("a", encoding="utf-8", buffering=1)
        self.timing_stream = (self.directory / "perception_timing.jsonl").open("a", encoding="utf-8", buffering=1)
        diagnostic = (getattr(args, "check_startup", False) or getattr(args, "perception_only", False) or
                      getattr(args, "diagnostic", False))
        self.metadata = dict(schema_version=1, run_id=self.run_id, data_kind="diagnostic" if diagnostic else "real",
                             entry_plan=args.entry_plan, entry_pose=args.entry_pose, requested_entry_pose=args.entry_pose,
                             return_no_turn=args.entry_plan in ("origin_no_turn", "reduced_backtrack"),
                             door_condition=args.door_condition, trial_note=args.trial_note,
                             entry_only=args.entry_only, observe_only=args.observe_only,
                             requested_view=args.view, motion_topic=args.motion_topic,
                             config=config, started_at=datetime.now(timezone.utc).isoformat())
        write_json(self.directory / "run.json", self.metadata)

    def start(self, rospy):
        import cv2
        import numpy as np
        from cv_bridge import CvBridge
        from sensor_msgs.msg import Image
        from std_msgs.msg import String
        from elevator_extra_perception import decode_image
        self.rospy, self.cv2, self.np, self.bridge = rospy, cv2, np, CvBridge()
        self.decode_image = decode_image
        self.start_ros = rospy.Time.now().to_sec()
        self.metadata["recorder_start_ros_stamp"] = self.start_ros
        self.metadata["door_node_parameters"] = {name: rospy.get_param("/elevator_vlm_api_perception/" + name, None)
            for name in ("camera", "inference_hz", "max_frame_age_sec", "max_result_age_sec", "temporal_filter", "jpeg_quality", "max_image_edge", "model_config")}
        write_json(self.directory / "run.json", self.metadata)
        self.subscribers.append(rospy.Subscriber(self.config["camera"]["topic"], Image, self.on_image,
                                                queue_size=10, buff_size=32*1024*1024))
        timing_subscription = rospy.Subscriber("/elevator/perception/stage_timing", String,
                                               self.on_timing, queue_size=1000)
        self.subscribers.append(timing_subscription)
        for source, topic in (("door", self.config["perception"]["door_state_topic"]),
                              ("extra", self.config["perception"]["extra_state_topic"])):
            self.subscribers.append(rospy.Subscriber(topic, String,
                lambda msg, source=source: self.on_state(source, msg), queue_size=100))
        # The recorder must see a fresh camera frame before any remote actions.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                if self.frames and timing_subscription.get_num_connections() >= 2:
                    return
                if self.fatal:
                    raise RuntimeError(self.fatal)
            time.sleep(.05)
        raise RuntimeError("量化记录器未收到新鲜图像或未接通两路阶段遥测，请检查相机并重启新版感知节点")

    def fail(self, exc):
        self.fatal = str(exc)

    def on_image(self, message):
        try:
            stamp = message.header.stamp.to_sec()
            if not number(stamp) or stamp <= 0 or not -0.1 <= self.rospy.Time.now().to_sec() - stamp <= 2:
                return
            image = self.decode_image(message, self.config["camera"]["yuv422_layout"], self.cv2, self.np, self.bridge)
            ok, data = self.cv2.imencode(".jpg", image, [self.cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise RuntimeError("JPEG 编码失败")
            encoded = data.tobytes()
            with self.lock:
                if self.closed:
                    return
                self.diagnostics["camera_frames"] += 1
                if stamp not in self.frames:
                    self.frames[stamp] = encoded
                    self.frame_bytes += len(encoded)
                retention = self.config["perception"]["max_result_age"] + 10
                while self.frames and (stamp - next(iter(self.frames)) > retention or self.frame_bytes > 256*1024*1024):
                    _, old = self.frames.popitem(last=False)
                    self.frame_bytes -= len(old)
                self.record_door_frame(stamp)
                self.record_button_frame(stamp)
                for row in self.observations:
                    if row["image_stamp"] == stamp and not row.get("image"):
                        row["image"] = self.save_frame(stamp)
                        row["image_match"] = "exact"
                remaining = []
                for request in self.pending_views:
                    if stamp >= request["not_before"]:
                        self.views.append(dict(view=request["view"], image=self.save_frame(stamp),
                                               image_stamp=stamp, head=request.get("head"), kind="head_settled"))
                        self.current_view = request["view"]
                    else:
                        remaining.append(request)
                self.pending_views = remaining
                if stamp - self.last_snapshot >= 2:
                    self.views.append(dict(view=self.current_view, image=self.save_frame(stamp),
                                           image_stamp=stamp, kind="timeline"))
                    self.last_snapshot = stamp
        except (OSError, RuntimeError) as exc:
            self.fail(exc)
        except Exception as exc:
            with self.lock:
                self.diagnostics["image_errors"] += 1
                self.diagnostics["last_error"] = str(exc)

    def save_frame(self, stamp):
        if stamp not in self.frames:
            return ""
        relative = "images/frame_" + format(stamp, ".9f") + ".jpg"
        path = self.directory / relative
        if not path.exists():
            path.write_bytes(self.frames[stamp])
        return relative

    def record_door_frame(self, stamp):
        start, end = self.door_window["start_ros_stamp"], self.door_window["end_ros_stamp"]
        if (start is None or stamp < start - 1 or (end is not None and stamp > end) or
                stamp - self.last_door_frame < DOOR_FRAME_INTERVAL_S - .000001):
            return
        relative = self.save_frame(stamp)
        if not relative:
            return
        row = dict(image_stamp=stamp, offset_s=round(stamp-start, 6), image=relative)
        self.door_frames.append(row)
        self.last_door_frame = stamp
        self.door_frames_stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    def record_button_frame(self, stamp):
        start, end = self.button_window["start_ros_stamp"], self.button_window["end_ros_stamp"]
        if (start is None or stamp < start - 1 or (end is not None and stamp > end) or
                stamp - self.last_button_frame < DOOR_FRAME_INTERVAL_S - .000001):
            return
        relative = self.save_frame(stamp)
        if relative:
            row = dict(image_stamp=stamp, offset_s=round(stamp-start, 6), image=relative)
            self.button_frames.append(row)
            self.last_button_frame = stamp
            self.button_frames_stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    def on_timing(self, message):
        try:
            row = json.loads(message.data)
            if (not isinstance(row, dict) or row.get("schema_version") != 1
                    or row.get("source") not in ("door", "extra")
                    or row.get("mode") not in ("door_monitor", "button_verify", "floor_monitor")
                    or not number(row.get("ros_stamp")) or row["ros_stamp"] < self.start_ros
                    or type(row.get("event_sequence")) is not int
                    or not row.get("stage_id") or not row.get("instance_id")):
                return
            key = (row["instance_id"], row["event_sequence"])
            with self.lock:
                if self.closed or key in self.timing_seen:
                    return
                self.timing_seen.add(key)
                self.timing_events.append(row)
                self.timing_stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")
                if (row.get("event") == "inference_result" and row.get("mode") == "button_verify"
                        and row.get("accepted") is True and row.get("status") == "ok"
                        and row.get("values", {}).get("button_lit") is False
                        and number(row.get("image_stamp"))
                        and row["image_stamp"] >= row.get("not_before_stamp", math.inf)
                        and row["stage_id"] not in self.ready_notified):
                    self.ready_notified.add(row["stage_id"])
                    print("[按键测试就绪] 已用本阶段新图像识别到按钮未亮，现在可以人工按下行按钮。"
                          " ROS时间=%.6f" % row["ros_stamp"], flush=True)
        except (ValueError, TypeError, KeyError):
            return
        except Exception as exc:
            self.fail(exc)

    def on_state(self, source, message):
        try:
            payload = json.loads(message.data)
            if not isinstance(payload, dict) or payload.get("message_type", "result") != "result":
                return
            stamp, seq = payload.get("image_stamp"), payload.get("sequence")
            mode = payload.get("mode")
            if not number(stamp) or stamp <= 0 or type(seq) is not int or mode not in ("door_monitor", "button_verify", "floor_monitor"):
                return
            if stamp < self.start_ros:
                return
            if payload.get("status") not in ("ok", "uncertain", "error"):
                return
            key = (source, seq, stamp, mode, payload.get("request_id"))
            with self.lock:
                if self.closed or key in self.seen:
                    return
                self.seen.add(key)
                field = {"door_monitor": "door_closed", "button_verify": "button_lit", "floor_monitor": "floor"}[mode]
                latency = payload.get("latency_ms")
                row = dict(id="obs_%05d" % (len(self.observations) + 1), source=source, sequence=seq,
                           mode=mode, field=field, prediction=payload.get(field), status=payload.get("status"),
                           confidence=payload.get("confidence", {}).get(field), image_stamp=stamp,
                           received_ros_stamp=self.rospy.Time.now().to_sec(), view=self.current_view,
                           head=self.latest_head, latency_ms=latency, payload=payload,
                           image=self.save_frame(stamp))
                row["image_match"] = "exact" if row["image"] else "missing"
                self.observations.append(row)
                self.observations_stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        except (ValueError, TypeError, AttributeError):
            return
        except Exception as exc:
            self.fail(exc)

    def on_line(self, line):
        if not line.startswith(EVENT_PREFIX):
            return
        try:
            event = json.loads(line[len(EVENT_PREFIX):])
            if event.get("run_id") != self.run_id:
                raise ValueError("量化事件 run_id 不匹配")
            with self.lock:
                expected = len(self.events) + 1
                if event.get("event_sequence") != expected:
                    raise ValueError("量化事件序号不连续")
                event["upper_received_ros_stamp"] = self.rospy.Time.now().to_sec()
                self.events_stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                self.events.append(event)
                if event["event"] == "observe_start" and event.get("mode") == "button_verify" and self.button_window["start_ros_stamp"] is None:
                    self.button_window.update(start_ros_stamp=event["ros_stamp"], request_id=event["request_id"])
                    for stamp in sorted(self.frames):
                        self.record_button_frame(stamp)
                elif event["event"] == "door_wait_start" and self.door_window["start_ros_stamp"] is None:
                    self.door_window["start_ros_stamp"] = event["ros_stamp"]
                    # SSH events can arrive after camera frames. Include a one-second lead-in.
                    for stamp in sorted(self.frames):
                        self.record_door_frame(stamp)
                elif event["event"] == "quantification_start":
                    self.metadata["config"] = event["config"]
                    if event["entry_plan"] == "reduced_backtrack":
                        pose = event["config"]["points"]["entry_wait"]
                        self.metadata["entry_pose"] = [pose[k] for k in ("x", "y", "theta")]
                    write_json(self.directory / "run.json", self.metadata)
                elif event["event"] in ("door_confirmed", "observe_confirmed"):
                    if (event["event"] == "observe_confirmed" and event["result"].get("mode") == "button_verify" and
                            event["result"].get("request_id") == self.button_window.get("request_id") and
                            self.button_window["end_ros_stamp"] is None):
                        self.button_window["end_ros_stamp"] = event["ros_stamp"] + 1
                    if event["event"] == "door_confirmed" and self.door_window["end_ros_stamp"] is None:
                        self.door_window["end_ros_stamp"] = event["ros_stamp"] + 1
                    # Also preserve the accepted result if its ROS delivery to this subscriber was lost.
                    payload = event["result"]
                    source = "door" if event["event"] == "door_confirmed" else "extra"
                    self.on_state(source, SimpleNamespace(data=json.dumps(payload)))
                elif event["event"] == "head":
                    self.current_view = "head_moving"
                    self.pending_views = []
                elif event["event"] == "head_reached":
                    view = "turned" if event["view"] == "floor" else "normal"
                    self.latest_head = event.get("measured")
                    self.pending_views.append(dict(view=view, not_before=event["ros_stamp"], head=self.latest_head))
                    # A frame can have arrived before its delayed SSH event.
                    available = next((s for s in self.frames if s >= event["ros_stamp"]), None)
                    if available is not None:
                        self.views.append(dict(view=view, image=self.save_frame(available), image_stamp=available,
                                               head=self.latest_head, kind="head_settled"))
                        self.pending_views = []
                        self.current_view = view
        except Exception as exc:
            self.diagnostics["event_errors"] += 1
            self.fail(exc)

    def close(self, exit_code):
        try:
            self._close(exit_code)
        except BaseException:
            if self.directory.exists() and self.directory.parent == self.pending_root:
                for stream in (self.events_stream, self.observations_stream, self.door_frames_stream, self.button_frames_stream, self.timing_stream):
                    stream.close()
                remove_pending(self.directory, self.pending_root)
            raise

    def _close(self, exit_code):
        for subscriber in self.subscribers:
            subscriber.unregister()
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.events_stream.close()
            self.observations_stream.close()
            self.door_frames_stream.close()
            self.button_frames_stream.close()
            self.timing_stream.close()
            contiguous = bool(self.events) and [e.get("event_sequence") for e in self.events] == list(range(1, len(self.events)+1))
            if exit_code != 0 or self.fatal or not contiguous or not workflow_complete(self.events, self.args):
                remove_pending(self.directory, self.pending_root)
                if getattr(self.args, "check_startup", False) and exit_code == 0:
                    print("部署启动检查结束，临时数据已清理，不生成业务测试结果。", flush=True)
                else:
                    print("本轮测试失败或未完成所选流程，临时数据已删除，result 不保存该轮。", flush=True)
                return
            end = self.door_window["end_ros_stamp"]
            door_frames = [row for row in self.door_frames if end is None or row["image_stamp"] <= end]
            write_json(self.directory / "door_frames.json", dict(**self.door_window, frames=door_frames))
            end = self.button_window["end_ros_stamp"]
            write_json(self.directory / "button_frames.json", dict(**self.button_window,
                frames=[row for row in self.button_frames if end is None or row["image_stamp"] <= end]))
            if self.args.view == "auto":
                head_events = [e for e in self.events if e["event"] in ("head", "head_reached")]
                for row in self.observations:
                    previous = [e for e in head_events if e["ros_stamp"] <= row["image_stamp"]]
                    if previous:
                        last = previous[-1]
                        row["view"] = "head_moving" if last["event"] == "head" else ("turned" if last["view"] == "floor" else "normal")
                        row["head"] = last.get("measured")
            metrics = dict(**entry_metrics(self.events), **button_metrics(self.events))
            complete_sequences = [e.get("event_sequence") for e in self.events] == list(range(1, len(self.events)+1))
            summary = dict(run_id=self.run_id, exit_code=exit_code, test_status="success", metrics=metrics,
                           diagnostics=self.diagnostics, recorder_error=self.fatal,
                           events_contiguous=complete_sequences, observation_count=len(self.observations),
                           exact_image_count=sum(bool(o["image"]) for o in self.observations),
                           latency_ms_by_mode={mode: statistics([o["latency_ms"] for o in self.observations if o["mode"] == mode])
                                               for mode in ("door_monitor", "button_verify", "floor_monitor")})
            summary["data_quality"] = dict(missing_exact_images=sum(not bool(o["image"]) for o in self.observations),
                                           pending_view_snapshots=len(self.pending_views))
            summary["stage_timeline"] = build_timeline(self.events, self.timing_events, self.observations, self.start_ros)
            if self.fatal or not complete_sequences:
                metrics["measurement_status"] = "incomplete"
            write_json(self.directory / "observations.json", self.observations)
            write_json(self.directory / "views.json", self.views)
            write_json(self.directory / "summary.json", summary)
            write_csv(self.directory / "timings.csv", [dict(run_id=self.run_id, **metrics)], ["run_id", *metrics])
            labels = [dict(id=o["id"], mode=o["mode"], view=o["view"], prediction=json.dumps(o["prediction"]),
                           image=o["image"], ground_truth="", note="") for o in self.observations]
            write_csv(self.directory / "labels.csv", labels, ["id", "mode", "view", "prediction", "image", "ground_truth", "note"])
            write_json(self.directory / "trial_review.json", dict(
                actual_door_condition="unknown", entry_success=None, actual_door_open_ros_stamp=None,
                actual_door_open_prev_ros_stamp=None, actual_button_lit_ros_stamp=None,
                actual_button_lit_prev_ros_stamp=None, note=""))
            self.metadata.update(finished_at=datetime.now(timezone.utc).isoformat(), exit_code=exit_code)
            write_json(self.directory / "run.json", self.metadata)
            render_report(self.directory, self.observations, self.views, summary)
            render_door_review(self.directory)
            self.result_root.mkdir(parents=True, exist_ok=True)
            destination = self.result_root / self.run_id
            os.replace(str(self.directory), str(destination))
            self.directory = destination
            (self.directory / ".pending.json").unlink()
        print("量化结果：" + str(self.directory), flush=True)


def render_report(directory, observations, views, summary, reviews=None):
    reviews = reviews or {}
    timeline = summary.get("stage_timeline") or load_stage_timeline(directory, observations)
    trial_path = directory / "trial_review.json"
    trial = json.loads(trial_path.read_text(encoding="utf-8")) if trial_path.exists() else {}
    alignments = stage_alignments(timeline, trial, directory)
    write_json(directory / "stage_timeline.json", timeline)
    rows_for_csv = anchor_rows(timeline)
    write_csv(directory / "stage_anchors.csv", rows_for_csv,
              ["stage", "mode", "anchor", "label", "ros_stamp", "from_run_start_s", "image_stamp", "source", "api_call_id", "status"])
    write_csv(directory / "workflow_anchors.csv", timeline.get("workflow",[]),
              ["event_sequence","event","label","ros_stamp","reported_ros_stamp","from_run_start_s","point","view","mode","scope"])
    (directory / "stage_timeline.html").write_text(timeline_html(timeline, alignments), encoding="utf-8")
    esc = lambda value: html.escape(str(value))
    cards = []
    for row in observations:
        review = reviews.get(row["id"], {})
        label = review.get("classification", "unreviewed")
        image = '<img loading="lazy" src="%s">' % esc(row["image"]) if row["image"] else "对应帧缺失，不能以邻帧代替"
        cards.append('<article class="%s"><h3>%s · %s · %s</h3>%s<p>预测 %s；人工真值 %s；%s</p><p>%s</p></article>' % (
            esc(label), esc(row["id"]), esc(row["mode"]), esc(row["view"]), image,
            esc(row["prediction"]), esc(review.get("ground_truth", "待标注")), esc(label), esc(review.get("note", ""))))
    snapshots = ''.join('<article><h3>%s · %s</h3><img loading="lazy" src="%s"><p>ROS %s</p></article>' % (
        esc(v["view"]), esc(v["kind"]), esc(v["image"]), esc(v["image_stamp"])) for v in views)
    names = {"button_wait_to_first_positive_s": "开始观察按键 → 首次识别点亮",
             "actual_button_lit_to_first_positive_s": "人工点亮帧 → 首次有效识别",
             "actual_button_lit_to_recognition_s": "人工点亮帧 → 识别确认通过",
             "actual_door_open_to_recognition_s": "人工全开帧 → 开门确认",
             "button_wait_to_recognition_s": "开始观察按键 → 识别确认通过",
             "button_confirmation_s": "首次识别点亮 → 识别确认通过", "button_inference_s": "按键确认帧推理耗时",
             "door_wait_to_recognition_s": "开始等门 → 程序确认全开", "door_inference_s": "开门识别推理耗时",
             "recognition_to_command_s": "确认全开 → 开始发令", "command_call_s": "发令调用耗时",
             "command_to_motion_s": "开始发令 → 实测起步", "motion_to_entry_s": "实测起步 → 导航到点",
             "recognition_to_entry_s": "确认全开 → 导航到点", "return_navigation_s": "回退导航耗时"}
    rows = ''.join('<tr><td>%s</td><td>%s</td></tr>' % (label, ("%.3f 秒" % summary["metrics"][key])
                   if number(summary["metrics"].get(key)) else "未采到") for key, label in names.items())
    rows = ''.join('<tr><td>%s事件与就绪锚点</td><td>%s</td></tr>' % ("按键" if kind=="button" else "开门", esc(check["message"]))
                   for kind,check in alignments.items()) + rows
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>电梯量化记录</title>
<style>body{font:16px system-ui;max-width:1200px;margin:32px auto;padding:0 20px;background:#f3f5f8;color:#17243b}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
article,pre{background:white;padding:16px;border-radius:10px;overflow:auto}img{width:100%%;height:auto}.incorrect{border:3px solid #c53939}.correct{border:3px solid #328052}</style>
<h1>电梯进梯量化记录</h1><p><a href="labels_review.html">看图核验识别对错</a>：逐张选真实状态，导出 labels.csv，无需对照表格找图片。</p><p>各段耗时已自动记录，不需要人工计时。门全开与按键点亮的画面可人工核对：<a href="door_review.html">打开开门时间核验</a> / <a href="button_review.html">打开按键点亮时间核验</a>。完成进梯以导航到点反馈为准，需结合人工验收。</p>
<p><a href="stage_timeline.html">打开阶段时间锚点</a>：查看到点、头部稳定、接图、VLM调用与确认的先后关系；人工提前操作的情况会单独提示。</p>
<h2>自动计时结果</h2><table cellpadding="9"><tbody>%s</tbody></table><details><summary>完整记录</summary><pre>%s</pre></details><h2>正常及转头视角</h2><div class="grid">%s</div><h2>识别实例</h2><div class="grid">%s</div></html>''' % (
        rows, esc(json.dumps(summary, ensure_ascii=False, indent=2)), snapshots, ''.join(cards))
    (directory / "report.html").write_text(page, encoding="utf-8")
    render_labels_review(directory, observations, summary["run_id"])


def load_stage_timeline(directory, observations=None):
    def read_lines(name):
        path = directory / name
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
    return build_timeline(read_lines("events.jsonl"), read_lines("perception_timing.jsonl"), observations or [])


def stage_alignments(timeline, trial, directory=None):
    checks = {}
    for kind,key in (("button","actual_button_lit"),("door","actual_door_open")):
        opened,previous=trial.get(key+"_ros_stamp"),trial.get(key+"_prev_ros_stamp")
        checks[kind]=annotation_alignment(timeline,kind,opened,previous)
        index=directory/(kind+"_frames.json") if directory else None
        if index and index.exists() and opened is not None and previous is None:
            frames=json.loads(index.read_text(encoding="utf-8")).get("frames",[])
            if frames and opened==frames[0].get("image_stamp"):
                checks[kind]=dict(status="onset_not_observed",message="首张核验图已变化且缺少变化前画面，起点未完整记录，不计算该事件延迟")
    return checks


def render_labels_review(directory, observations, run_id):
    with (directory / "labels.csv").open(encoding="utf-8-sig", newline="") as stream:
        labels = {row["id"]: dict(ground_truth=row.get("ground_truth", "").strip(), note=row.get("note", ""))
                  for row in csv.DictReader(stream)}
    payload = dict(run_id=run_id, observations=[{key: row.get(key) for key in
                   ("id", "mode", "field", "prediction", "image", "view", "status", "image_stamp")} for row in observations], labels=labels)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    template = (ROOT / "templates" / "labels_review.html").read_text(encoding="utf-8")
    (directory / "labels_review.html").write_text(template.replace("__DATA__", encoded), encoding="utf-8")


def prepare_review(directory):
    """Generate offline review pages without changing labels or recorded measurements."""
    observations = json.loads((directory / "observations.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    views = json.loads((directory / "views.json").read_text(encoding="utf-8"))
    path = directory / "reviewed_examples.json"
    reviews = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    render_report(directory, observations, views, summary, reviews)
    render_door_review(directory)
    return dict(directory=str(directory), recognition_review="labels_review.html", timing_review="door_review.html")


def render_door_review(directory):
    """One page keeps door and button annotations together in the same export."""
    trial = json.loads((directory / "trial_review.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    metrics = summary["metrics"]
    timeline = summary.get("stage_timeline") or load_stage_timeline(directory)
    data = dict(trial=trial, kinds={})
    for kind, name, key, label in (("door", "门完全打开", "actual_door_open", "全开"),
                                   ("button", "下行按钮点亮", "actual_button_lit", "点亮")):
        index = directory / (kind + "_frames.json")
        data["kinds"][kind] = dict(index=json.loads(index.read_text(encoding="utf-8")) if index.exists() else dict(frames=[]),
            name=name, key=key, label=label, recognized=metrics.get(kind + "_confirmed_ros_stamp"),
            first=metrics.get("button_first_positive_ros_stamp") if kind == "button" else None)
        stage = next((s for s in timeline.get("stages", []) if s["mode"] == ("button_verify" if kind=="button" else "door_monitor")), {})
        data["kinds"][kind]["anchors"] = stage.get("anchors", {})
    page = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>门与按键时间核验</title>
<style>body{font:17px system-ui;max-width:1000px;margin:24px auto;padding:0 20px;background:#f4f6fa;color:#17243b}img{width:100%;max-height:60vh;object-fit:contain;background:#152133}button,select{font:inherit;padding:8px;margin:5px}input[type=range]{width:100%}textarea{display:block;width:95%;min-height:65px}pre{white-space:pre-wrap;background:white;padding:16px}</style>
<h1>门与按键时间核验</h1><p>自动记录的推理、确认、发令和运动耗时无需手工计时。这里只需选出画面变化前后的两帧，程序读取图片时间。</p>
<label>核验对象 <select id="kind"><option value="door">门完全打开</option><option value="button">下行按钮点亮</option></select></label>
<p id="help"></p><p>不用每帧都标。推荐只标最后一张变化前、第一张变化后；仅标变化后也能算到该帧的延迟，但没有两帧时间范围。门和按键可切换并一起导出；不确定时清除。目标采样间隔约0.1秒，实际时间范围取决于两张标记图的间隔。按键检测表示看见点亮，并不证明发生了机械按压。</p>
<img id="frame" alt="没有可核验的连续图像"><input id="slider" type="range" min="0" value="0" step="1">
<div><button id="prev">上一帧</button><button id="next">下一帧</button><button id="play">播放/暂停</button></div><pre id="time"></pre>
<div><button id="markPrev">标记变化前</button><button id="markOpen">标记变化后</button><button id="clear">无法确定/清除</button></div>
<label>实际挡门情况 <select id="condition"><option value="unknown">待核验</option><option value="unassisted">无人挡门</option><option value="held">有人挡门</option></select></label>
<label>实际进梯成功 <select id="success"><option value="null">待核验</option><option value="true">成功</option><option value="false">失败</option></select></label>
<textarea id="note" placeholder="备注"></textarea><pre id="selection"></pre>
<p>继续已有标注时先导入之前导出的文件：<input id="import" type="file" accept=".json,application/json"></p>
<button id="export">导出 trial_review.json</button><p id="notice"></p><p><a href="report.html">返回报告</a></p>
<script>
const data=__DATA__, trial=data.trial, byId=id=>document.getElementById(id), slider=byId('slider');
let frames=[], current, timer=null;
byId('condition').value=trial.actual_door_condition;byId('success').value=JSON.stringify(trial.entry_success);byId('note').value=trial.note||'';
function selected(){return frames[Number(slider.value)];}
function stop(){clearTimeout(timer);timer=null;}
function draw(){let f=selected();if(!f){byId('frame').removeAttribute('src');byId('time').textContent='这轮没有连续图像。旧数据无法补回精确开门时刻或点亮时刻；请重新采集。';return;}
byId('frame').src=f.image;byId('time').textContent=`第 ${Number(slider.value)+1}/${frames.length} 帧 · 相对开始观察 ${f.offset_s.toFixed(3)} 秒\n本帧时间 ${f.image_stamp.toFixed(9)}（自动读取，不用手抄）`;
let prev=frames[Number(slider.value)-1];if(prev)byId('time').textContent+=`\n与上一帧间隔 ${(f.image_stamp-prev.image_stamp).toFixed(3)} 秒`;}
function show(){let o=trial[current.key+'_ros_stamp'],p=trial[current.key+'_prev_ros_stamp'];
let text=`最后未${current.label}：${p??'未标记'}\n第一张${current.label}：${o??'未标记'}`;
if(o!=null&&current.recognized!=null){let d=current.recognized-o;text+=`\n第一张${current.label}帧 → 识别确认通过：${d.toFixed(3)} 秒`;
if(d<0)text+='（确认早于人工标记，需检查误识别或标记）';
if(p!=null&&p<o)text+=`\n画面变化位于两帧之间，间隔 ${(o-p).toFixed(3)} 秒；延迟范围 [${d.toFixed(3)}, ${(current.recognized-p).toFixed(3)}) 秒。`;}
if(o!=null&&current.first!=null)text+=`\n第一张点亮帧 → 首次有效识别：${(current.first-o).toFixed(3)} 秒`;
let anchors=current.anchors||{}, sent=anchors.stage_request_sent?.ros_stamp, fresh=anchors.first_frame_captured?.ros_stamp,
ready=(byId('kind').value==='button'?anchors.button_unlit_ready:anchors.first_frame_captured)?.ros_stamp,nl=String.fromCharCode(10);
let onsetMissing=o!=null&&frames.length&&o===frames[0].image_stamp&&p==null;
if(onsetMissing)text=text.split(nl).slice(0,2).join(nl);
text+=nl+`本阶段开始：${sent??'未记录'}；首张新图像拍摄：${fresh??'未记录'}`;
text+=nl+`${byId('kind').value==='button'?'识别未亮、允许按键':'新图像就绪'}：${ready??'未记录'}`;
if(o!=null){
 if(onsetMissing)text+=nl+'⚠ 首张图已变化，变化起点未被完整记录，请清除，不能据此测延迟。';
 else if(sent!=null&&o<sent)text+=nl+'⚠ 事件早于阶段开始，耗时包含准备等待。';
 else if(fresh!=null&&o<=fresh)text+=nl+'⚠ 事件早于或就在首张新图像，需检查是否漏拍变化起点。';
 else if(ready==null)text+=nl+'⚠ 缺少事件前就绪基准，不能把端到端耗时当作纯推理耗时。';
 else if(o<ready)text+=nl+'⚠ 点亮早于未亮识别就绪提示，耗时包含识别准备阶段。';
 else if(p==null)text+=nl+'仅标变化后画面，缺少前边界。';
 else if(p<ready)text+=nl+'⚠ 变化时间范围跨过就绪锚点，无法确定是否提前操作。';
 else text+=nl+'变化前后画面均晚于就绪锚点。';}
byId('selection').textContent=text;}
function choose(){stop();current=data.kinds[byId('kind').value];frames=current.index.frames||[];slider.value=0;slider.max=Math.max(0,frames.length-1);
byId('markPrev').textContent='标记未'+current.label;byId('markOpen').textContent='标记'+current.label;
byId('help').textContent=byId('kind').value==='door'?'半开、开门过程中属于未全开。若开头已全开、遮挡或看不清，请清除。':'选择下行按钮最后未亮、第一张点亮的画面；如果开头已经亮着，不能由此推断按下时刻，请清除。';draw();show();}
byId('kind').onchange=choose;slider.oninput=draw;
byId('prev').onclick=()=>{slider.value=Math.max(0,Number(slider.value)-1);draw();};
byId('next').onclick=()=>{slider.value=Math.min(frames.length-1,Number(slider.value)+1);draw();};
byId('play').onclick=()=>{if(timer){stop();return;}function step(){if(Number(slider.value)>=frames.length-1){stop();return;}let f=selected();byId('next').click();timer=setTimeout(step,Math.max(30,Math.min(1500,(selected().image_stamp-f.image_stamp)*1000)));}timer=setTimeout(step,100);};
byId('markPrev').onclick=()=>{if(selected()){trial[current.key+'_prev_ros_stamp']=selected().image_stamp;trial[byId('kind').value+'_time_annotation_source']='camera_frame_review';show();}};
byId('markOpen').onclick=()=>{if(selected()){trial[current.key+'_ros_stamp']=selected().image_stamp;trial[byId('kind').value+'_time_annotation_source']='camera_frame_review';show();}};
byId('clear').onclick=()=>{trial[current.key+'_ros_stamp']=null;trial[current.key+'_prev_ros_stamp']=null;show();};
byId('import').onchange=async()=>{try{Object.assign(trial,JSON.parse(await byId('import').files[0].text()));byId('condition').value=trial.actual_door_condition;byId('success').value=JSON.stringify(trial.entry_success);byId('note').value=trial.note||'';show();byId('notice').textContent='已导入，继续标注后导出。';}catch(e){byId('notice').textContent='导入失败：'+e.message;}};
byId('export').onclick=()=>{for(const c of Object.values(data.kinds)){let o=trial[c.key+'_ros_stamp'],p=trial[c.key+'_prev_ros_stamp'];if(p!=null&&(o==null||p>=o)){byId('notice').textContent=c.name+'标记顺序不正确：变化前画面必须在变化后画面之前。';return;}}
trial.actual_door_condition=byId('condition').value;trial.entry_success=JSON.parse(byId('success').value);trial.note=byId('note').value;
let url=URL.createObjectURL(new Blob([JSON.stringify(trial,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='trial_review.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
byId('notice').textContent='已导出。将文件放回本轮目录覆盖 trial_review.json，再运行 README 中的 review 命令。';};
byId('kind').value='__KIND__';choose();
</script></html>'''
    encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    for kind in ("door", "button"):
        (directory / (kind + "_review.html")).write_text(page.replace("__DATA__", encoded).replace("__KIND__", kind), encoding="utf-8")


def visual_delay(directory, trial, metrics, kind):
    key = "actual_door_open" if kind == "door" else "actual_button_lit"
    opened, previous = trial.get(key + "_ros_stamp"), trial.get(key + "_prev_ros_stamp")
    recognized = metrics.get(kind + "_confirmed_ros_stamp")
    if opened is not None and (not number(opened) or opened <= 0):
        raise ValueError("核验画面时间须为正 ROS 时间戳")
    if previous is not None and (not number(previous) or previous <= 0 or opened is None or previous >= opened):
        raise ValueError("变化前画面必须位于变化后画面之前")
    if trial.get(kind + "_time_annotation_source") == "camera_frame_review" and opened is not None:
        index = json.loads((directory / (kind + "_frames.json")).read_text(encoding="utf-8"))
        stamps = {f["image_stamp"] for f in index["frames"]}
        if opened not in stamps or (previous is not None and previous not in stamps):
            raise ValueError("选择的核验帧不在本轮连续图像索引中")
    delay = recognized-opened if opened is not None and number(recognized) else None
    index_path = directory / (kind + "_frames.json")
    origin_missing = False
    if previous is None and opened is not None and index_path.exists():
        frames = json.loads(index_path.read_text(encoding="utf-8")).get("frames", [])
        origin_missing = bool(frames) and opened == frames[0].get("image_stamp")
        if origin_missing:
            delay = None
    prefix = "door_open" if kind == "door" else "button_lit"
    result = {key + "_to_recognition_s": delay,
              key + "_to_recognition_interval_s": dict(min=delay, max_exclusive=recognized-previous) if delay is not None and previous is not None else None,
              prefix + "_annotation_gap_s": opened-previous if previous is not None else None,
              kind + "_confirmation_precedes_marked_" + ("open" if kind == "door" else "lit") + "_frame": delay < 0 if delay is not None else None}
    result[kind+"_visual_onset_status"] = "onset_not_observed" if origin_missing else ("bracketed" if previous is not None else "single_frame_only" if opened is not None else "unmarked")
    if kind == "button":
        first = metrics.get("button_first_positive_ros_stamp")
        first_delay = first-opened if opened is not None and number(first) and not origin_missing else None
        result["actual_button_lit_to_first_positive_s"] = first_delay
        result["actual_button_lit_to_first_positive_interval_s"] = dict(min=first_delay, max_exclusive=first-previous) if first_delay is not None and previous is not None else None
    return result


def review_run(directory):
    """Rebuild correctness from manual ground truth; never label by confidence."""
    observations = json.loads((directory / "observations.json").read_text(encoding="utf-8"))
    with (directory / "labels.csv").open(encoding="utf-8-sig", newline="") as stream:
        labels = {row["id"]: row for row in csv.DictReader(stream)}
    reviews, counts = {}, {}
    for row in observations:
        label = labels.get(row["id"], {})
        raw = label.get("ground_truth", "").strip()
        ground_truth = None
        classification = "unreviewed"
        if raw:
            ground_truth = json.loads(raw)
            if ground_truth is not None:
                valid = (type(ground_truth) is int and -10 <= ground_truth <= 150) if row["field"] == "floor" else type(ground_truth) is bool
                if not valid:
                    raise ValueError(row["id"] + " 真值须为 true/false、整数楼层或 null")
            if ground_truth is None:
                classification = "ground_truth_unknown"
            elif row["prediction"] is None or row["status"] != "ok":
                classification = "abstained"
            else:
                classification = "correct" if type(ground_truth) is type(row["prediction"]) and ground_truth == row["prediction"] else "incorrect"
        group = row["mode"] + "/" + row["view"]
        counts.setdefault(group, dict(correct=0, incorrect=0, abstained=0, ground_truth_unknown=0, unreviewed=0))[classification] += 1
        reviews[row["id"]] = dict(ground_truth=ground_truth, classification=classification, note=label.get("note", ""))
    for group in counts.values():
        known = group["correct"] + group["incorrect"] + group["abstained"]
        predicted = group["correct"] + group["incorrect"]
        group.update(known_ground_truth_n=known,
                     accuracy_including_abstentions=group["correct"]/known if known else None,
                     accuracy_on_answered=group["correct"]/predicted if predicted else None,
                     coverage=predicted/known if known else None)
    trial = json.loads((directory / "trial_review.json").read_text(encoding="utf-8"))
    if trial.get("actual_door_condition") not in ("unknown", "unassisted", "held") or type(trial.get("entry_success")) not in (bool, type(None)):
        raise ValueError("trial_review 的门条件须为 unknown/unassisted/held；进梯成功须为 true/false/null")
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    summary.update(review_counts=counts, trial_review=trial,
                   **visual_delay(directory, trial, summary["metrics"], "door"),
                   **visual_delay(directory, trial, summary["metrics"], "button"))
    summary["stage_timeline"] = summary.get("stage_timeline") or load_stage_timeline(directory, observations)
    summary["timing_alignment"] = stage_alignments(summary["stage_timeline"], trial, directory)
    for key in ("actual_door_open_to_recognition_s", "actual_button_lit_to_recognition_s", "actual_button_lit_to_first_positive_s"):
        summary["metrics"][key] = summary[key]
    write_csv(directory / "timings.csv", [dict(run_id=summary["run_id"], **summary["metrics"])], ["run_id", *summary["metrics"]])
    write_json(directory / "reviewed_examples.json", reviews)
    write_json(directory / "summary.json", summary)
    render_report(directory, observations, json.loads((directory / "views.json").read_text(encoding="utf-8")), summary, reviews)
    render_door_review(directory)
    return summary


def aggregate(root):
    groups, rows = {}, []
    for path in sorted(root.glob("*/summary.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))
        meta = json.loads((path.parent / "run.json").read_text(encoding="utf-8"))
        if meta.get("data_kind") != "real":
            continue
        trial = summary.get("trial_review", {})
        row = dict(run_id=meta["run_id"], entry_plan=meta["entry_plan"], entry_pose=json.dumps(meta["entry_pose"]),
                   return_no_turn=meta.get("return_no_turn", False),
                   requested_door_condition=meta["door_condition"], actual_door_condition=trial.get("actual_door_condition", "unknown"),
                   entry_success=trial.get("entry_success"), exit_code=summary["exit_code"], **summary["metrics"])
        rows.append(row)
        # Different points, views, speed or door conditions must not be pooled silently.
        key = json.dumps(dict(entry_plan=meta["entry_plan"], entry_pose=meta["entry_pose"],
                              return_no_turn=meta.get("return_no_turn", False),
                              actual_door_condition=row["actual_door_condition"],
                              entry_only=meta["entry_only"], observe_only=meta["observe_only"],
                              points=meta["config"]["points"], map_name=meta["config"]["map_name"],
                              perception=meta["config"]["perception"],
                              head=meta["config"]["head"], navigation=meta["config"]["navigation"]), ensure_ascii=False, sort_keys=True)
        group = groups.setdefault(key, dict(trials=0, reviewed_trials=0, successes=0, metrics={}))
        group["trials"] += 1
        if type(row["entry_success"]) is bool:
            group["reviewed_trials"] += 1
            group["successes"] += int(row["entry_success"])
        for name, value in summary["metrics"].items():
            if name.endswith("_s") and number(value):
                group["metrics"].setdefault(name, []).append(value)
    for group in groups.values():
        group["success_rate"] = group["successes"]/group["reviewed_trials"] if group["reviewed_trials"] else None
        group["metrics"] = {name: statistics(values) for name, values in group["metrics"].items()}
    write_json(root / "comparison.json", groups)
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ["run_id"]
    write_csv(root / "comparison.csv", rows, fields)
    return groups


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("review", "aggregate", "prepare-review"):
        parser = argparse.ArgumentParser(description="离线人工标注统计，不连接机器人")
        parser.add_argument("action", choices=("review", "aggregate", "prepare-review"))
        parser.add_argument("directory", type=Path)
        args = parser.parse_args(argv)
        result = {"review": review_run, "aggregate": aggregate, "prepare-review": prepare_review}[args.action](args.directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    sys.path.insert(0, str(ROOT / "deploy"))
    sys.path.insert(0, "/media/data/vlm/Demo_kuavo_vlm_api")
    from elevator_demo_launcher import main as launch
    return launch(["--program", "quantify", *argv])


if __name__ == "__main__":
    raise SystemExit(main())
