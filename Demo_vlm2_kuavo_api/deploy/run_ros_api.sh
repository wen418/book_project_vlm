#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR="/media/data/vlm/Demo_vlm2_kuavo_api"
readonly WORKSPACE="/media/data/vlm/elevator_vlm_api_ws"
readonly SECRET_FILE="/media/data/vlm/secrets/elevator_vlm_api.env"

# shellcheck source=api_env.sh
source "${PROJECT_DIR}/deploy/api_env.sh"

if [[ -n "${CONDA_PREFIX:-}" ]]; then
  echo "检测到 Conda 环境：${CONDA_PREFIX}" >&2
  echo "请先执行 conda deactivate，直到提示符前不再出现 (base)/(env)。" >&2
  exit 2
fi

if [[ ! -f "${SECRET_FILE}" ]]; then
  echo "缺少 API 配置文件：${SECRET_FILE}" >&2
  echo "请按照 deploy/API_DEPLOY.md 创建，并执行 chmod 600。" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "${SECRET_FILE}"
set +a

: "${VLM_API_URL:?秘密配置中缺少 VLM_API_URL}"
: "${VLM_MODEL:?秘密配置中缺少 VLM_MODEL}"
: "${VLM_API_KEY:?秘密配置中缺少 VLM_API_KEY}"

# ROS setup scripts may read optional variables before initializing them.
set +u
source /opt/ros/noetic/setup.bash
source "${WORKSPACE}/devel/setup.bash"
set -u

exec roslaunch elevator_vlm_api_perception elevator_vlm.launch \
  project_dir:="${PROJECT_DIR}"
