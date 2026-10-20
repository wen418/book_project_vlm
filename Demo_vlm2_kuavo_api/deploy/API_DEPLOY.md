# Kuavo 电梯 VLM 云端 API 部署手册

本手册从当前本地版“第8步”切换到远程 API。你不需要重装 Ubuntu、ROS、相机驱动或重新配置 `/media/data/vlm`；只需要停止本地模型服务，上传新的 API 工程并配置供应商参数。

## 0. 开始前必须知道

API方案需要供应商提供三个值：

| 名称 | 示例格式 | 含义 |
|---|---|---|
| `VLM_API_URL` | `https://供应商域名/v1/chat/completions` | 必须是完整聊天补全地址 |
| `VLM_MODEL` | `供应商给出的视觉模型ID` | 必须支持图片输入和多图输入 |
| `VLM_API_KEY` | `sk-...` | 密钥，只放真机私密配置文件 |

模型必须同时支持：

1. OpenAI-compatible `messages` 请求；
2. `image_url` 数据URL，即 `data:image/jpeg;base64,...`；
3. 一次请求至少3张图用于门模式，按钮模式至少5张图；
4. 足够长的上下文；
5. 最好支持 `response_format={"type":"json_object"}`。

如果供应商不支持第5项，可在 `config.api.json` 中把 `use_json_object` 改成 `false`，提示词和本地解析器仍会约束JSON，但稳定性会低一些。

> 重要：AAEON此前访问GitHub/Hugging Face发生过超时。Windows代理不会自动传给SSH里的Ubuntu。API域名必须由AAEON自己访问成功，否则API方案同样不能运行。

## 1. 当前进度怎么处理

### 1.1 停止本地模型，但不要删除

在运行 `llama-server` 的终端按 `Ctrl+C`。确认：

```bash
pgrep -af llama-server
```

没有相关进程即可。不要删除以下目录，等API方案现场验证成功后再决定是否清理：

```text
/media/data/vlm/Demo_vlm2_kuavo
/media/data/vlm/llama.cpp
/media/data/vlm/models
/media/data/vlm/llama-model-cache
```

### 1.2 API版使用的新目录

```text
/media/data/vlm/
├── Demo_vlm2_kuavo/              # 原本地版，保留
├── Demo_vlm2_kuavo_api/          # 新API版工程
├── elevator_vlm_api_ws/          # API版catkin工作空间
├── secrets/                      # API密钥，权限600
└── elevator_api_test_logs/       # 测试日志和rosbag
```

创建目录：

```bash
mkdir -p /media/data/vlm/Demo_vlm2_kuavo_api
mkdir -p /media/data/vlm/elevator_vlm_api_ws/src
mkdir -p /media/data/vlm/secrets
mkdir -p /media/data/vlm/elevator_api_test_logs
```

## 2. 从Windows上传新工程

在Windows PowerShell执行：

```powershell
$ProjectParent = "E:\book_project\yolo_v8"
$Archive = "$env:TEMP\Demo_vlm2_kuavo_api.tar.gz"
tar -czf $Archive --exclude="Demo_vlm2_kuavo_api/__pycache__" --exclude="Demo_vlm2_kuavo_api/tests/__pycache__" --exclude="Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception/scripts/__pycache__" --exclude="Demo_vlm2_kuavo_api/runtime" -C $ProjectParent Demo_vlm2_kuavo_api
scp $Archive leju_kuavo@192.168.5.244:/media/data/vlm/Demo_vlm2_kuavo_api/
```

在AAEON执行：

```bash
cd /media/data/vlm
tar -xzf /media/data/vlm/Demo_vlm2_kuavo_api/Demo_vlm2_kuavo_api.tar.gz
rm /media/data/vlm/Demo_vlm2_kuavo_api/Demo_vlm2_kuavo_api.tar.gz
```

转换Windows换行并添加执行权限：

```bash
find /media/data/vlm/Demo_vlm2_kuavo_api -type f \( -name "*.py" -o -name "*.sh" \) -exec sed -i 's/\r$//' {} +
chmod +x /media/data/vlm/Demo_vlm2_kuavo_api/deploy/run_ros_api.sh
chmod +x /media/data/vlm/Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception/scripts/elevator_vlm_node.py
```

## 3. 退出Conda并检查系统Python

如果提示符前有 `(base)`：

```bash
conda deactivate
```

必要时重复，直到括号消失。检查：

```bash
which python3
/usr/bin/python3 --version
source /opt/ros/noetic/setup.bash
/usr/bin/python3 -c "import cv2, yaml; from cv_bridge import CvBridge; print('ROS Python: OK')"
```

`which python3`应为 `/usr/bin/python3`，最后应显示 `ROS Python: OK`。

## 4. 离线检查工程

这一步不访问API，也不消耗费用：

```bash
cd /media/data/vlm/Demo_vlm2_kuavo_api
/usr/bin/python3 main.py --validate-only
/usr/bin/python3 -m unittest discover -s tests -v
```

