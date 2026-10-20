#!/usr/bin/env python3
"""Kuavo 电梯门感知与进出电梯单轮演示。

流程：
    门外原点等待完整开门 -> 进入轿厢并以最终朝向面向门
    -> 确认门已关闭 -> 等待门再次完整打开 -> 返回门外原点。

预览和 mock 不需要 ROS。实机模式订阅 VLM 节点发布的 JSON 状态，不直接
调用云端 API。任何未知、过期、模式不符或不连续的结果都不会放行导航。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import signal
import threading
import time

DEFAULT_CONFIG = Path(__file__).resolve().parent / "params" / "elevator_demo.yaml"


def load_config(path):
    """优先使用 PyYAML；没有安装时仍可读取本项目的 JSON 兼容 YAML。"""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        return json.loads(text)
    return yaml.safe_load(text)


def number(value, field, lower=None, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数值，当前为 {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field} 不能是 NaN/Infinity")
    if (lower is not None and value < lower) or (upper is not None and value > upper):
        raise ValueError(f"{field} 超出允许范围 [{lower}, {upper}]")
    return value


def positive_int(value, field, lower=1, upper=20):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{field} 必须是 [{lower}, {upper}] 内的整数")
    return value


def build_demo(config):
    """校验配置并返回起点、两个导航目标和感知参数。"""
    if type(config.get("floor")) is not int or config["floor"] != 21:
        raise ValueError("本演示限定 21 楼，不会自动切图或修改楼层记录")
    points = config["points"]
    outside, inside = points["outside"], points["inside"]
    start = {
        "name": "OutsideOriginFacingElevator",
        "x": number(outside.get("x"), "outside.x"),
        "y": number(outside.get("y"), "outside.y"),
        "theta": number(outside.get("heading_in_deg"), "outside.heading_in_deg", -180, 180),
    }
    inside_xy = {
        "x": number(inside.get("x"), "inside.x"),
        "y": number(inside.get("y"), "inside.y"),
    }
    if math.hypot(start["x"] - inside_xy["x"], start["y"] - inside_xy["y"]) < 0.1:
        raise ValueError("门外点与轿厢内点不能重合或相距不足 0.1 米")

    nav = config["navigation"]
    for key in ("avoid_enabled", "allow_rotation"):
        if nav.get(key) is not True:
            raise ValueError(f"本演示要求 navigation.{key}: true")
    nav_options = {
        "linear_velocity": number(nav.get("linear_velocity"), "linear_velocity", 0.01, 0.7),
        "angular_velocity": number(nav.get("angular_velocity"), "angular_velocity", 0.01, 0.4),
        "position_threshold": number(nav.get("position_threshold"), "position_threshold", 0.001, 0.1),
        "angle_threshold": number(nav.get("angle_threshold"), "angle_threshold", 0.001, 0.1),
        "timeout": number(nav.get("timeout"), "navigation.timeout", 1, 120),
        "avoid_enabled": True,
        "allow_rotation": True,
    }
    targets = [
        dict(
            nav_options,
            name="EnterInsideAndFaceDoor",
            **inside_xy,
            theta=number(inside.get("heading_out_deg"), "inside.heading_out_deg", -180, 180),
        ),
        dict(
            nav_options,
            name="ExitToOutsideOrigin",
            x=start["x"],
            y=start["y"],
            theta=number(outside.get("heading_out_deg"), "outside.heading_out_deg", -180, 180),
        ),
    ]

    perception = config["perception"]
    perception_options = {
        "state_topic": str(perception.get("state_topic", "")).strip(),
        "mode_topic": str(perception.get("mode_topic", "")).strip(),
        "required_mode": str(perception.get("required_mode", "door_monitor")).strip(),
        "wait_timeout": number(perception.get("wait_timeout"), "perception.wait_timeout", 1, 600),
        "max_result_age": number(perception.get("max_result_age"), "perception.max_result_age", 0.1, 60),
        "min_confidence": number(perception.get("min_confidence"), "perception.min_confidence", 0, 1),
        "confirmations": positive_int(perception.get("confirmations"), "perception.confirmations"),
        "min_confirmation_span": number(
            perception.get("min_confirmation_span"),
            "perception.min_confirmation_span",
            0,
            60,
        ),
    }
    if not perception_options["state_topic"] or not perception_options["mode_topic"]:
        raise ValueError("perception.state_topic 和 mode_topic 不能为空")
    if perception_options["required_mode"] != "door_monitor":
        raise ValueError("当前快速配置只允许 perception.required_mode: door_monitor")
    settle_seconds = number(config.get("settle_seconds"), "settle_seconds", 0, 10)
    return start, targets, perception_options, settle_seconds


class DoorGate:
    """只接受 arm 之后产生的、新鲜且连续一致的门状态。"""

    def __init__(
        self,
        required_mode,
        max_result_age,
        min_confidence,
        confirmations,
        min_confirmation_span,
    ):
        self.required_mode = required_mode
        self.max_result_age = max_result_age
        self.min_confidence = min_confidence
        self.confirmations = confirmations
        self.min_confirmation_span = min_confirmation_span
        self._condition = threading.Condition()
        self._not_before_stamp = 0.0
        self._target_closed = None
        self._last_sequence = None
        self._count = 0
        self._first_match_at = None
        self._ready = False
        self._reason = "尚未开始等待"

    @property
    def reason(self):
        with self._condition:
            return self._reason

    def arm(self, target_closed, not_before_stamp):
        if type(target_closed) is not bool:
            raise ValueError("target_closed 必须是 bool")
        with self._condition:
            self._target_closed = target_closed
            self._not_before_stamp = float(not_before_stamp)
            self._last_sequence = None
            self._count = 0
            self._first_match_at = None
            self._ready = False
            self._reason = "等待新的门状态"

    def _reset_match(self, reason):
        self._count = 0
        self._first_match_at = None
        self._ready = False
        self._reason = reason

    def feed(self, text, now_ros, now_monotonic=None):
        """处理一条 std_msgs/String.data；格式错误只会关闭门控，不向外抛出。"""
        received_at = time.monotonic() if now_monotonic is None else float(now_monotonic)
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("JSON 顶层不是对象")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            with self._condition:
                self._reset_match(f"无效 JSON：{exc}")
                self._condition.notify_all()
            return False

        with self._condition:
            if self._target_closed is None:
                self._reset_match("尚未 arm")
                return False
            sequence = payload.get("sequence")
            if type(sequence) is not int:
                self._reset_match("sequence 缺失或不是整数")
                return False
            if self._last_sequence is not None and sequence <= self._last_sequence:
                self._reason = "忽略重复或倒序结果"
                return self._ready
            self._last_sequence = sequence

            image_stamp = payload.get("image_stamp")
            if isinstance(image_stamp, bool) or not isinstance(image_stamp, (int, float)):
                self._reset_match("image_stamp 缺失")
                return False
            image_stamp = float(image_stamp)
            age = float(now_ros) - image_stamp
            if image_stamp < self._not_before_stamp:
                self._reset_match("忽略等待阶段开始前拍摄的图像")
                return False
            if age < -1.0 or age > self.max_result_age:
                self._reset_match(f"图像结果过期或时钟异常，age={age:.2f}s")
                return False
            if payload.get("mode") != self.required_mode:
                self._reset_match("感知模式不符")
                return False
            if payload.get("status") != "ok":
                self._reset_match("感知结果未知或错误")
                return False
            if payload.get("schema_version") != 2:
                self._reset_match("感知 schema_version 不兼容")
                return False
            confidence = payload.get("confidence")
            door_confidence = confidence.get("door_closed") if isinstance(confidence, dict) else None
            if (
                isinstance(door_confidence, bool)
                or not isinstance(door_confidence, (int, float))
                or not math.isfinite(float(door_confidence))
                or float(door_confidence) < self.min_confidence
            ):
                self._reset_match("门状态置信度不足")
                return False
            observed = payload.get("door_closed")
            if type(observed) is not bool or observed is not self._target_closed:
                wanted = "关门" if self._target_closed else "完整开门"
                self._reset_match(f"当前结果尚未确认{wanted}")
                return False

            if self._count == 0:
                self._first_match_at = received_at
            self._count += 1
            span = received_at - self._first_match_at
            self._ready = self._count >= self.confirmations and span >= self.min_confirmation_span
            self._reason = (
                f"连续匹配 {self._count}/{self.confirmations}，时间跨度 "
                f"{span:.1f}/{self.min_confirmation_span:.1f}s"
            )
            self._condition.notify_all()
            return self._ready

    def wait(self, timeout, is_shutdown=lambda: False):
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._ready:
                if is_shutdown():
                    raise RuntimeError("ROS 已关闭")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"等待门状态超时：{self._reason}")
                self._condition.wait(timeout=min(0.2, remaining))


def execute_demo(wait_for_door, navigate, settle, settle_seconds, set_mode):
    """与 ROS 解耦的状态机，便于离线验证顺序。"""
    try:
        set_mode("door_monitor")
        print("[1/5] 门外原点：门关闭时保持不动，等待完整开门", flush=True)
        wait_for_door(False, "outside_open")

        print("[2/5] 门已完整打开：进入轿厢并转身面向电梯门", flush=True)
        if not navigate("inside"):
            raise RuntimeError("进入轿厢或转身失败，后续步骤已取消")
        settle(settle_seconds)

        print("[3/5] 轿厢内面向门：等待确认电梯门关闭", flush=True)
        wait_for_door(True, "inside_closed")

        print("[4/5] 电梯门已关闭：继续等待再次完整开门", flush=True)
        wait_for_door(False, "inside_open")

        print("[5/5] 电梯门再次完整打开：退出到门外原点", flush=True)
        if not navigate("outside"):
            raise RuntimeError("退出电梯失败")
        settle(settle_seconds)
    finally:
        set_mode("idle")


def validate_live(config, args):
    if config.get("calibrated") is not True:
        raise ValueError("点位确认开关未启用：实机要求 calibrated: true")
    if not args.preflight_confirmed or not args.perception_confirmed:
        raise ValueError("实机必须传 --preflight-confirmed --perception-confirmed")
    if not args.estop_ready:
        raise ValueError("实机必须传 --estop-ready，确认物理急停有效且有专人看护")


def run_live(targets, perception, settle_seconds):
    # 延迟导入：预览、mock 和单元测试不要求 ROS/机器人 SDK。
    import rospy
    from py_trees.common import Status
    from std_msgs.msg import String
    import api

    rospy.init_node("elevator_vlm_in_out_demo", anonymous=True, disable_signals=True)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    gate = DoorGate(
        required_mode=perception["required_mode"],
        max_result_age=perception["max_result_age"],
        min_confidence=perception["min_confidence"],
        confirmations=perception["confirmations"],
        min_confirmation_span=perception["min_confirmation_span"],
    )

    def perception_callback(message):
        ready = gate.feed(
            message.data,
            now_ros=rospy.Time.now().to_sec(),
            now_monotonic=time.monotonic(),
        )
        if ready:
            rospy.loginfo("door gate ready: %s", gate.reason)
        else:
            rospy.loginfo_throttle(2.0, "door gate: %s", gate.reason)

    state_subscriber = rospy.Subscriber(
        perception["state_topic"], String, perception_callback, queue_size=10
    )
    mode_publisher = rospy.Publisher(perception["mode_topic"], String, queue_size=1, latch=True)
    rospy.sleep(0.5)

    target_by_key = {"inside": targets[0], "outside": targets[1]}

    def set_mode(mode):
        mode_publisher.publish(String(data=mode))
        rospy.loginfo("elevator perception mode: %s", mode)

    def wait_for_door(target_closed, stage):
        gate.arm(target_closed, rospy.Time.now().to_sec())
        wanted = "关闭" if target_closed else "完整打开"
        rospy.loginfo("%s: waiting for door %s", stage, wanted)
        gate.wait(perception["wait_timeout"], is_shutdown=rospy.is_shutdown)

    def settle(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if rospy.is_shutdown():
                raise RuntimeError("ROS 已关闭")
            time.sleep(0.05)

    def navigate(key):
        target = target_by_key[key]
        params = dict(target)
        timeout = params.pop("timeout")
        sender = api.NodeNavigationToTarget(**params)
        waiter = api.NodeWaitForArrival(target["name"] + "Arrival", timeout=timeout)
        deadline = time.monotonic() + timeout
        try:
            for node in (sender, waiter):
                while True:
                    if rospy.is_shutdown() or time.monotonic() >= deadline:
                        return False
                    node.tick_once()
                    if node.status == Status.SUCCESS:
                        break
                    if node.status == Status.FAILURE:
                        return False
                    time.sleep(0.05)
            return True
        finally:
            for node in (sender, waiter):
                if node.status == Status.RUNNING:
                    node.stop(Status.INVALID)

    try:
        execute_demo(wait_for_door, navigate, settle, settle_seconds, set_mode)
    finally:
        # 给最后一条 latch 的 idle 消息留出发送时间，并保持订阅对象存活至流程结束。
        rospy.sleep(0.2)
        assert state_subscriber is not None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="实机执行一轮；默认仅预览")
    mode.add_argument("--mock", action="store_true", help="离线模拟完整状态机")
    parser.add_argument("--preflight-confirmed", action="store_true", help="确认点位、空间、避障和朝向已现场验证")
    parser.add_argument("--perception-confirmed", action="store_true", help="确认当前门视角已完成 shadow test，完整开门/关门识别正确")
    parser.add_argument("--estop-ready", action="store_true", help="确认物理急停有效并有专人看护")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        start, targets, perception, settle_seconds = build_demo(config)
        print(
            f"起点（程序不导航到此点）: x={start['x']} m, y={start['y']} m, "
            f"theta={start['theta']} deg"
        )
        for target in targets:
            print(f"{target['name']}: x={target['x']} m, y={target['y']} m, theta={target['theta']} deg")
        print(
            f"门控: topic={perception['state_topic']}, "
            f"连续确认={perception['confirmations']} 次, "
            f"最短跨度={perception['min_confirmation_span']} s, "
            f"最低置信度={perception['min_confidence']}"
        )
        if args.run:
            validate_live(config, args)
            print("即将实机运行一轮；未知/过期/半开状态不会放行。异常立即拍物理急停。", flush=True)
            run_live(targets, perception, settle_seconds)
            print("单轮流程完成；请现场确认机器人已停稳。")
        elif args.mock:
            events = []

            def mock_wait(target_closed, stage):
                events.append(("door", stage, target_closed))

            execute_demo(
                mock_wait,
                lambda key: events.append(("navigate", key)) or True,
                lambda seconds: events.append(("settle", seconds)),
                settle_seconds,
                lambda mode_name: events.append(("mode", mode_name)),
            )
            print("[MOCK] " + " -> ".join(str(item) for item in events))
            print("[MOCK] 只验证流程顺序，不代表感知、导航或硬件安全已经验证。")
        else:
            print("仅预览：未连接机器人。实机需显式 --run 及三个确认参数。")
        return 0
    except KeyboardInterrupt:
        print("演示中断；程序退出不保证底盘停车，请现场确认并按需使用物理急停。")
        return 130
    except Exception as exc:
        print(f"演示未启动或已终止：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
