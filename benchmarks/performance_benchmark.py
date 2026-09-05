"""Reproducible local performance checks for Project X media/input changes."""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capture_backends import MonitorGeometry
from server import RemoteServer
from shared_capture import _LatestFrameStore
from webrtc_media import (
    SharedScreenTrack,
    VIDEO_TOOLBOX_AVAILABLE,
    VideoToolboxH264Encoder,
)


def video_frame(width, height, pts):
    # A moving high-contrast region resembles desktop content better than a
    # constant black frame while remaining deterministic.
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[:, :, 3] = 255
    x = (pts * 13) % max(1, width - 160)
    pixels[80:240, x:x + 160, :3] = (235, 235, 235)
    frame = av.VideoFrame.from_numpy_buffer(pixels, format="bgra")
    frame.pts = pts * 3000
    frame.time_base = fractions.Fraction(1, 90_000)
    return frame


def encoder_benchmark(frames=30, width=1280, height=720):
    if not VIDEO_TOOLBOX_AVAILABLE:
        return {"available": False}

    VideoToolboxH264Encoder.clear_shared_pool()
    started = time.perf_counter()
    for _viewer in range(2):
        VideoToolboxH264Encoder.clear_shared_pool()
        encoder = VideoToolboxH264Encoder()
        for index in range(frames):
            encoder.encode(
                video_frame(width, height, index),
                force_keyframe=index == 0,
            )
    baseline_ms = (time.perf_counter() - started) * 1000

    VideoToolboxH264Encoder.clear_shared_pool()
    first = VideoToolboxH264Encoder()
    second = VideoToolboxH264Encoder()
    started = time.perf_counter()
    for index in range(frames):
        first.encode(
            video_frame(width, height, index),
            force_keyframe=index == 0,
        )
        second.encode(
            video_frame(width, height, index),
            force_keyframe=index == 0,
        )
    optimized_ms = (time.perf_counter() - started) * 1000
    stats = VideoToolboxH264Encoder.shared_pool_stats()
    return {
        "available": True,
        "frames_per_viewer": frames,
        "baseline_two_encoders_ms": round(baseline_ms, 2),
        "shared_encoder_ms": round(optimized_ms, 2),
        "elapsed_reduction_percent": round(
            (baseline_ms - optimized_ms) / baseline_ms * 100,
            1,
        ),
        "hardware_encodes": stats["encodes"],
        "reused_frames": stats["cache_hits"],
        "encode_operation_reduction_percent": round(
            stats["cache_hits"] / max(1, stats["encodes"] + stats["cache_hits"]) * 100,
            1,
        ),
    }


async def pacing_benchmark(fps, slots):
    track = SharedScreenTrack(
        capture_hub=None,
        client_session=SimpleNamespace(target_fps=fps),
    )
    started = time.perf_counter()
    for _ in range(slots):
        await track._wait_for_frame_slot()
    elapsed = time.perf_counter() - started
    return {
        "requested_fps": fps,
        "slots": slots,
        "elapsed_ms": round(elapsed * 1000, 2),
        "measured_fps": round((slots - 1) / max(elapsed, 0.001), 2),
    }


def capture_copy_benchmark(frames=60, width=1920, height=1080):
    monitor = MonitorGeometry(1, 0, 0, width, height, True)
    store = _LatestFrameStore(monitor)
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    for index in range(frames):
        frame[0, 0, 0] = index % 255
        store.publish(frame)
    return store.metrics()


async def input_dispatch_benchmark(events=1000):
    class FakeServer:
        async def handle_input_message(self, _data, _ws, _session):
            return None

    class FakeChannel:
        label = "projectx-pointer"
        readyState = "open"

        def send(self, _message):
            return None

    session = SimpleNamespace(
        authenticated=True,
        control_ws=None,
        input_count=0,
        input_processing_total_ms=0.0,
        input_processing_max_ms=0.0,
    )
    server = FakeServer()
    channel = FakeChannel()
    started = time.perf_counter()
    for index in range(events):
        await RemoteServer._handle_realtime_channel_input(
            server,
            {"type": "mouse_move", "x": index / events, "y": 0.5},
            session,
            channel,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "events": events,
        "total_ms": round(elapsed_ms, 2),
        "per_event_ms": round(elapsed_ms / events, 4),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--network-rtt-ms", type=float, default=None)
    args = parser.parse_args()
    result = {
        "encoder": encoder_benchmark(),
        "pacing_30_fps": await pacing_benchmark(30, 31),
        "pacing_15_fps": await pacing_benchmark(15, 16),
        "capture_copy": capture_copy_benchmark(),
        "input_dispatch": await input_dispatch_benchmark(),
        "configuration": {
            "old_initial_bitrate_mbps": 3.5,
            "new_initial_bitrate_mbps": 2.5,
            "minimum_adaptive_bitrate_mbps": 0.8,
            "screen_capture_queue_old": 3,
            "screen_capture_queue_new": 2,
        },
    }
    if args.network_rtt_ms is not None:
        result["network"] = {
            "measured_rtt_ms": args.network_rtt_ms,
            "unavoidable_one_way_floor_ms": round(args.network_rtt_ms / 2, 1),
        }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