预期看到32张样例、平衡采样列表以及全部单元测试 `OK`。

## 5. 创建私密API配置

不要把密钥写进 `config.api.json`。创建工程外的私密文件：

```bash
umask 077
nano /media/data/vlm/secrets/elevator_vlm_api.env
```

填入供应商提供的真实值：

```bash
VLM_API_URL='https://供应商域名/v1/chat/completions'
VLM_MODEL='供应商视觉模型ID'
VLM_API_KEY='供应商API密钥'
```

保存后：

```bash
chmod 600 /media/data/vlm/secrets/elevator_vlm_api.env
ls -l /media/data/vlm/secrets/elevator_vlm_api.env
```

权限必须类似 `-rw-------`。不要运行 `cat` 把密钥显示到录屏或聊天中。

加载环境变量：

```bash
source /media/data/vlm/Demo_vlm2_kuavo_api/deploy/api_env.sh
set -a
source /media/data/vlm/secrets/elevator_vlm_api.env
set +a
```

只检查是否已设置，不打印真实密钥：

```bash
/usr/bin/python3 -c "import os; print('URL:', os.getenv('VLM_API_URL')); print('MODEL:', os.getenv('VLM_MODEL')); print('KEY_SET:', bool(os.getenv('VLM_API_KEY')))"
```

`KEY_SET`必须为 `True`。

## 6. 从AAEON检查API网络

先提取API主机名：

```bash
/usr/bin/python3 -c "import os,urllib.parse; print(urllib.parse.urlparse(os.environ['VLM_API_URL']).hostname)"
```

把输出的域名代入下面命令：

```bash
getent hosts 供应商域名
curl -I --connect-timeout 10 https://供应商域名
```

返回任意HTTP状态都说明DNS和HTTPS基本可达；超时说明AAEON无法访问该供应商。此时不要继续发送模型请求。

如果必须经过Windows代理，代理软件需要开启“允许局域网连接”，然后在AAEON设置Windows的局域网IP和代理端口，例如：

```bash
export HTTPS_PROXY='http://Windows局域网IP:代理端口'
export HTTP_PROXY="$HTTPS_PROXY"
```

不能在AAEON中填写 `127.0.0.1:代理端口`，那代表AAEON自己。正式真机测试前必须确认没有Windows笔记本时网络仍可用，或者部署固定的局域网API网关。

## 7. 第一次API冒烟测试

当前真机阶段已切换为“仅电梯门、零 few-shot”快速版。已经完成前 6 步的用户请直接按 `deploy/DOOR_ONLY_FAST.md` 更新工程并继续测试；以下命令也统一改用快速配置。

使用画面明确的关门测试图：

```bash
cd /media/data/vlm/Demo_vlm2_kuavo_api
time /usr/bin/python3 main.py --config config.door_fast.json --door-image few-shots/inclose12f.jpg --sequence 1 --no-temporal-filter
```

预期：

```text
status=ok
door_closed=true
confidence.door_closed >= 0.72
few_shot_count=0
```

再测试明确开门图：

```bash
time /usr/bin/python3 main.py --config config.door_fast.json --door-image few-shots/inopen21f2.jpg --sequence 2 --no-temporal-filter
```

预期 `door_closed=false`。按钮字段固定为 `null` 是正常的，因为快速版不执行按钮识别。

常见错误：

- `远程 API 未配置密钥`：没有加载秘密文件；
- HTTP 401/403：密钥、项目权限或模型权限错误；
- HTTP 404：API URL或模型ID错误；
- HTTP 400且提到 `response_format`：把 `config.api.json` 的 `use_json_object` 改为 `false`；
- HTTP 413：图片或请求体过大，需要缩小图片/减少few-shot；
- HTTP 429：频率或额度超限，不要无限重试；
- 网络超时：AAEON到API的网络不可用；
- `status=uncertain`：查看 `evidence`，不要直接降低安全阈值。

## 8. API费用和数据安全检查

在接入相机前确认：

1. 供应商是否允许上传机器人现场图片；
2. 图片是否包含人脸、工牌、屏幕或其他敏感信息；
3. 是否有数据留存/训练开关；
4. 单次多图请求的计费方式；
5. 账户余额、每分钟请求限制和每日预算告警；
6. 断网或额度耗尽时规划模块必须进入安全分支。

API版默认把ROS图像最长边缩放到1024、JPEG质量设为85。应继续配置门和按钮ROI，避免上传无关区域。

## 9. 编译API版ROS包

不要把API包与旧的同名包混在一个工作空间。执行：

```bash
cd /media/data/vlm/elevator_vlm_api_ws/src
ln -sfn /media/data/vlm/Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception elevator_vlm_api_perception
cd /media/data/vlm/elevator_vlm_api_ws
source /opt/ros/noetic/setup.bash
catkin_make
source /media/data/vlm/elevator_vlm_api_ws/devel/setup.bash
rospack find elevator_vlm_api_perception
```

