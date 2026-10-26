"""Runtime adapters: one-time readiness, compact door gate, retained motion telemetry."""
import json
import time


def compact_door_gate(base):
    class DoorGate(base):
        def feed(self, text, now):
            if self.mode != 'door_monitor': return super().feed(text, now)
            with self.condition:
                try:
                    p = json.loads(text)
                    if p.get('mode') != self.mode or p.get('message_type', 'result') != 'result': return False
                    if p.get('schema_version') != 2 or p.get('recognition_contract') != 'compact_state_v1':
                        raise ValueError('门状态接口不符')
                    seq, stamp = p.get('sequence'), p.get('image_stamp')
                    if type(seq) is not int: raise ValueError('门 sequence 无效')
                    if self.sequence is not None and seq <= self.sequence: return False
                    self.sequence = seq
                    if type(stamp) not in (int, float) or not self.not_before < stamp <= now+.1 or now-stamp > self.max_age:
                        raise ValueError('门画面过期或来自旧阶段')
                    if self.frame_stamp is not None and stamp <= self.frame_stamp: return False
                    self.frame_stamp = stamp
                    if p.get('status') != 'ok' or type(p.get('door_closed')) is not bool or p['door_closed'] != self.expected:
                        raise ValueError('门未开启或看不清')
                    self.last_payload = p
                    self.first_stamp, self.count, self.ready = stamp, 1, True
                    self.reason = '当前新画面确认电梯门开启'
                except (ValueError, TypeError, AttributeError) as exc:
                    self.reset(str(exc))
                self.condition.notify_all()
                return self.ready
    return DoorGate


def install(module):
    original_ready = module.require_head_ready
    initialized = False

    def readiness_once(rospy):
        nonlocal initialized
        if not initialized:
            original_ready(rospy)
            initialized = True

    module.require_head_ready = readiness_once

    original_connections = module.check_connections

    def connections_once(rospy, config, require_perception=True, require_navigation=True):
        original_connections(rospy, config, require_perception, require_navigation)
        if require_navigation:
            # Only availability discovery is cached. Service calls and their real
            # failures still run normally for every navigation/arrival operation.
            waiter = rospy.wait_for_service
            names = {rospy.resolve_name(n) for n in
                     ('/move_base/move_to_target', '/move_base/check_arrived', '/enable_vel_control')}
            for name in sorted(names): waiter(name, timeout=5)

            def cached_wait(name, timeout=None):
                if rospy.resolve_name(name) not in names: return waiter(name, timeout=timeout)

            rospy.wait_for_service = cached_wait

    module.check_connections = connections_once

    def move_head(head, pose, wait_seconds, rospy, monitor, name, timeout=None):
        if rospy.is_shutdown(): raise RuntimeError('ROS 已关闭')
        if not head.control_head(pose['yaw'], pose['pitch']): raise RuntimeError('HeadAPI 拒绝头部指令')
        deadline = time.monotonic()+wait_seconds
        while time.monotonic() < deadline:
            if rospy.is_shutdown(): raise RuntimeError('ROS 已关闭')
            time.sleep(min(.05, max(0, deadline-time.monotonic())))
        return {'commanded_yaw': pose['yaw'], 'commanded_pitch': pose['pitch'],
                'settle_seconds': wait_seconds, 'arrival_verified': False}

    module.move_head = move_head

    original_prepare = module.MotionMonitor.prepare

    def prepare(monitor, point):
        if not getattr(monitor, '_startup_baseline_checked', False):
            original_prepare(monitor, point)
            monitor._startup_baseline_checked = True
            return
        # One nonblocking baseline snapshot is needed for onset/heading metrics;
        # do not repeat the startup static-pose waiting loop on every navigation.
        with monitor.lock:
            if not monitor.samples: raise RuntimeError('量化定位反馈缺失')
            baseline = monitor.samples[-1]
            if not -.1 <= monitor.runtime.now()-baseline['stamp'] <= .5:
                raise RuntimeError('量化定位反馈过期')
            monitor.prepared = dict(point=point, baseline=baseline, previous=baseline, above=[])

    module.MotionMonitor.prepare = prepare
