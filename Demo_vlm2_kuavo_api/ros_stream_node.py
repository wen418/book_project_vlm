#!/usr/bin/env python3
"""ROS 1 continuous elevator perception node.

ROS imports are intentionally delayed so the core module remains testable on a
development machine without ROS installed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from elevator_perception import ElevatorPerception, Settings
from few_shot import bytes_to_data_url
from perception_schema import FIELDS, PlannerPayload, error_payload


VALID_MODES = ("idle", "both", "door_monitor", "button_verify")


@dataclass(frozen=True)
class FrameRecord:
    message: Any
    stamp: float
    revision: int


class LatestFrame:
    """Thread-safe one-element buffer; new camera frames replace old frames."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._record: Optional[FrameRecord] = None
        self._revision = 0

    def update(self, message: Any, stamp: float) -> None:
        with self._lock:
            self._revision += 1
            self._record = FrameRecord(message, stamp, self._revision)

    def snapshot(self) -> Optional[FrameRecord]:
        with self._lock:
            return self._record


class WaitingForInput(RuntimeError):
    pass


class ContinuousRosNode:
    def __init__(self, project_dir: Path) -> None:
        import cv2
        import numpy as np
        import rospy
        from cv_bridge import CvBridge
        from sensor_msgs.msg import CompressedImage, Image
        from std_msgs.msg import String

        self.cv2 = cv2
        self.np = np
        self.rospy = rospy
        self.Image = Image
        self.CompressedImage = CompressedImage
        self.String = String
        self.bridge = CvBridge()
        self.project_dir = Path(rospy.get_param("~project_dir", str(project_dir))).resolve()

        model_config = self._path_param(
            "~model_config", self.project_dir / "config.api.json"
        )
        manifest = self._path_param(
            "~manifest", self.project_dir / "few-shots" / "manifest.json"
        )
        state_file_text = str(rospy.get_param("~state_file", "")).strip()
        state_file = Path(state_file_text).expanduser() if state_file_text else None
        settings = Settings.from_json(model_config)
        self.model_name = settings.model
        self.pipeline = ElevatorPerception(
            settings=settings,
            manifest_path=manifest,
            state_path=state_file,
            enable_temporal_filter=bool(rospy.get_param("~temporal_filter", True)),
        )

        self.inference_hz = float(rospy.get_param("~inference_hz", 1.0))
        self.max_frame_age = float(rospy.get_param("~max_frame_age_sec", 2.0))
        self.max_result_age = float(rospy.get_param("~max_result_age_sec", 5.0))
        self.max_pair_skew = float(rospy.get_param("~max_pair_skew_sec", 0.5))
        self.jpeg_quality = int(rospy.get_param("~jpeg_quality", 90))
        self.max_image_edge = int(rospy.get_param("~max_image_edge", 1024))
        self.status_period = 1.0 / max(0.1, float(rospy.get_param("~status_hz", 1.0)))
        if self.inference_hz <= 0:
            raise ValueError("~inference_hz 必须大于 0")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("~jpeg_quality 必须在 1 到 100 之间")
        if self.max_image_edge < 0:
            raise ValueError("~max_image_edge 不能小于 0")

        self.output_topic = str(
            rospy.get_param("~output_topic", "/elevator/perception/state")
        )
        self.publisher = rospy.Publisher(self.output_topic, String, queue_size=10)

        self.scene = LatestFrame()
        self.door = LatestFrame()
        self.button = LatestFrame()
        self.scene_topic = str(rospy.get_param("~camera/scene_topic", "")).strip()
        self.door_topic = str(rospy.get_param("~camera/door_topic", "")).strip()
        self.button_topic = str(rospy.get_param("~camera/button_topic", "")).strip()
        self.scene_roi = self._roi_param("~camera/scene_roi")
        self.door_roi = self._roi_param("~camera/door_roi")
        self.button_roi = self._roi_param("~camera/button_roi")
        self.yuv422_layout = str(
            rospy.get_param("~camera/yuv422_layout", "uyvy")
        ).strip().lower()
        if self.yuv422_layout not in ("uyvy", "yuyv"):
            raise ValueError("~camera/yuv422_layout 只能是 uyvy 或 yuyv")

        self._subscribers = []
        self._subscribe(self.scene_topic, self.scene, "scene")
        self._subscribe(self.door_topic, self.door, "door")
        self._subscribe(self.button_topic, self.button, "button")

        self._mode_lock = threading.Lock()
        self._mode_from_topic: Optional[str] = None
        mode_topic = str(rospy.get_param("~mode_topic", "")).strip()
        if mode_topic:
            self._subscribers.append(
                rospy.Subscriber(mode_topic, String, self._mode_callback, queue_size=1)
            )

        self._sequence = 0
        self._last_signature: Optional[Tuple[Any, ...]] = None
        self._last_status_at = 0.0
        self._last_status_message = ""
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop, name="elevator_vlm_worker", daemon=True
        )
        rospy.on_shutdown(self._stop.set)

    def _path_param(self, name: str, default: Path) -> Path:
        text = str(self.rospy.get_param(name, str(default))).strip()
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.project_dir / path
        return path.resolve()

    def _roi_param(self, name: str) -> Optional[Tuple[int, int, int, int]]:
        value = self.rospy.get_param(name, [])
        if value in (None, "", []):
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("%s 必须是空数组或 [x, y, width, height]" % name)
        roi = tuple(int(item) for item in value)
        if any(item < 0 for item in roi[:2]) or roi[2] <= 0 or roi[3] <= 0:
            raise ValueError("%s 的坐标必须非负且宽高必须大于 0" % name)
        return roi

    def _subscribe(self, topic: str, slot: LatestFrame, label: str) -> None:
        if not topic:
            return

        def callback(message: Any) -> None:
            try:
                stamp = float(message.header.stamp.to_sec())
            except (AttributeError, TypeError, ValueError):
                stamp = 0.0
            if stamp <= 0:
                stamp = self.rospy.Time.now().to_sec()
            slot.update(message, stamp)

        if topic.endswith("/compressedDepth"):
            raise ValueError("不支持深度压缩话题作为 VLM 彩色输入：%s" % topic)
        message_type = self.CompressedImage if topic.endswith("/compressed") else self.Image
        self._subscribers.append(
            self.rospy.Subscriber(
                topic,
                message_type,
                callback,
                queue_size=1,
                buff_size=32 * 1024 * 1024,
            )
        )
        self.rospy.loginfo("elevator_vlm: subscribed %s image: %s", label, topic)

    def _mode_callback(self, message: Any) -> None:
        mode = str(message.data).strip()
        if mode not in VALID_MODES:
            self.rospy.logwarn("elevator_vlm: ignore invalid mode: %s", mode)
            return
        with self._mode_lock:
            self._mode_from_topic = mode

    def _mode(self) -> str:
        with self._mode_lock:
            topic_mode = self._mode_from_topic
        mode = topic_mode or str(self.rospy.get_param("~mode", "both")).strip()
        if mode not in VALID_MODES:
            raise ValueError("无效感知模式 %r，可选值：%s" % (mode, ", ".join(VALID_MODES)))
        return mode

    def _encode(
        self, record: FrameRecord, roi: Optional[Tuple[int, int, int, int]]
    ) -> str:
        if isinstance(record.message, self.CompressedImage):
            image_format = str(getattr(record.message, "format", "")).lower()
            if (
                roi is None
                and self.max_image_edge == 0
                and ("jpeg" in image_format or "jpg" in image_format)
            ):
                return bytes_to_data_url(bytes(record.message.data), "image/jpeg")
            compressed = self.np.frombuffer(record.message.data, dtype=self.np.uint8)
            image = self.cv2.imdecode(compressed, self.cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError("ROS CompressedImage 解码失败")
        else:
            if (
                str(record.message.encoding).lower() == "yuv422"
                and self.yuv422_layout == "yuyv"
            ):
                height = int(record.message.height)
                width = int(record.message.width)
                step = int(record.message.step)
                row_bytes = width * 2
                if height <= 0 or width <= 0 or width % 2 or step < row_bytes:
                    raise ValueError("YUYV 图像尺寸或 step 无效")
                data = self.np.frombuffer(bytes(record.message.data), dtype=self.np.uint8)
                if data.size < height * step:
                    raise ValueError("YUYV 图像数据长度不足")
                packed = data[: height * step].reshape(height, step)[:, :row_bytes]
                packed = self.np.ascontiguousarray(packed).reshape(height, width, 2)
                image = self.cv2.cvtColor(packed, self.cv2.COLOR_YUV2BGR_YUY2)
            else:
                image = self.bridge.imgmsg_to_cv2(record.message, desired_encoding="bgr8")
        if roi:
            x, y, width, height = roi
            image_height, image_width = image.shape[:2]
            if x + width > image_width or y + height > image_height:
                raise ValueError(
                    "ROI %s 超出图像尺寸 %sx%s" % (roi, image_width, image_height)
                )
            image = image[y : y + height, x : x + width]
        if self.max_image_edge > 0:
            image_height, image_width = image.shape[:2]
            longest = max(image_height, image_width)
            if longest > self.max_image_edge:
                scale = self.max_image_edge / float(longest)
                image = self.cv2.resize(
                    image,
                    (max(1, int(image_width * scale)), max(1, int(image_height * scale))),
                    interpolation=self.cv2.INTER_AREA,
                )
        success, encoded = self.cv2.imencode(
            ".jpg", image, [int(self.cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        if not success:
            raise RuntimeError("ROS 图像编码 JPEG 失败")
        return bytes_to_data_url(encoded.tobytes(), "image/jpeg")

    def _fresh(self, record: Optional[FrameRecord], label: str, now: float) -> FrameRecord:
        if record is None:
            raise WaitingForInput("等待%s图像" % label)
        age = now - record.stamp
        if age > self.max_frame_age:
            raise WaitingForInput("%s图像已过期 %.2f 秒" % (label, age))
        if age < -1.0:
            raise WaitingForInput("%s图像时间戳比 ROS 时间超前 %.2f 秒" % (label, -age))
        return record

    def _collect(
        self, mode: str
    ) -> Tuple[Sequence[Dict[str, str]], Sequence[str], float, Tuple[Any, ...]]:
        now = self.rospy.Time.now().to_sec()
        current = []
        fields = []
        records = []

        if mode == "door_monitor":
            if self.door_topic:
                record = self._fresh(self.door.snapshot(), "电梯门", now)
                roi = self.door_roi
            elif self.scene_topic:
                record = self._fresh(self.scene.snapshot(), "全景", now)
                roi = self.door_roi or self.scene_roi
            else:
                raise WaitingForInput("尚未配置 camera/door_topic 或 camera/scene_topic")
            current.append({"label": "当前电梯门图：", "data_url": self._encode(record, roi)})
            fields.append("door_closed")
            records.append(record)

        elif mode == "button_verify":
            if self.button_topic:
                record = self._fresh(self.button.snapshot(), "按钮", now)
                roi = self.button_roi
            elif self.scene_topic:
                record = self._fresh(self.scene.snapshot(), "全景", now)
                roi = self.button_roi or self.scene_roi
            else:
                raise WaitingForInput("尚未配置 camera/button_topic 或 camera/scene_topic")
            current.append({"label": "当前上/下按钮图：", "data_url": self._encode(record, roi)})
            fields.extend(("up_pressed", "down_pressed"))
            records.append(record)

        elif mode == "both":
            if self.door_topic and self.button_topic:
                door = self._fresh(self.door.snapshot(), "电梯门", now)
                button = self._fresh(self.button.snapshot(), "按钮", now)
                if abs(door.stamp - button.stamp) > self.max_pair_skew:
                    raise WaitingForInput("门与按钮图像时间差超过限制")
                current.append(
                    {"label": "当前电梯门图：", "data_url": self._encode(door, self.door_roi)}
                )
                current.append(
                    {"label": "当前上/下按钮图：", "data_url": self._encode(button, self.button_roi)}
                )
                records.extend((door, button))
            elif self.scene_topic:
                scene = self._fresh(self.scene.snapshot(), "全景", now)
                current.append(
                    {
                        "label": "当前全景图（检查门和按钮）：",
                        "data_url": self._encode(scene, self.scene_roi),
                    }
                )
                records.append(scene)
            else:
                raise WaitingForInput(
                    "both 模式需要 scene_topic，或同时配置 door_topic 和 button_topic"
                )
            fields.extend(("door_closed", "up_pressed", "down_pressed"))
        else:
            raise WaitingForInput("idle")

        image_stamp = max(record.stamp for record in records)
        signature = (mode,) + tuple(record.revision for record in records)
        return current, fields, image_stamp, signature

    def _publish(self, payload: PlannerPayload) -> None:
        self.publisher.publish(self.String(data=payload.to_json()))

    def _publish_waiting(self, message: str, mode: str) -> None:
        now = time.monotonic()
        if message == self._last_status_message and now - self._last_status_at < self.status_period:
            return
        self._last_status_message = message
        self._last_status_at = now
        if message == "idle":
            payload = PlannerPayload(
                sequence=self._sequence,
                status="idle",
                door_closed=None,
                up_pressed=None,
                down_pressed=None,
                confidence={name: 0.0 for name in FIELDS},
                evidence={name: "" for name in FIELDS},
                latency_ms=None,
                model=self.model_name,
                few_shot_count=0,
                mode=mode,
            )
        else:
            payload = error_payload(
                self._sequence, self.model_name, message, mode=mode
            )
        self._sequence += 1
        self._publish(payload)

    def _run_once(self) -> None:
        mode = self._mode()
        try:
            current, fields, image_stamp, signature = self._collect(mode)
        except WaitingForInput as exc:
            self._publish_waiting(str(exc), mode)
            return
        if signature == self._last_signature:
            return
        self._last_signature = signature
        sequence = self._sequence
        self._sequence += 1
        try:
            payload = self.pipeline.infer_data_urls(
                sequence=sequence,
                current_images=current,
                required_fields=fields,
                image_stamp=image_stamp,
                mode=mode,
            )
            if self._mode() != mode:
                self.rospy.loginfo(
                    "elevator_vlm: discard result because mode changed during inference"
                )
                return
            result_age = self.rospy.Time.now().to_sec() - image_stamp
            if result_age > self.max_result_age:
                payload = error_payload(
                    sequence,
                    self.model_name,
                    "推理完成时图像结果已过期 %.2f 秒" % result_age,
                    mode=mode,
                    image_stamp=image_stamp,
                )
        except Exception as exc:
            self.rospy.logerr("elevator_vlm inference failed: %s", exc)
            payload = error_payload(
                sequence, self.model_name, str(exc), mode=mode, image_stamp=image_stamp
            )
        self._publish(payload)

    def _worker_loop(self) -> None:
        period = 1.0 / self.inference_hz
        while not self.rospy.is_shutdown() and not self._stop.is_set():
            started = time.monotonic()
            try:
                self._run_once()
            except Exception as exc:
                self.rospy.logerr("elevator_vlm worker error: %s", exc)
                try:
                    self._publish_waiting(str(exc), "error")
                except Exception:
                    pass
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.01, period - elapsed))

    def spin(self) -> None:
        if not any((self.scene_topic, self.door_topic, self.button_topic)):
            self.rospy.logwarn(
                "elevator_vlm: camera topics are empty; fill config/perception.yaml and restart"
            )
        self._worker.start()
        self.rospy.loginfo(
            "elevator_vlm started: output=%s inference_hz=%.2f",
            self.output_topic,
            self.inference_hz,
        )
        self.rospy.spin()
        self._stop.set()
        self._worker.join(timeout=2.0)


def run(project_dir: Path) -> None:
    import rospy

    rospy.init_node("elevator_vlm_api_perception", anonymous=False)
    node = ContinuousRosNode(project_dir)
    node.spin()


if __name__ == "__main__":
    run(Path(__file__).resolve().parent)
