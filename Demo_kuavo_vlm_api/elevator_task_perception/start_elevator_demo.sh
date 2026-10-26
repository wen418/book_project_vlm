#!/usr/bin/env bash
set -euo pipefail
export ELEVATOR_LAUNCH_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${CONDA_PREFIX:-}" ]]; then
  echo "请先退出 Conda，再启动机器人测试。" >&2
  exit 2
fi
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://kuavo_master:11311}"
export ROS_IP="${ROS_IP:-192.168.26.12}"
unset ROS_HOSTNAME
set +u
source /opt/ros/noetic/setup.bash
source /media/data/vlm/elevator_vlm_api_ws/devel/setup.bash
set -u
# Share the original launcher's lock; old/new one-click schemes cannot run together.
exec flock -n -E 73 /media/data/vlm/Demo_kuavo_vlm_api/.one_click.lock \
  /usr/bin/python3 -u "${ELEVATOR_LAUNCH_DIR}/launcher.py" "$@"
