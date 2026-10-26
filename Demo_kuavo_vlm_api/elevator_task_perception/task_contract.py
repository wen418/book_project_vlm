"""Planner-owned trip intent, separate from observed elevator movement."""
import math


def trip(current, target):
    if any(type(v) is not int or not -10 <= v <= 150 for v in (current, target)):
        raise ValueError('current-floor/target-floor 必须为 -10 到 150 的整数')
    if current == target:
        raise ValueError('起始楼层等于目标楼层，应由规划直接跳过乘梯')
    return {'current_floor': current, 'target_floor': target,
            'button_direction': 'up' if target > current else 'down'}


def validate_request(request):
    value = trip(request.get('current_floor'), request.get('target_floor'))
    if request.get('button_direction') != value['button_direction']:
        raise ValueError('按钮方向与起始/目标楼层不一致')
    stamp = request.get('task_started_stamp')
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        raise ValueError('task_started_stamp 缺失或无效')
    if stamp > request['not_before_stamp']:
        raise ValueError('任务起始时间晚于观察请求')
    return value
