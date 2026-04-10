#!/usr/bin/env python3
"""Route virtual/CoreAudio devices into specific DJM-900nexus channel pairs."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

try:
    from scipy.signal import lfilter
except Exception:  # noqa: BLE001
    lfilter = None


TARGET_DEVICE = "DJM-900nexus"
LOW_SPLIT_HZ = 180.0
HIGH_SPLIT_HZ = 2400.0
LOG_ROTATE_BYTES = 2 * 1024 * 1024


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
    first = int(cleaned[0]) - 1
    second = int(cleaned[1]) - 1
    return first, second


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


class StereoFx:
    def __init__(
        self,
        *,
        samplerate: int,
        gain: float,
        delay_seconds: float,
        low_gain: float,
        mid_gain: float,
        high_gain: float,
    ) -> None:
        self.current_gain = np.float32(gain)
        self.target_gain = float(gain)
        self.current_low_gain = np.float32(low_gain)
        self.target_low_gain = float(low_gain)
        self.current_mid_gain = np.float32(mid_gain)
        self.target_mid_gain = float(mid_gain)
        self.current_high_gain = np.float32(high_gain)
        self.target_high_gain = float(high_gain)
        self.low_alpha = np.float32(1.0 - math.exp((-2.0 * math.pi * LOW_SPLIT_HZ) / samplerate))
        self.high_alpha = np.float32(1.0 - math.exp((-2.0 * math.pi * HIGH_SPLIT_HZ) / samplerate))
        self.low_pole = np.float32(1.0 - self.low_alpha)
        self.high_pole = np.float32(1.0 - self.high_alpha)
        self.low_state = np.zeros(2, dtype=np.float32)
        self.high_lp_state = np.zeros(2, dtype=np.float32)
        self.low_zi = np.zeros((1, 2), dtype=np.float32)
        self.high_lp_zi = np.zeros((1, 2), dtype=np.float32)

        delay_frames = max(0, int(round(delay_seconds * samplerate)))
        self.delay_frames = delay_frames
        if delay_frames > 0:
            self.delay_buffer = np.zeros((delay_frames, 2), dtype=np.float32)
        else:
            self.delay_buffer = None
        self.delay_index = 0

    def _is_flat_eq(self) -> bool:
        return (
            abs(float(self.current_low_gain) - 1.0) < 1e-4
            and abs(float(self.target_low_gain) - 1.0) < 1e-4
            and abs(float(self.current_mid_gain) - 1.0) < 1e-4
            and abs(float(self.target_mid_gain) - 1.0) < 1e-4
            and abs(float(self.current_high_gain) - 1.0) < 1e-4
            and abs(float(self.target_high_gain) - 1.0) < 1e-4
        )

    def _apply_delay(self, stereo: np.ndarray) -> np.ndarray:
        if self.delay_buffer is None:
            return stereo
        frames = stereo.shape[0]
        out = np.empty_like(stereo, dtype=np.float32)
        idx = self.delay_index
        remaining = frames
        offset = 0
        while remaining > 0:
            chunk = min(remaining, self.delay_frames - idx)
            out[offset : offset + chunk] = self.delay_buffer[idx : idx + chunk]
            self.delay_buffer[idx : idx + chunk] = stereo[offset : offset + chunk]
            idx = (idx + chunk) % self.delay_frames
            offset += chunk
            remaining -= chunk
        self.delay_index = idx
        return out

    def update_params(
        self,
        *,
        gain: float | None = None,
        low_gain: float | None = None,
        mid_gain: float | None = None,
        high_gain: float | None = None,
    ) -> None:
        if gain is not None:
            self.target_gain = float(gain)
        if low_gain is not None:
            self.target_low_gain = float(low_gain)
        if mid_gain is not None:
            self.target_mid_gain = float(mid_gain)
        if high_gain is not None:
            self.target_high_gain = float(high_gain)

    def _ramp(self, current: np.float32, target: float, frames: int) -> np.ndarray:
        target32 = np.float32(target)
        if frames <= 0:
            return np.zeros((0, 1), dtype=np.float32)
        if abs(float(current) - float(target32)) < 1e-5:
            return np.full((frames, 1), current, dtype=np.float32)
        ramp = np.linspace(float(current), float(target32), num=frames, dtype=np.float32).reshape(-1, 1)
        return ramp

    def process(self, stereo: np.ndarray) -> np.ndarray:
        frames = stereo.shape[0]
        gain_ramp = self._ramp(self.current_gain, self.target_gain, frames)
        low_gain_ramp = self._ramp(self.current_low_gain, self.target_low_gain, frames)
        mid_gain_ramp = self._ramp(self.current_mid_gain, self.target_mid_gain, frames)
        high_gain_ramp = self._ramp(self.current_high_gain, self.target_high_gain, frames)
        self.current_gain = np.float32(self.target_gain)
        self.current_low_gain = np.float32(self.target_low_gain)
        self.current_mid_gain = np.float32(self.target_mid_gain)
        self.current_high_gain = np.float32(self.target_high_gain)

        if self._is_flat_eq():
            gained = np.clip(stereo * gain_ramp, -1.0, 1.0).astype(np.float32, copy=False)
            return self._apply_delay(gained)

        if lfilter is not None:
            x = np.asarray(stereo, dtype=np.float32) * gain_ramp
            low, self.low_zi = lfilter(
                [float(self.low_alpha)],
                [1.0, -float(self.low_pole)],
                x,
                axis=0,
                zi=self.low_zi,
            )
            high_lp, self.high_lp_zi = lfilter(
                [float(self.high_alpha)],
                [1.0, -float(self.high_pole)],
                x,
                axis=0,
                zi=self.high_lp_zi,
            )
            low = np.asarray(low, dtype=np.float32)
            high_lp = np.asarray(high_lp, dtype=np.float32)
            self.low_state = low[-1].copy()
            self.high_lp_state = high_lp[-1].copy()
            high = x - high_lp
            mid = x - low - high
            shaped = low * low_gain_ramp + mid * mid_gain_ramp + high * high_gain_ramp
            return self._apply_delay(np.clip(shaped, -1.0, 1.0).astype(np.float32, copy=False))

        out = np.zeros_like(stereo, dtype=np.float32)
        for i in range(stereo.shape[0]):
            x = stereo[i] * gain_ramp[i, 0]
            self.low_state += self.low_alpha * (x - self.low_state)
            low = self.low_state.copy()
            self.high_lp_state += self.high_alpha * (x - self.high_lp_state)
            high = x - self.high_lp_state
            mid = x - low - high
            y = (
                low * low_gain_ramp[i, 0]
                + mid * mid_gain_ramp[i, 0]
                + high * high_gain_ramp[i, 0]
            )
            y = np.clip(y, -1.0, 1.0)
            if self.delay_buffer is not None:
                delayed = self.delay_buffer[self.delay_index].copy()
                self.delay_buffer[self.delay_index] = y
                self.delay_index = (self.delay_index + 1) % self.delay_frames
                out[i] = delayed
            else:
                out[i] = y
        return out


def route_input_to_djm(
    input_name: str,
    pair: str,
    samplerate: int,
    blocksize: int,
    latency: str,
    gain: float,
    delay_seconds: float,
    low_gain: float,
    mid_gain: float,
    high_gain: float,
    control_file: str | None,
    stats_file: str | None,
) -> None:
    input_device = find_device(input_name, wants_input=True)
    output_device = find_device(TARGET_DEVICE, wants_output=True)
    ch_a, ch_b = parse_pair(pair)

    if output_device.output_channels < max(ch_a, ch_b) + 1:
        raise SystemExit(f"{output_device.name} doesn't expose channels for pair {pair}")

    print(
        f"Routing {input_device.name} -> {output_device.name} channels {pair}. "
        f"gain={gain:.2f} delay={delay_seconds:.2f}s blocksize={blocksize or 0} latency={latency} "
        f"low={low_gain:.2f} mid={mid_gain:.2f} high={high_gain:.2f}. "
        "Press Ctrl+C to stop."
    )

    # Allow this relay to survive shell exit when started under nohup.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    fx = StereoFx(
        samplerate=samplerate,
        gain=gain,
        delay_seconds=delay_seconds,
        low_gain=low_gain,
        mid_gain=mid_gain,
        high_gain=high_gain,
    )
    control_path = Path(control_file) if control_file else None
    stats_path = Path(stats_file) if stats_file else None
    stop_event = threading.Event()
    control_state = {
        "gain": float(gain),
        "low_gain": float(low_gain),
        "mid_gain": float(mid_gain),
        "high_gain": float(high_gain),
    }
    stats = {
        "started_at": time.time(),
        "input_callbacks": 0,
        "output_callbacks": 0,
        "input_overflow_events": 0,
        "input_underflow_events": 0,
        "output_overflow_events": 0,
        "output_underflow_events": 0,
        "ring_overwrite_events": 0,
        "ring_underrun_events": 0,
        "ring_dropped_input_frames": 0,
        "ring_silence_output_frames": 0,
        "last_input_status": "",
        "last_output_status": "",
        "last_input_callback_at": None,
        "last_output_callback_at": None,
        "buffer_frames": max(blocksize * 16, 16384),
        "buffer_peak_frames": 0,
        "buffer_fill_frames": 0,
    }
    stats_lock = threading.Lock()
    ring_frames = int(stats["buffer_frames"])
    ring = np.zeros((ring_frames, 2), dtype=np.float32)
    ring_write = 0
    ring_read = 0
    ring_fill = 0
    ring_lock = threading.Lock()

    def control_watcher() -> None:
        control_mtime: float | None = None
        while not stop_event.wait(0.5):
            if control_path is None:
                continue
            try:
                stat = control_path.stat()
            except FileNotFoundError:
                continue
            if control_mtime is not None and stat.st_mtime <= control_mtime:
                continue
            try:
                payload = json.loads(control_path.read_text())
            except Exception as exc:  # noqa: BLE001
                print(f"warning: failed to parse control file {control_path}: {exc}", file=sys.stderr)
                continue
            if not isinstance(payload, dict):
                continue
            control_mtime = stat.st_mtime
            next_state = dict(control_state)
            for key in ("gain", "low_gain", "mid_gain", "high_gain"):
                value = payload.get(key)
                if isinstance(value, (int, float)):
                    next_state[key] = float(value)
            control_state.update(next_state)
            fx.update_params(
                gain=next_state["gain"],
                low_gain=next_state["low_gain"],
                mid_gain=next_state["mid_gain"],
                high_gain=next_state["high_gain"],
            )

    def stats_writer() -> None:
        last_snapshot: str | None = None
        while not stop_event.wait(1.0):
            if stats_path is None:
                continue
            with stats_lock:
                snapshot = dict(stats)
            snapshot["control"] = dict(control_state)
            encoded = json.dumps(snapshot, sort_keys=True)
            if encoded == last_snapshot:
                continue
            atomic_write_json(stats_path, snapshot)
            last_snapshot = encoded
        if stats_path is not None:
            with stats_lock:
                snapshot = dict(stats)
            snapshot["control"] = dict(control_state)
            atomic_write_json(stats_path, snapshot)

    watcher = None
    if control_path is not None:
        watcher = threading.Thread(target=control_watcher, name="djm-router-control", daemon=True)
        watcher.start()
    stats_thread = threading.Thread(target=stats_writer, name="djm-router-stats", daemon=True)
    stats_thread.start()

    def input_callback(indata, frames, time_info, status):  # noqa: ANN001
        nonlocal ring_write, ring_read, ring_fill
        processed = fx.process(np.asarray(indata, dtype=np.float32))
        now = time.time()
        with stats_lock:
            stats["input_callbacks"] += 1
            stats["last_input_callback_at"] = now
            if status:
                if getattr(status, "input_overflow", False):
                    stats["input_overflow_events"] += 1
                if getattr(status, "input_underflow", False):
                    stats["input_underflow_events"] += 1
                stats["last_input_status"] = str(status)
        with ring_lock:
            free = ring_frames - ring_fill
            if frames > free:
                drop = frames - free
                ring_read = (ring_read + drop) % ring_frames
                ring_fill -= drop
                with stats_lock:
                    stats["ring_overwrite_events"] += 1
                    stats["ring_dropped_input_frames"] += drop
            first = min(frames, ring_frames - ring_write)
            ring[ring_write:ring_write + first] = processed[:first]
            remaining = frames - first
            if remaining > 0:
                ring[0:remaining] = processed[first:first + remaining]
            ring_write = (ring_write + frames) % ring_frames
            ring_fill += frames
            with stats_lock:
                stats["buffer_fill_frames"] = ring_fill
                stats["buffer_peak_frames"] = max(int(stats["buffer_peak_frames"]), ring_fill)

    def output_callback(outdata, frames, time_info, status):  # noqa: ANN001
        nonlocal ring_write, ring_read, ring_fill
        outdata.fill(0)
        now = time.time()
        with stats_lock:
            stats["output_callbacks"] += 1
            stats["last_output_callback_at"] = now
            if status:
                if getattr(status, "output_underflow", False):
                    stats["output_underflow_events"] += 1
                if getattr(status, "output_overflow", False):
                    stats["output_overflow_events"] += 1
                stats["last_output_status"] = str(status)
        stereo = np.zeros((frames, 2), dtype=np.float32)
        available = 0
        with ring_lock:
            available = min(frames, ring_fill)
            if available > 0:
                first = min(available, ring_frames - ring_read)
                stereo[:first] = ring[ring_read:ring_read + first]
                remaining = available - first
                if remaining > 0:
                    stereo[first:first + remaining] = ring[0:remaining]
                ring_read = (ring_read + available) % ring_frames
                ring_fill -= available
            with stats_lock:
                stats["buffer_fill_frames"] = ring_fill
        if available < frames:
            with stats_lock:
                stats["ring_underrun_events"] += 1
                stats["ring_silence_output_frames"] += (frames - available)
        outdata[:, ch_a] = stereo[:, 0]
        outdata[:, ch_b] = stereo[:, 1]

    while True:
        try:
            input_device = find_device(input_name, wants_input=True)
            output_device = find_device(TARGET_DEVICE, wants_output=True)
            with stats_lock:
                stats["device_state"] = "running"
                stats["device_name"] = output_device.name
                stats["last_error"] = ""
            with sd.InputStream(
                device=input_device.index,
                samplerate=samplerate,
                blocksize=blocksize,
                latency=latency,
                dtype="float32",
                channels=2,
                callback=input_callback,
            ), sd.OutputStream(
                device=output_device.index,
                samplerate=samplerate,
                blocksize=blocksize,
                latency=latency,
                dtype="float32",
                channels=output_device.output_channels,
                callback=output_callback,
            ):
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    break
        except SystemExit as exc:
            with stats_lock:
                stats["device_state"] = "waiting-for-device"
                stats["last_error"] = str(exc)
            time.sleep(1.0)
            continue
        except Exception as exc:  # noqa: BLE001
            with stats_lock:
                stats["device_state"] = "stream-error"
                stats["last_error"] = str(exc)
            time.sleep(0.5)
            continue
        else:
            break
    stop_event.set()
    if watcher is not None:
        watcher.join(timeout=1.0)
    stats_thread.join(timeout=1.0)


def play_test_tone(pair: str, seconds: float, samplerate: int, hz: float) -> None:
    output_device = find_device(TARGET_DEVICE, wants_output=True)
    ch_a, ch_b = parse_pair(pair)
    frames = int(seconds * samplerate)

    t = np.arange(frames, dtype=np.float32) / np.float32(samplerate)
    wave = (0.18 * np.sin(np.float32(2.0 * math.pi) * np.float32(hz) * t)).astype(np.float32)
    out = np.zeros((frames, output_device.output_channels), dtype=np.float32)
    out[:, ch_a] = wave
    out[:, ch_b] = wave

    print(f"Playing {hz:.1f} Hz tone on {output_device.name} channels {pair} for {seconds:.1f}s")
    sd.play(out, samplerate=samplerate, device=output_device.index, blocking=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-devices", help="List CoreAudio devices visible to PortAudio")

    route_parser = subparsers.add_parser(
        "route", help="Route a stereo input device into a stereo pair on the DJM"
    )
    route_parser.add_argument("--input", required=True, help="Input device substring, e.g. 'BlackHole 2ch'")
    route_parser.add_argument("--pair", default="3/4", help="DJM output pair: 1/2, 3/4, 5/6, or 7/8")
    route_parser.add_argument("--samplerate", type=int, default=48000)
    route_parser.add_argument("--blocksize", type=int, default=0)
    route_parser.add_argument("--latency", choices=["low", "high"], default="high")
    route_parser.add_argument("--gain", type=float, default=1.0, help="Digital gain for the routed stereo input")
    route_parser.add_argument("--delay-seconds", type=float, default=0.0, help="Optional fixed delay on the routed stereo input")
    route_parser.add_argument("--low-gain", type=float, default=1.0, help="Low-band gain for the routed stereo input")
    route_parser.add_argument("--mid-gain", type=float, default=1.0, help="Mid-band gain for the routed stereo input")
    route_parser.add_argument("--high-gain", type=float, default=1.0, help="High-band gain for the routed stereo input")
    route_parser.add_argument("--control-file", help="Optional JSON control file for live gain/EQ updates")
    route_parser.add_argument("--stats-file", help="Optional JSON stats file for overflow/underrun diagnostics")

    tone_parser = subparsers.add_parser("test-tone", help="Play a tone directly to a DJM stereo pair")
    tone_parser.add_argument("--pair", default="3/4", help="DJM output pair: 1/2, 3/4, 5/6, or 7/8")
    tone_parser.add_argument("--seconds", type=float, default=4.0)
    tone_parser.add_argument("--samplerate", type=int, default=48000)
    tone_parser.add_argument("--hz", type=float, default=523.25)

    args = parser.parse_args()

    if args.command == "list-devices":
        list_devices()
        return

    if args.command == "route":
        route_input_to_djm(
            input_name=args.input,
            pair=args.pair,
            samplerate=args.samplerate,
            blocksize=args.blocksize,
            latency=args.latency,
            gain=args.gain,
            delay_seconds=args.delay_seconds,
            low_gain=args.low_gain,
            mid_gain=args.mid_gain,
            high_gain=args.high_gain,
            control_file=args.control_file,
            stats_file=args.stats_file,
        )
        return

    if args.command == "test-tone":
        play_test_tone(
            pair=args.pair,
            seconds=args.seconds,
            samplerate=args.samplerate,
            hz=args.hz,
        )
        return

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
