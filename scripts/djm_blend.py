#!/usr/bin/env python3
"""Software-side dual-input routing and auto-crossfade for the DJM-900nexus."""

from __future__ import annotations

import argparse
import collections
import math
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Deque

import numpy as np
import sounddevice as sd


TARGET_DEVICE = "DJM-900nexus"


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    input_channels: int
    output_channels: int


def iter_devices() -> list[DeviceInfo]:
    devices = []
    for index, raw in enumerate(sd.query_devices()):
        devices.append(
            DeviceInfo(
                index=index,
                name=str(raw["name"]),
                input_channels=int(raw["max_input_channels"]),
                output_channels=int(raw["max_output_channels"]),
            )
        )
    return devices


def find_device(name: str, *, wants_input: bool = False, wants_output: bool = False) -> DeviceInfo:
    lowered = name.lower()
    for device in iter_devices():
        if lowered in device.name.lower():
            if wants_input and device.input_channels <= 0:
                continue
            if wants_output and device.output_channels <= 0:
                continue
            return device
    raise SystemExit(f"Device not found: {name!r}")


def list_devices() -> None:
    for device in iter_devices():
        print(
            f"{device.index:>2}  {device.name}  "
            f"in={device.input_channels} out={device.output_channels}"
        )


def parse_pair(pair: str) -> tuple[int, int]:
    cleaned = pair.replace("/", "").replace("-", "")
    if cleaned not in {"12", "34", "56", "78"}:
        raise SystemExit("pair must be one of: 1/2, 3/4, 5/6, 7/8")
    return int(cleaned[0]) - 1, int(cleaned[1]) - 1


def equal_power_gains(crossfade: float) -> tuple[float, float]:
    value = max(-1.0, min(1.0, crossfade))
    theta = (value + 1.0) * (math.pi / 4.0)
    return math.cos(theta), math.sin(theta)


class StereoBuffer:
    def __init__(self, channels: int, *, max_chunks: int = 256) -> None:
        self.channels = channels
        self.max_chunks = max_chunks
        self._chunks: Deque[np.ndarray] = collections.deque()
        self._offset = 0
        self._lock = threading.Lock()

    def push(self, chunk: np.ndarray) -> None:
        stereo = np.zeros((chunk.shape[0], 2), dtype=np.float32)
        width = min(self.channels, 2, chunk.shape[1])
        stereo[:, :width] = chunk[:, :width]
        with self._lock:
            self._chunks.append(stereo)
            while len(self._chunks) > self.max_chunks:
                self._chunks.popleft()
                self._offset = 0

    def read(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), dtype=np.float32)
        written = 0
        with self._lock:
            while written < frames and self._chunks:
                chunk = self._chunks[0]
                available = chunk.shape[0] - self._offset
                take = min(frames - written, available)
                out[written : written + take] = chunk[self._offset : self._offset + take]
                written += take
                self._offset += take
                if self._offset >= chunk.shape[0]:
                    self._chunks.popleft()
                    self._offset = 0
        return out


def open_stereo_input(
    device: DeviceInfo,
    buffer: StereoBuffer,
    *,
    samplerate: int,
    blocksize: int,
) -> sd.InputStream:
    channels = min(max(device.input_channels, 1), 2)

    def callback(indata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"{device.name}: {status}", file=sys.stderr)
        buffer.push(np.asarray(indata, dtype=np.float32))

    return sd.InputStream(
        device=device.index,
        samplerate=samplerate,
        blocksize=blocksize,
        dtype="float32",
        channels=channels,
        callback=callback,
    )


