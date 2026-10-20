#!/usr/bin/env python3
"""catkin entry point for the project-level continuous ROS node."""

import os
import sys
from pathlib import Path


deployed_default = Path("/media/data/vlm/Demo_vlm2_kuavo_api")
source_default = (
    deployed_default
    if (deployed_default / "ros_stream_node.py").is_file()
    else Path(__file__).resolve().parents[3]
)
project_dir = Path(
    os.environ.get("ELEVATOR_VLM_API_PROJECT_DIR", str(source_default))
).resolve()
if str(project_dir) not in sys.path:
    sys.path.insert(0, str(project_dir))

from ros_stream_node import run


if __name__ == "__main__":
    run(project_dir)
