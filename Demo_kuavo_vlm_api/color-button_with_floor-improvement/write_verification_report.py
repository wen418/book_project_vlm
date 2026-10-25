"""Summarize development replay, abstentions and first-target-trigger simulations."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    out=ROOT/'verification'
    summary=json.loads((out/'floor_history_summary.json').read_text())
    targets=json.loads((out/'floor_target_comparison.json').read_text())
    rows=json.loads((out/'floor_history_results.json').read_text())
    lines=['# 楼层时间过滤：开发回归与接口验证','',
           '本轮没有真实API调用或机器人运动。主评估重放原API历史输出，不是重跑VLM，不是独立盲测。',
           '所有时间约束使用图像采集时间，而不是API响应时间或失败次数。人工标签只用于评分。',
           '16张人工未知在进入序列及计分前均排除；保留真实时间间隔，不插入/复制图像。','',
           '|阶段|明确标签|正确|错误|未知|正确/全部|明确回答准确率|',
           '|---|---:|---:|---:|---:|---:|---:|']
    for key,name in [('raw_api','原API'),('filtered_api','楼层过滤后')]:
        s=summary[key]
        lines.append('|%s|%d|%d|%d|%d|%.2f%%|%.2f%%|'%(name,s['n'],s['correct'],s['incorrect'],s['unknown'],s['accuracy_including_unknown']*100,s['answered_accuracy']*100))
    lines+=['','过滤了%d次原始错误，也扣住了%d次原本正确的读数。'%(summary['filtered_wrong_raws'],summary['withheld_correct_raws']),
            '这是一种错误/拒答权衡；不能把明确回答的准确率当成全部样本准确率。剩余错误仍存在，尤其相邻或持续误读不能仅靠时间规则消除。',
            '规则平均处理%.4f毫秒；不包括API、相机、ROS、确认等待或机器人动作。'%summary['filter_mean_ms'],
            '', '## 各目标层：每次请求首次触发（历史模拟）','',
            '|目标|原单帧首次误触发|新门禁首次误触发|原首次正确触发|新门禁首次正确触发|',
            '|---|---:|---:|---:|---:|']
    for target in targets:
        events=target['first_trigger_per_request']
        counts=[sum(e[key] is not None and e[key]['correct_at_trigger']==correct for e in events)
                for correct in (False,True) for key in ('raw_trigger','guarded_trigger')]
        lines.append('|%d|%d|%d|%d|%d|'%(target['target_floor'],*counts))
    lines+=['','同一批历史数据被按多个假设目标重复模拟，不是独立乘梯次数，不能将表格各层相加当总体成功率。未触发也不是成功，只能说明没有得到足够证据。',
            '请求首次触发时，按该时刻人工楼层是否等于目标评分；不使用目标修改任何模型读数。新门禁使用真实序号、图像时间戳和原置信度。',
            '', '## 剩余明确错误','']
    for r in rows:
        if r['filtered'] is not None and r['filtered']!=r['ground_truth']:
            lines.append('- %s / %s：人工%s，原API%s，过滤后%s。'%(r['run_id'],r['id'],r['ground_truth'],r['raw'],r['filtered']))
    lines+=['','## 已验证范围','',
            '44项测试覆盖跳变拒绝、范围随时间扩大、同层停留、旧/重复图、未知及低置信度、超时后重新建立基准、控制端目标双确认、按钮单帧及门原门禁。',
            '独立ROS master只验证消息链路：模拟API读19建立基准、读13拒绝、读18恢复；不是真实环境动态准确率或实机反应时间。',
            '下位机SSH mock保留原导航/回退/头部阶段顺序；不连接ROS、不发送机器人动作。原方案文件不修改。',
            '', '## 开发过程与局限','',
            '初版对所有楼层变化要求双确认，仅59/170帧有明确输出（58对、1错），可用率不足；已在开发阶段调整为普通合理读数单次、起始双确认、目标层双确认。初版代码/参数/回放结果保留于history/initial_all_floors_two_confirmations.tar.gz。',
            '本历史集因此只能作为开发回归，不能当作调整后的独立泛化证明。没有按标签拟合4秒/层等数值参数；该参数来自用户观测，不是硬物理上限。',
            '下一步应在原姿态静止观察楼层，再用新的连续乘梯数据验证不同速度、方向、停层、漏帧和持续误读；所有机器人动作需现场确认。']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('REPORT',out/'REPORT.md')


if __name__=='__main__':main()
