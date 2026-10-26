#!/usr/bin/env bash
set -euo pipefail
readonly RELEASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source /media/data/vlm/Demo_vlm2_kuavo_api/deploy/api_env.sh
set -a
source /media/data/vlm/secrets/elevator_vlm_api.env
set +a
set +u
source /opt/ros/noetic/setup.bash
source /media/data/vlm/elevator_vlm_api_ws/devel/setup.bash
set -u
exec /usr/bin/python3 -u "${RELEASE}/compact_door_node.py"
