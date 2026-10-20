# 电梯门单任务零样本快速版

本流程适用于已经完成 API 版部署、正进行第 7 步冒烟测试的上位机。目标是只判断电梯门是否完全关闭，不再识别上下按钮，也不向 API 上传 few-shot 参考图片。

## 1. 本版改了什么

- 新增 `config.door_fast.json`，其中 `door_only=true`、`few_shot_count=0`；
- 门任务使用更短的专用提示词和紧凑 JSON；
- 每次请求只上传一张当前门图；
- ROS 默认使用该快速配置；
- ROS 上传图最长边从 1024 调为 768，JPEG 质量从 85 调为 80；
- 关闭网络失败自动重试，避免故障时一张图等待两次请求；
- 输出接口不变，按钮字段输出 `null`。

摄像头仍持续接收图像，但节点只保留最新一帧，推理期间到达的旧帧不会排队。

## 2. 从 Windows 更新工程

以下命令在 Windows PowerShell 执行：

```powershell
cd E:\book_project\yolo_v8
tar -czf Demo_vlm2_kuavo_api_door_fast.tar.gz Demo_vlm2_kuavo_api
scp .\Demo_vlm2_kuavo_api_door_fast.tar.gz leju_kuavo@192.168.5.244:/media/data/vlm/
```

API 密钥文件位于工程外的 `/media/data/vlm/secrets`，本次覆盖工程不会修改它。

## 3. 在上位机解压

以下命令必须在 SSH 登录后的 Ubuntu 终端执行，不能在 Windows PowerShell 执行：

```bash
cd /media/data/vlm
tar -xzf Demo_vlm2_kuavo_api_door_fast.tar.gz -C /media/data/vlm

find /media/data/vlm/Demo_vlm2_kuavo_api -type f \( -name "*.py" -o -name "*.sh" \) -exec sed -i 's/\r$//' {} +
chmod +x /media/data/vlm/Demo_vlm2_kuavo_api/deploy/*.sh
chmod +x /media/data/vlm/Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception/scripts/elevator_vlm_node.py
```

不要删除 `/media/data/vlm/secrets/elevator_vlm_api.env`，也不要把密钥发到聊天或截图中。

## 4. 加载 API 配置

```bash
cd /media/data/vlm/Demo_vlm2_kuavo_api
source deploy/api_env.sh
set -a
source /media/data/vlm/secrets/elevator_vlm_api.env
set +a
```

只检查是否加载成功，不显示密钥：

```bash
/usr/bin/python3 -c "import os; print('URL_SET:',bool(os.getenv('VLM_API_URL'))); print('MODEL:',os.getenv('VLM_MODEL')); print('KEY_SET:',bool(os.getenv('VLM_API_KEY')))"
```

`URL_SET` 和 `KEY_SET` 必须都是 `True`。

## 5. 离线检查

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 main.py --config config.door_fast.json --validate-only
```

单元测试应为 `OK`。配置检查应包含：

```text
manifest_ok=true
selected=
```

`selected` 为空正是零 few-shot，不是错误。

## 6. 门图片冒烟测试

先测试明确关门图片：

```bash
time /usr/bin/python3 main.py \
  --config config.door_fast.json \
  --door-image few-shots/inclose12f.jpg \
  --sequence 1 \
  --no-temporal-filter
```

再测试明确开门图片：

```bash
time /usr/bin/python3 main.py \
  --config config.door_fast.json \
  --door-image few-shots/inopen21f2.jpg \
  --sequence 2 \
  --no-temporal-filter
```

重点检查：

- 关门图 `door_closed=true`；
- 开门图 `door_closed=false`；
- `few_shot_count=0`；
- `up_pressed=null`、`down_pressed=null`；
- `status=ok`；
- 记录 `latency_ms`，连续测 3 次再比较，不使用第一次请求代表稳定速度。

快速配置会拒绝 `--button-image`。单张门图优先使用 `--door-image`，不要误用按钮图。

## 7. 启动 ROS 摄像头版本

如果旧节点正在运行，先在旧节点终端按 `Ctrl+C`。Python 和 YAML 已通过工程路径加载，这次通常不需要重新 `catkin_make`；如果工作空间还没编译过，仍按主部署文档第 9 步编译一次。

确认参数：

```bash
grep -E 'model_config|mode:|jpeg_quality|max_image_edge' \
  ros1/elevator_vlm_api_perception/config/perception.yaml
```

应看到：

```text
model_config: "config.door_fast.json"
mode: "door_monitor"
jpeg_quality: 80
max_image_edge: 768
```

启动：

```bash
/media/data/vlm/Demo_vlm2_kuavo_api/deploy/run_ros_api.sh
```

新开一个 Ubuntu 终端观察结果：

```bash
source /opt/ros/noetic/setup.bash
rostopic echo /elevator/perception/state
```

每条有效结果应为 `mode=door_monitor`、`few_shot_count=0`，两个按钮字段为 `null`。停止请求：

```bash
rostopic pub -1 /elevator/perception/mode std_msgs/String "data: 'idle'"
```

恢复门监测：

```bash
rostopic pub -1 /elevator/perception/mode std_msgs/String "data: 'door_monitor'"
```

快速配置不支持 `button_verify` 或 `both`；误切到这些模式会发布 `status=error`，不会偷偷执行按钮识别。

## 8. 如何评价优化结果

分别保存修改前后至少 10 次的 `latency_ms`，比较中位数和最大值。只看一次结果容易受首次连接、网络抖动和云端排队影响。

如果零 few-shot 后仍接近 4 秒，说明主要耗时在公网往返和云端模型推理，而不是 few-shot。此时继续删提示词意义很小，下一步应比较供应商的更快视觉模型、缩小门 ROI，或训练本地二分类/检测模型。

云端 VLM 的秒级结果只能先用于 shadow test 和上层语义确认，不应单独承担机器人穿越电梯门时的实时安全停机。门沿传感器、深度/激光避障或本地高帧率视觉仍应作为安全链路。