def route_two_inputs(
    *,
    input_a_name: str,
    input_b_name: str,
    pair_a: str,
    pair_b: str,
    samplerate: int,
    blocksize: int,
    gain_a: float,
    gain_b: float,
    crossfade: float,
    target_crossfade: float | None,
    fade_seconds: float,
) -> None:
    input_a = find_device(input_a_name, wants_input=True)
    input_b = find_device(input_b_name, wants_input=True)
    output = find_device(TARGET_DEVICE, wants_output=True)
    out_a1, out_a2 = parse_pair(pair_a)
    out_b1, out_b2 = parse_pair(pair_b)

    if output.output_channels < max(out_a1, out_a2, out_b1, out_b2) + 1:
        raise SystemExit("DJM output device does not expose the requested channel pairs")

    buffer_a = StereoBuffer(input_a.input_channels)
    buffer_b = StereoBuffer(input_b.input_channels)
    start_crossfade = max(-1.0, min(1.0, crossfade))
    end_crossfade = (
        max(-1.0, min(1.0, target_crossfade))
        if target_crossfade is not None
        else start_crossfade
    )
    fade_start = time.monotonic()

    print(
        f"Routing {input_a.name} -> {pair_a} and {input_b.name} -> {pair_b} on {output.name}. "
        "Press Ctrl+C to stop."
    )
    print(
        f"crossfade start={start_crossfade:.2f} end={end_crossfade:.2f} "
        f"fade_seconds={fade_seconds:.1f} gain_a={gain_a:.2f} gain_b={gain_b:.2f}"
    )

    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def output_callback(outdata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"{output.name}: {status}", file=sys.stderr)
        now = time.monotonic()
        if fade_seconds > 0 and target_crossfade is not None:
            progress = min(1.0, (now - fade_start) / fade_seconds)
            current_crossfade = start_crossfade + (end_crossfade - start_crossfade) * progress
        else:
            current_crossfade = start_crossfade

        auto_a, auto_b = equal_power_gains(current_crossfade)
        mixed_a = buffer_a.read(frames) * np.float32(gain_a * auto_a)
        mixed_b = buffer_b.read(frames) * np.float32(gain_b * auto_b)

        outdata.fill(0)
        outdata[:, out_a1] = mixed_a[:, 0]
        outdata[:, out_a2] = mixed_a[:, 1]
        outdata[:, out_b1] = mixed_b[:, 0]
        outdata[:, out_b2] = mixed_b[:, 1]

    with open_stereo_input(
        input_a, buffer_a, samplerate=samplerate, blocksize=blocksize
    ), open_stereo_input(
        input_b, buffer_b, samplerate=samplerate, blocksize=blocksize
    ), sd.OutputStream(
        device=output.index,
        samplerate=samplerate,
        blocksize=blocksize,
        dtype="float32",
        channels=output.output_channels,
        callback=output_callback,
    ):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-devices", help="List CoreAudio devices visible to PortAudio")

    route = subparsers.add_parser(
        "route-two",
        help="Route two stereo input devices to separate DJM channel pairs with optional auto-crossfade",
    )
    route.add_argument("--input-a", required=True, help="First input device substring")
    route.add_argument("--input-b", required=True, help="Second input device substring")
    route.add_argument("--pair-a", default="1/2", help="DJM output pair for input A")
    route.add_argument("--pair-b", default="3/4", help="DJM output pair for input B")
    route.add_argument("--samplerate", type=int, default=48000)
    route.add_argument("--blocksize", type=int, default=1024)
    route.add_argument("--gain-a", type=float, default=1.0, help="Static gain applied to input A")
    route.add_argument("--gain-b", type=float, default=1.0, help="Static gain applied to input B")
    route.add_argument(
        "--crossfade",
        type=float,
        default=-1.0,
        help="-1.0 = full A, 0 = equal-power center, 1.0 = full B",
    )
    route.add_argument(
        "--target-crossfade",
        type=float,
        help="Optional crossfade destination for a one-shot auto-fade",
    )
    route.add_argument(
        "--fade-seconds",
        type=float,
        default=0.0,
        help="Duration for the one-shot auto-fade",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list-devices":
        list_devices()
        return 0

    if args.command == "route-two":
        route_two_inputs(
            input_a_name=args.input_a,
            input_b_name=args.input_b,
            pair_a=args.pair_a,
            pair_b=args.pair_b,
            samplerate=args.samplerate,
            blocksize=args.blocksize,
            gain_a=args.gain_a,
            gain_b=args.gain_b,
            crossfade=args.crossfade,
            target_crossfade=args.target_crossfade,
            fade_seconds=args.fade_seconds,
        )
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
