#!/usr/bin/env bash
# Source this file in every terminal that runs the API client or ROS node.

export VLM_ROOT="/media/data/vlm"
export ELEVATOR_VLM_API_PROJECT_DIR="${VLM_ROOT}/Demo_vlm2_kuavo_api"
export PIP_CACHE_DIR="${VLM_ROOT}/pip-cache"
export TMPDIR="${VLM_ROOT}/tmp"

# Keep secrets out of this file and out of Git. Set these three values in the
# current terminal before testing:
# export VLM_API_URL="https://provider.example/v1/chat/completions"
# export VLM_MODEL="provider-vision-model"
# export VLM_API_KEY="secret"