最后一条应输出API版工作空间中的包路径。

## 10. 检查相机

确认ROS Master和相机已经运行：

```bash
source /opt/ros/noetic/setup.bash
rostopic info /cam_h/color/image_raw/compressed
rostopic hz /cam_h/color/image_raw/compressed
```

查看画面：

```bash
rosrun rqt_image_view rqt_image_view /cam_h/color/image_raw/compressed
```

必须确认机器人固定观察位姿能看到完整门框、入口中央和关门中缝。不要沿用只看到墙面或单侧门框的ROI。

参数文件：

```text
/media/data/vlm/Demo_vlm2_kuavo_api/ros1/elevator_vlm_api_perception/config/perception.yaml
```

## 11. 启动ROS API感知节点

确保旧本地版节点没有运行：

```bash
rosnode list | grep elevator_vlm
```

API版提供安全启动脚本，它会检查Conda、密钥和catkin工作空间：

```bash
/media/data/vlm/Demo_vlm2_kuavo_api/deploy/run_ros_api.sh
```

保持终端运行。默认：

- 模式：`door_monitor`；
- 最多每5秒请求一次；
- 相机：`/cam_h/color/image_raw/compressed`；
- 输出：`/elevator/perception/state`；
- 图像最长边：1024；
- 超过15秒的结果作废。

## 12. 查看输出和控制费用

新开终端：

```bash
source /opt/ros/noetic/setup.bash
rostopic echo /elevator/perception/state
```

测试结束立刻切到idle，停止产生API请求：

```bash
rostopic pub -1 /elevator/perception/mode std_msgs/String "data: 'idle'"
```

需要监测门时：

```bash
rostopic pub -1 /elevator/perception/mode std_msgs/String "data: 'door_monitor'"
```

按键动作后检查按钮：

```bash
rostopic pub -1 /elevator/perception/mode std_msgs/String "data: 'button_verify'"
```

同时识别三个字段成本最高，只在必要时使用：

```bash
rostopic pub -1 /elevator/perception/mode std_msgs/String "data: 'both'"
```

规划模块推荐流程：平时 `idle`，到门口切 `door_monitor`，取得连续确认结果后切 `idle`；按键后切 `button_verify`，确认灯亮后再切 `idle`。

## 13. 规划控制接入

规划程序订阅 `/elevator/perception/state`，不要直接调用供应商API。必须检查：

1. `status == "ok"`；
2. 当前任务需要的字段不是 `null`；
3. `image_stamp`和结果没有过期；
4. 模式与当前规划阶段一致；
5. 门关闭必须经过跨帧确认；
6. `error`、`uncertain`、超时、断网、429和余额不足都进入停止/重拍/人工接管分支。

严禁让VLM节点直接发布机械臂或底盘控制命令。

## 14. 真机shadow test

首次测试只记录，不驱动机器人动作：

```bash
mkdir -p /media/data/vlm/elevator_api_test_logs
rosbag record -O /media/data/vlm/elevator_api_test_logs/api_shadow.bag /cam_h/color/image_raw/compressed /elevator/perception/state /elevator/perception/mode
```

至少覆盖：

- 门完全开、正在关、留小缝、完全关；
- 强反光、暗光、过曝、遮挡和运动模糊；
- 上键亮、下键亮、都亮、都灭；
- 断网、超时、401、429和API服务异常；
- 不同楼层和机器人轻微位姿偏差。

统计三个字段的 `true/false/null` 混淆矩阵，尤其统计“门未关却判为已关”的次数。

## 15. 正确停止

先停止请求：

```bash
rostopic pub -1 /elevator/perception/mode std_msgs/String "data: 'idle'"
```

然后在ROS节点终端按 `Ctrl+C`，最后停止rosbag。确认：

```bash
pgrep -af 'elevator_vlm_api|main.py'
```

## 16. 是否删除本地模型

不要立即删除。满足以下条件后再讨论清理：

- API版已连续完成至少一次完整现场shadow test；
- AAEON到API的网络在真实运行环境稳定；
- API费用和额度可接受；
- 规划模块已正确处理断网/超时；
- 有明确的回退方案。

本手册不授权删除原GGUF、llama.cpp或本地工程。

## 17. 最终检查清单

- [ ] API版工程位于 `/media/data/vlm/Demo_vlm2_kuavo_api`；
- [ ] 原本地版仍保留；
- [ ] 系统Python与ROS导入正常；
- [ ] 离线单元测试全部通过；
- [ ] 密钥文件权限为600；
- [ ] AAEON能直接访问API域名；
- [ ] 关门、开门、按钮离线图片测试正确；
- [ ] API版catkin包编译成功；
- [ ] 相机ROI只包含任务相关区域；
- [ ] ROS输出含时间戳、状态和置信度；
- [ ] `idle`模式能停止API请求；
- [ ] shadow test不驱动真实动作；
- [ ] 断网、超时、429均进入安全分支；
- [ ] 未在工程、日志、截图或Git中泄露API密钥。
