"""Build auditable observation timelines without ROS or inferred timestamps."""
import html
import math

MODES = {"button_verify": "下行按钮", "floor_monitor": "楼层显示", "door_monitor": "电梯门"}
ANCHORS = {
    "position_arrived": "导航报告到点", "navigation_settled": "到点后稳定完成",
    "head_started": "开始调整头部", "head_settled": "实测头部稳定",
    "stage_request_sent": "开始本阶段观察／发出请求", "stage_request_received": "感知节点收到请求",
    "first_frame_captured": "本阶段首张新图像拍摄", "first_frame_received": "感知节点收到该新图像",
    "first_frame_selected": "首次选择推理源帧", "first_vlm_request": "首次调用VLM客户端",
    "first_vlm_response": "该次客户端调用结束／返回", "first_valid_result": "首次有效识别结果",
    "button_unlit_ready": "已识别未亮，允许人工按键", "first_positive": "首次识别目标状态",
    "confirmation": "本阶段识别确认通过",
}
WORKFLOW_EVENTS = {
    "navigation_start":"开始导航", "command_issue":"开始发令", "command_accepted":"发令返回",
    "motion_started":"实测开始运动", "arrival_reported":"导航报告到点", "navigation_arrived":"到点后稳定完成",
    "head":"开始调整头部", "head_reached":"实测头部稳定", "observe_start":"开始观察",
    "observe_confirmed":"观察确认通过", "button_first_positive":"首次识别按键点亮",
    "door_wait_start":"开始等门", "door_confirmed":"确认门全开", "workflow_completed":"所选流程完成",
}


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def build_timeline(events, timing, observations, reference=None):
    starts = [(i, e) for i, e in enumerate(events) if e["event"] == "door_wait_start" or
              (e["event"] == "observe_start" and e.get("mode") in MODES)]
    stages = []
    for index, (position, start) in enumerate(starts):
        mode = "door_monitor" if start["event"] == "door_wait_start" else start["mode"]
        next_pos = starts[index+1][0] if index+1 < len(starts) else len(events)
        end_stamp = events[next_pos].get("ros_stamp", math.inf) if next_pos < len(events) else math.inf
        before, window = events[:position], events[position:next_pos]
        first = lambda rows: next(iter(rows), None)
        last = lambda rows: next(iter(list(rows)[::-1]), None)
        anchor = {}

        def put(key, event, stamp=None):
            if event is not None and numeric(event.get("ros_stamp") if stamp is None else stamp):
                anchor[key] = dict(ros_stamp=event["ros_stamp"] if stamp is None else stamp,
                                   source=event.get("source", "lower"), image_stamp=event.get("image_stamp"),
                                   status=event.get("status"),
                                   inference_id=event.get("inference_id"), api_call_id=event.get("api_call_id"))

        nav = last(e for e in before if e["event"] == "navigation_start")
        if nav:
            for key, name in (("position_arrived", "arrival_reported"), ("navigation_settled", "navigation_arrived")):
                put(key, last(e for e in before if e["event"] == name and e.get("point") == nav.get("point")
                              and e.get("ros_stamp", -math.inf) >= nav.get("ros_stamp", math.inf)))
        view = {"button_verify": "button", "floor_monitor": "floor", "door_monitor": "door"}[mode]
        put("head_started", last(e for e in before if e["event"] == "head" and e.get("view") == view))
        put("head_settled", last(e for e in before if e["event"] == "head_reached" and e.get("view") == view))
        put("stage_request_sent", start, start.get("not_before_stamp", start.get("ros_stamp")))
        candidates = [e for e in timing if e.get("event") == "stage_request_received" and e.get("mode") == mode
                      and (e.get("request_id") == start.get("request_id") if mode != "door_monitor"
                           else start.get("ros_stamp", math.inf)-.2 <= e.get("ros_stamp", -math.inf) < end_stamp)]
        session = first(sorted(candidates, key=lambda e: e["ros_stamp"]))
        samples = [e for e in timing if session and e.get("stage_id") == session.get("stage_id")
                   and e.get("instance_id") == session.get("instance_id")]
        samples.sort(key=lambda e: e["ros_stamp"])
        pick = lambda name: first(e for e in samples if e.get("event") == name)
        put("stage_request_received", session)
        frame = pick("first_fresh_frame_received")
        put("first_frame_received", frame)
        if frame:
            put("first_frame_captured", frame, frame.get("image_stamp"))
        put("first_frame_selected", pick("frame_selected"))
        request = pick("vlm_request_started")
        put("first_vlm_request", request)
        put("first_vlm_response", first(e for e in samples if request and e.get("event") == "vlm_response_received"
                                       and e.get("api_call_id") == request.get("api_call_id")))
        valid = [e for e in samples if e.get("event") == "inference_result" and e.get("accepted") is True
                 and e.get("status") == "ok" and e.get("image_stamp", -math.inf) >= start.get("not_before_stamp", start.get("ros_stamp", math.inf))]
        put("first_valid_result", first(valid))
        field = {"button_verify": "button_lit", "floor_monitor": "floor", "door_monitor": "door_closed"}[mode]
        expected = start.get("expected", False if mode == "door_monitor" else True)
        put("first_positive", first(e for e in valid if type(e.get("values", {}).get(field)) is type(expected)
                                    and e["values"][field] == expected))
        if mode == "button_verify":
            put("button_unlit_ready", first(e for e in valid if e.get("values", {}).get("button_lit") is False))
        confirmed = first(e for e in window if (e["event"] == "door_confirmed" if mode == "door_monitor" else
                          e["event"] == "observe_confirmed" and e.get("result", {}).get("request_id") == start.get("request_id")))
        put("confirmation", confirmed)
        warnings = []
        if not session:
            warnings.append("缺少感知阶段遥测，不能补造接图或推理起点；旧数据只能显示已有节点。")
        if len(candidates) > 1:
            warnings.append("观察窗口内有多次请求；表中显示首次请求，请结合原始遥测排查重启或重复发令。")
        if mode == "button_verify" and "button_unlit_ready" not in anchor:
            warnings.append("未记录到有效的按钮未亮基准；无法证明人工按键前识别链路已就绪。")
        first_pair = [("stage_request_sent", "stage_request_received"), ("first_vlm_request", "first_vlm_response")]
        if any(a in anchor and b in anchor and anchor[b]["ros_stamp"] < anchor[a]["ros_stamp"]-.2 for a,b in first_pair):
            warnings.append("跨节点时刻顺序异常；请检查上下位机时钟同步，不把负差值作为正常耗时。")
        durations = {}
        for name,a,b in (("arrival_to_observation_s","position_arrived","stage_request_sent"),
                         ("fresh_frame_to_vlm_s","first_frame_received","first_vlm_request"),
                         ("first_client_call_s","first_vlm_request","first_vlm_response"),
                         ("first_valid_to_confirmation_s","first_valid_result","confirmation")):
            delta = anchor[b]["ros_stamp"]-anchor[a]["ros_stamp"] if a in anchor and b in anchor else None
            durations[name] = round(delta,6) if numeric(delta) and delta>=0 else None
        calls=[]
        for call in (e for e in samples if e.get("event")=="vlm_request_started"):
            response=first(e for e in samples if e.get("event")=="vlm_response_received" and e.get("api_call_id")==call.get("api_call_id"))
            parsed=first(e for e in samples if e.get("event")=="inference_result" and e.get("inference_id")==call.get("inference_id"))
            calls.append(dict(api_call_id=call.get("api_call_id"),inference_id=call.get("inference_id"),
                              image_stamp=call.get("image_stamp"),start_ros_stamp=call.get("ros_stamp"),
                              end_ros_stamp=response.get("ros_stamp") if response else None,
                              duration_s=response.get("duration_s") if response else None,
                              status=response.get("status") if response else "missing",
                              accepted=parsed.get("accepted") if parsed else None))
        if calls and numeric(calls[0]["duration_s"]) and calls[0]["duration_s"]>=0:
            durations["first_client_call_s"]=calls[0]["duration_s"]
        stages.append(dict(index=index+1, mode=mode, name=MODES[mode], request_id=start.get("request_id"),
                           stage_id=session.get("stage_id") if session else None,
                           correlation="request_id" if mode!="door_monitor" else "mode_and_time_window",
                           anchors=anchor, durations=durations, client_calls=calls, warnings=warnings))
    if reference is None:
        reference = next((e.get("ros_stamp") for e in events if numeric(e.get("ros_stamp"))), None)
    workflow=[]
    for event in events:
        if event["event"] not in WORKFLOW_EVENTS:
            continue
        stamp=event.get("onset_ros_stamp") if event["event"]=="motion_started" else event.get("ros_stamp")
        workflow.append(dict(event=event["event"],label=WORKFLOW_EVENTS[event["event"]],
                             event_sequence=event.get("event_sequence"),ros_stamp=stamp,
                             reported_ros_stamp=event.get("ros_stamp"),point=event.get("point"),view=event.get("view"),
                             mode=event.get("mode",event.get("result",{}).get("mode")),scope=event.get("scope"),
                             from_run_start_s=round(stamp-reference,6) if numeric(stamp) and numeric(reference) else None))
    return dict(schema_version=1, reference_ros_stamp=reference, stages=stages,workflow=workflow)


