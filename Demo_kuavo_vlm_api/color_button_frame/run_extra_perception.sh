#!/usr/bin/env bash
set -euo pipefail
readonly RELEASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${CONDA_PREFIX:-}" ]]; then
  echo "请退出 Conda，使用原系统 Python / ROS 环境。" >&2
  exit 2
fi
# Floor perception still uses the original API; do not print credentials.
source /media/data/vlm/Demo_vlm2_kuavo_api/deploy/api_env.sh
set -a
source /media/data/vlm/secrets/elevator_vlm_api.env
set +a
: "${VLM_API_KEY:?缺少楼层API凭据}"
set +u
source /opt/ros/noetic/setup.bash
source /media/data/vlm/elevator_vlm_api_ws/devel/setup.bash
set -u
exec /usr/bin/python3 -u "${RELEASE}/extra_color_node.py" "$@"
