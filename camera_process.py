"""Standalone OpenCV webcam worker with a framed stdout protocol."""

from __future__ import annotations

import argparse
import logging
import os
import queue
import struct
import subprocess
import sys
import threading
import time


logger = logging.getLogger("app")
FRAME_HEADER = struct.Struct("<I")


def _read_exact(stream, size):
    parts = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def camera_worker(index, width, height, fps, quality):
    # Imported only in this clean process; PyAV is never loaded here.
    import cv2

    capture = None
    output = sys.stdout.buffer
    try:
        if sys.platform == "darwin":
            capture = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        elif sys.platform == "win32":
            capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        else:
            capture = cv2.VideoCapture(index)
        if not capture or not capture.isOpened():
            capture = cv2.VideoCapture(index)
        if not capture or not capture.isOpened():
            return 2

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_FPS, fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        capture.read()
        output.write(b"READY\n")
        output.flush()
        interval = 1.0 / fps

        while True:
            started = time.perf_counter()
            ok, frame = capture.read()
            if ok and frame is not None:
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(
                        frame, (width, height), interpolation=cv2.INTER_LINEAR
                    )
                encoded, jpeg = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]
                )
                if encoded:
                    payload = jpeg.tobytes()
                    output.write(FRAME_HEADER.pack(len(payload)))
                    output.write(payload)
                    output.flush()
            time.sleep(max(0.001, interval - (time.perf_counter() - started)))
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    finally:
        if capture:
            capture.release()


class CameraCaptureProcess:
    def __init__(self, camera_index=0, width=320, height=240, fps=20, quality=35):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        self.is_running = False
        self.process = None
        self._frames = queue.Queue(maxsize=2)
        self._ready = threading.Event()
        self._reader = None

    def _read_frames(self):
        stream = self.process.stdout
        if stream.readline() != b"READY\n":
            self._ready.set()
            return
        self.is_running = True
        self._ready.set()
        while self.process and self.process.poll() is None:
            header = _read_exact(stream, FRAME_HEADER.size)
            if header is None:
                break
            size = FRAME_HEADER.unpack(header)[0]
            if size <= 0 or size > 5 * 1024 * 1024:
                break
            frame = _read_exact(stream, size)
            if frame is None:
                break
            if self._frames.full():
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                pass
        self.is_running = False

    def start(self):
        if self.is_running:
            return True
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break
        self._ready.clear()
        worker_options = [
            "--camera-worker",
            "--index",
            str(self.camera_index),
            "--width",
            str(self.width),
            "--height",
            str(self.height),
            "--fps",
            str(self.fps),
            "--quality",
            str(self.quality),
        ]
        if getattr(sys, "frozen", False):
            command = [sys.executable, *worker_options]
        else:
            command = [
                sys.executable,
                os.path.abspath(__file__),
                "--worker",
                *worker_options[1:],
            ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        self._reader = threading.Thread(
            target=self._read_frames,
            name="io-reader",
            daemon=True,
        )
        self._reader.start()
        self._ready.wait(8)
        if self.is_running:
            logger.info(
                "Webcam process started at %dx%d, target %d FPS.",
                self.width,
                self.height,
                self.fps,
            )
            return True
        logger.error("Could not start isolated webcam process")
        self.stop()
        return False

    def stop(self):
        was_running = self.is_running or self.process is not None
        self.is_running = False
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1)
        self._reader = None
        if was_running:
            logger.info("Webcam process released.")

    def grab_frame(self):
        if not self.is_running:
            return None
        try:
            return self._frames.get(timeout=0.2)
        except queue.Empty:
            return None


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--quality", type=int, default=35)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.worker:
        raise SystemExit("camera_process.py is an internal worker")
    raise SystemExit(
        camera_worker(
            args.index,
            args.width,
            args.height,
            args.fps,
            args.quality,
        )
    )
