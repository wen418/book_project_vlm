# Kuavo 电梯 VLM 云端 API 版

该工程把本地 llama.cpp 推理替换为远程 OpenAI-compatible 多模态 API。当前真机默认使用 `config.door_fast.json`：只判断电梯门、零 few-shot，并保留结构化输出、置信度门控和跨帧确认。原来的门+按钮能力仍保留，供后续恢复。

## 数据流

```text
Kuavo 彩色相机
  -> ROS最新帧缓冲
  -> 门区域ROI与最长边768缩放
  -> 仅1张当前门图（零few-shot）
  -> 远程多模态VLM API
  -> JSON与概率一致性校验
  -> 跨帧安全过滤
  -> /elevator/perception/state
  -> 规划控制模块
```

规划侧接口没有改变：

- `door_closed`：完整关门为 `true`；当前 `door_fast` 通行模式仅在入口完整敞开时为 `false`，半开或正在开关为 `null`；
- 当前快速模式下 `up_pressed`、`down_pressed` 固定为 `null`；
- 无法确认时为 `null`，规划模块不得把 `null` 当成 `false`；
- 结果发布到 `/elevator/perception/state`，消息类型为 `std_msgs/String`。

## 与本地版的关系

- 本地版目录：`/media/data/vlm/Demo_vlm2_kuavo`，保留作离线回退；
- API版目录：`/media/data/vlm/Demo_vlm2_kuavo_api`；
- API版不需要运行 `llama-server`，也不读取本地GGUF权重；
- 两个ROS感知节点不能同时向同一输出话题发布。

## 配置原则

`config.api.json`保存非秘密参数。以下三个值优先从环境变量读取：

```text
VLM_API_URL    完整的 /v1/chat/completions 地址
VLM_MODEL      供应商提供的视觉模型ID
VLM_API_KEY    API密钥
```

密钥不得写入工程、提交到Git或出现在截图中。完整部署流程见 `deploy/API_DEPLOY.md`。

当前阶段请使用 `config.door_fast.json`，具体更新和测试步骤见 `deploy/DOOR_ONLY_FAST.md`。

`Kuavo_demo/elevator_text_main.py` 已提供单轮联动演示：门外等待完整开门、进入并转身、确认关门、等待再次完整开门、退出原点。它会在规划侧再次检查结果新鲜度和连续性。