def annotation_alignment(timeline, kind, opened, previous):
    mode = "button_verify" if kind == "button" else "door_monitor"
    stage = next((s for s in timeline.get("stages", []) if s["mode"] == mode), None)
    if not numeric(opened):
        return dict(status="unmarked", message="尚未标记事件发生画面")
    if not stage:
        return dict(status="timing_unavailable", message="本轮缺少阶段锚点，无法判断事件发生时系统准备到了哪一步")
    a = stage["anchors"]
    sent = a.get("stage_request_sent",{}).get("ros_stamp")
    fresh = a.get("first_frame_captured",{}).get("ros_stamp")
    ready = a.get("button_unlit_ready",{}).get("ros_stamp") if kind=="button" else fresh
    if numeric(sent) and opened < sent:
        return dict(status="before_observation", message="事件发生在本阶段开始观察之前；端到端耗时包含阶段准备等待")
    if numeric(fresh) and opened <= fresh:
        return dict(status="before_first_fresh_frame", message="事件早于或就在首张新图像，无法证明完整记录了变化起点")
    if ready is None:
        return dict(status="baseline_unverified", message="未确认事件前的识别就绪基准，不能把端到端耗时当作纯推理耗时")
    if opened < ready:
        return dict(status="before_ready", message="点亮早于未亮识别就绪提示；该耗时包含识别准备阶段")
    if not numeric(previous):
        return dict(status="single_frame_only", message="只标记变化后画面，缺少变化前边界；仅能给到该帧的近似延迟")
    if previous < ready:
        return dict(status="straddles_readiness", message="事件时间范围跨过识别就绪锚点，无法确定是否提前操作")
    return dict(status="after_ready", message="变化前后画面均晚于就绪锚点，可用于评估就绪后的系统响应")


