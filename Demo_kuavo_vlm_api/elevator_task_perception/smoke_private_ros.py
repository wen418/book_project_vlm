"""Private ROS transport test with a stub VLM; never connects to the robot master."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import xmlrpc.client

ROOT = Path(__file__).resolve().parent


def wait_for(predicate, timeout=10):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(.01)
    raise TimeoutError('private smoke timed out')


def main():
    port = 11382
    with socket.socket() as probe: probe.bind(('127.0.0.1', port))
    os.environ.update(ROS_MASTER_URI='http://127.0.0.1:11382', ROS_IP='127.0.0.1')
    os.environ.pop('ROS_HOSTNAME', None)
    out = ROOT/'verification'; out.mkdir(exist_ok=True)
    os.environ['ROS_LOG_DIR'] = str(out/'private_ros_logs')
    node = None
    with (out/'private_roscore.log').open('w') as log:
        master = subprocess.Popen(['roscore', '-p', str(port)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            def online():
                try: return xmlrpc.client.ServerProxy(os.environ['ROS_MASTER_URI']).getPid('/smoke')[0] == 1
                except OSError: return False
            wait_for(online, 15)
            import cv2
            import numpy as np
            import rospy
            from sensor_msgs.msg import Image
            from std_msgs.msg import String
            from extra_color_node import ColorExtraNode, load_config
            from floor_filter import adapted_floor_gate
            from button_contract import adapted_gate
            from upstream.elevator_button_floor_main import EvidenceGate
            from task_contract import trip

            class Stub:
                value = '21'
                calls = 0
                def complete(self, *args, **kwargs):
                    self.calls += 1
                    assert kwargs['max_tokens'] == 8
                    return self.value

            rospy.init_node('task_perception_private_test', disable_signals=True)
            client = Stub(); config = load_config(ROOT/'upstream/perception.json')
            node = ColorExtraNode(config, client=client)
            worker = threading.Thread(target=node.run, daemon=True); worker.start()
            received = []
            sub = rospy.Subscriber(config['perception']['extra_state_topic'], String,
                                   lambda m: received.append(json.loads(m.data)), queue_size=100)
            camera = rospy.Publisher(config['camera']['topic'], Image, queue_size=5)
            requests = rospy.Publisher(config['perception']['extra_request_topic'], String, queue_size=1)
            wait_for(lambda: camera.get_num_connections() and requests.get_num_connections() and node.publisher.get_num_connections())

            def request(name, mode, current=19, target=20):
                stamp = rospy.Time.now().to_sec()
                requests.publish(String(data=json.dumps({**trip(current, target), 'mode': mode, 'request_id': name,
                                      'not_before_stamp': stamp, 'task_started_stamp': stamp})))
                wait_for(lambda: node.request['request_id'] == name)
                return stamp

            def send(image, name, stamp=None):
                msg = Image(); msg.header.stamp = rospy.Time.from_sec(stamp) if stamp else rospy.Time.now()
                msg.height, msg.width = image.shape[:2]; msg.encoding = 'bgr8'; msg.step = msg.width*3; msg.data = image.tobytes()
                before = len(received); camera.publish(msg)
                return wait_for(lambda: next((r for r in received[before:] if r.get('message_type') == 'result'
                        and r.get('request_id') == name and r.get('image_stamp') == msg.header.stamp.to_sec()), None))

            image = np.full((480, 848, 3), 115, dtype=np.uint8)
            for i, lit in enumerate((True, False)):
                x, y = 235, 200+65*i
                cv2.rectangle(image, (x, y), (x+34, y+32), (20, 20, 20), 2)
                cv2.arrowedLine(image, (x+17, y+25 if i == 0 else y+7), (x+17, y+7 if i == 0 else y+25),
                                (0, 0, 255) if lit else (45, 45, 45), 3, tipLength=.4)
            t = request('up', 'button_verify')
            gate = adapted_gate(EvidenceGate)(15, .72, 1, 0)
            gate.arm('button_verify', 'button_lit', True, t, request_id='up'); gate.button_direction = 'up'
            p = send(image, 'up')
            assert p['up_lit'] is True and p['down_lit'] is False and gate.feed(json.dumps(p), rospy.Time.now().to_sec())
            request('down', 'button_verify', 19, 18)
            p = send(image, 'down'); assert p['button_lit'] is False and p['selected_direction'] == 'down'
            assert client.calls == 0
            duplicate = send(image, 'down', p['image_stamp']); assert duplicate['button_lit'] is None

            t = request('floor', 'floor_monitor', 21, 20)
            gate = adapted_floor_gate(EvidenceGate)(15, .72, 1, 0)
            gate.arm('floor_monitor', 'floor', 20, t, request_id='floor')
            client.value = '21'; p = send(image, 'floor'); assert p['floor'] is None
            time.sleep(.55); p = send(image, 'floor'); assert p['floor'] == 21
            assert not gate.feed(json.dumps(p), rospy.Time.now().to_sec())
            client.value = '20'; p = send(image, 'floor'); assert p['floor'] == 20
            assert not gate.feed(json.dumps(p), rospy.Time.now().to_sec())
            client.value = 'U'; p = send(image, 'floor'); assert p['floor'] is None
            assert not gate.feed(json.dumps(p), rospy.Time.now().to_sec())
            client.value = '20'; p = send(image, 'floor'); assert gate.feed(json.dumps(p), rospy.Time.now().to_sec())
            client.value = '13'; p = send(image, 'floor')
            assert p['floor'] is None and p['floor_filter']['reason'] == 'implausible_jump'
            report = {'passed': True, 'robot_master_used': False, 'real_api_calls': 0, 'motion_publishers': False,
                      'cases': ['up/down independent ROI and selected-direction gate', 'single-frame confirmation',
                                'opposite button does not pass', 'duplicate timestamp rejected',
                                'floor short-output transport', 'adjacent target, unknown, target confirmed', 'large jump rejected'],
                      'note': 'Synthetic frames and fresh test timestamps validate communication, not real-world accuracy.'}
            (out/'private_ros_smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            print(json.dumps(report, ensure_ascii=False), flush=True)
            node.stop.set(); worker.join(timeout=3)
            sub.unregister(); camera.unregister(); requests.unregister(); rospy.signal_shutdown('complete')
        finally:
            if node: node.stop.set()
            os.killpg(master.pid, signal.SIGINT)
            try: master.wait(timeout=8)
            except subprocess.TimeoutExpired: master.terminate(); master.wait(timeout=5)


if __name__ == '__main__': main()
