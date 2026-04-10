#!/usr/bin/env python3
"""Beat-aware software autofade between two browser-routed DJM inputs."""

from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from chrome_audio_split import ChromeCDP, find_page  # noqa: E402
from djm_blend import StereoBuffer, TARGET_DEVICE, find_device, open_stereo_input, parse_pair  # noqa: E402


def onset_envelope(audio: np.ndarray, samplerate: int, env_rate: int = 200) -> np.ndarray:
    if audio.ndim == 2:
        mono = audio.mean(axis=1)
    else:
        mono = audio
    if mono.size < samplerate:
        return np.zeros(0, dtype=np.float32)
    mono = mono.astype(np.float32)
    diff = np.diff(mono, prepend=mono[0])
    env = np.abs(diff)
    smooth = max(1, samplerate // 200)
    kernel = np.ones(smooth, dtype=np.float32) / np.float32(smooth)
    env = np.convolve(env, kernel, mode="same")
    step = max(1, samplerate // env_rate)
    env = env[::step]
    env -= env.mean() if env.size else 0
    env = np.maximum(env, 0)
    return env.astype(np.float32)


@dataclass
class TempoEstimate:
    bpm: float
    confidence: float
    next_beat_delay: float


def estimate_tempo(audio: np.ndarray, samplerate: int) -> TempoEstimate | None:
    env_rate = 200
    env = onset_envelope(audio, samplerate, env_rate=env_rate)
    if env.size < env_rate * 8:
        return None
    min_bpm = 80.0
    max_bpm = 170.0
    min_lag = int(env_rate * 60.0 / max_bpm)
    max_lag = int(env_rate * 60.0 / min_bpm)
    if max_lag >= env.size:
        max_lag = env.size - 1
    if min_lag >= max_lag:
        return None

    corr = np.array(
        [float(np.dot(env[:-lag], env[lag:])) for lag in range(min_lag, max_lag + 1)],
        dtype=np.float64,
    )
    if not np.any(corr > 0):
        return None
    best_index = int(np.argmax(corr))
    best_lag = min_lag + best_index
    bpm = 60.0 * env_rate / float(best_lag)
    sorted_corr = np.sort(corr)
    second = float(sorted_corr[-2]) if sorted_corr.size >= 2 else 1e-9
    confidence = float(corr[best_index] / max(second, 1e-9))

    period = env_rate * 60.0 / bpm
    phase_candidates = max(1, int(round(period)))
    best_phase = 0
    best_phase_score = -1.0
    for offset in range(phase_candidates):
        positions = offset + np.arange(0, env.size, period)
        indices = np.clip(np.round(positions).astype(int), 0, env.size - 1)
        score = float(env[indices].sum())
        if score > best_phase_score:
            best_phase_score = score
            best_phase = offset

    last_index = env.size - 1
    beats_since_phase = max(0.0, (last_index - best_phase) / period)
    next_beat_index = best_phase + math.ceil(beats_since_phase) * period
    if next_beat_index <= last_index:
        next_beat_index += period
    next_beat_delay = max(0.0, (next_beat_index - last_index) / env_rate)
    return TempoEstimate(bpm=bpm, confidence=confidence, next_beat_delay=next_beat_delay)


class RollingCapture:
    def __init__(self, channels: int, samplerate: int, seconds: float = 24.0) -> None:
        self.channels = channels
        self.max_frames = int(seconds * samplerate)
        self.buffer = np.zeros((0, channels), dtype=np.float32)
        self._lock = threading.Lock()

    def push(self, chunk: np.ndarray) -> None:
        stereo = np.zeros((chunk.shape[0], self.channels), dtype=np.float32)
        width = min(self.channels, chunk.shape[1])
        stereo[:, :width] = chunk[:, :width]
        with self._lock:
            self.buffer = np.concatenate((self.buffer, stereo), axis=0)
            if self.buffer.shape[0] > self.max_frames:
                self.buffer = self.buffer[-self.max_frames :]

    def snapshot(self) -> np.ndarray:
        with self._lock:
            return self.buffer.copy()


@dataclass
class FadeState:
    start_time: float | None = None
    duration: float = 0.0


def set_tab_rate(port: int, *, title: str | None, index: int | None, rate: float) -> dict[str, object]:
    cdp = ChromeCDP(port)
    page = find_page(cdp, title_substring=title, index=index)
    result = cdp.eval(page, f"""
(() => {{
  const rate = {rate!r};
  const elements = [...document.querySelectorAll('audio,video')];
  elements.forEach(element => {{
    element.preservesPitch = true;
    element.playbackRate = rate;
  }});
  return {{
    title: document.title,
    playbackRate: elements.map(element => element.playbackRate),
    mediaCount: elements.length
  }};
}})()
""")
    return result


def route_and_fade(
    *,
    input_a_name: str,
    input_b_name: str,
    pair_a: str,
    pair_b: str,
    samplerate: int,
    blocksize: int,
    port: int,
    incoming_title: str | None,
    incoming_index: int | None,
    settle_seconds: float,
    fade_beats: int,
    max_rate_adjust: float,
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
    capture_a = RollingCapture(2, samplerate)
    capture_b = RollingCapture(2, samplerate)
    fade_state = FadeState()

    def input_stream(device, stereo_buffer, capture):
        channels = min(max(device.input_channels, 1), 2)

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                print(f"{device.name}: {status}", file=sys.stderr)
            chunk = np.asarray(indata, dtype=np.float32)
            stereo_buffer.push(chunk)
            capture.push(chunk[:, : min(chunk.shape[1], 2)])

        return sd.InputStream(
            device=device.index,
            samplerate=samplerate,
            blocksize=blocksize,
            dtype="float32",
            channels=channels,
            callback=callback,
        )

    def output_callback(outdata, frames, time_info, status):  # noqa: ANN001
        if status:
            print(f"{output.name}: {status}", file=sys.stderr)
        chunk_a = buffer_a.read(frames)
        chunk_b = buffer_b.read(frames)
        now = time.monotonic()
        if fade_state.start_time is None or fade_state.duration <= 0:
            gain_a, gain_b = 1.0, 0.0
        else:
            progress = min(1.0, max(0.0, (now - fade_state.start_time) / fade_state.duration))
            gain_a = math.cos(progress * (math.pi / 2.0))
            gain_b = math.sin(progress * (math.pi / 2.0))
        outdata.fill(0)
        outdata[:, out_a1] = chunk_a[:, 0] * np.float32(gain_a)
        outdata[:, out_a2] = chunk_a[:, 1] * np.float32(gain_a)
        outdata[:, out_b1] = chunk_b[:, 0] * np.float32(gain_b)
        outdata[:, out_b2] = chunk_b[:, 1] * np.float32(gain_b)

    print(
        f"Monitoring {input_a.name} and {input_b.name}; outgoing on {pair_a}, incoming on {pair_b}."
    )
    print("Collecting tempo data before fade.")

    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    with input_stream(input_a, buffer_a, capture_a), input_stream(input_b, buffer_b, capture_b), sd.OutputStream(
        device=output.index,
        samplerate=samplerate,
        blocksize=blocksize,
        dtype="float32",
        channels=output.output_channels,
        callback=output_callback,
    ):
        time.sleep(settle_seconds)
        tempo_a = estimate_tempo(capture_a.snapshot(), samplerate)
        tempo_b = estimate_tempo(capture_b.snapshot(), samplerate)

        if tempo_a is None or tempo_b is None:
            raise SystemExit("Could not estimate tempo from one or both inputs")

        print(
            f"tempo A={tempo_a.bpm:.2f} BPM conf={tempo_a.confidence:.2f}; "
            f"tempo B={tempo_b.bpm:.2f} BPM conf={tempo_b.confidence:.2f}"
        )

        desired_rate = max(
            1.0 - max_rate_adjust,
            min(1.0 + max_rate_adjust, tempo_a.bpm / tempo_b.bpm),
        )
        rate_result = set_tab_rate(port, title=incoming_title, index=incoming_index, rate=desired_rate)
        print(f"set incoming playback rate -> {desired_rate:.4f} ({rate_result})")

        time.sleep(4.0)
        tempo_a = estimate_tempo(capture_a.snapshot(), samplerate) or tempo_a
        tempo_b = estimate_tempo(capture_b.snapshot(), samplerate) or tempo_b
        matched_bpm = (tempo_a.bpm + tempo_b.bpm) / 2.0
        fade_duration = max(6.0, min(20.0, fade_beats * 60.0 / matched_bpm))
        wait_delay = max(tempo_a.next_beat_delay, 0.1)

        print(
            f"matched A={tempo_a.bpm:.2f} B={tempo_b.bpm:.2f}; "
            f"waiting {wait_delay:.2f}s for next beat, then fading over {fade_duration:.2f}s"
        )
        time.sleep(wait_delay)
        fade_state.start_time = time.monotonic()
        fade_state.duration = fade_duration
        print("fade started")

        try:
            while True:
                if fade_state.start_time is not None and (time.monotonic() - fade_state.start_time) >= fade_state.duration + 0.25:
                    break
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-a", default="BlackHole 2ch")
    parser.add_argument("--input-b", default="BlackHole 16ch")
    parser.add_argument("--pair-a", default="1/2")
    parser.add_argument("--pair-b", default="3/4")
    parser.add_argument("--samplerate", type=int, default=48000)
    parser.add_argument("--blocksize", type=int, default=1024)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--incoming-title", help="Title substring for the tab on input B")
    parser.add_argument("--incoming-index", type=int, help="Fallback music tab index for input B")
    parser.add_argument("--settle-seconds", type=float, default=10.0)
    parser.add_argument("--fade-beats", type=int, default=16)
    parser.add_argument("--max-rate-adjust", type=float, default=0.05)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    route_and_fade(
        input_a_name=args.input_a,
        input_b_name=args.input_b,
        pair_a=args.pair_a,
        pair_b=args.pair_b,
        samplerate=args.samplerate,
        blocksize=args.blocksize,
        port=args.port,
        incoming_title=args.incoming_title,
        incoming_index=args.incoming_index,
        settle_seconds=args.settle_seconds,
        fade_beats=args.fade_beats,
        max_rate_adjust=args.max_rate_adjust,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