def anchor_rows(timeline):
    reference=timeline.get("reference_ros_stamp")
    return [dict(stage=s["index"],mode=s["mode"],anchor=key,label=label,ros_stamp=a.get("ros_stamp"),
                 from_run_start_s=round(a["ros_stamp"]-reference,6) if key in s["anchors"] and numeric(reference) else None,
                 image_stamp=a.get("image_stamp"),source=a.get("source"),api_call_id=a.get("api_call_id"),status=a.get("status"))
            for s in timeline["stages"] for key,label in ANCHORS.items() for a in [s["anchors"].get(key,{})]
            if key!="button_unlit_ready" or s["mode"]=="button_verify"]


def timeline_html(timeline, alignments=None):
    esc=lambda x:html.escape(str(x))
    rows=anchor_rows(timeline)
    cards=[]
    for stage in timeline["stages"]:
        table=[]
        for row in sorted((r for r in rows if r["stage"]==stage["index"]),
                          key=lambda r:r["ros_stamp"] if numeric(r["ros_stamp"]) else math.inf):
            stamp=row["ros_stamp"]
            offset=row["from_run_start_s"]
            table.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                esc(row["label"]+('（调用异常）' if row.get("status")=="error" else '')), '%.6f'%stamp if numeric(stamp) else '未记录',
                '%+.3f 秒'%offset if numeric(offset) else '—',esc(row.get("source") or '—')))
        warnings=''.join('<p class="warning">%s</p>'%esc(w) for w in stage["warnings"])
        calls=''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'%(
            esc(c.get("image_stamp")),esc(c.get("start_ros_stamp")),
            '%.3f 秒'%c['duration_s'] if numeric(c.get('duration_s')) else '未记录',
            esc(('已弃用，' if c.get('accepted') is False else '')+c['status'])) for c in stage.get('client_calls',[]))
        details='<details><summary>本阶段全部客户端调用（%s次）</summary><table><tr><th>源帧拍摄时间</th><th>调用起点</th><th>调用耗时</th><th>状态</th></tr>%s</table><p>格式纠错可能对同一帧多次调用；这些调用不会重复增加准确率样本。</p></details>'%(
            len(stage.get('client_calls',[])),calls)
        cards.append('<section><h2>观察阶段 %s：%s</h2>%s<table><tr><th>锚点</th><th>ROS时间</th><th>相对本轮开始</th><th>来源</th></tr>%s</table>%s<p>门阶段按模式及时间窗口关联；按键和楼层按request_id关联。未记录的锚点不推算填补。</p></section>'%(
            stage["index"],esc(stage["name"]),warnings,''.join(table),details))
    checks=''.join('<p><strong>%s：</strong>%s</p>'%(esc(k),esc(v["message"])) for k,v in (alignments or {}).items())
    points={'button':'按钮观察点','origin':'原点','entry_wait':'减少回退观察点','inside':'电梯内部','exit':'电梯外部'}
    flow=''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'%(
        esc(r['label']),esc(points.get(r.get('point'),r.get('view') or r.get('mode') or r.get('scope') or '—')),
        '%.6f'%r['ros_stamp'] if numeric(r.get('ros_stamp')) else '未记录',
        '%+.3f 秒'%r['from_run_start_s'] if numeric(r.get('from_run_start_s')) else '—')
        for r in sorted(timeline.get('workflow',[]),key=lambda r:r['ros_stamp'] if numeric(r['ros_stamp']) else math.inf))
    workflow='<section><h2>全流程节点：导航、观察与最终退出</h2><table><tr><th>节点</th><th>位置／对象</th><th>ROS时间</th><th>相对本轮开始</th></tr>%s</table></section>'%flow
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>阶段时间锚点</title>
<style>body{font:16px system-ui;max-width:1150px;margin:30px auto;padding:0 20px;background:#f3f5f8;color:#17243b}section{background:white;padding:20px;margin:20px 0}table{border-collapse:collapse;width:100%%}th,td{padding:10px;text-align:left;border-bottom:1px solid #ddd}.warning{color:#a33422}</style>
<h1>阶段时间锚点</h1><p><a href="report.html">返回报告</a></p><p>本页区分阶段准备、接图、客户端调用和识别确认。客户端调用包含网络等待及其内部重试，并不等同于云端纯计算耗时。跨机器ROS时间需要同步。</p>%s%s</html>'''%(
        checks,workflow+(''.join(cards) or '<p>本轮没有可用的观察阶段事件，旧数据不能补造锚点。</p>'))
