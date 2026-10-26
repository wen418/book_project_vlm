# 电梯任务感知方案 elevator_task_perception

本版面向规划调度接入：规划明确传入本次乘梯的起始楼层和目标楼层，感知核实按钮、楼层和门的实际状态。独立部署，旧的 `color-button_with_floor-improvement` 和下位机原入口不覆盖。

## 一键启动

登录上位机（当前 `leju_kuavo@192.168.121.215`）后执行：

```bash
cd /media/data/vlm/Demo_kuavo_vlm_api/elevator_task_perception
# 只读初始化检查；不启动运动。
./start_elevator_demo.sh --current-floor 21 --target-floor 20 --check
# 启动空闲感知并核对接口后退出；不调用 VLM、不运动。
./start_elevator_demo.sh --current-floor 21 --target-floor 20 --check-startup
# 只测当前镜头中的按钮，不导航、不转头。此例等待19楼上行按钮亮。
./start_elevator_demo.sh --current-floor 19 --target-floor 20 --program quantify --observe-only button
# 完整实机流程，会移动机器人；由现场操作人员启动。
./start_elevator_demo.sh --current-floor 21 --target-floor 20 --program quantify --entry-plan reduced_backtrack
```

下位机 SSH 默认 `lab@kuavo_master`，通过上位机内部网络连接，不绑定下位机Wi-Fi地址。可用 `--lower-host 主机名` 或 `ELEVATOR_LOWER_HOST` 覆盖。上位机ROS_IP默认根据到该主机的路由自动选择；下位机ROS_IP从实际SSH连接的服务端地址取得；ROS_MASTER_URI默认使用该内部主机名。显式设置的ROS_MASTER_URI/ROS_IP仍优先。上下位机需同步时间。内部主机名应由现场DNS或hosts正确解析，内部网段改变后无需修改程序中的地址。

两个楼层参数必须显式给出；相同楼层直接报错，由规划跳过乘梯。本版仍使用现场原地图和局部导航坐标，不自动切换地图；能识别某层不意味着该层的导航点已经标定。按钮观察点保持 x=-0.039m、y=0.914m、theta=1.0°。

## 识别策略

- **按钮**：本地 OpenCV 边缘/轮廓定位 + HSV、红色通道差值、连通域颜色规则，单张新相机帧确认。双按钮分别裁剪和判断；选择哪个由起始/目标楼层决定。输出 `up_lit`、`down_lit`、`selected_direction`，兼容字段 `button_lit` 仅代表本次所选方向。相反方向按钮亮不能放行。
- **面板配置**：`button_layout.json` 中双按钮搜索框为 [150,100,250,320]，单按钮搜索框沿用 [190,175,135,120]，分辨率848×480。目前明确配置21楼只有下键；其他楼层按“上键在上、下键在下”的竖直双键布局处理。算法不识别箭头语义；竖直布局必须现场核实。轮廓去重后要求尺寸、对齐、间距符合按钮组；缺一个或存在歧义返回未知，不把唯一可见按钮猜成目标方向。其他单按钮楼层须显式配置。
- **楼层**：VLM只读取本帧数字或 U。完全取消升降方向投票和反向拒绝，上升、下降、停留都允许。按经过时间限制跳变，参考约4秒/层并留1层余量；不是保证物理速度的硬定律。保留近期历史约束，避免连续相邻误读快速漂移。起始楼层仅作为有时限的初始参考，不是视觉确认票；超过12秒无可信读数则放弃旧参考，以3张新读数重新建立锚点，避免永久锁住。
- **到达目标**：5秒内最近3个结果至少2张有效目标图，跨度≥0.5秒。允许中间1次未知；有效非目标楼层清空确认；重复时间戳、心跳、旧请求、过期画面不能计票。确认后仍检查目标楼层的门状态，不要求先看到中间楼层或确认运动方向。
- **门**：独立VLM调用，仅输出 C（关）、O（入口完整敞开）、U（未知）。沿用原门规则：部分开、窄缝、运动中不放行，不新增“部分打开”业务状态。
- **短输出**：门为 C/O/U，楼层为整数/U，max_tokens=8，关闭思考、JSON强制格式、few-shot和格式修复重试。严格解析，额外文字或截断不抢救成有效状态。概率和证据不再由模型生成；接口 `confidence` 为 null，不伪造高置信度。模型仍为现有配置/环境指定的 API 模型，只有按钮离线；本版不是完整离线部署。

