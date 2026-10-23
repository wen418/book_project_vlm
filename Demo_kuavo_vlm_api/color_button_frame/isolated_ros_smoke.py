"""Private ROS-master transport test. No robot master, API calls or motion publishers."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import xmlrpc.client

ROOT=Path(__file__).resolve().parent


def wait_for(predicate, timeout=8):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.01)
    raise TimeoutError('Isolated test condition timed out')


def main():
    port=11381
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',port))  # Refuse to reuse or stop an existing master.
    os.environ.update(ROS_MASTER_URI='http://127.0.0.1:11381',ROS_IP='127.0.0.1')
    os.environ.pop('ROS_HOSTNAME',None)
    out=ROOT/'verification';out.mkdir(exist_ok=True)
    os.environ['ROS_LOG_DIR']=str(out/'ros_logs')
    with (out/'isolated_roscore.log').open('w') as log:
        master=subprocess.Popen(['roscore','-p',str(port)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        node=None
        try:
            def online():
                try:return xmlrpc.client.ServerProxy(os.environ['ROS_MASTER_URI']).getPid('/smoke')[0]==1
                except OSError:return False
            wait_for(online,15)
            import cv2
            import numpy as np
            import rospy
            from sensor_msgs.msg import Image
            from std_msgs.msg import String
            from extra_color_node import ColorExtraNode, load_config
            from button_contract import adapted_gate
            from elevator_button_floor_main import EvidenceGate

            class FloorStub:
                calls=0
                def complete(self,*a,**kw):
                    self.calls+=1
                    return '{"floor":15,"confidence":0.99,"evidence":"isolated test stub"}'

            rospy.init_node('color_button_private_test',disable_signals=True)
            config=load_config(ROOT/'upstream/perception.json')
            client=FloorStub()
            node=ColorExtraNode(config,client=client)
            worker=threading.Thread(target=node.run,daemon=True);worker.start()
            received=[]
            state_sub=rospy.Subscriber(config['perception']['extra_state_topic'],String,
                lambda m:received.append(json.loads(m.data)),queue_size=100)
            camera=rospy.Publisher(config['camera']['topic'],Image,queue_size=10)
            requests=rospy.Publisher(config['perception']['extra_request_topic'],String,queue_size=1)
            wait_for(lambda:camera.get_num_connections() and requests.get_num_connections())
            data=Path('/media/data/vlm/elevator_button_floor_demo/result')
            records=json.loads(Path('/media/data/vlm/qwen35_elevator/output/multirun_20261023_161347/results.json').read_text())
            known=[r for r in records if r['mode']=='button_verify']
            lit=[r for r in known if r['ground_truth'] is True and r['run_id']=='20261020_214211_61a85bd2']
            off=next(r for r in known if r['ground_truth'] is False and r['run_id']=='20261021_000006_050e1946')
            images=[cv2.imread(str(data/r['run_id']/r['image'])) for r in [off,*lit]]
            counter=0

            def request(name,mode='button_verify'):
                stamp=rospy.Time.now().to_sec()
                requests.publish(String(data=json.dumps({'mode':mode,'request_id':name,'not_before_stamp':stamp})))
                wait_for(lambda:node.request['request_id']==name)
                return stamp

            def send(image,name,stamp=None):
                nonlocal counter
                counter+=1
                msg=Image();msg.header.seq=counter
                msg.header.stamp=rospy.Time.from_sec(stamp) if stamp is not None else rospy.Time.now()
                msg.height,msg.width=image.shape[:2];msg.encoding='bgr8';msg.step=msg.width*3;msg.data=image.tobytes()
                previous=len(received);camera.publish(msg)
                result=wait_for(lambda:next((r for r in received[previous:] if r.get('message_type')=='result'
                    and r.get('request_id')==name and r.get('image_stamp')==msg.header.stamp.to_sec()),None))
                return result

            cases=[]
            not_before=request('normal')
            gate=adapted_gate(EvidenceGate)(15,.72,1,0)
            gate.arm('button_verify','button_lit',True,not_before,request_id='normal')
            first=send(images[0],'normal');assert first['button_lit'] is False
            assert not gate.feed(json.dumps(first),rospy.Time.now().to_sec())
            first=send(images[1],'normal');assert first['button_lit'] is True
            assert gate.feed(json.dumps(first),rospy.Time.now().to_sec())
            second=send(images[2],'normal');assert second['button_lit'] is True
            assert gate.feed(json.dumps(second),rospy.Time.now().to_sec())
            assert second['confidence']['button_lit'] is None
            cases.append('first fresh positive image immediately accepted by lower gate, no second frame needed')
            request('duplicate')
            first=send(images[1],'duplicate');assert first['button_lit'] is True
            duplicate=send(images[1],'duplicate',first['image_stamp']);assert duplicate['button_lit'] is None
            first=send(images[2],'duplicate');assert first['button_lit'] is True
            cases.append('duplicate timestamp rejected; next fresh frame is independent')
            request('interrupt')
            first=send(images[1],'interrupt');assert first['button_lit'] is True
            blank=send(np.zeros_like(images[1]),'interrupt');assert blank['button_lit'] is None
            first=send(images[2],'interrupt');assert first['button_lit'] is True
            cases.append('unknown frame remains unknown; next fresh positive accepted immediately')
            request('old_stage');send(images[1],'old_stage')
            request('new_stage');first=send(images[2],'new_stage');assert first['button_lit'] is True
            cases.append('new stage independently accepts its own fresh positive image')
            assert client.calls==0
            request('stale');stale=send(images[1],'stale',rospy.Time.now().to_sec()-3)
            assert stale['status']=='error' and stale['button_lit'] is None
            cases.append('stale frame rejected without API call')
            request('floor','floor_monitor')
            floor=send(images[2],'floor')
            assert floor['schema_version']==1 and floor['floor']==15 and client.calls==1
            cases.append('original floor API code still used, tested with local stub not real API')
            report={'passed':True,'private_master':os.environ['ROS_MASTER_URI'],'robot_master_used':False,
                    'motion_publishers':False,'real_api_calls':0,'cases':cases,
                    'confirmation_frames':1,
                    'note':'Recorded images were retimestamped for transport testing; not dynamic accuracy or reaction-latency measurement.'}
            (out/'single_frame_ros_smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(report,ensure_ascii=False),flush=True)
            node.stop.set();worker.join(timeout=3)
            state_sub.unregister();camera.unregister();requests.unregister()
            rospy.signal_shutdown('private test complete')
        finally:
            if node is not None:node.stop.set()
            os.killpg(master.pid,signal.SIGINT)
            try:master.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(master.pid,signal.SIGTERM);master.wait(timeout=5)


if __name__=='__main__':main()
