# color_button_frame：单帧按钮颜色规则分支

仓库：`wen418/book_project_vlm`；分支：`color-button-frame`。

本分支基于本仓库main创建，只新增`Demo_kuavo_vlm_api/color_button_frame`，不覆盖原一键入口、原按钮/楼层节点或下位机文件。

## 部署与运行

代码目录对应上位机路径：`/media/data/vlm/Demo_kuavo_vlm_api/color_button_frame`。本方案面向已部署原门/楼层API和ROS环境的上位机，不是整套机器人软件的全新安装包。不要复制模型、凭据或覆盖其他原有目录。

先退出Conda和旧启动脚本/附加感知节点，再检查：

```bash
cd /media/data/vlm/Demo_kuavo_vlm_api/color_button_frame
./start_elevator_demo.sh --check
```

保持当前姿态、人工对准按钮后，只测试按钮（不导航、不转头）：

```bash
./start_elevator_demo.sh --program quantify --observe-only button
```

现场确认地图点位、感知效果、主控就绪与急停后，再人工运行完整流程。下面命令**会驱动机器人**：

```bash
./start_elevator_demo.sh --program quantify --entry-plan reduced_backtrack --target-floor 15
```

不带检查参数默认也会运行完整演示。程序不负责物理按键，目标楼层参数不改变地图或导航点。退出脚本不保证底盘制动，仍使用现场急停。

## 单帧逻辑和兼容性

- 按钮使用精确ROI＋颜色规则；一张有效新图像判断已亮即可继续，不等待第二帧、不做连续计数。
- 未亮继续等待；未知、解码失败、定位失败、过曝等不会当成已亮。旧请求、旧帧、重复时间戳被拒绝。固定848×480相机，图像年龄不超过0.5秒，上下位机ROS时钟需要同步。
- 按钮使用schema4、`roi_color_single_frame_v2`和`confirmed_frames=1`；不伪造模型置信度。下位机通过SSH在内存执行对应门禁适配，不覆盖原文件。
- 门、楼层、运动阶段、点位、原安全检查保持不变；门和楼层仍使用原API，因此整体仍需网络。
- `upstream/`是本次已验证的原程序快照。若实际下位机原入口哈希发生变化会拒绝运行，需先核对并更新快照及manifest，不能绕过检查沿用旧配置。
- 原门入口仍是`/media/data/vlm/Demo_vlm2_kuavo_api/deploy/run_ros_api.sh`；原量化工具仍是`/media/data/vlm/elevator_button_floor_demo/elevator_quantification.py`；楼层配置与阶段计时继续引用原部署路径。
- 新量化结果默认在本目录`result/`，普通日志在`logs/`；凭据、图片、日志、模型、运行验证产物和历史压缩包不提交。
- 原方案仍在父目录`start_elevator_demo.sh`；新旧方案不能同时启动。退出新方案后可回到原入口，不需恢复原文件。

## 验证与限制

运行代码与已部署、已验证的单帧版本一致；19项测试、独立本地ROS消息测试及下位机SSH模拟通过。52张历史单帧回归为48正确、0明确错误、4未知。接口测试使用重新赋时间戳的历史图和模拟楼层API，不等于动态准确率或实机反应时间验收。

未执行真实机器人运动。此前实际只读预检通过依赖检查，但在`/hardware/is_ready=None`处停止；需现场正常启动主控后重新检查，不自动初始化整机。

`HISTORY_TWO_FRAME.md`仅为历史说明，当前以本文件及schema4的单帧实现为准。本分支不包含模型权重、API凭据或历史图片。
