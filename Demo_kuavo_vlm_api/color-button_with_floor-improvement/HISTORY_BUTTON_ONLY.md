# 历史基线：仅改按钮的单帧版本（当前楼层改进方案请看README.md）

按用户要求，已从两帧改为**单帧判断**，目录统一命名为`color_button_frame`。旧目录不保留同名入口，请使用下面的新路径。

上位机部署位置：`/media/data/vlm/Demo_kuavo_vlm_api/color_button_frame`。

本地源代码：`E:\book_project\yolo_v8\Demo_vlm2_kuavo\color_button_frame`。

Git分支：`codex/color-button-frame`。这是新增的独立方案分支；父目录原一键脚本和下位机原文件不覆盖，原方案仍可使用。本目录针对已经部署原门/楼层API及ROS依赖的上位机，不是整套机器人软件的全新安装包。

## 当前行为

- 当前请求之后的一张新图像，按钮定位和图像质量检查通过、颜色规则判断已亮，即发布`button_lit=true`并允许控制端继续。不等待第二帧，不做连续计数或时序投票。
- 判断未亮则发布false；定位失败、解码错误、曝光不合格或颜色边界仍发布未知/错误，不根据之前的已亮结果猜测。
- 仍检查请求ID、模式、消息序号、图像时间戳；旧阶段图、过期图、重复时间戳或推理途中切换请求不会触发放行。当前图像年龄要求不超过0.5秒；上下位机ROS时钟需同步。
- 精确ROI定位和颜色阈值没有修改。固定848×480相机；尺寸不符拒绝启动，站位偏移导致找不到按钮则返回未知。
- 按钮接口为`schema_version=4`、`button_backend=roi_color_single_frame_v2`，证明中`confirmed_frames=1`。控制端和启动器同步更新，不接受旧两帧版本混用。不伪造模型置信度，`confidence.button_lit=null`。
- 按钮不调用API/VLM，也没有VLM回退。门、楼层、运动阶段、点位、急停检查和量化流程不变；门和楼层仍使用原API，整套系统仍需网络。

相邻两帧通常高度相关，不是两次独立验证。此前两帧可以过滤部分瞬态干扰，但不能保证解决持续反光。本次遵照用户选择取消确认等待，不代表单帧识别已获得新的动态准确率保证。

## 运行命令不变

在上位机原系统Python/ROS环境运行，先退出Conda及旧启动脚本/节点。脚本不会自动杀掉已有程序。

```bash
cd /media/data/vlm/Demo_kuavo_vlm_api/color_button_frame
./start_elevator_demo.sh --check
```

只读预检不发送动作。此前检查遇到`/hardware/is_ready=None`，请现场按正常流程启动主控，不会自动初始化整机。

保持当前姿态，只观察按钮（不导航、不转头，需人工对准按钮）：

```bash
./start_elevator_demo.sh --program quantify --observe-only button
```

现场确认感知、地图点位和急停后，再人工运行完整流程；下面命令**会驱动机器人**：

```bash
./start_elevator_demo.sh --program quantify --entry-plan reduced_backtrack --target-floor 15
```

其他原参数保留。`--check-startup`只启动空闲感知检查后退出。默认不加检查参数仍会运行完整演示。原流程仍不负责物理按键；目标楼层参数不会改变导航点或地图。

## 目录、依赖和回退

- `button_color_rule.py`：不变的精确ROI＋颜色规则；`button_contract.py`：单帧状态证明和控制端验收。
- `extra_color_node.py`：按钮按相机新帧处理；楼层委托原实现。`latency_ms`是当前帧处理耗时，非相机到运动反应时间；单帧确认跨度字段为0。
- `launcher.py`：复用原启动器并适配按钮新接口；与原入口共享启动锁，拒绝复用已有附加感知发布者。
- `upstream/`：原启动器、附加感知、配置和下位机流程快照。下位机通过SSH在内存执行适配，不覆盖下位机原文件；原入口哈希改变时拒绝继续，避免沿用旧配置。
- `manifest.json`：发布文件校验清单，防误改而非签名认证。
- 仓库仅保存代码、参数快照和说明；日志、相机图片、验证运行产物、模型、凭据和历史压缩包不提交。Git强制本目录文本使用LF换行，避免跨平台改变文件哈希。
- 原门启动器：`/media/data/vlm/Demo_vlm2_kuavo_api/deploy/run_ros_api.sh`；原量化工具：`/media/data/vlm/elevator_button_floor_demo/elevator_quantification.py`；原阶段计时和楼层API配置仍引用既有路径。凭据只引用原文件，不复制、不打印。
- 新方案量化结果默认在本目录`result/`，普通演示日志在`logs/`；原数据和人工标注不修改。
- 旧两帧版本完整备份：`history/two_frame_v1_before_single_frame_v2.tar.gz`。`HISTORY_TWO_FRAME.md`仅为历史文档，当前逻辑以本README为准。
- 如需回到更早的原API按钮方案，退出新节点后使用父目录的原`start_elevator_demo.sh`；原文件没有被覆盖。退出脚本不保证底盘制动，仍依赖现场急停。

## 验证

单帧接口测试验证第一张有效已亮图立即放行、旧版协议拒绝、未知和错误不复用已亮、旧帧/重复帧/换请求拒绝，以及门/楼层原置信度门槛不变。

52张历史图片仅用于单帧回归，未更改定位或颜色阈值。独立ROS master（127.0.0.1:11381）验证消息链路，使用重新赋时间戳的历史图片及模拟楼层API，不连接机器人主控、不真实调用API、不发送运动。详情见`verification/single_frame_*`。

没有执行真实机器人运动或新的动态准确率验收；历史静态准确率不代表所有反光和站位条件下的性能。
