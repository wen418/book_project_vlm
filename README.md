# 电梯感知、任务流程与量化测试

当前发布：**version1.0**。仓库根目录是上位机 `/media/data/vlm`，远程仓库为 https://github.com/wen418/book_project_vlm 。原有启动路径保留。

## 目录用途

| 目录 | 用途 |
| --- | --- |
| `Demo_kuavo_vlm_api/` | 一键入口与调度器 |
| `Demo_vlm2_kuavo_api/` | 原门感知程序、VLM 客户端、少样本图像及 ROS 包 |
| `elevator_button_floor_demo/` | 按键灯、楼层识别、流程控制、量化记录、网页核验、部署程序与测试 |
| `elevator_vlm_api_ws/src/` | ROS 工作空间入口与包链接；编译产物不入库 |
| `docs/` | Git 使用及迁移说明 |

这几个目录是同一个项目的不同组成部分，统一由根目录的 Git 仓库管理。详细功能说明见 `elevator_button_floor_demo/README.md`；最新现场记忆见 `Demo_vlm2_kuavo_api/最近工作交接_2026-10-19.md`。

## version1.0 范围

- 门全开、下行呼梯按键灯和楼层显示的 VLM 感知，最多每秒发起一次识别请求。
- 用户已选定减少回退方案 `reduced_backtrack`，等待点为 x=-0.901 m、y=0.004 m、朝向 6°。保留其他方案以供代码兼容。
- 记录识别、推理、发令、运动开始和进梯等耗时；提供开门和按键点亮的选帧时间核验、看图准确性标注及多轮汇总。
- `result/` 只保留到达所选流程最后阶段的轮次，中途终止的轮次不保存。统计总尝试成功率时，需要另外记录失败次数。
- 楼层已有单次推理耗时，尚未实现独立的“屏幕发生变化到确认目标楼层”的人工边界延迟核验。
- 已提交到 Git 的文件更新时使用 Git 历史恢复，停止重复生成 `.backup_*`；未提交的修改会阻止覆盖。尚未纳入 Git 的下位机文件保留原备份机制。

## 运行

机器人启动及现场准备按原使用说明执行。以下命令会进入实际流程，应由操作者在现场运行。

```bash
cd /media/data/vlm/Demo_kuavo_vlm_api
# 完整量化流程；目标楼层可以修改
./start_elevator_demo.sh --program quantify --entry-plan reduced_backtrack --target-floor 15
# 只测进梯
./start_elevator_demo.sh --program quantify --entry-plan reduced_backtrack --entry-only
# 原演示流程
./start_elevator_demo.sh --program demo --target-floor 15
```

结果仍保存在 `/media/data/vlm/elevator_button_floor_demo/result/`，不会随源码推送到 GitHub。网页核验和汇总方法见该目录上一级的 README。

## 保存修改

```bash
cd /media/data/vlm
git status --short
git diff
git add -u
# 如有新增源码，再用 git add 指定文件路径
git commit -m "说明这次改了什么"
git push
```

`version1.0` 是固定发布标签，后续修改继续提交到 `main`。确认新的稳定版本后新建标签，例如 `git tag -a version1.1 -m "version1.1"`，再执行 `git push origin version1.1`。不要移动旧发布标签。

## 不上传的内容

API 密钥与 SSH 私钥、实验结果及视频、日志、缓存、模型权重、ROS 编译产物、无关旧工程均不入库。运行需要的少样本示例图像属于源码资源，已经纳入版本管理。密钥仍保存在机器原有秘密配置中，不会因克隆仓库自动获得。

源码备份不能代替实验数据备份，`result/` 中有价值的试验应另行下载保存。

## 离线验证

```bash
cd /media/data/vlm/elevator_button_floor_demo
python3 -m unittest discover -s tests -p 'test_elevator*.py'
cd /media/data/vlm/Demo_vlm2_kuavo_api
python3 -m unittest discover -s tests
```

这些测试不连接机器人、不发送动作。新机器还需安装 ROS Noetic、相机与机器人 SDK，并恢复站点参数和秘密配置；在 `/media/data/vlm/elevator_vlm_api_ws` 运行 `catkin_make` 后才能恢复工作空间编译环境。参见 `docs/GIT使用说明.md`。
