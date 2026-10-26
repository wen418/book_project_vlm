#!/usr/bin/python3
"""Use the existing camera/timing ROS node with a state-only VLM pipeline."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/media/data/vlm/Demo_vlm2_kuavo_api')
sys.path.insert(0, str(PROJECT))
import ros_stream_node as ros_node
from perception_schema import PlannerPayload
from vlm_client import OpenAICompatibleVLM
from compact_vlm import configure, infer_value


class CompactPipeline:
    def __init__(self, settings, **kwargs):
        self.client = configure(OpenAICompatibleVLM(settings.api_url, settings.model, settings.api_key,
                                                   timeout_seconds=settings.timeout_seconds))
        self.client._require_credentials()
        self.model = settings.model

    def infer_data_urls(self, sequence, current_images, required_fields, image_stamp=None, mode=''):
        if mode != 'door_monitor' or tuple(required_fields) != ('door_closed',) or len(current_images) != 1:
            raise ValueError('本节点仅支持独立门识别')
        start = time.perf_counter()
        value = infer_value(self.client, current_images[0]['data_url'], mode)
        return PlannerPayload(sequence=sequence, status='ok' if value is not None else 'uncertain',
                              door_closed=value, up_pressed=None, down_pressed=None,
                              confidence={'door_closed': None, 'up_pressed': None, 'down_pressed': None},
                              evidence={}, latency_ms=round((time.perf_counter()-start)*1000, 3),
                              model=self.model, few_shot_count=0, image_stamp=image_stamp, mode=mode)


ros_node.ElevatorPerception = CompactPipeline


class CompactDoorNode(ros_node.ContinuousRosNode):
    def _publish(self, payload):
        value = json.loads(payload.to_json())
        value.update(stage_timing_version=1, recognition_contract='compact_state_v1')
        self.publisher.publish(self.String(data=json.dumps(value, ensure_ascii=False)))


def main():
    import rospy
    import yaml
    rospy.init_node('elevator_vlm_api_perception', anonymous=False)
    settings = yaml.safe_load((ROOT/'door_ros.yaml').read_text())
    for key, value in settings.items(): rospy.set_param('~'+key, value)
    CompactDoorNode(PROJECT).spin()


if __name__ == '__main__': main()
