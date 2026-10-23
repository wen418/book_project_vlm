# 历史版本说明：两帧确认（已由单帧版替代，勿按本文件判断当前行为）

以下路径、命令和指标为改名前的历史记录，不是当前运行入口。当前单帧方案在`/media/data/vlm/Demo_kuavo_vlm_api/color_button_frame`，以README.md为准。

安装位置：`/media/data/vlm/Demo_kuavo_vlm_api/color_button_two_frame`。

仅替换厅外下行按钮判断；门、楼层和运动流程不变。按钮不调用VLM/API，也没有VLM回退；不确定则继续等待。门和楼层仍然调用原API，因此整个方案尚不是完全离线。

## 运行

在上位机原系统Python/ROS环境中执行，先退出Conda。先退出旧一键脚本及旧附加感知节点；新旧方案不能同时运行。本方案不会自动停止或杀掉已有节点。

仅检查依赖、SSH、硬件和相机，不启动感知/运动（沿用旧入口的只读检查）：

```bash
cd /media/data/vlm/Demo_kuavo_vlm_api/color_button_two_frame
./start_elevator_demo.sh --check
```

启动空闲感知并检查后退出，不转头、不导航、不自动发按钮观察请求（会加载原API凭据，但空闲时不推理）：

```bash
./start_elevator_demo.sh --check-startup
```

保持当前姿态，只观察按钮；不导航、不转头。请人工先对准按钮，现场仍保持急停可用：

```bash
./start_elevator_demo.sh --program quantify --observe-only button
```

确认静止感知、ROI和现场点位后，再人工启动完整流程（下面命令会驱动机器人）：

```bash
./start_elevator_demo.sh --program quantify --entry-plan reduced_backtrack --target-floor 15
```

目标楼层仍由参数控制，不会修改地图/导航点。原流程仍不负责物理按键。普通demo也保留：`./start_elevator_demo.sh --target-floor 15`。不加检查参数默认仍是完整演示，会运动。

## 两帧确认和接口

- 精确ROI及颜色阈值与已测版本一致：搜索框`[230,235,105,90]`，多通道Canny/轮廓定位、3像素外扩、HSV＋红色优势＋面积/连通域判据。
- 按钮按相机新帧处理，后台队列最多4帧。两次回调序号必须相邻、时间戳严格增加、间隔不超过0.35秒，两张均须在当前请求之后且处理时年龄不超过0.5秒。不会把同一帧重复推理当成两帧。
- 第一张已亮只发布未知/待确认；第二张合格已亮才发布`button_lit=true`。未亮、未知、解码错误、重复/逆序时间戳、丢帧、过大间隔或请求切换都会打断连续性；下一张已亮重新计数。
- 未亮可单帧报告`false`，但不会触发后退。未知不当成未亮。计数不跨请求、转头、阶段或节点重启。心跳不是新观察，不能用于确认。
- 按钮结果使用`schema_version=3`及`button_backend=roi_color_two_frame_v1`，携带两帧时间戳、相邻回调序号、两个原始True的证明；`confidence.button_lit=null`，不伪造模型置信度。
- 控制端显式适配按钮schema3，并独立校验请求ID、消息序号、图像新鲜度和两帧证明；楼层schema1、门schema2及原置信度门槛不变。执行前还会检查结果年龄。
- 兼容旧就绪接口的`inference_hz=1.0`用于保留楼层最大1Hz契约，按钮实际由相机驱动，不是1Hz。两帧确认时间取决于相机帧率，不等于算法耗时乘2。
- 固定848×480头部相机。分辨率不符时拒绝启动；姿态偏移导致定位失败时返回未知。不自动缩放、猜ROI或用上帧识别结果代替当前帧。
- 上下位机ROS时钟必须同步；按钮结果的新鲜度要求比原15秒更严格。`latency_ms`记录当前帧解码/规则处理耗时，不是两帧确认或机器人反应时间；`button_confirmation_span_ms`另记两帧图像跨度。

两帧能过滤部分短暂干扰，但持续反光也可能连续出现，不能假设误报概率简单平方。尚未验证真实连续视频的两帧准确率。

## 启动链与文件

新`start_elevator_demo.sh` → `launcher.py` → 原门启动脚本＋新`run_extra_perception.sh` → `extra_color_node.py` → 原下位机流程快照＋按钮门禁适配。

- `button_color_rule.py`：冻结的单帧规则；`button_contract.py`：两帧计数和控制端验收。
- `upstream/`：本次部署时上/下位机相关源文件、原参数的只读快照；不修改原文件。
- `launcher.py`：沿用原启动器，保留量化参数、硬件检查、共享锁、急停确认、清理和日志机制；拒绝复用已有附加感知发布者。
- 下位机代码由SSH传入内存执行，适配只在新方案进程内生效。不覆盖、不安装任何下位机文件。原入口SHA256与快照不一致则拒绝运行，避免使用旧点位。若现场原入口配置更新，需重新核对并更新方案快照及清单，不能直接绕过哈希检查。
- `manifest.json`：发布文件SHA256清单；启动时校验。本清单是防误改，不是签名认证机制。
- 新方案量化记录默认写入本目录`result/`，启动日志在对应结果的`logs/`，普通demo日志在本目录`logs/`。原量化记录与人工标签不改动。
- 原门：`/media/data/vlm/Demo_vlm2_kuavo_api/deploy/run_ros_api.sh`。
- 原楼层实现快照来自`/media/data/vlm/elevator_button_floor_demo/elevator_extra_perception.py`；仍用原`config.door_fast.json`和凭据加载方式。
- 原量化工具：`/media/data/vlm/elevator_button_floor_demo/elevator_quantification.py`；阶段计时：原`/media/data/vlm/Demo_vlm2_kuavo_api/stage_timing.py`。

凭据仍只引用原文件，不复制、不写入日志。

## 本次验证与限制

19项接口/时序测试通过；52张单帧回归48正确、0错误、4未知，与原规则结果完全一致。

独立ROS master（127.0.0.1:11381）测试通过：真实消息传输下，两帧证明被控制端接收；重复时间戳、未知图、换阶段、旧帧均不放行；楼层仍调用原代码（使用本地模拟API，无真实API调用）。测试图片被重新赋予时间戳，仅用于接口测试，不是动态准确率或实机反应延迟测试。

经实际SSH在下位机运行`--mock --quantify --entry-plan reduced_backtrack --target-floor 15`通过，完整阶段顺序与原方案一致，没有连接ROS或发送动作。

详情见`verification/`。未执行真实运动、真实API冒烟或真实相机连续按键验收。原门和楼层的识别能力没有因本次改动提升，仍需单独验收。

实际执行新入口`--check`时，下位机运行依赖检查通过，但`/hardware/is_ready=None`，因此沿用原安全逻辑在整机就绪检查处停止。这不是实机验收通过；需现场正常启动主控后重新检查，本方案不会自动初始化整机。

## 回退

退出新方案及其节点后，运行原`/media/data/vlm/Demo_kuavo_vlm_api/start_elevator_demo.sh`即可使用旧方案；没有原文件需要恢复。退出程序不保证底盘制动，仍使用现场急停机制。
