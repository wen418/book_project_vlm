#!/usr/bin/env bash
set -euo pipefail
readonly DEMO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly VLM_PROJECT="/media/data/vlm/Demo_vlm2_kuavo_api"
readonly SECRET_FILE="/media/data/vlm/secrets/elevator_vlm_api.env"
if [[ -n "${CONDA_PREFIX:-}" ]]; then
  echo "请在与原门感知一致的系统 Python / ROS 环境运行，先退出 Conda。" >&2
  exit 2
fi
source "${VLM_PROJECT}/deploy/api_env.sh"
set -a
source "${SECRET_FILE}"
set +a
: "${VLM_API_KEY:?缺少 VLM_API_KEY}"
: "${VLM_API_URL:?缺少 VLM_API_URL}"
: "${VLM_MODEL:?缺少 VLM_MODEL}"
set +u
source /opt/ros/noetic/setup.bash
source /media/data/vlm/elevator_vlm_api_ws/devel/setup.bash
set -u
exec /usr/bin/python3 "${DEMO_DIR}/elevator_extra_perception.py" --run "$@"
