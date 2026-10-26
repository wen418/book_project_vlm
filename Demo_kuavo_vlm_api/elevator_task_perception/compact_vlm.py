"""Short model answers; transport metadata is assembled by Python, never generated."""
import re

PROMPTS = {
    'floor_monitor': ('你是电梯当前运行楼层显示屏的视觉读取器，只读取屏幕此刻明确显示的楼层整数。'
                      '不要读取按钮编号、门牌、楼层贴纸或目标楼层。不要根据任务、上一帧或数字变化规律猜测。'
                      '输入只有当前一帧。屏幕不在画面、数字不完整、过曝、模糊、遮挡或刷新缺段导致无法确定时，只输出 U。'
                      '不要把缺段数字或局部图案补全成另一个楼层，不能把其他红光当成楼层数字。'
                      '只输出楼层整数或 U，不解释，不输出JSON。'),
    'door_monitor': ('只判断当前电梯门。完全关闭输出 C；入口完整敞开、没有门扇侵入通道输出 O；'
                     '窄缝、部分开启、运动中、看不清或不确定输出 U。门缩入两侧且入口完整敞开也算 O。'
                     '只输出一个字母，不解释，不输出JSON。')}


def configure(client):
    client.use_json_object = False
    client.max_tokens = 8
    client.enable_thinking = False
    client.max_retries = 0
    return client


def parse(text, mode):
    value = text.strip()
    if value == 'U':
        return None
    if mode == 'door_monitor':
        if value not in ('C', 'O'):
            raise ValueError('门状态必须是 C/O/U')
        return value == 'C'
    if mode != 'floor_monitor' or not re.fullmatch(r'-?(0|[1-9][0-9]{0,2})', value):
        raise ValueError('楼层必须是整数或 U')
    value = int(value)
    if not -10 <= value <= 150:
        raise ValueError('楼层超出配置范围')
    return value


def infer_value(client, data_url, mode):
    messages = [{'role': 'system', 'content': PROMPTS[mode]},
                {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': data_url}}]}]
    return parse(client.complete(messages, max_tokens=8), mode)


def infer_floor(client, data_url, mode, threshold):
    if mode != 'floor_monitor':
        raise ValueError('按钮必须走本地颜色规则')
    value = infer_value(client, data_url, mode)
    return {'floor': value, 'confidence': {'floor': None}, 'evidence': {'floor': ''},
            'status': 'ok' if value is not None else 'uncertain',
            'recognition_contract': 'compact_state_v1'}
