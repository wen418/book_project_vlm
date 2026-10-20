# Git 使用与版本恢复

## 仓库位置

统一仓库：`/media/data/vlm/.git`。在三个程序子目录内执行 `git status`，查看的也是这个仓库。`main` 保存当前源码；`version1.0` 固定记录本次稳定基线。

上位机使用仓库专用部署密钥推送，只授权 `wen418/book_project_vlm`。私钥在上位机用户的 `.ssh` 内，不放入仓库。仓库设置中的 Deploy keys 可撤销该授权。HTTPS 远程地址用于读取，SSH 推送地址使用 GitHub 的 443 端口。Git 身份、SSH 命令均只写在本仓库的本机配置中。

```bash
cd /media/data/vlm
git remote -v
git status --short
git log --oneline --decorate -8
git show version1.0:VERSION
```

## 修改前后

修改前先看 `git status`。更新程序时若提示存在未提交修改，先确认修改内容并提交，不能直接覆盖或删除这些改动。新部署只负责安装，安装后仍需自行提交和推送。

```bash
cd /media/data/vlm
git diff
git add -u
git add 路径/新增源码.py
git diff --cached --stat
git commit -m "描述修改目的"
git push
```

不要上传 `secrets/`、`.env`、私钥，也不要使用 `git add -f` 强行加入被忽略的运行数据。`git add -u` 只保存已跟踪文件的修改；新文件需单独指定路径。

## 查看或恢复旧程序

只看旧文件，不影响运行文件：

```bash
cd /media/data/vlm
git show version1.0:elevator_button_floor_demo/elevator_quantification.py | less
```

恢复某个文件前，停止相关测试，并先提交当前工作。以下命令会替换指定文件：

```bash
cd /media/data/vlm
git restore --source version1.0 -- elevator_button_floor_demo/elevator_quantification.py
git diff
git add -u
git commit -m "恢复 version1.0 的量化程序"
git push
```

完整发布由多个文件共同组成；只恢复一个文件可能造成接口不兼容。整组回退应一起恢复相关程序、配置和模板，完成离线检查后再运行。不要在机器人运行时切换版本，也不要对整个 `/media/data/vlm` 使用 `git clean -fdx`，因为该目录还保留未入库数据与配置。

## 原有备份去了哪里

迁移的第一个提交保留了迁移前的正式程序和 `.legacy_versions/` 历史树。回退副本、旧 API 工程目录、两个 API 分发压缩包中的有效源码与图像已保存在该历史树，压缩包中的 Python 缓存不保留。最终工作目录不再保留这些副本。

```bash
cd /media/data/vlm
git log --reverse --oneline
# 第一条提交就是迁移前归档；将下面的“归档提交号”替换为其提交号
git ls-tree -r --name-only 归档提交号 .legacy_versions/ | less
git show '归档提交号:.legacy_versions/对应文件路径' | less
```

具体归档路径、哈希和待清理目标记录在 `docs/迁移清单.json`。该清单是本次迁移的固定审计记录，不需要每次提交都更新。删除操作只在 GitHub 上确认 `main` 和 `version1.0` 都存在且一致后执行。

无关的本地模型工程、模型、缓存和秘密配置备份不属于本次项目清理范围，保持原位，不上传。实验结果、日志等是未入库数据，按其自身生命周期管理。

## 克隆到新机器

```bash
git clone https://github.com/wen418/book_project_vlm.git
```

现有脚本默认 `/media/data/vlm`。新机器应将受管目录放到对应位置，恢复自己的秘密配置与 ROS/机器人环境，并重新编译工作空间。Git 克隆不会恢复上位机 `.git/config` 中的专用推送认证；新机器需要单独配置自己的访问权限，不能把现有私钥复制进仓库。
