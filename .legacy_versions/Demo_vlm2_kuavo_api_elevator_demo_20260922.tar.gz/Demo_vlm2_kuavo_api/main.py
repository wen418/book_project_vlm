#!/usr/bin/env python3
"""Command-line entry point for elevator VLM perception."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from elevator_perception import ElevatorPerception, Settings
from perception_schema import error_payload


ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="电梯门与呼梯按钮 VLM 状态识别")
    parser.add_argument("--image", type=Path, help="同时包含门和按钮的全景图")
    parser.add_argument("--door-image", type=Path, help="电梯门视角图片")
    parser.add_argument("--button-image", type=Path, help="上/下呼梯按钮近景")
    parser.add_argument("--config", type=Path, default=ROOT / "config.api.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "few-shots" / "manifest.json")
    parser.add_argument("--state-file", type=Path, default=ROOT / "runtime" / "state.json")
    parser.add_argument("--sequence", type=int, default=0)
    parser.add_argument("--image-stamp", type=float, default=None)
    parser.add_argument("--no-temporal-filter", action="store_true")
    parser.add_argument("--validate-only", action="store_true", help="只检查配置、标注和平衡采样")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model_name = "unknown"
    try:
        settings = Settings.from_json(args.config)
        model_name = settings.model
        pipeline = ElevatorPerception(
            settings=settings,
            manifest_path=args.manifest,
            state_path=args.state_file,
            enable_temporal_filter=not args.no_temporal_filter,
        )
        if args.validate_only:
            names = pipeline.selected_example_names(
                ("door_closed", "up_pressed", "down_pressed")
            )
            print("manifest_ok=true")
            print("examples_total=%d" % len(pipeline.examples))
            print("selected=%s" % ",".join(names))
            return 0
        result = pipeline.infer(
            sequence=args.sequence,
            image=args.image,
            door_image=args.door_image,
            button_image=args.button_image,
            image_stamp=args.image_stamp if args.image_stamp is not None else time.time(),
        )
        print(result.to_json())
        return 0 if result.status == "ok" else 2
    except Exception as exc:
        print(error_payload(args.sequence, model_name, str(exc)).to_json())
        return 1


if __name__ == "__main__":
    sys.exit(main())