## 检查与量化

硬件就绪、接口、初始实测头姿、定位静止/新鲜度检查集中在初始化。运行时头部命令只发送一次并等待配置的2秒，不再循环核验实测角度，不把指令值宣称为实测到位；保留原 `head_reached` 事件名用于量化视角采样，但 measured=null、arrival_verified=false、completion_policy=command_plus_settle_delay。量化中不应将其解释成实测头部到达时间。

每段导航不再反复等待底盘静止来准备量化；只读一次最新定位作为记录基线。缓存初始化成功的导航服务可用性检查；真实服务调用、到达等待/超时、回退禁转和真实定位采样保留。图像新鲜度、请求隔离、进程退出和错误反馈仍实时生效。这些是执行与数据有效性条件，不是重复启动体检。

成功量化输出 `./result/<run_id>/`；中间文件 `./.result_pending/`；失败文本诊断保存在 `./verification/runtime_failures/<run_id>/`，不计入成功试验。继续使用原量化器，保留事件序号严格检查和混合日志的完整事件拆分修复。按钮首阳性、导航到达、回退量化事件保持原接口。头部实测检查的语义变化已在上文注明。

## 规划调度接入

最小方式是规划以子进程调用上面的一键脚本，传入每段任务的两个楼层，读取进程完成/失败和量化结果。完整流程当前仍不负责实体按键：会等待所选厅外按钮已经点亮，按钮动作由其他模块或现场人员完成。

规划也可复用感知节点，向 `/elevator/demo/observe`（std_msgs/String，JSON）发送：

```json
{
  "mode": "button_verify",
  "request_id": "每次观察唯一的ID",
  "current_floor": 19,
  "target_floor": 20,
  "button_direction": "up",
  "task_started_stamp": 1792928000.0,
  "not_before_stamp": 1792928002.0
}
```

示例中的时间仅说明格式，集成时必须填写本段任务开始、本次观察开始时真实的ROS时间秒。楼层观察把 mode 改为 `floor_monitor`。每次阶段/任务变更使用新 request_id；task_started_stamp 取本段任务实际开始时间，不要每帧刷新来伪造起始楼层参考。新请求清空历史。状态发布 `/elevator/demo/state`，含同一任务信息、image_stamp、floor_raw、过滤后的floor以及过滤原因。等待结果必须按当前request_id、模式和新鲜图像过滤，不能将 latched/heartbeat 当新证据。可直接复用本目录 button_contract/floor_filter 中的 gate。门继续使用 `/elevator/perception/mode` 与 `/elevator/perception/state`。

主流程正式接入时，地图/导航点应由规划负责。本版并未增加到站方向灯识别；电梯可能先反向服务其他乘客，楼层读取允许该情况，但不会据参数伪造运动方向。

## 验证与维护

```bash
/usr/bin/python3 -m unittest discover -s tests -v
# 独立11382端口私有ROS master，使用合成图和假VLM，无机器人运动/真实API调用。
/usr/bin/python3 smoke_private_ros.py
```

双按钮合成测试仅验证算法分离和接口，不等于现场准确率；现场双按钮完整画面仍需核验搜索区域和上下布局。三张历史单按钮图片仅回归已核实结果。实际API兼容性/短输出耗时记录放在 verification 下，不将小样本结果宣称为动态准确率。

修改本目录配置或运行代码后，先测试，再运行 `build_manifest.py` 更新校验。`manifest.json` 记录本版和共享API依赖的SHA256。下位机原入口仅检查原始摘要后在内存加载本版副本，不覆盖磁盘原文件；机器人API/量化器仍来自既有路径。旧方案不修改，新旧节点不可混用。
