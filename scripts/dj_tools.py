#!/usr/bin/env python3
"""Small DJ control surface for the current YouTube Music + DJM setup."""

from __future__ import annotations

import argparse
import difflib
import fcntl
import html
import json
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import statistics
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from chrome_audio_split import (  # noqa: E402
    ChromeCDP,
    analyze_media_script,
    choose_track_script,
    page_status_script,
    persistent_audio_control_script,
    playlist_tracks_script,
    queue_lock_script,
    select_music_pages,
    set_rate_script,
    set_sink_script,
    set_volume_script,
    transport_script,
    command_crossfade,
    command_staged_mix,
)


PORT = 9333
DIRECT_LABEL = "DJM-900nexus"
DIRECT_RELAY_LABEL = "BlackHole 16ch"
ROUTED_LABEL = "BlackHole 2ch"
ROUTER_PIDFILE = Path("/tmp/dj_tools_routed.pid")
ROUTER_LOG = Path("/tmp/dj_tools_routed.log")
ROUTER_CONTROL = Path("/tmp/dj_tools_routed_control.json")
ROUTER_STATS = Path("/tmp/dj_tools_routed_stats.json")
DIRECT_ROUTER_PIDFILE = Path("/tmp/dj_tools_direct.pid")
DIRECT_ROUTER_LOG = Path("/tmp/dj_tools_direct.log")
DIRECT_ROUTER_CONTROL = Path("/tmp/dj_tools_direct_control.json")
DIRECT_ROUTER_STATS = Path("/tmp/dj_tools_direct_stats.json")
DEFAULT_INDEX_PATH = Path("artifacts/dj/house_playlist_index.json")
TRANSITION_DIR = Path("artifacts/dj")
STEM_CACHE_DIR = Path("artifacts/dj/stems")
STEM_RENDER_DIR = Path("artifacts/dj/stem_renders")
YT_DLP_BIN = os.environ.get("YT_DLP_BIN", "yt-dlp")
STEM_PREVIEW_SECONDS = 48.0
STRUCTURE_ANALYSIS_VERSION = 2
ROUTER_LOG_ROTATE_BYTES = 2 * 1024 * 1024
ROUTER_STOP_TIMEOUT = 4.0
AUBIO_BIN = os.environ.get("AUBIO_BIN", shutil.which("aubio") or "/opt/homebrew/bin/aubio")
RUBBERBAND_BIN = os.environ.get("RUBBERBAND_BIN", shutil.which("rubberband") or "/opt/homebrew/bin/rubberband")
LIBKEYFINDER_CLI = SCRIPT_DIR / "bin" / "libkeyfinder_cli"
AUDIO_METRICS_VERSION = 1
VOCAL_CUE_VERSION = 1
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "tiny")
TRANSITION_DATASET_VERSION = 1
SEMITONES = {
    "C": 0,
    "B#": 0,
    "C#": 1,
    "DB": 1,
    "D": 2,
    "D#": 3,
    "EB": 3,
    "E": 4,
    "FB": 4,
    "F": 5,
    "E#": 5,
    "F#": 6,
    "GB": 6,
    "G": 7,
    "G#": 8,
    "AB": 8,
    "A": 9,
    "A#": 10,
    "BB": 10,
    "B": 11,
    "CB": 11,
}


def page_status(cdp: ChromeCDP, page) -> dict[str, object]:
    status = cdp.eval(page, page_status_script())
    if not isinstance(status, dict):
        raise SystemExit("Unexpected page status payload")
    return status


def classify_page(status: dict[str, object]) -> str | None:
    desired = status.get("desired")
    if not isinstance(desired, dict):
        return None
    label = str(desired.get("label", ""))
    if DIRECT_LABEL.lower() in label.lower() or DIRECT_RELAY_LABEL.lower() in label.lower():
        return "direct"
    if ROUTED_LABEL.lower() in label.lower():
        return "routed"
    return None


def resolve_roles(port: int = PORT) -> dict[str, tuple[object, dict[str, object]]]:
    cdp = ChromeCDP(port)
    roles: dict[str, tuple[object, dict[str, object]]] = {}
    for page in select_music_pages(cdp):
        status = page_status(cdp, page)
        role = classify_page(status)
        if role:
            if role in roles:
                existing_page, existing_status = roles[role]
                raise SystemExit(
                    f"Duplicate role assignment for {role}: "
                    f"{getattr(existing_page, 'title', '<unknown>')} and {getattr(page, 'title', '<unknown>')}. "
                    "Re-route tabs so each role is unique."
                )
            roles[role] = (page, status)
    return roles


def queue_looks_locked(status: dict[str, object] | None) -> bool:
    if not isinstance(status, dict):
        return False
    repeat_label = str(status.get("repeatLabel") or "")
    shuffle_label = str(status.get("shuffleLabel") or "")
    return bool(re.search(r"repeat off", repeat_label, re.I)) and bool(re.search(r"shuffle$", shuffle_label, re.I))


def ensure_queue_locked(
    role: str,
    *,
    port: int = PORT,
    cdp: ChromeCDP | None = None,
    roles: dict[str, tuple[object, dict[str, object]]] | None = None,
) -> dict[str, object]:
    if roles is None:
        roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    page, status = roles[role]
    local_cdp = cdp or ChromeCDP(port)
    if queue_looks_locked(status):
        return {"role": role, "lock": {"ok": True, "skipped": True}, "status": status}
    result = local_cdp.eval(page, queue_lock_script(repeat_off=True, shuffle_off=True))
    return {"role": role, "lock": result, "status": page_status(local_cdp, page)}


def media_clock_seconds(status: dict[str, object] | None) -> float | None:
    if not isinstance(status, dict):
        return None
    values = status.get("currentTime")
    if not isinstance(values, list) or not values:
        return None
    raw = values[0]
    if not isinstance(raw, (int, float)):
        return None
    return float(raw)


def wait_for_role_progress(
    role: str,
    *,
    port: int = PORT,
    timeout: float = 4.0,
    min_progress: float = 0.18,
    interval: float = 0.12,
    min_duration: float = 5.0,
) -> dict[str, object]:
    cdp = ChromeCDP(port)
    roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    page, initial_status = roles[role]
    start_status = initial_status
    start_time = media_clock_seconds(start_status)
    started_at = time.monotonic()
    last_status = start_status
    while time.monotonic() - started_at <= timeout:
        status = page_status(cdp, page)
        last_status = status
        paused = status.get("paused") if isinstance(status, dict) else None
        duration = status.get("duration") if isinstance(status, dict) else None
        media_count = status.get("mediaCount") if isinstance(status, dict) else 0
        current_time = media_clock_seconds(status)
        duration_value = None
        if isinstance(duration, list) and duration and isinstance(duration[0], (int, float)):
            duration_value = float(duration[0])
        if (
            media_count
            and duration_value is not None
            and duration_value >= min_duration
            and isinstance(paused, list)
            and paused
            and paused[0] is False
            and current_time is not None
            and start_time is not None
            and (current_time - start_time) >= min_progress
        ):
            return {
                "ok": True,
                "role": role,
                "waited_seconds": round(time.monotonic() - started_at, 3),
                "status": status,
            }
        time.sleep(interval)
    return {
        "ok": False,
        "role": role,
        "waited_seconds": round(time.monotonic() - started_at, 3),
        "status": last_status,
        "error": "media-did-not-advance",
    }


def ensure_role_playing(
    role: str,
    *,
    port: int = PORT,
    timeout: float = 4.0,
) -> dict[str, object]:
    cdp = ChromeCDP(port)
    page, status = role_page_and_status(role, port=port)
    paused = status.get("paused") if isinstance(status, dict) else None
    transport = None
    if isinstance(paused, list) and paused and paused[0] is True:
        transport = cdp.eval(page, transport_script(action="play"))
        time.sleep(0.15)
    readiness = wait_for_role_progress(role, port=port, timeout=timeout)
    if not readiness.get("ok"):
        cdp.eval(page, persistent_audio_control_script(play_after=True))
        time.sleep(0.2)
        readiness = wait_for_role_progress(role, port=port, timeout=max(1.5, timeout / 2))
    return {
        "role": role,
        "transport": transport,
        "readiness": readiness,
    }


def expected_sink_label(role: str) -> str:
    if role == "direct":
        return DIRECT_RELAY_LABEL if router_running(read_router_pid(DIRECT_ROUTER_PIDFILE)) else DIRECT_LABEL
    if role == "routed":
        return ROUTED_LABEL
    raise SystemExit(f"Unsupported role: {role}")


def safe_read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def tail_text(path: Path, *, limit: int = 1000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(-limit, os.SEEK_END)
        return handle.read().decode("utf-8", errors="replace")


def rotate_log_if_needed(path: Path, *, limit_bytes: int = ROUTER_LOG_ROTATE_BYTES) -> None:
    if not path.exists():
        return
    if path.stat().st_size < limit_bytes:
        return
    backup = path.with_suffix(path.suffix + ".1")
    backup.unlink(missing_ok=True)
    path.replace(backup)


def process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def process_matches_router(pid: int, *, script_name: str) -> bool:
    command = process_command(pid)
    return bool(command) and script_name in command


def terminate_router(
    pidfile: Path,
    *,
    script_name: str,
    control_file: Path | None = None,
    stats_file: Path | None = None,
    timeout: float = ROUTER_STOP_TIMEOUT,
) -> dict[str, object]:
    pid = read_router_pid(pidfile)
    if not router_running(pid):
        pidfile.unlink(missing_ok=True)
        if control_file is not None:
            control_file.unlink(missing_ok=True)
        if stats_file is not None:
            stats_file.unlink(missing_ok=True)
        return {"ok": True, "message": "router not running", "pid": pid}
    if not process_matches_router(pid, script_name=script_name):
        return {"ok": False, "error": f"pid {pid} does not match expected process {script_name}"}
    os.kill(pid, signal.SIGTERM)
    started = time.monotonic()
    while time.monotonic() - started <= timeout:
        if not router_running(pid):
            pidfile.unlink(missing_ok=True)
            if control_file is not None:
                control_file.unlink(missing_ok=True)
            if stats_file is not None:
                stats_file.unlink(missing_ok=True)
            return {"ok": True, "stopped_pid": pid}
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pidfile.unlink(missing_ok=True)
    if control_file is not None:
        control_file.unlink(missing_ok=True)
    if stats_file is not None:
        stats_file.unlink(missing_ok=True)
    return {"ok": True, "stopped_pid": pid, "forced": True}


def reapply_role_controls(
    role: str,
    *,
    volume: float | None = None,
    playback_rate: float | None = None,
    muted: bool | None = None,
    port: int = PORT,
) -> dict[str, object]:
    cdp = ChromeCDP(port)
    roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    page, status = roles[role]
    sink_label = expected_sink_label(role)
    desired = status.get("desired") if isinstance(status, dict) else None
    desired_label = str(desired.get("label", "")) if isinstance(desired, dict) else ""
    desired_sink_id = str(desired.get("sinkId", "")) if isinstance(desired, dict) else ""
    sink_ids = [str(value) for value in status.get("sinkIds", []) if value] if isinstance(status, dict) else []
    sink_matches = (
        sink_label.lower() in desired_label.lower()
        and bool(desired_sink_id)
        and bool(sink_ids)
        and all(value == desired_sink_id for value in sink_ids)
    )
    if not sink_matches:
        cdp.eval(page, set_sink_script(sink_label, play=False))
    if playback_rate is not None or volume is not None:
        cdp.eval(
            page,
            persistent_audio_control_script(
                rate=playback_rate,
                volume=volume,
                muted=muted if muted is not None else (False if volume is not None else None),
            ),
        )
    return page_status(cdp, page)


def lock_role_queue(role: str, *, port: int = PORT) -> dict[str, object]:
    return ensure_queue_locked(role, port=port)


def set_role_volume(role: str, volume: float, port: int = PORT, muted: bool | None = False) -> dict[str, object]:
    cdp = ChromeCDP(port)
    roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    page, _ = roles[role]
    result = cdp.eval(page, set_volume_script(volume, muted=muted))
    status = page_status(cdp, page)
    return {"role": role, "result": result, "status": status}


def park_role_silent(role: str, *, port: int = PORT, playback_rate: float | None = None) -> dict[str, object]:
    return reapply_role_controls(role, volume=0.0, playback_rate=playback_rate, muted=True, port=port)


def role_page_and_status(role: str, port: int = PORT):
    roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    return roles[role]


def command_status(args: argparse.Namespace) -> None:
    roles = resolve_roles(args.port)
    rendered = {}
    for role, (_page, status) in roles.items():
        rendered[role] = status
    print(json.dumps(rendered, indent=2))


def probe_audio_target() -> dict[str, object]:
    try:
        from djm_router import TARGET_DEVICE, iter_devices
    except Exception as exc:
        return {"available": False, "error": f"audio-probe-import: {exc}"}

    try:
        devices = iter_devices()
    except Exception as exc:
        return {"available": False, "error": f"audio-probe-runtime: {exc}"}

    target_matches = [
        {
            "index": device.index,
            "name": device.name,
            "input_channels": device.input_channels,
            "output_channels": device.output_channels,
        }
        for device in devices
        if TARGET_DEVICE.lower() in device.name.lower()
    ]
    return {
        "available": bool(target_matches),
        "target_device": TARGET_DEVICE,
        "matches": target_matches,
    }


def probe_djm_usb_control() -> dict[str, object]:
    try:
        import usb.core  # type: ignore[import-untyped]
    except Exception as exc:
        return {"available": False, "error": f"pyusb-import: {exc}"}

    try:
        from djm900ctl import PRODUCT_ID, VENDOR_ID
    except Exception as exc:
        return {"available": False, "error": f"djm900ctl-import: {exc}"}

    try:
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    except Exception as exc:
        return {"available": False, "error": f"usb-probe-runtime: {exc}"}
    return {
        "available": dev is not None,
        "vendor_id": hex(VENDOR_ID),
        "product_id": hex(PRODUCT_ID),
    }


def probe_pioneer_midi() -> dict[str, object]:
    try:
        import rtmidi  # type: ignore[import-untyped]
    except Exception as exc:
        return {"available": False, "error": f"rtmidi-import: {exc}"}

    try:
        midi_in = rtmidi.MidiIn()
        midi_out = rtmidi.MidiOut()
        inputs = list(midi_in.get_ports())
        outputs = list(midi_out.get_ports())
    except Exception as exc:
        return {"available": False, "error": f"rtmidi-runtime: {exc}"}

    pioneer_inputs = [name for name in inputs if re.search(r"(pioneer|alphatheta|cdj|djm)", name, re.I)]
    pioneer_outputs = [name for name in outputs if re.search(r"(pioneer|alphatheta|cdj|djm)", name, re.I)]
    return {
        "available": bool(pioneer_inputs or pioneer_outputs),
        "inputs": pioneer_inputs,
        "outputs": pioneer_outputs,
    }


def command_doctor(args: argparse.Namespace) -> None:
    payload: dict[str, object] = {"port": args.port}
    try:
        payload["roles"] = {role: status for role, (_page, status) in resolve_roles(args.port).items()}
    except Exception as exc:
        payload["roles_error"] = str(exc)
    payload["routed_router"] = {
        "pid": read_router_pid(ROUTER_PIDFILE),
        "running": router_running(read_router_pid(ROUTER_PIDFILE)),
        "control": safe_read_json(ROUTER_CONTROL),
        "stats": safe_read_json(ROUTER_STATS),
        "log_tail": tail_text(ROUTER_LOG),
    }
    payload["direct_router"] = {
        "pid": read_router_pid(DIRECT_ROUTER_PIDFILE),
        "running": router_running(read_router_pid(DIRECT_ROUTER_PIDFILE)),
        "control": safe_read_json(DIRECT_ROUTER_CONTROL),
        "stats": safe_read_json(DIRECT_ROUTER_STATS),
        "log_tail": tail_text(DIRECT_ROUTER_LOG),
    }
    audio_probe = probe_audio_target()
    usb_probe = probe_djm_usb_control()
    midi_probe = probe_pioneer_midi()
    warnings: list[str] = []
    if not audio_probe.get("available"):
        warnings.append("DJM audio target is not visible to CoreAudio")
    if not usb_probe.get("available"):
        warnings.append("DJM USB control device is not visible")
    if not midi_probe.get("available"):
        warnings.append("No Pioneer MIDI ports are visible")
    for name in ("routed_router", "direct_router"):
        router_state = payload.get(name)
        if not isinstance(router_state, dict) or not router_state.get("running"):
            continue
        stats = router_state.get("stats")
        if isinstance(stats, dict):
            device_state = str(stats.get("device_state") or "")
            if device_state and device_state != "running":
                warnings.append(f"{name} state={device_state}: {stats.get('last_error')}")
            if int(stats.get("input_overflow_events", 0)) > 0:
                warnings.append(f"{name} input overflow events={int(stats.get('input_overflow_events', 0))}")
            if int(stats.get("ring_underrun_events", 0)) > 0:
                warnings.append(f"{name} ring underrun events={int(stats.get('ring_underrun_events', 0))}")
        else:
            log_tail = str(router_state.get("log_tail") or "")
            if "overflow" in log_tail.lower():
                warnings.append(f"{name} is reporting overflow")
    payload["hardware"] = {
        "audio_target": audio_probe,
        "usb_control": usb_probe,
        "midi": midi_probe,
        "warnings": warnings,
    }
    print(json.dumps(payload, indent=2))


def command_hot(args: argparse.Namespace) -> None:
    cold_role = "routed" if args.role == "direct" else "direct"
    result = [
        set_role_volume(args.role, 1.0, port=args.port, muted=False),
        park_role_silent(cold_role, port=args.port),
    ]
    print(json.dumps(result, indent=2))


def command_lock_queue(args: argparse.Namespace) -> None:
    print(json.dumps(lock_role_queue(args.role, port=args.port), indent=2))


def command_playlist(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    page, status = role_page_and_status(args.role, port=args.port)
    tracks = cdp.eval(page, playlist_tracks_script())
    print(json.dumps({"role": args.role, "status": status, "playlist": tracks}, indent=2))


def choose_role_track(
    role: str,
    *,
    title: str | None = None,
    index: int | None = None,
    settle_seconds: float = 1.5,
    port: int = PORT,
) -> dict[str, object]:
    if (title is None) == (index is None):
        raise SystemExit("Provide exactly one of --title or --index")
    cdp = ChromeCDP(port)
    roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    page, status = roles[role]
    ensure_queue_locked(role, port=port, cdp=cdp, roles=roles)
    result = cdp.eval(page, choose_track_script(title_substring=title, index=index))
    time.sleep(settle_seconds)
    current_volume = None
    current_rate = None
    volumes = status.get("volume") if isinstance(status, dict) else None
    rates = status.get("playbackRate") if isinstance(status, dict) else None
    if volumes:
        current_volume = float(volumes[0])
    if rates:
        current_rate = float(rates[0])
    if current_volume is not None and current_volume <= 1e-3:
        reapply_role_controls(
            role,
            volume=0.0,
            playback_rate=current_rate,
            muted=True,
            port=port,
        )
    ready = ensure_role_playing(role, port=port, timeout=max(2.0, settle_seconds + 1.5))
    post = reapply_role_controls(role, volume=current_volume, playback_rate=current_rate, port=port)
    if current_volume is not None and current_volume <= 1e-3:
        post = reapply_role_controls(role, volume=0.0, playback_rate=current_rate, muted=True, port=port)
    return {"role": role, "choose": result, "ready": ready, "status": post}


def command_choose(args: argparse.Namespace) -> None:
    print(json.dumps(choose_role_track(args.role, title=args.title, index=args.index, settle_seconds=args.settle_seconds, port=args.port), indent=2))


def command_transport(args: argparse.Namespace) -> None:
    if args.action.startswith("seek") and args.seconds is None:
        raise SystemExit("--seconds is required for seek-abs and seek-rel")
    cdp = ChromeCDP(args.port)
    page, _status = role_page_and_status(args.role, port=args.port)
    if args.action in {"next", "prev"}:
        cdp.eval(page, queue_lock_script(repeat_off=True, shuffle_off=True))
    result = cdp.eval(page, transport_script(action=args.action, seconds=args.seconds))
    time.sleep(args.settle_seconds)
    post = reapply_role_controls(args.role, port=args.port)
    print(json.dumps({"role": args.role, "transport": result, "status": post}, indent=2))


def playlist_tracks_for_role(role: str, port: int = PORT) -> tuple[list[dict[str, object]], dict[str, object]]:
    cdp = ChromeCDP(port)
    page, status = role_page_and_status(role, port=port)
    payload = cdp.eval(page, playlist_tracks_script())
    tracks = payload.get("tracks") if isinstance(payload, dict) else None
    if not isinstance(tracks, list):
        raise SystemExit("Could not read playlist tracks from the current role")
    normalized = [track for track in tracks if isinstance(track, dict)]
    return normalized, status


def primary_artist(track: dict[str, object]) -> str:
    subtitle = str(track.get("subtitle") or "").strip()
    if not subtitle:
        return ""
    return subtitle.split(" • ", 1)[0].strip()


def slugify(value: str) -> str:
    text = simplify_for_match(value)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "track"


def video_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    values = query.get("v")
    if values and values[0]:
        return values[0]
    return None


def stem_track_key(record: dict[str, object]) -> str:
    video_id = video_id_from_url(str(record.get("url") or ""))
    if video_id:
        return video_id
    artist = primary_artist(record)
    title = str(record.get("title") or "track")
    return slugify(f"{artist}-{title}")


def stem_track_dir(record: dict[str, object]) -> Path:
    return STEM_CACHE_DIR / stem_track_key(record)


def stem_source_path(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "source.wav"


def stem_metadata_path(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "stem_analysis.json"


def audio_metrics_path(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "audio_metrics.json"


def vocal_cue_path(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "vocal_cues.json"


def structure_plot_path(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "structure_plot.png"


def transition_dataset_path(name: str = "transition_candidates") -> Path:
    return TRANSITION_DIR / f"{name}.jsonl"


def stem_preview_path(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "source_preview.wav"


def stem_output_dir(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "preview_demucs" / "htdemucs" / stem_preview_path(record).stem


def stem_paths(record: dict[str, object]) -> dict[str, Path]:
    root = stem_output_dir(record)
    return {
        "vocals": root / "vocals.wav",
        "drums": root / "drums.wav",
        "bass": root / "bass.wav",
        "other": root / "other.wav",
    }


def match_index_record(payload: dict[str, object], track: dict[str, object]) -> dict[str, object] | None:
    records = payload.get("records")
    if not isinstance(records, list):
        return None
    target_url = str(track.get("url") or "")
    target_title = normalize_title(str(track.get("title") or ""))
    target_artist = normalize_title(primary_artist(track))
    best: tuple[float, dict[str, object]] | None = None
    for candidate in records:
        if not isinstance(candidate, dict):
            continue
        if target_url and str(candidate.get("url") or "") == target_url:
            return candidate
        score = title_match_score(target_title, str(candidate.get("title") or ""))
        if target_artist:
            score = 0.8 * score + 0.2 * artist_match_score(target_artist, str(candidate.get("primary_artist") or ""))
        if best is None or score > best[0]:
            best = (score, candidate)
    if best and best[0] >= 0.72:
        return best[1]
    return None


def current_track_record_for_role(role: str, *, port: int = PORT, index_payload: dict[str, object] | None = None) -> tuple[dict[str, object], dict[str, object]]:
    tracks, status = playlist_tracks_for_role(role, port=port)
    current_title = normalize_title(str(status.get("playerBarTitle") or status.get("title") or ""))
    current_artist = normalize_title(str(status.get("playerBarByline") or ""))
    best: tuple[float, dict[str, object]] | None = None
    for track in tracks:
        title_score = title_match_score(current_title, str(track.get("title") or ""))
        artist_score = artist_match_score(current_artist, str(track.get("subtitle") or ""))
        score = 0.8 * title_score + 0.2 * artist_score
        if best is None or score > best[0]:
            best = (score, track)
    if best and best[0] >= 0.68:
        chosen = dict(best[1])
    else:
        chosen = {
            "index": None,
            "title": status.get("playerBarTitle") or status.get("title"),
            "subtitle": status.get("playerBarByline"),
            "primary_artist": str(status.get("playerBarByline") or "").split(" • ", 1)[0].strip(),
            "url": status.get("url"),
        }
    if index_payload:
        matched = match_index_record(index_payload, chosen)
        if matched:
            merged = dict(matched)
            for key, value in chosen.items():
                if merged.get(key) in (None, "", []):
                    merged[key] = value
            chosen = merged
    return chosen, status


def recommended_entry_seconds(record: dict[str, object]) -> float:
    stem_meta = stem_metadata_for_record(record)
    if isinstance(stem_meta, dict):
        analysis = stem_meta.get("analysis")
        if isinstance(analysis, dict):
            value = analysis.get("instrumental_entry_seconds")
            if isinstance(value, (int, float)):
                return float(value)
    structure_meta = structure_metadata_for_record(record)
    if isinstance(structure_meta, dict):
        analysis = structure_meta.get("analysis")
        if isinstance(analysis, dict):
            value = analysis.get("recommended_entry_seconds")
            if isinstance(value, (int, float)):
                return float(value)
    bpm = metric_bpm(record)
    if bpm is None or bpm <= 0:
        return 12.0

    online = record.get("online")
    metrics = online.get("metrics") if isinstance(online, dict) else None
    duration_text = metrics.get("duration") if isinstance(metrics, dict) else None
    beats_per_bar = metrics.get("time_signature_beats") if isinstance(metrics, dict) else None
    total_seconds = None
    if isinstance(duration_text, str) and ":" in duration_text:
        try:
            mins, secs = duration_text.split(":", 1)
            total_seconds = int(mins) * 60 + int(secs)
        except ValueError:
            total_seconds = None

    title = normalize_title(str(record.get("title") or ""))
    has_long_intro = any(token in title for token in ("remix", "mix", "edit", "extended"))
    bars = 16 if has_long_intro or (total_seconds is not None and total_seconds >= 240) else 8
    beats = beats_per_bar if isinstance(beats_per_bar, int) and beats_per_bar > 0 else 4
    seconds = (60.0 / bpm) * beats * bars
    return max(8.0, min(32.0, round(seconds, 2)))


def audio_metrics_for_record(record: dict[str, object]) -> dict[str, object] | None:
    path = audio_metrics_path(record)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _parse_aubio_bpm(output: str) -> tuple[float | None, float | None]:
    bpm = None
    confidence = None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*bpm", line, re.I)
        if match:
            bpm = float(match.group(1))
        confidence_match = re.search(r"confidence[:=]?\s*([0-9]+(?:\.[0-9]+)?)", line, re.I)
        if confidence_match:
            confidence = float(confidence_match.group(1))
    return bpm, confidence


def _parse_onset_times(output: str) -> list[float]:
    onsets: list[float] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            onsets.append(float(line))
        except ValueError:
            continue
    return onsets


def ensure_libkeyfinder_cli() -> Path:
    source = SCRIPT_DIR / "libkeyfinder_cli.cpp"
    binary = LIBKEYFINDER_CLI
    if binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime:
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "clang++",
        "-std=c++17",
        "-O2",
        str(source),
        "-o",
        str(binary),
        "-I/opt/homebrew/include",
        "-L/opt/homebrew/lib",
        "-Wl,-rpath,/opt/homebrew/lib",
        "-lkeyfinder",
        "-lsndfile",
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else str(exc)
        raise SystemExit(f"Failed to build libkeyfinder helper: {stderr}") from exc
    if not binary.exists():
        raise SystemExit(f"libkeyfinder helper was not created: {binary}")
    return binary


def ensure_audio_metrics(record: dict[str, object], *, force: bool = False) -> dict[str, object]:
    import soundfile as sf

    cached = audio_metrics_for_record(record)
    if not force and isinstance(cached, dict) and int(cached.get("version", 0)) == AUDIO_METRICS_VERSION:
        return cached

    source = ensure_track_audio(record)
    audio_info, sample_rate = sf.read(source, always_2d=True)
    channels = int(audio_info.shape[1]) if audio_info.ndim == 2 else 1
    duration_seconds = float(len(audio_info) / sample_rate) if sample_rate else 0.0

    bpm_value = None
    bpm_confidence = None
    tempo_error = None
    if AUBIO_BIN and Path(AUBIO_BIN).exists():
        try:
            tempo_run = subprocess.run(
                [AUBIO_BIN, "tempo", "-i", str(source)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            bpm_value, bpm_confidence = _parse_aubio_bpm(tempo_run.stdout)
        except subprocess.CalledProcessError as exc:
            tempo_error = exc.stderr.strip() if isinstance(exc.stderr, str) else str(exc)
    else:
        tempo_error = f"aubio not found: {AUBIO_BIN}"

    onset_times: list[float] = []
    onset_error = None
    if AUBIO_BIN and Path(AUBIO_BIN).exists():
        try:
            onset_run = subprocess.run(
                ["aubioonset", "-i", str(source)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            onset_times = _parse_onset_times(onset_run.stdout)
        except subprocess.CalledProcessError as exc:
            onset_error = exc.stderr.strip() if isinstance(exc.stderr, str) else str(exc)
    else:
        onset_error = f"aubioonset not found near {AUBIO_BIN}"

    key_value = None
    key_code = None
    key_error = None
    try:
        key_cli = ensure_libkeyfinder_cli()
        key_run = subprocess.run(
            [str(key_cli), str(source)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        key_payload = json.loads(key_run.stdout)
        if isinstance(key_payload, dict):
            key_name = key_payload.get("key")
            key_idx = key_payload.get("key_code")
            if isinstance(key_name, str) and key_name.strip() and key_name.strip().lower() != "silence":
                key_value = key_name.strip()
            if isinstance(key_idx, int):
                key_code = key_idx
    except Exception as exc:
        key_error = str(exc)

    onset_count = len(onset_times)
    onset_density = onset_count / max(duration_seconds, 1e-6) if duration_seconds > 0 else 0.0
    early_onset_density = (
        sum(1 for onset in onset_times if onset <= min(30.0, duration_seconds)) / max(1.0, min(30.0, duration_seconds))
        if onset_times and duration_seconds > 0
        else 0.0
    )
    onset_intervals = [
        max(0.0, onset_times[index + 1] - onset_times[index])
        for index in range(len(onset_times) - 1)
        if onset_times[index + 1] > onset_times[index]
    ]
    onset_interval_median = statistics.median(onset_intervals) if onset_intervals else None

    payload = {
        "version": AUDIO_METRICS_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "track": {
            "title": record.get("title"),
            "primary_artist": record.get("primary_artist") or primary_artist(record),
            "url": record.get("url"),
            "key": stem_track_key(record),
        },
        "source_audio": str(source),
        "tools": {
            "aubio_bin": AUBIO_BIN,
            "libkeyfinder_cli": str(LIBKEYFINDER_CLI),
        },
        "metrics": {
            "duration_seconds": round(duration_seconds, 3),
            "sample_rate": int(sample_rate),
            "channels": channels,
            "bpm": round(float(bpm_value), 3) if isinstance(bpm_value, (int, float)) else None,
            "bpm_confidence": round(float(bpm_confidence), 4) if isinstance(bpm_confidence, (int, float)) else None,
            "key": key_value,
            "key_code": key_code,
            "onset_count": onset_count,
            "onset_density": round(float(onset_density), 5),
            "early_onset_density": round(float(early_onset_density), 5),
            "onset_interval_median": round(float(onset_interval_median), 5) if isinstance(onset_interval_median, (int, float)) else None,
            "first_onsets": [round(value, 4) for value in onset_times[:32]],
        },
        "errors": {
            "tempo": tempo_error,
            "onset": onset_error,
            "key": key_error,
        },
    }
    path = audio_metrics_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def extract_songbpm_metrics(html_text: str) -> dict[str, object] | None:
    title_match = re.search(r"<h1[^>]*>\s*(.*?)\s*</h1>", html_text, re.S)
    artist_match = re.search(r"\|\s*Tempo for .*? by (.*?) \| SongBPM", html_text, re.S)
    metrics_match = re.search(
        r"Song Metrics.*?Key\s*</dt>\s*<dd[^>]*>\s*(.*?)\s*</dd>.*?Duration\s*</dt>\s*<dd[^>]*>\s*(.*?)\s*</dd>.*?Tempo \(BPM\)\s*</dt>\s*<dd[^>]*>\s*(\d{1,3}(?:\.\d+)?)\s*</dd>",
        html_text,
        re.S,
    )
    if not title_match or not metrics_match:
        return None
    energy_match = re.search(r"has\s*<span[^>]*>\s*([^<]+?)\s*</span>\s*energy", html_text, re.S)
    dance_match = re.search(r"and is\s*<span[^>]*>\s*([^<]+?)\s*</span>\s*with a time signature", html_text, re.S)
    time_signature_match = re.search(r"time signature of\s*<span[^>]*>\s*(\d+)\s*beats per bar", html_text, re.S)
    return {
        "title": html.unescape(title_match.group(1).strip()),
        "artist": html.unescape(artist_match.group(1).strip()) if artist_match else None,
        "key": html.unescape(metrics_match.group(1).strip()),
        "duration": html.unescape(metrics_match.group(2).strip()),
        "bpm": float(metrics_match.group(3)),
        "energy_text": html.unescape(energy_match.group(1).strip()) if energy_match else None,
        "danceability_text": html.unescape(dance_match.group(1).strip()) if dance_match else None,
        "time_signature_beats": int(time_signature_match.group(1)) if time_signature_match else None,
    }


def http_get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def songbpm_search_page(query: str) -> str:
    body = urllib.parse.urlencode({"query": query}).encode("utf-8")
    request = urllib.request.Request(
        "https://songbpm.com/searches",
        data=body,
        method="POST",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://songbpm.com",
            "Referer": "https://songbpm.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        response = opener.open(request, timeout=20)
        location = response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = exc.headers.get("Location")
    if not location:
        raise RuntimeError("SongBPM search did not return a redirect location")
    if location.startswith("/"):
        return "https://songbpm.com" + location
    return location


def songbpm_result_url(query: str) -> str | None:
    search_page_url = songbpm_search_page(query)
    search_html = http_get_text(search_page_url)
    candidates = re.findall(r'href="(/@[^"#]+)"', search_html)
    for candidate in candidates:
        if candidate.endswith("/apple-music"):
            continue
        if candidate.count("/") < 2:
            continue
        return "https://songbpm.com" + candidate
    return None


def best_songbpm_match(track: dict[str, object]) -> dict[str, object] | None:
    title = str(track.get("title") or "").strip()
    artist = primary_artist(track)
    query = title + (f" {artist}" if artist else "")
    search_page_url = songbpm_search_page(query)
    search_html = http_get_text(search_page_url)
    seen: set[str] = set()
    candidate_urls: list[str] = []
    for candidate in re.findall(r'href="(/@[^"#]+)"', search_html):
        if candidate.endswith("/apple-music"):
            continue
        if candidate.count("/") < 2:
            continue
        full_url = "https://songbpm.com" + candidate
        if full_url in seen:
            continue
        seen.add(full_url)
        candidate_urls.append(full_url)

    best: dict[str, object] | None = None
    for candidate_url in candidate_urls[:5]:
        try:
            candidate_html = http_get_text(candidate_url)
            metrics = extract_songbpm_metrics(candidate_html)
        except Exception:
            continue
        if metrics is None:
            continue
        title_score = title_match_score(title, str(metrics.get("title") or ""))
        artist_score = artist_match_score(artist, metrics.get("artist"))
        total = 0.8 * title_score + 0.2 * artist_score
        entry = {
            "url": candidate_url,
            "metrics": metrics,
            "title_score": title_score,
            "artist_score": artist_score,
            "match_score": total,
        }
        if best is None or float(entry["match_score"]) > float(best["match_score"]):
            best = entry

    return best


def resolve_online_track_metadata(track: dict[str, object]) -> dict[str, object]:
    title = str(track.get("title") or "").strip()
    artist = primary_artist(track)
    query = title
    if artist:
        query += f" {artist}"

    result: dict[str, object] = {
        "query": query,
        "source": None,
        "url": None,
        "metrics": None,
        "status": "not-found",
    }
    best_match = best_songbpm_match(track)
    if not best_match:
        return result

    result["source"] = "SongBPM"
    result["url"] = best_match["url"]
    result["metrics"] = best_match["metrics"]
    result["match_score"] = round(float(best_match["match_score"]), 4)
    result["title_score"] = round(float(best_match["title_score"]), 4)
    result["artist_score"] = round(float(best_match["artist_score"]), 4)
    if float(best_match["match_score"]) < 0.72:
        result["status"] = "low-confidence"
        return result
    result["status"] = "ok"
    return result


def normalize_title(value: str) -> str:
    normalized = value.lower().strip()
    normalized = normalized.replace("’", "'").replace("‘", "'")
    normalized = normalized.replace("“", '"').replace("”", '"')
    normalized = re.sub(r"\s+\|\s+youtube music$", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def simplify_for_match(value: str) -> str:
    value = normalize_title(value)
    value = value.replace("&", " and ")
    value = value.replace("+", " ")
    value = re.sub(r"[\[\]\(\)\-_/,:;.!?]", " ", value)
    value = re.sub(r"\bfeat(?:uring)?\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def token_set(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", simplify_for_match(value)) if len(token) > 1}


def title_match_score(expected_title: str, candidate_title: str) -> float:
    expected_simple = simplify_for_match(expected_title)
    candidate_simple = simplify_for_match(candidate_title)
    if not expected_simple or not candidate_simple:
        return 0.0
    ratio = difflib.SequenceMatcher(None, expected_simple, candidate_simple).ratio()
    expected_tokens = token_set(expected_title)
    candidate_tokens = token_set(candidate_title)
    overlap = len(expected_tokens & candidate_tokens)
    coverage = overlap / max(1, len(expected_tokens))
    precision = overlap / max(1, len(candidate_tokens))
    return 0.5 * ratio + 0.35 * coverage + 0.15 * precision


def artist_match_score(expected_artist: str, candidate_artist: str | None) -> float:
    if not expected_artist or not candidate_artist:
        return 0.0
    expected_simple = simplify_for_match(expected_artist)
    candidate_simple = simplify_for_match(candidate_artist)
    if not expected_simple or not candidate_simple:
        return 0.0
    ratio = difflib.SequenceMatcher(None, expected_simple, candidate_simple).ratio()
    expected_tokens = token_set(expected_artist)
    candidate_tokens = token_set(candidate_artist)
    overlap = len(expected_tokens & candidate_tokens)
    coverage = overlap / max(1, len(expected_tokens))
    return 0.65 * ratio + 0.35 * coverage


def load_index(path: Path) -> dict[str, object]:
    if not path.exists():
        raise SystemExit(f"Index file not found: {path}")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise SystemExit(f"Invalid index payload in {path}")
    return payload


def metric_bpm(record: dict[str, object]) -> float | None:
    local_audio = audio_metrics_for_record(record)
    local_audio_bpm = None
    local_audio_confidence = None
    if isinstance(local_audio, dict):
        metrics = local_audio.get("metrics")
        if isinstance(metrics, dict):
            value = metrics.get("bpm")
            if isinstance(value, (int, float)):
                local_audio_bpm = float(value)
            confidence_value = metrics.get("bpm_confidence")
            if isinstance(confidence_value, (int, float)):
                local_audio_confidence = float(confidence_value)
    online = record.get("online")
    online_bpm = None
    if isinstance(online, dict) and online.get("status") == "ok":
        metrics = online.get("metrics")
        if isinstance(metrics, dict):
            value = metrics.get("bpm")
            if isinstance(value, (int, float)):
                online_bpm = float(value)
    local_scan = record.get("local_scan")
    local_bpm = None
    if isinstance(local_scan, dict):
        bpm_payload = local_scan.get("bpm")
        if isinstance(bpm_payload, dict):
            value = bpm_payload.get("bpm")
            if isinstance(value, (int, float)):
                local_bpm = float(value)
    if online_bpm is not None and local_audio_bpm is not None and abs(online_bpm - local_audio_bpm) > 3.0:
        if local_audio_confidence is None or local_audio_confidence >= 0.1:
            return local_audio_bpm
    if local_bpm is not None and local_audio_bpm is not None and abs(local_bpm - local_audio_bpm) > 3.0:
        if local_audio_confidence is None or local_audio_confidence >= 0.1:
            return local_audio_bpm
    if online_bpm is not None and local_bpm is not None and abs(online_bpm - local_bpm) > 3.0:
        return local_bpm
    if local_audio_bpm is not None:
        return local_audio_bpm
    if online_bpm is not None:
        return online_bpm
    if local_bpm is not None:
        return local_bpm
    structure = structure_metadata_for_record(record)
    analysis = structure.get("analysis") if isinstance(structure, dict) else None
    if isinstance(analysis, dict):
        value = analysis.get("tempo_bpm")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def metric_key(record: dict[str, object]) -> str | None:
    local_audio = audio_metrics_for_record(record)
    if isinstance(local_audio, dict):
        metrics = local_audio.get("metrics")
        if isinstance(metrics, dict):
            value = metrics.get("key")
            if isinstance(value, str) and value.strip():
                return value.strip()
    online = record.get("online")
    if isinstance(online, dict) and online.get("status") == "ok":
        metrics = online.get("metrics")
        if isinstance(metrics, dict):
            value = metrics.get("key")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def metric_danceability(record: dict[str, object]) -> str | None:
    online = record.get("online")
    if isinstance(online, dict) and online.get("status") == "ok":
        metrics = online.get("metrics")
        if isinstance(metrics, dict):
            value = metrics.get("danceability_text")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def stem_metadata_for_record(record: dict[str, object]) -> dict[str, object] | None:
    path = stem_metadata_path(record)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def structure_metadata_path(record: dict[str, object]) -> Path:
    return stem_track_dir(record) / "structure_analysis.json"


def structure_metadata_for_record(record: dict[str, object]) -> dict[str, object] | None:
    path = structure_metadata_path(record)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def vocal_cue_metadata_for_record(record: dict[str, object]) -> dict[str, object] | None:
    path = vocal_cue_path(record)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def ensure_track_audio(record: dict[str, object]) -> Path:
    source = stem_source_path(record)
    if source.exists() and source.stat().st_size > 0:
        return source
    url = str(record.get("url") or "")
    if not url:
        raise SystemExit("Track URL is missing; cannot download source audio")
    source.parent.mkdir(parents=True, exist_ok=True)
    command = [
        YT_DLP_BIN,
        "--no-playlist",
        "-x",
        "--audio-format",
        "wav",
        "-o",
        str(source),
        url,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        if not (source.exists() and source.stat().st_size > 0):
            stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else str(exc)
            raise SystemExit(f"yt-dlp failed for {url}: {stderr}") from exc
    if not source.exists():
        raise SystemExit(f"yt-dlp completed but source file was not created: {source}")
    return source


def ensure_preview_audio(record: dict[str, object], *, preview_seconds: float = STEM_PREVIEW_SECONDS) -> Path:
    preview = stem_preview_path(record)
    if preview.exists() and preview.stat().st_size > 0:
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(preview),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            duration = float((probe.stdout or "0").strip() or 0.0)
        except Exception:
            duration = 0.0
        if 0.0 < duration <= preview_seconds + 0.75:
            return preview
    preview.parent.mkdir(parents=True, exist_ok=True)
    source = ensure_track_audio(record)
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-t",
        f"{float(preview_seconds):.3f}",
        str(preview),
    ]
    subprocess.run(command, check=True)
    if not preview.exists():
        raise SystemExit(f"ffmpeg completed but preview audio was not created: {preview}")
    return preview


def ensure_track_stems(record: dict[str, object], *, model: str = "htdemucs") -> dict[str, Path]:
    source = ensure_preview_audio(record)
    stems = stem_paths(record)
    if all(path.exists() and path.stat().st_size > 0 for path in stems.values()):
        return stems
    out_dir = stem_track_dir(record) / "preview_demucs"
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "demucs.separate",
        "-n",
        model,
        "-d",
        "cpu",
        "-o",
        str(out_dir),
        str(source),
    ]
    subprocess.run(command, check=True)
    missing = [name for name, path in stems.items() if not path.exists()]
    if missing:
        raise SystemExit(f"Demucs finished but stems are missing: {', '.join(missing)}")
    return stems


def _window_slice(values, times, start_seconds: float, end_seconds: float):
    import numpy as np

    mask = (times >= start_seconds) & (times < end_seconds)
    selected = values[mask]
    if selected.size:
        return selected
    nearest = int(np.argmin(np.abs(times - start_seconds)))
    return values[max(0, nearest - 1):nearest + 2]


def _safe_percentile(values, q: float, fallback: float) -> float:
    import numpy as np

    if values.size == 0:
        return fallback
    return float(np.percentile(values, q))


def _classify_window(window: dict[str, float]) -> str:
    if window["outgoing_score"] >= max(window["incoming_score"], 1.0):
        if window["position_ratio"] >= 0.7:
            return "outro"
        if window["valley_score"] >= 0.45:
            return "breakdown"
        return "release"
    if window["entry_score"] >= 0.65 and window["position_ratio"] <= 0.25:
        return "intro"
    if window["build_score"] >= 0.35 and window["drop_score"] >= 0.2:
        return "build-drop"
    if window["entry_sparsity"] >= 0.55:
        return "instrumental-entry"
    return "phrase"


def _normalize_pattern(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    energy = math.sqrt(sum(value * value for value in centered))
    if energy <= 1e-9:
        return [0.0 for _ in centered]
    return [value / energy for value in centered]


def bass_pattern_alignment(source_pattern: list[float], target_pattern: list[float], *, max_shift: int = 2) -> dict[str, float]:
    if not source_pattern or not target_pattern:
        return {"score": 0.0, "shift": 0.0}
    length = min(len(source_pattern), len(target_pattern))
    if length <= 1:
        return {"score": 0.0, "shift": 0.0}
    source = _normalize_pattern(source_pattern[:length])
    target = _normalize_pattern(target_pattern[:length])
    best_score = -1.0
    best_shift = 0
    for shift in range(-max_shift, max_shift + 1):
        aligned_source: list[float] = []
        aligned_target: list[float] = []
        for index in range(length):
            j = index + shift
            if 0 <= j < length:
                aligned_source.append(source[index])
                aligned_target.append(target[j])
        if len(aligned_source) < 2:
            continue
        aligned_source = _normalize_pattern(aligned_source)
        aligned_target = _normalize_pattern(aligned_target)
        score = sum(a * b for a, b in zip(aligned_source, aligned_target))
        if score > best_score:
            best_score = score
            best_shift = shift
    return {"score": max(0.0, round(best_score, 4)), "shift": float(best_shift)}


def analyze_track_structure(record: dict[str, object], *, force: bool = False) -> dict[str, object]:
    import librosa
    import numpy as np
    from scipy import signal as scipy_signal

    cached = structure_metadata_for_record(record)
    if (
        not force
        and isinstance(cached, dict)
        and int(cached.get("version", 0)) == STRUCTURE_ANALYSIS_VERSION
    ):
        return cached

    source = ensure_track_audio(record)
    audio, sample_rate = librosa.load(str(source), sr=22050, mono=True)
    if audio.size == 0:
        raise SystemExit(f"Unable to analyze empty audio file: {source}")

    hop_length = 512
    frame_length = 2048
    duration_seconds = float(audio.size / sample_rate)
    rms = librosa.feature.rms(y=audio, frame_length=frame_length, hop_length=hop_length)[0]
    onset_env = librosa.onset.onset_strength(y=audio, sr=sample_rate, hop_length=hop_length)
    centroid = librosa.feature.spectral_centroid(y=audio, sr=sample_rate, hop_length=hop_length)[0]
    band_sos = scipy_signal.butter(4, [35.0, 180.0], btype="bandpass", fs=sample_rate, output="sos")
    low_band = scipy_signal.sosfiltfilt(band_sos, audio).astype("float32", copy=False)
    low_rms = librosa.feature.rms(y=low_band, frame_length=frame_length, hop_length=hop_length)[0]
    low_flux = np.maximum(0.0, np.diff(low_rms, prepend=low_rms[:1]))
    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=sample_rate, hop_length=hop_length)

    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env,
        sr=sample_rate,
        hop_length=hop_length,
    )
    synthetic_beats = False
    try:
        tempo_value = float(tempo)
    except TypeError:
        import numpy as np

        tempo_value = float(np.asarray(tempo).reshape(-1)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sample_rate, hop_length=hop_length)
    metric = metric_bpm(record)
    if beat_times.size < 24:
        fallback_bpm = metric or (tempo_value if tempo_value > 0 else 120.0)
        beat_interval = 60.0 / max(1e-6, fallback_bpm)
        beat_times = np.arange(0.0, duration_seconds, beat_interval, dtype=np.float32)
        tempo_value = fallback_bpm
        synthetic_beats = True

    rms_ref = max(_safe_percentile(rms, 90, 1e-4), 1e-4)
    onset_ref = max(_safe_percentile(onset_env, 90, 1e-4), 1e-4)
    centroid_ref = max(_safe_percentile(centroid, 90, 1.0), 1.0)
    low_rms_ref = max(_safe_percentile(low_rms, 90, 1e-4), 1e-4)
    low_flux_ref = max(_safe_percentile(low_flux, 90, 1e-4), 1e-4)

    beat_low_levels: list[float] = []
    beat_kick_levels: list[float] = []
    for beat_index in range(len(beat_times) - 1):
        beat_start = float(beat_times[beat_index])
        beat_end = float(beat_times[beat_index + 1])
        beat_chunk = _window_slice(low_rms, frame_times, beat_start, beat_end)
        kick_chunk = _window_slice(low_flux, frame_times, beat_start, beat_end)
        beat_low_levels.append(float(np.mean(beat_chunk) / low_rms_ref) if beat_chunk.size else 0.0)
        beat_kick_levels.append(float(np.mean(kick_chunk) / low_flux_ref) if kick_chunk.size else 0.0)

    stem_meta = stem_metadata_for_record(record)
    stem_candidates = []
    if isinstance(stem_meta, dict):
        stem_analysis = stem_meta.get("analysis")
        if isinstance(stem_analysis, dict):
            raw_candidates = stem_analysis.get("candidates")
            if isinstance(raw_candidates, list):
                stem_candidates = [item for item in raw_candidates if isinstance(item, dict)]

    windows: list[dict[str, float | str]] = []
    stride = 4
    beats_per_phrase = 8
    for index in range(beats_per_phrase, max(beats_per_phrase, len(beat_times) - beats_per_phrase), stride):
        boundary = float(beat_times[index])
        prev_start = float(beat_times[index - beats_per_phrase])
        next_end = float(beat_times[min(index + beats_per_phrase, len(beat_times) - 1)])
        prev_rms = float(np.mean(_window_slice(rms, frame_times, prev_start, boundary)) / rms_ref)
        next_rms = float(np.mean(_window_slice(rms, frame_times, boundary, next_end)) / rms_ref)
        prev_onset = float(np.mean(_window_slice(onset_env, frame_times, prev_start, boundary)) / onset_ref)
        next_onset = float(np.mean(_window_slice(onset_env, frame_times, boundary, next_end)) / onset_ref)
        prev_centroid = float(np.mean(_window_slice(centroid, frame_times, prev_start, boundary)) / centroid_ref)
        next_centroid = float(np.mean(_window_slice(centroid, frame_times, boundary, next_end)) / centroid_ref)
        prev_low_rms = float(np.mean(_window_slice(low_rms, frame_times, prev_start, boundary)) / low_rms_ref)
        next_low_rms = float(np.mean(_window_slice(low_rms, frame_times, boundary, next_end)) / low_rms_ref)
        prev_low_flux = float(np.mean(_window_slice(low_flux, frame_times, prev_start, boundary)) / low_flux_ref)
        next_low_flux = float(np.mean(_window_slice(low_flux, frame_times, boundary, next_end)) / low_flux_ref)
        position_ratio = boundary / max(duration_seconds, 1e-6)
        valley_score = max(0.0, prev_rms - next_rms) + 0.8 * max(0.0, prev_onset - next_onset)
        drop_score = max(0.0, next_rms - prev_rms) + 0.65 * max(0.0, next_onset - prev_onset)
        build_score = 0.55 * max(0.0, next_onset - prev_onset) + 0.35 * max(0.0, next_centroid - prev_centroid)
        entry_sparsity = max(0.0, 1.1 - next_onset) + 0.6 * max(0.0, 1.0 - next_rms)
        outro_bias = max(0.0, min(1.0, (position_ratio - 0.62) / 0.28))
        intro_bias = max(0.0, min(1.0, (0.3 - position_ratio) / 0.3))
        stem_bonus = 0.0
        vocal_penalty = 0.0
        stem_vocals = 0.0
        stem_instrumental_score = 0.0
        if stem_candidates:
            nearest = min(stem_candidates, key=lambda item: abs(float(item.get("start_seconds", 0.0)) - boundary))
            if abs(float(nearest.get("start_seconds", 0.0)) - boundary) <= 6.0:
                stem_instrumental_score = max(0.0, float(nearest.get("instrumental_score", 0.0)))
                stem_vocals = max(0.0, float(nearest.get("vocals", 0.0)))
                stem_bonus = stem_instrumental_score * 0.18
                vocal_penalty = max(0.0, stem_vocals - 0.55) * 0.45
        bass_release = max(0.0, prev_low_rms - next_low_rms) + 0.8 * max(0.0, prev_low_flux - next_low_flux)
        bass_pickup = max(0.0, next_low_rms - prev_low_rms) + 0.8 * max(0.0, next_low_flux - prev_low_flux)
        outgoing_score = 1.15 * valley_score + 0.95 * bass_release + 0.8 * outro_bias + 0.28 * entry_sparsity - 0.4 * vocal_penalty
        incoming_score = 0.85 * intro_bias + 0.72 * build_score + 0.62 * entry_sparsity + 0.3 * drop_score + 0.95 * bass_pickup + stem_bonus - 0.25 * vocal_penalty
        entry_score = incoming_score
        beat_index = max(0, min(index, len(beat_low_levels)))
        outgoing_bass_pattern = [round(value, 4) for value in beat_low_levels[max(0, beat_index - beats_per_phrase):beat_index]]
        incoming_bass_pattern = [round(value, 4) for value in beat_low_levels[beat_index:min(len(beat_low_levels), beat_index + beats_per_phrase)]]
        outgoing_kick_pattern = [round(value, 4) for value in beat_kick_levels[max(0, beat_index - beats_per_phrase):beat_index]]
        incoming_kick_pattern = [round(value, 4) for value in beat_kick_levels[beat_index:min(len(beat_kick_levels), beat_index + beats_per_phrase)]]
        window = {
            "boundary_seconds": round(boundary, 2),
            "prev_start_seconds": round(prev_start, 2),
            "next_end_seconds": round(next_end, 2),
            "position_ratio": round(position_ratio, 4),
            "prev_rms": round(prev_rms, 4),
            "next_rms": round(next_rms, 4),
            "prev_onset": round(prev_onset, 4),
            "next_onset": round(next_onset, 4),
            "prev_centroid": round(prev_centroid, 4),
            "next_centroid": round(next_centroid, 4),
            "prev_low_rms": round(prev_low_rms, 4),
            "next_low_rms": round(next_low_rms, 4),
            "prev_low_flux": round(prev_low_flux, 4),
            "next_low_flux": round(next_low_flux, 4),
            "valley_score": round(valley_score, 4),
            "drop_score": round(drop_score, 4),
            "build_score": round(build_score, 4),
            "bass_release": round(bass_release, 4),
            "bass_pickup": round(bass_pickup, 4),
            "entry_sparsity": round(entry_sparsity, 4),
            "entry_score": round(entry_score, 4),
            "outgoing_score": round(outgoing_score, 4),
            "incoming_score": round(incoming_score, 4),
            "stem_bonus": round(stem_bonus, 4),
            "vocal_penalty": round(vocal_penalty, 4),
            "stem_vocals": round(stem_vocals, 4),
            "stem_instrumental_score": round(stem_instrumental_score, 4),
            "outgoing_bass_pattern": outgoing_bass_pattern,
            "incoming_bass_pattern": incoming_bass_pattern,
            "outgoing_kick_pattern": outgoing_kick_pattern,
            "incoming_kick_pattern": incoming_kick_pattern,
        }
        window["label"] = _classify_window(window)  # type: ignore[index]
        windows.append(window)

    top_outgoing = sorted(windows, key=lambda item: float(item["outgoing_score"]), reverse=True)[:8]
    top_incoming = sorted(windows, key=lambda item: float(item["incoming_score"]), reverse=True)[:8]
    best_entry = top_incoming[0]["boundary_seconds"] if top_incoming else recommended_entry_seconds(record)
    metadata = {
        "version": STRUCTURE_ANALYSIS_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "track": {
            "title": record.get("title"),
            "primary_artist": record.get("primary_artist") or primary_artist(record),
            "url": record.get("url"),
            "key": stem_track_key(record),
        },
        "source_audio": str(source),
        "analysis": {
            "sample_rate": sample_rate,
            "duration_seconds": round(duration_seconds, 3),
            "tempo_bpm": round(float(tempo_value), 3),
            "beat_count": int(beat_times.size),
            "synthetic_beats": synthetic_beats,
            "recommended_entry_seconds": best_entry,
            "top_outgoing_windows": top_outgoing,
            "top_incoming_windows": top_incoming,
        },
    }
    path = structure_metadata_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def detect_transition_windows_between(
    source_record: dict[str, object],
    target_record: dict[str, object],
    *,
    force: bool = False,
    prepare_vocals: bool = False,
) -> dict[str, object]:
    source_structure = analyze_track_structure(source_record, force=force)
    target_structure = analyze_track_structure(target_record, force=force)
    source_analysis = source_structure.get("analysis") if isinstance(source_structure.get("analysis"), dict) else {}
    target_analysis = target_structure.get("analysis") if isinstance(target_structure.get("analysis"), dict) else {}
    source_windows = source_analysis.get("top_outgoing_windows") if isinstance(source_analysis, dict) else []
    target_windows = target_analysis.get("top_incoming_windows") if isinstance(target_analysis, dict) else []
    if not isinstance(source_windows, list) or not isinstance(target_windows, list):
        raise SystemExit("Missing structure windows for pair detection")

    normalized = normalize_bpm_pair(metric_bpm(source_record), metric_bpm(target_record))
    bpm_delta = normalized.get("delta")
    if bpm_delta is None:
        bpm_score = 0.0
    else:
        bpm_score = max(0.0, 1.0 - min(float(bpm_delta), 12.0) / 12.0)
    key_score, key_reason = key_compatibility_score(metric_key(source_record), metric_key(target_record))
    source_synthetic = bool(source_analysis.get("synthetic_beats"))
    target_synthetic = bool(target_analysis.get("synthetic_beats"))
    structure_confidence_penalty = 0.2 * int(source_synthetic) + 0.2 * int(target_synthetic)
    vocal_matches = _vocal_transition_matches(source_record, target_record, prepare=prepare_vocals)
    best_wordplay = vocal_matches.get("wordplay") if isinstance(vocal_matches, dict) else None
    best_stutter = vocal_matches.get("stutter") if isinstance(vocal_matches, dict) else None
    best_pause = vocal_matches.get("pause_punch") if isinstance(vocal_matches, dict) else None
    best_switch = vocal_matches.get("same_song_switchup") if isinstance(vocal_matches, dict) else None

    pairings: list[dict[str, object]] = []
    for source_window in source_windows[:6]:
        for target_window in target_windows[:6]:
            bass_alignment = bass_pattern_alignment(
                [float(value) for value in source_window.get("outgoing_bass_pattern", [])],
                [float(value) for value in target_window.get("incoming_bass_pattern", [])],
            )
            kick_alignment = bass_pattern_alignment(
                [float(value) for value in source_window.get("outgoing_kick_pattern", [])],
                [float(value) for value in target_window.get("incoming_kick_pattern", [])],
                max_shift=1,
            )
            bass_swap_shape = (
                0.7 * max(0.0, float(source_window.get("bass_release", 0.0)))
                + 0.7 * max(0.0, float(target_window.get("bass_pickup", 0.0)))
                + 0.35 * max(0.0, float(source_window.get("prev_low_rms", 0.0)) - float(target_window.get("prev_low_rms", 0.0)))
            )
            vocal_clash = max(
                0.0,
                float(source_window.get("stem_vocals", 0.0)) + float(target_window.get("stem_vocals", 0.0)) - 0.78,
            )
            source_label = str(source_window.get("label") or "")
            target_label = str(target_window.get("label") or "")
            section_match = 0.0
            if source_label == "outro" and target_label == "intro":
                section_match = 1.0
            elif source_label in {"outro", "breakdown", "release"} and target_label in {"intro", "instrumental-entry"}:
                section_match = 0.84
            elif source_label == "breakdown" and target_label == "build-drop":
                section_match = 0.7
            elif source_label == "release" and target_label in {"instrumental-entry", "intro"}:
                section_match = 0.62
            elif target_label == "instrumental-entry":
                section_match = 0.5
            overlap_risk = (
                0.55 * max(0.0, float(source_window.get("next_rms", 0.0)) - 0.85)
                + 0.45 * max(0.0, float(target_window.get("next_rms", 0.0)) - 0.95)
                + 0.35 * max(0.0, float(source_window.get("next_onset", 0.0)) + float(target_window.get("next_onset", 0.0)) - 1.75)
                + 0.25 * max(0.0, float(source_window.get("next_low_rms", 0.0)) + float(target_window.get("next_low_rms", 0.0)) - 1.55)
            )
            handoff_bonus = 0.0
            if source_label in {"outro", "breakdown", "release"}:
                handoff_bonus += 0.35
            if target_label in {"intro", "instrumental-entry", "build-drop"}:
                handoff_bonus += 0.35
            stability_score = max(
                0.0,
                1.0
                - 0.75 * overlap_risk
                - 0.8 * vocal_clash
                - 0.35 * structure_confidence_penalty,
            )
            loop_roll_score = max(
                0.0,
                0.34 * float(source_window.get("prev_onset", 0.0))
                + 0.28 * float(source_window.get("prev_low_flux", 0.0))
                + 0.22 * float(target_window.get("entry_sparsity", 0.0))
                + 0.16 * (1.0 - min(1.0, overlap_risk)),
            )
            echo_out_score = max(
                0.0,
                0.42 * float(source_window.get("valley_score", 0.0))
                + 0.24 * float(source_window.get("bass_release", 0.0))
                + 0.18 * float(target_window.get("entry_sparsity", 0.0))
                + 0.16 * max(0.0, 1.0 - vocal_clash),
            )
            brake_score = max(
                0.0,
                0.3 * float(source_window.get("prev_rms", 0.0))
                + 0.25 * float(target_window.get("drop_score", 0.0))
                + 0.2 * float(target_window.get("incoming_score", 0.0))
                + 0.15 * max(0.0, 1.0 - bpm_score)
                + 0.1 * overlap_risk,
            )
            transform_score = max(
                0.0,
                0.36 * overlap_risk
                + 0.28 * vocal_clash
                + 0.18 * float(source_window.get("prev_onset", 0.0))
                + 0.18 * float(target_window.get("next_onset", 0.0)),
            )
            clean_score = max(
                0.0,
                0.32 * section_match
                + 0.16 * bpm_score
                + 0.12 * key_score
                + 0.16 * float(bass_alignment.get("score", 0.0))
                + 0.12 * float(kick_alignment.get("score", 0.0))
                + 0.12 * stability_score
                - 0.18 * vocal_clash
                - 0.12 * overlap_risk,
            )
            build_drop_score = max(
                0.0,
                0.38 * float(source_window.get("build_score", 0.0))
                + 0.3 * float(target_window.get("drop_score", 0.0))
                + 0.18 * bpm_score
                + 0.14 * key_score,
            )
            score = (
                0.42 * float(source_window.get("outgoing_score", 0.0))
                + 0.48 * float(target_window.get("incoming_score", 0.0))
                + 0.3 * float(bass_alignment.get("score", 0.0))
                + 0.26 * float(kick_alignment.get("score", 0.0))
                + 0.24 * bass_swap_shape
                + 0.35 * bpm_score
                + 0.15 * key_score
                + 0.42 * section_match
                + handoff_bonus
                - 0.55 * overlap_risk
                - 0.4 * vocal_clash
                - structure_confidence_penalty
            )
            confidence = max(
                0.0,
                min(
                    1.0,
                    0.3 * bpm_score
                    + 0.16 * key_score
                    + 0.24 * section_match
                    + 0.14 * float(bass_alignment.get("score", 0.0))
                    + 0.12 * float(kick_alignment.get("score", 0.0))
                    + 0.12 * stability_score,
                ),
            )
            transition_type = "cover-transform"
            execution_preset = "transform-swap"
            execution_support = "native"
            transition_family = "cover"
            rationale: list[str] = []
            wordplay_score = float(best_wordplay.get("score", 0.0)) if isinstance(best_wordplay, dict) else 0.0
            stutter_score = float(best_stutter.get("score", 0.0)) if isinstance(best_stutter, dict) else 0.0
            pause_score = float(best_pause.get("score", 0.0)) if isinstance(best_pause, dict) else 0.0
            same_song_score = float(best_switch.get("score", 0.0)) if isinstance(best_switch, dict) else 0.0

            if same_song_score >= 0.82 and title_similarity_bonus(str(source_record.get("title") or ""), str(target_record.get("title") or "")) >= 0.06:
                transition_type = "same-song-switchup"
                execution_preset = "transform-swap"
                execution_support = "fallback"
                transition_family = "vocal"
                rationale.append("matched same-song/version-style vocal cue")
            elif wordplay_score >= 0.76 and vocal_clash <= 0.2:
                transition_type = "wordplay-cut"
                execution_preset = "transform-swap"
                execution_support = "fallback"
                transition_family = "vocal"
                rationale.append("high lexical/cadence vocal match")
            elif stutter_score >= 0.72 and overlap_risk <= 0.62:
                transition_type = "stutter-cut"
                execution_preset = "transform-swap"
                execution_support = "fallback"
                transition_family = "vocal"
                rationale.append("strong stutter-ready source cue with target punch-in")
            elif pause_score >= 0.72 and overlap_risk <= 0.45:
                transition_type = "pause-and-punch-in"
                execution_preset = "staged-swap"
                execution_support = "fallback"
                transition_family = "vocal"
                rationale.append("pause exit aligns with target vocal start")
            elif (
                bpm_delta is not None
                and bpm_delta <= 3
                and section_match >= 0.72
                and overlap_risk <= 0.42
                and float(bass_alignment.get("score", 0.0)) >= 0.18
                and float(kick_alignment.get("score", 0.0)) >= 0.22
                and vocal_clash <= 0.08
            ):
                transition_type = "automix-bass-swap"
                execution_preset = "bass-swap"
                transition_family = "automix"
                rationale.append("tight bpm/section/bass fit")
            elif build_drop_score >= 0.78 and bpm_score >= 0.56:
                transition_type = "automix-build-drop"
                execution_preset = "transform-swap"
                execution_support = "fallback"
                transition_family = "automix"
                rationale.append("build-drop structure lines up")
            elif (
                section_match >= 0.62
                and stability_score >= 0.5
                and overlap_risk <= 0.34
                and vocal_clash <= 0.12
                and target_label in {"intro", "instrumental-entry"}
            ):
                transition_type = "automix-clean"
                execution_preset = "staged-swap"
                transition_family = "automix"
                rationale.append("stable section-aware handoff")
            elif clean_score >= 0.52 and overlap_risk <= 0.28:
                transition_type = "automix-crossfade"
                execution_preset = "staged-swap"
                transition_family = "automix"
                rationale.append("clean pair but not a bass-swap")
            elif echo_out_score >= max(loop_roll_score, transform_score) and echo_out_score >= 0.7:
                transition_type = "cover-echo-out"
                execution_preset = "loop-roll-swap"
                execution_support = "fallback"
                transition_family = "cover"
                rationale.append("release/valley favors echo-style exit")
            elif brake_score >= max(loop_roll_score, transform_score) and brake_score >= 0.78:
                transition_type = "cover-brake"
                execution_preset = "transform-swap"
                execution_support = "fallback"
                transition_family = "cover"
                rationale.append("hard reset score beats blend score")
            elif loop_roll_score >= transform_score and loop_roll_score >= 0.55:
                transition_type = "cover-loop-roll"
                execution_preset = "loop-roll-swap"
                transition_family = "cover"
                rationale.append("periodic outgoing slice suitable for loop roll")
            elif confidence < 0.22 and clean_score < 0.4 and loop_roll_score < 0.45 and transform_score < 0.45:
                transition_type = "automix-none"
                execution_preset = "simple-transition"
                execution_support = "fallback"
                transition_family = "automix"
                rationale.append("confidence too low for stylized transition")
            else:
                transition_type = "cover-transform"
                execution_preset = "transform-swap"
                transition_family = "cover"
                rationale.append("mask overlap with transform-style cover")
            pairings.append(
                {
                    "score": round(score, 4),
                    "recommended_transition": execution_preset,
                    "transition_family": transition_family,
                    "transition_type": transition_type,
                    "execution_preset": execution_preset,
                    "execution_support": execution_support,
                    "source_boundary_seconds": source_window.get("boundary_seconds"),
                    "target_entry_seconds": target_window.get("boundary_seconds"),
                    "source_label": source_label,
                    "target_label": target_label,
                    "section_match_score": round(section_match, 4),
                    "stability_score": round(stability_score, 4),
                    "confidence": round(confidence, 4),
                    "overlap_risk": round(overlap_risk, 4),
                    "bass_alignment_score": round(float(bass_alignment.get("score", 0.0)), 4),
                    "bass_alignment_shift_beats": round(float(bass_alignment.get("shift", 0.0)), 4),
                    "kick_alignment_score": round(float(kick_alignment.get("score", 0.0)), 4),
                    "kick_alignment_shift_beats": round(float(kick_alignment.get("shift", 0.0)), 4),
                    "bass_swap_shape": round(bass_swap_shape, 4),
                    "vocal_clash_score": round(vocal_clash, 4),
                    "clean_handoff_score": round(clean_score, 4),
                    "build_drop_score": round(build_drop_score, 4),
                    "loop_roll_score": round(loop_roll_score, 4),
                    "echo_out_score": round(echo_out_score, 4),
                    "brake_score": round(brake_score, 4),
                    "transform_cover_score": round(transform_score, 4),
                    "wordplay_score": round(wordplay_score, 4),
                    "stutter_score": round(stutter_score, 4),
                    "pause_punch_score": round(pause_score, 4),
                    "same_song_switch_score": round(same_song_score, 4),
                    "structure_confidence_penalty": round(structure_confidence_penalty, 4),
                    "bpm_delta": bpm_delta,
                    "key_reason": key_reason,
                    "rationale": rationale,
                    "wordplay_match": best_wordplay,
                    "stutter_match": best_stutter,
                    "pause_punch_match": best_pause,
                    "same_song_switch_match": best_switch,
                    "source_window": source_window,
                    "target_window": target_window,
                }
            )
    pairings.sort(key=lambda item: float(item["score"]), reverse=True)
    return {
        "source_track": {
            "title": source_record.get("title"),
            "bpm": metric_bpm(source_record),
            "key": metric_key(source_record),
        },
        "target_track": {
            "title": target_record.get("title"),
            "bpm": metric_bpm(target_record),
            "key": metric_key(target_record),
        },
        "normalized_bpm": normalized,
        "key_reason": key_reason,
        "vocal_matches": vocal_matches,
        "pairings": pairings[:8],
        "source_structure": source_structure,
        "target_structure": target_structure,
    }


def analyze_prepared_stems(record: dict[str, object]) -> dict[str, object]:
    import numpy as np
    import soundfile as sf

    stems = ensure_track_stems(record)
    buffers: dict[str, np.ndarray] = {}
    sample_rate = None
    for name, path in stems.items():
        audio, sr = sf.read(path, always_2d=True)
        if sample_rate is None:
            sample_rate = int(sr)
        elif int(sr) != sample_rate:
            raise SystemExit(f"Stem sample-rate mismatch in {path}")
        buffers[name] = audio.mean(axis=1).astype("float32", copy=False)

    if sample_rate is None:
        raise SystemExit("Could not read prepared stems")
    duration = min(len(buffer) for buffer in buffers.values()) / sample_rate
    bpm = metric_bpm(record)
    bar_seconds = (60.0 / bpm) * 4 if bpm and bpm > 0 else 2.0
    window_seconds = min(max(bar_seconds * 4, 6.0), 16.0)
    hop_seconds = min(max(bar_seconds, 1.0), 4.0)
    search_start = 8.0
    search_end = min(max(duration * 0.55, 40.0), max(search_start + window_seconds, duration - window_seconds))

    refs: dict[str, float] = {}
    for name, buffer in buffers.items():
        rms = np.sqrt(np.maximum(1e-12, np.convolve(buffer * buffer, np.ones(2048) / 2048, mode="valid")))
        refs[name] = float(np.percentile(rms, 85)) if rms.size else 1e-4
        refs[name] = max(refs[name], 1e-4)

    candidates: list[dict[str, float]] = []
    position = search_start
    while position + window_seconds <= search_end + 1e-6:
        start = int(position * sample_rate)
        stop = int((position + window_seconds) * sample_rate)
        features: dict[str, float] = {}
        for name, buffer in buffers.items():
            chunk = buffer[start:stop]
            rms = float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0
            features[name] = rms / refs[name]
        rhythm = 1.25 * features["drums"] + 0.85 * features["bass"] + 0.7 * features["other"]
        vocal_penalty = 2.3 * features["vocals"]
        quiet_penalty = max(0.0, 0.8 - (features["drums"] + features["other"])) * 1.8
        instrumental_score = rhythm - vocal_penalty - quiet_penalty
        candidates.append(
            {
                "start_seconds": round(position, 2),
                "end_seconds": round(position + window_seconds, 2),
                "instrumental_score": round(instrumental_score, 4),
                "vocals": round(features["vocals"], 4),
                "drums": round(features["drums"], 4),
                "bass": round(features["bass"], 4),
                "other": round(features["other"], 4),
            }
        )
        position += hop_seconds

    ranked = sorted(candidates, key=lambda item: item["instrumental_score"], reverse=True)
    best = ranked[0] if ranked else {"start_seconds": 12.0}
    metadata = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "track": {
            "title": record.get("title"),
            "primary_artist": record.get("primary_artist") or primary_artist(record),
            "url": record.get("url"),
            "key": stem_track_key(record),
        },
        "source_audio": str(stem_source_path(record)),
        "preview_audio": str(stem_preview_path(record)),
        "stems": {name: str(path) for name, path in stems.items()},
        "analysis": {
            "sample_rate": sample_rate,
            "duration_seconds": round(duration, 3),
            "preview_seconds": STEM_PREVIEW_SECONDS,
            "window_seconds": round(window_seconds, 3),
            "hop_seconds": round(hop_seconds, 3),
            "instrumental_entry_seconds": best["start_seconds"],
            "candidates": ranked[:8],
        },
    }
    metadata_path = stem_metadata_path(record)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def ensure_vocal_cues(
    record: dict[str, object],
    *,
    force: bool = False,
    prepare: bool = False,
) -> dict[str, object]:
    cached = vocal_cue_metadata_for_record(record)
    if not force and isinstance(cached, dict) and int(cached.get("version", 0)) == VOCAL_CUE_VERSION:
        return cached
    if not prepare:
        return {
            "version": VOCAL_CUE_VERSION,
            "available": False,
            "prepared": False,
            "reason": "not-prepared",
        }

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        return {
            "version": VOCAL_CUE_VERSION,
            "available": False,
            "prepared": False,
            "reason": f"faster-whisper-unavailable: {type(exc).__name__}",
        }

    stems = ensure_track_stems(record)
    source = stems["vocals"] if stems["vocals"].exists() else ensure_preview_audio(record)
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(source), beam_size=1, vad_filter=True, word_timestamps=True)
    words: list[dict[str, object]] = []
    segment_payloads: list[dict[str, object]] = []
    for segment in segments:
        raw_words = getattr(segment, "words", None) or []
        clean_words: list[dict[str, object]] = []
        for word in raw_words:
            text = str(getattr(word, "word", "") or "").strip()
            start = getattr(word, "start", None)
            end = getattr(word, "end", None)
            probability = getattr(word, "probability", None)
            if not text or not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
                continue
            payload = {
                "text": text,
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "probability": round(float(probability), 4) if isinstance(probability, (int, float)) else None,
            }
            clean_words.append(payload)
            words.append(payload)
        segment_payloads.append(
            {
                "id": int(getattr(segment, "id", len(segment_payloads))),
                "start": round(float(getattr(segment, "start", 0.0)), 3),
                "end": round(float(getattr(segment, "end", 0.0)), 3),
                "text": str(getattr(segment, "text", "") or "").strip(),
                "avg_logprob": round(float(getattr(segment, "avg_logprob", 0.0)), 4),
                "words": clean_words,
            }
        )

    bpm = metric_bpm(record) or 120.0
    beat_seconds = 60.0 / max(bpm, 1e-6)
    cues: list[dict[str, object]] = []
    previous_end = 0.0
    for index, word in enumerate(words):
        text = str(word.get("text") or "").strip()
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        duration = max(0.02, end - start)
        next_start = float(words[index + 1]["start"]) if index + 1 < len(words) else end + 0.4
        gap_before = max(0.0, start - previous_end)
        gap_after = max(0.0, next_start - end)
        beat_pos = (start / beat_seconds) % 1.0 if beat_seconds > 0 else 0.0
        onbeat = 1.0 - min(abs(beat_pos - round(beat_pos)), 0.5) / 0.5
        short_word = max(0.0, 1.0 - abs(duration - 0.18) / 0.18)
        alpha_chars = re.sub(r"[^a-zA-Z]", "", text)
        alpha_ratio = len(alpha_chars) / max(1, len(text))
        probable = float(word.get("probability") or 0.0)
        phrase_exit_score = 0.5 * min(1.0, gap_after / 0.35) + 0.25 * onbeat + 0.25 * probable
        word_start_score = 0.4 * onbeat + 0.2 * min(1.0, gap_before / 0.2) + 0.2 * short_word + 0.2 * probable
        stutter_score = 0.45 * onbeat + 0.35 * short_word + 0.1 * alpha_ratio + 0.1 * probable
        pause_score = 0.6 * min(1.0, gap_after / 0.5) + 0.2 * onbeat + 0.2 * probable
        cues.append(
            {
                "text": text,
                "normalized_text": re.sub(r"[^a-z0-9]", "", text.lower()),
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(duration, 3),
                "gap_before": round(gap_before, 3),
                "gap_after": round(gap_after, 3),
                "beat_position": round(beat_pos, 4),
                "word_start_score": round(word_start_score, 4),
                "phrase_exit_score": round(phrase_exit_score, 4),
                "stutter_score": round(stutter_score, 4),
                "pause_score": round(pause_score, 4),
                "probability": round(probable, 4),
            }
        )
        previous_end = end

    top_wordplay = sorted(cues, key=lambda item: float(item["word_start_score"]), reverse=True)[:20]
    top_phrase_exits = sorted(cues, key=lambda item: float(item["phrase_exit_score"]), reverse=True)[:20]
    top_stutters = sorted(cues, key=lambda item: float(item["stutter_score"]), reverse=True)[:20]
    top_pauses = sorted(cues, key=lambda item: float(item["pause_score"]), reverse=True)[:20]
    payload = {
        "version": VOCAL_CUE_VERSION,
        "available": True,
        "prepared": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "track": {
            "title": record.get("title"),
            "primary_artist": record.get("primary_artist") or primary_artist(record),
            "url": record.get("url"),
            "key": stem_track_key(record),
        },
        "source_audio": str(source),
        "model": {
            "type": "faster-whisper",
            "size": WHISPER_MODEL_SIZE,
            "language": getattr(info, "language", None),
            "duration_after_vad": round(float(getattr(info, "duration_after_vad", 0.0)), 3),
        },
        "segments": segment_payloads,
        "analysis": {
            "word_count": len(words),
            "top_wordplay_cues": top_wordplay,
            "top_phrase_exit_cues": top_phrase_exits,
            "top_stutter_cues": top_stutters,
            "top_pause_cues": top_pauses,
        },
    }
    path = vocal_cue_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def _suffix_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    max_len = min(len(a), len(b), 4)
    for size in range(max_len, 1, -1):
        if a[-size:] == b[-size:]:
            return size / 4.0
    return 0.0


def _vocal_transition_matches(
    source_record: dict[str, object],
    target_record: dict[str, object],
    *,
    prepare: bool = False,
) -> dict[str, object]:
    source_meta = ensure_vocal_cues(source_record, prepare=prepare)
    target_meta = ensure_vocal_cues(target_record, prepare=prepare)
    if not bool(source_meta.get("available")) or not bool(target_meta.get("available")):
        return {
            "available": False,
            "reason": "missing-vocal-cues",
            "source": source_meta,
            "target": target_meta,
        }

    source_analysis = source_meta.get("analysis") if isinstance(source_meta.get("analysis"), dict) else {}
    target_analysis = target_meta.get("analysis") if isinstance(target_meta.get("analysis"), dict) else {}
    source_words = source_analysis.get("top_wordplay_cues") if isinstance(source_analysis, dict) else []
    source_exits = source_analysis.get("top_phrase_exit_cues") if isinstance(source_analysis, dict) else []
    source_stutters = source_analysis.get("top_stutter_cues") if isinstance(source_analysis, dict) else []
    source_pauses = source_analysis.get("top_pause_cues") if isinstance(source_analysis, dict) else []
    target_words = target_analysis.get("top_wordplay_cues") if isinstance(target_analysis, dict) else []
    if not isinstance(source_words, list) or not isinstance(target_words, list):
        return {"available": False, "reason": "invalid-vocal-cue-shape"}

    best_wordplay: dict[str, object] | None = None
    best_stutter: dict[str, object] | None = None
    best_pause: dict[str, object] | None = None
    best_switch: dict[str, object] | None = None

    title_bonus = title_similarity_bonus(str(source_record.get("title") or ""), str(target_record.get("title") or ""))
    source_title_norm = normalize_title(str(source_record.get("title") or ""))
    target_title_norm = normalize_title(str(target_record.get("title") or ""))

    for source_cue in source_words[:12]:
        source_text = str(source_cue.get("normalized_text") or "")
        for target_cue in target_words[:12]:
            target_text = str(target_cue.get("normalized_text") or "")
            lexical = 0.7 * difflib.SequenceMatcher(None, source_text, target_text).ratio() + 0.3 * _suffix_similarity(source_text, target_text)
            cadence = max(0.0, 1.0 - abs(float(source_cue.get("beat_position") or 0.0) - float(target_cue.get("beat_position") or 0.0)) / 0.5)
            confidence = 0.4 * lexical + 0.3 * cadence + 0.15 * float(source_cue.get("word_start_score") or 0.0) + 0.15 * float(target_cue.get("word_start_score") or 0.0)
            candidate = {
                "score": round(confidence, 4),
                "source_word": source_cue,
                "target_word": target_cue,
            }
            if best_wordplay is None or float(candidate["score"]) > float(best_wordplay["score"]):
                best_wordplay = candidate

            switch_score = confidence + 0.3 * title_bonus
            if source_title_norm == target_title_norm:
                switch_score += 0.35
            switch_candidate = {
                "score": round(switch_score, 4),
                "source_word": source_cue,
                "target_word": target_cue,
            }
            if best_switch is None or float(switch_candidate["score"]) > float(best_switch["score"]):
                best_switch = switch_candidate

    if isinstance(source_stutters, list) and isinstance(target_words, list):
        for source_cue in source_stutters[:12]:
            for target_cue in target_words[:12]:
                cadence = max(0.0, 1.0 - abs(float(source_cue.get("beat_position") or 0.0) - float(target_cue.get("beat_position") or 0.0)) / 0.5)
                score = 0.5 * float(source_cue.get("stutter_score") or 0.0) + 0.25 * float(target_cue.get("word_start_score") or 0.0) + 0.25 * cadence
                candidate = {
                    "score": round(score, 4),
                    "source_word": source_cue,
                    "target_word": target_cue,
                }
                if best_stutter is None or float(candidate["score"]) > float(best_stutter["score"]):
                    best_stutter = candidate

    if isinstance(source_pauses, list) and isinstance(target_words, list):
        for source_cue in source_pauses[:12]:
            for target_cue in target_words[:12]:
                cadence = max(0.0, 1.0 - abs(float(source_cue.get("beat_position") or 0.0) - float(target_cue.get("beat_position") or 0.0)) / 0.5)
                score = 0.55 * float(source_cue.get("pause_score") or 0.0) + 0.25 * float(target_cue.get("word_start_score") or 0.0) + 0.2 * cadence
                candidate = {
                    "score": round(score, 4),
                    "source_word": source_cue,
                    "target_word": target_cue,
                }
                if best_pause is None or float(candidate["score"]) > float(best_pause["score"]):
                    best_pause = candidate

    return {
        "available": True,
        "wordplay": best_wordplay,
        "stutter": best_stutter,
        "pause_punch": best_pause,
        "same_song_switchup": best_switch,
        "source_word_count": source_analysis.get("word_count"),
        "target_word_count": target_analysis.get("word_count"),
    }


def render_rubberband_preview(
    record: dict[str, object],
    *,
    tempo_ratio: float = 1.0,
    pitch_semitones: float = 0.0,
    use_preview: bool = True,
) -> Path:
    source = ensure_preview_audio(record) if use_preview else ensure_track_audio(record)
    STEM_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    output = STEM_RENDER_DIR / (
        f"{stem_track_key(record)}__rb_t{tempo_ratio:.3f}_p{pitch_semitones:.2f}"
        f"{'_preview' if use_preview else ''}.wav"
    )
    command = [
        RUBBERBAND_BIN,
        "--tempo",
        f"{float(tempo_ratio):.6f}",
        "--pitch",
        f"{float(pitch_semitones):.6f}",
        str(source),
        str(output),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else str(exc)
        raise SystemExit(f"rubberband failed: {stderr}") from exc
    if not output.exists():
        raise SystemExit(f"rubberband completed but output was not created: {output}")
    return output


def render_stem_mix(
    record: dict[str, object],
    *,
    vocals: float,
    drums: float,
    bass: float,
    other: float,
) -> Path:
    import numpy as np
    import soundfile as sf

    stems = ensure_track_stems(record)
    STEM_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    parts = []
    sample_rate = None
    mixed = None
    for name, gain in (("vocals", vocals), ("drums", drums), ("bass", bass), ("other", other)):
        audio, sr = sf.read(stems[name], always_2d=True)
        if sample_rate is None:
            sample_rate = int(sr)
            mixed = np.zeros_like(audio, dtype=np.float32)
        elif int(sr) != sample_rate:
            raise SystemExit(f"Stem sample-rate mismatch in {stems[name]}")
        mixed += audio.astype(np.float32, copy=False) * float(gain)
        parts.append(f"{name[0]}{gain:g}")
    assert mixed is not None and sample_rate is not None
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 0.999:
        mixed = mixed / peak * 0.999
    output = STEM_RENDER_DIR / f"{stem_track_key(record)}__{'_'.join(parts)}.wav"
    sf.write(output, mixed, sample_rate, subtype="PCM_16")
    return output


def parse_key_signature(key: str | None) -> tuple[int, str] | None:
    if not key:
        return None
    token = key.split("/", 1)[0].strip().replace("♯", "#").replace("♭", "b")
    match = re.match(r"^([A-Ga-g][#bB]?)(.*)$", token)
    if not match:
        return None
    note = match.group(1).upper().replace("B", "b") if len(match.group(1)) > 1 and match.group(1)[1] in "bB" else match.group(1).upper()
    note = note.replace("b", "B")
    mode_tail = match.group(2).strip().lower()
    mode = "minor" if "m" in mode_tail or "minor" in mode_tail else "major"
    pc = SEMITONES.get(note)
    if pc is None:
        return None
    return pc, mode


def key_to_pitch_class(key: str | None) -> int | None:
    parsed = parse_key_signature(key)
    return parsed[0] if parsed else None


def key_compatibility_score(a: str | None, b: str | None) -> tuple[float, str]:
    a_parsed = parse_key_signature(a)
    b_parsed = parse_key_signature(b)
    if a_parsed is None or b_parsed is None:
        return 0.0, "missing key"
    a_pc, a_mode = a_parsed
    b_pc, b_mode = b_parsed
    delta = min((a_pc - b_pc) % 12, (b_pc - a_pc) % 12)
    if delta == 0 and a_mode == b_mode:
        return 1.0, "same key"
    if delta in (3, 4) and a_mode != b_mode:
        return 0.82, "relative major/minor"
    if delta in (5, 7):
        return 0.7, "compatible fifth/fourth"
    if delta in (1, 2):
        return 0.35, "nearby key"
    return 0.0, "distant key"


def title_similarity_bonus(source_title: str, candidate_title: str) -> float:
    source_tokens = {token for token in re.findall(r"[a-z0-9]+", normalize_title(source_title)) if len(token) > 2}
    candidate_tokens = {token for token in re.findall(r"[a-z0-9]+", normalize_title(candidate_title)) if len(token) > 2}
    if not source_tokens or not candidate_tokens:
        return 0.0
    overlap = len(source_tokens & candidate_tokens)
    return min(0.1, overlap * 0.03)


def danceability_score(source: str | None, candidate: str | None) -> float:
    if not source or not candidate:
        return 0.0
    if source == candidate:
        return 0.1
    if "very danceable" in (source, candidate) and "somewhat danceable" in (source, candidate):
        return 0.05
    return 0.0


def find_record_by_live_title(index_payload: dict[str, object], live_title: str) -> dict[str, object] | None:
    target = normalize_title(live_title)
    for record in index_payload["records"]:
        if not isinstance(record, dict):
            continue
        record_title = str(record.get("title") or "")
        if normalize_title(record_title) == target:
            return record
    return None


def score_candidate(source: dict[str, object], candidate: dict[str, object]) -> dict[str, object]:
    source_bpm = metric_bpm(source)
    candidate_bpm = metric_bpm(candidate)
    normalized = normalize_bpm_pair(source_bpm, candidate_bpm)
    bpm_delta = normalized.get("delta")
    if bpm_delta is None:
        bpm_score = 0.0
        bpm_reason = "missing bpm"
    elif bpm_delta <= 1:
        bpm_score = 1.0
        bpm_reason = f"tight bpm ({bpm_delta:.1f})"
    elif bpm_delta <= 3:
        bpm_score = 0.82
        bpm_reason = f"close bpm ({bpm_delta:.1f})"
    elif bpm_delta <= 6:
        bpm_score = 0.56
        bpm_reason = f"workable bpm ({bpm_delta:.1f})"
    elif bpm_delta <= 10:
        bpm_score = 0.26
        bpm_reason = f"wide bpm ({bpm_delta:.1f})"
    else:
        bpm_score = 0.0
        bpm_reason = f"bad bpm ({bpm_delta:.1f})"

    key_score, key_reason = key_compatibility_score(metric_key(source), metric_key(candidate))
    dance_score = danceability_score(metric_danceability(source), metric_danceability(candidate))
    title_bonus = title_similarity_bonus(str(source.get("title") or ""), str(candidate.get("title") or ""))
    remix_bonus = 0.05 if ("remix" in normalize_title(str(candidate.get("title") or "")) or "edit" in normalize_title(str(candidate.get("title") or ""))) else 0.0
    total = 100 * (0.65 * bpm_score + 0.22 * key_score + 0.08 * dance_score + 0.03 * title_bonus + 0.02 * remix_bonus)
    return {
        "score": round(total, 2),
        "bpm_delta": bpm_delta,
        "normalized_bpm": normalized,
        "reasons": [bpm_reason, key_reason],
    }


def recommend_candidates(index_payload: dict[str, object], *, from_role: str, to_role: str, port: int = PORT, limit: int = 5) -> dict[str, object]:
    roles = resolve_roles(port)
    if from_role not in roles or to_role not in roles:
        raise SystemExit("Both roles must be live")
    _from_page, from_status = roles[from_role]
    _to_page, to_status = roles[to_role]

    from_live_title = str(from_status.get("playerBarTitle") or from_status.get("title") or "")
    to_live_title = str(to_status.get("playerBarTitle") or to_status.get("title") or "")
    source_record = find_record_by_live_title(index_payload, from_live_title)
    target_record = find_record_by_live_title(index_payload, to_live_title)
    if source_record is None:
        raise SystemExit(f"Current {from_role} track not found in index: {from_live_title!r}")

    source_title_norm = normalize_title(str(source_record.get("title") or ""))
    target_title_norm = normalize_title(str(target_record.get("title") or "")) if target_record else ""

    scored: list[dict[str, object]] = []
    for candidate in index_payload["records"]:
        if not isinstance(candidate, dict):
            continue
        candidate_title = str(candidate.get("title") or "")
        candidate_norm = normalize_title(candidate_title)
        if candidate_norm == source_title_norm:
            continue
        if target_title_norm and candidate_norm == target_title_norm:
            continue
        ranking = score_candidate(source_record, candidate)
        scored.append(
            {
                "index": candidate.get("index"),
                "title": candidate_title,
                "subtitle": candidate.get("subtitle"),
                "bpm": metric_bpm(candidate),
                "key": metric_key(candidate),
                "danceability": metric_danceability(candidate),
                "suggested_entry_seconds": recommended_entry_seconds(candidate),
                "score": ranking["score"],
                "bpm_delta": ranking["bpm_delta"],
                "normalized_bpm": ranking["normalized_bpm"],
                "reasons": ranking["reasons"],
            }
        )

    scored.sort(key=lambda item: (-float(item["score"]), abs(item["bpm_delta"] or 999), str(item["title"])))
    return {
        "from_role": from_role,
        "to_role": to_role,
        "from_track": {
            "title": source_record.get("title"),
            "subtitle": source_record.get("subtitle"),
            "bpm": metric_bpm(source_record),
            "key": metric_key(source_record),
            "danceability": metric_danceability(source_record),
        },
        "to_current_track": {
            "title": target_record.get("title"),
            "subtitle": target_record.get("subtitle"),
            "bpm": metric_bpm(target_record) if target_record else None,
            "key": metric_key(target_record) if target_record else None,
        } if target_record else None,
        "recommendations": scored[:limit],
    }


def run_fade(args: argparse.Namespace) -> dict[str, object]:
    cdp = ChromeCDP(args.port)
    roles = resolve_roles(args.port)
    if args.from_role not in roles or args.to_role not in roles:
        raise SystemExit("Both from-role and to-role must resolve to live tabs")
    if args.from_role == args.to_role:
        raise SystemExit("from-role and to-role must be different")

    from_page, _from_status = roles[args.from_role]
    to_page, _to_status = roles[args.to_role]
    seconds = max(0.0, float(args.seconds))
    cdp.eval(from_page, persistent_audio_control_script(volume=1.0, muted=False))
    cdp.eval(to_page, persistent_audio_control_script(volume=0.0, muted=False))

    duration_ms = int(round(seconds * 1000))
    start_at_ms = int(getattr(args, "start_at_ms", 0) or (time.time() * 1000 + 180))
    from_script = volume_ramp_script(mode=args.mode, side="from", duration_ms=duration_ms, start_at_ms=start_at_ms)
    to_script = volume_ramp_script(mode=args.mode, side="to", duration_ms=duration_ms, start_at_ms=start_at_ms)
    cdp.eval(from_page, from_script)
    cdp.eval(to_page, to_script)
    baseline_metrics = sample_role_metrics(args.from_role, seconds=0.18, interval_ms=25, port=args.port)
    baseline_rms = max(1e-4, float(baseline_metrics.get("avgRms", 1e-4)))
    baseline_bass = max(1e-4, float(baseline_metrics.get("bassEnergyRatio", 1e-4)))
    baseline_centroid = max(1e-4, float(baseline_metrics.get("avgCentroidHz", 1.0)))
    baseline_transient = max(1e-4, float(baseline_metrics.get("transientDensity", 1e-4)))
    last_from_metrics = dict(baseline_metrics)
    last_to_metrics: dict[str, float] = {}
    metrics_interval = max(0.28, min(0.6, seconds / 4.0 if seconds > 0 else 0.35))
    next_metrics_sample = time.monotonic()
    telemetry: list[dict[str, float]] = []

    def target_volume(side: str, progress: float) -> float:
        progress = max(0.0, min(1.0, progress))
        if args.mode == "crossfade":
            if side == "from":
                return math.cos(progress * math.pi / 2.0)
            return math.sin(progress * math.pi / 2.0)
        if side == "from":
            if progress < 0.35:
                return 1.0
            tail = (progress - 0.35) / 0.65
            return max(0.0, min(1.0, 1.0 - tail))
        if progress < 0.2:
            return progress / 0.2 * 0.65
        if progress < 0.55:
            return 0.65 + ((progress - 0.2) / 0.35) * 0.35
        return 1.0

    start_monotonic = time.monotonic()
    scheduled_wall = start_at_ms / 1000.0
    end_wall = scheduled_wall + seconds
    sample_window = max(0.05, min(0.12, seconds / 6 if seconds > 0 else 0.05))
    sample_interval = max(0.035, min(0.08, seconds / 8 if seconds > 0 else 0.04))
    while True:
        now_wall = time.time()
        if now_wall >= end_wall + 0.16:
            break
        if now_wall < scheduled_wall:
            time.sleep(min(0.05, max(0.0, scheduled_wall - now_wall)))
            continue
        progress = 1.0 if seconds <= 1e-6 else min(1.0, max(0.0, (now_wall - scheduled_wall) / seconds))
        now_monotonic = time.monotonic()
        if now_monotonic >= next_metrics_sample:
            sampled_from = sample_role_metrics(args.from_role, seconds=sample_window, interval_ms=15, port=args.port)
            sampled_to = sample_role_metrics(args.to_role, seconds=sample_window, interval_ms=15, port=args.port)
            if sampled_from:
                last_from_metrics = sampled_from
            if sampled_to:
                last_to_metrics = sampled_to
            next_metrics_sample = now_monotonic + metrics_interval
        from_metrics = last_from_metrics
        to_metrics = last_to_metrics
        from_rms = float(from_metrics.get("avgRms", 0.0))
        to_rms = float(to_metrics.get("avgRms", 0.0))
        combined_rms = math.sqrt(from_rms * from_rms + to_rms * to_rms)
        combined_bass = float(from_metrics.get("bassEnergyRatio", 0.0) + to_metrics.get("bassEnergyRatio", 0.0))
        combined_centroid = 0.5 * (float(from_metrics.get("avgCentroidHz", 0.0)) + float(to_metrics.get("avgCentroidHz", 0.0)))
        combined_transient = 0.5 * (float(from_metrics.get("transientDensity", 0.0)) + float(to_metrics.get("transientDensity", 0.0)))
        telemetry.append(
            {
                "elapsed": round(time.monotonic() - start_monotonic, 3),
                "progress": round(progress, 4),
                "from_volume": round(target_volume("from", progress), 4),
                "to_volume": round(target_volume("to", progress), 4),
                "from_rms": from_rms,
                "to_rms": to_rms,
                "combined_rms": combined_rms,
                "smoothed_combined_rms": combined_rms,
                "target_rms": baseline_rms,
                "from_bass_ratio": float(from_metrics.get("bassEnergyRatio", 0.0)),
                "to_bass_ratio": float(to_metrics.get("bassEnergyRatio", 0.0)),
                "combined_bass_ratio": combined_bass / baseline_bass,
                "combined_sub_ratio": float(from_metrics.get("subEnergyRatio", 0.0) + to_metrics.get("subEnergyRatio", 0.0)),
                "combined_vocal_ratio": float(from_metrics.get("vocalEnergyRatio", 0.0) + to_metrics.get("vocalEnergyRatio", 0.0)),
                "combined_presence_ratio": float(from_metrics.get("presenceEnergyRatio", 0.0) + to_metrics.get("presenceEnergyRatio", 0.0)),
                "combined_high_ratio": float(from_metrics.get("highEnergyRatio", 0.0) + to_metrics.get("highEnergyRatio", 0.0)),
                "combined_centroid_ratio": combined_centroid / baseline_centroid,
                "combined_rolloff_ratio": (
                    0.5 * (float(from_metrics.get("avgRolloffHz", 0.0)) + float(to_metrics.get("avgRolloffHz", 0.0)))
                ) / max(1e-4, float(baseline_metrics.get("avgRolloffHz", 1.0))),
                "combined_transient_ratio": combined_transient / baseline_transient,
                "combined_flux_ratio": (
                    0.5 * (float(from_metrics.get("fluxMean", 0.0)) + float(to_metrics.get("fluxMean", 0.0)))
                ) / max(1e-4, float(baseline_metrics.get("fluxMean", 1e-4))),
                "combined_dynamic_ratio": (
                    0.5 * (float(from_metrics.get("dynamicRange", 0.0)) + float(to_metrics.get("dynamicRange", 0.0)))
                ) / max(1e-4, float(baseline_metrics.get("dynamicRange", 1e-4))),
                "from_crest_factor": float(from_metrics.get("crestFactor", 0.0)),
                "to_crest_factor": float(to_metrics.get("crestFactor", 0.0)),
                "vocal_overlap_ratio": min(
                    float(from_metrics.get("vocalEnergyRatio", 0.0)),
                    float(to_metrics.get("vocalEnergyRatio", 0.0)),
                ),
                "bass_overlap_ratio": min(
                    float(from_metrics.get("bassEnergyRatio", 0.0)),
                    float(to_metrics.get("bassEnergyRatio", 0.0)),
                ),
                "transient_mismatch": abs(
                    float(from_metrics.get("transientDensity", 0.0)) - float(to_metrics.get("transientDensity", 0.0))
                ),
                "router_gain": 1.0,
                "low_gain": 1.0,
            }
        )
        time.sleep(sample_interval)

    final_from = reapply_role_controls(args.from_role, volume=0.0, muted=True, port=args.port)
    final_to = reapply_role_controls(args.to_role, volume=1.0, muted=False, port=args.port)
    graph = save_transition_graph(telemetry, baseline_rms) if telemetry else None
    summary = summarize_transition_telemetry(telemetry, baseline_rms) if telemetry else None
    return {
        "from": {
            "role": args.from_role,
            "status": final_from,
        },
        "to": {
            "role": args.to_role,
            "status": final_to,
        },
        "seconds": seconds,
        "mode": args.mode,
        "scheduled_start_at_ms": start_at_ms,
        "telemetry": {
            "points": len(telemetry),
            "target_rms": baseline_rms,
            "summary": summary,
            "json": str(graph["json"]) if graph else None,
            "png": str(graph["png"]) if graph else None,
        },
    }


def infer_simple_transition_roles(port: int = PORT) -> tuple[str, str]:
    roles = resolve_roles(port)
    if "direct" not in roles or "routed" not in roles:
        raise SystemExit("Both direct and routed roles must be live")

    def role_level(role: str) -> float:
        _page, status = roles[role]
        volumes = status.get("volume") if isinstance(status, dict) else None
        muted = status.get("muted") if isinstance(status, dict) else None
        if isinstance(muted, list) and muted and muted[0] is True:
            return 0.0
        if isinstance(volumes, list) and volumes and isinstance(volumes[0], (int, float)):
            return float(volumes[0])
        return 0.0

    direct_level = role_level("direct")
    routed_level = role_level("routed")
    if direct_level >= routed_level:
        return "direct", "routed"
    return "routed", "direct"


def run_simple_transition(args: argparse.Namespace) -> dict[str, object]:
    from_role = args.from_role
    to_role = args.to_role
    if from_role is None or to_role is None:
        from_role, to_role = infer_simple_transition_roles(args.port)

    readiness = ensure_role_playing(to_role, port=args.port, timeout=max(2.0, float(args.ready_timeout)))
    fade_payload = run_fade(
        argparse.Namespace(
            from_role=from_role,
            to_role=to_role,
            mode=args.mode,
            seconds=args.seconds,
            steps=args.steps,
            port=args.port,
        )
    )
    return {
        "kind": "simple-transition",
        "from_role": from_role,
        "to_role": to_role,
        "ready_timeout": float(args.ready_timeout),
        "target_readiness": readiness,
        "fade": fade_payload,
    }


def command_fade(args: argparse.Namespace) -> None:
    print(json.dumps(run_fade(args), indent=2))


def command_simple_transition(args: argparse.Namespace) -> None:
    print(json.dumps(run_simple_transition(args), indent=2))


def run_bass_swap_transition(
    from_role: str,
    to_role: str,
    *,
    seconds: float,
    steps: int,
    port: int = PORT,
    incoming_low_start: float = 0.18,
) -> dict[str, object]:
    if to_role != "routed":
        raise SystemExit("bass-swap currently requires the incoming track to be on the routed role")

    total_seconds = max(1.5, float(seconds))
    total_steps = max(6, int(steps))
    step_seconds = total_seconds / total_steps
    router_control = read_router_control()
    base_gain = float(router_control.get("gain", 1.35))
    max_gain = min(1.7, max(base_gain * 1.15, base_gain + 0.15))
    min_gain = max(1.0, base_gain * 0.95)

    lock_role_queue(from_role, port=port)
    lock_role_queue(to_role, port=port)
    set_role_volume(from_role, 1.0, port=port, muted=False)
    set_role_volume(to_role, 0.0, port=port, muted=True)
    write_router_control(gain=base_gain, low_gain=incoming_low_start, mid_gain=1.0, high_gain=1.0)
    baseline_samples = []
    for _ in range(5):
        baseline_samples.append(sample_role_metrics(from_role, seconds=0.18, interval_ms=25, port=port))
        time.sleep(0.04)
    def baseline_stat(key: str, floor: float = 1e-4) -> float:
        values = [sample.get(key, 0.0) for sample in baseline_samples if isinstance(sample, dict) and sample.get(key, 0.0) > 0]
        return max(floor, float(statistics.median(values) if values else floor))

    baseline_rms = baseline_stat("avgRms")
    baseline_bass = baseline_stat("bassEnergyRatio")
    baseline_sub = baseline_stat("subEnergyRatio")
    baseline_centroid = baseline_stat("avgCentroidHz", floor=1.0)
    baseline_rolloff = baseline_stat("avgRolloffHz", floor=1.0)
    baseline_transient = baseline_stat("transientDensity")
    baseline_flux = baseline_stat("fluxMean")
    baseline_dynamic = baseline_stat("dynamicRange")
    telemetry: list[dict[str, float]] = []
    started_at = time.monotonic()
    combined_history = [baseline_rms]

    for index in range(total_steps + 1):
        progress = index / total_steps
        theta = progress * (math.pi / 2.0)
        outgoing_volume = math.cos(theta)
        incoming_volume = math.sin(theta)

        if progress < 0.45:
            low_gain = incoming_low_start
        elif progress < 0.8:
            inner = (progress - 0.45) / 0.35
            low_gain = incoming_low_start + (0.95 - incoming_low_start) * inner
        else:
            inner = (progress - 0.8) / 0.2
            low_gain = 0.95 + 0.05 * inner

        set_role_volume(to_role, incoming_volume, port=port, muted=(incoming_volume <= 1e-4))
        write_router_control(gain=base_gain, low_gain=low_gain)
        set_role_volume(from_role, outgoing_volume, port=port, muted=(outgoing_volume <= 1e-4))
        settle = step_seconds * 0.55
        if index < total_steps:
            time.sleep(settle)

        outgoing_metrics = sample_role_metrics(from_role, seconds=0.18, interval_ms=25, port=port)
        incoming_metrics = sample_role_metrics(to_role, seconds=0.18, interval_ms=25, port=port)
        outgoing_rms = float(outgoing_metrics.get("avgRms", 0.0))
        incoming_rms = float(incoming_metrics.get("avgRms", 0.0))
        combined_rms = math.sqrt(outgoing_rms * outgoing_rms + incoming_rms * incoming_rms)
        combined_history.append(combined_rms)
        combined_history = combined_history[-5:]
        smoothed_combined = float(statistics.median(combined_history))
        if smoothed_combined > 1e-4:
            correction = max(0.94, min(1.1, baseline_rms / smoothed_combined))
        else:
            correction = 1.0
        gain_boost = base_gain * correction
        adjusted_gain = max(min_gain, min(max_gain, gain_boost))
        write_router_control(gain=adjusted_gain, low_gain=low_gain)

        combined_bass = float(outgoing_metrics.get("bassEnergyRatio", 0.0) + incoming_metrics.get("bassEnergyRatio", 0.0))
        combined_sub = float(outgoing_metrics.get("subEnergyRatio", 0.0) + incoming_metrics.get("subEnergyRatio", 0.0))
        combined_vocal = float(outgoing_metrics.get("vocalEnergyRatio", 0.0) + incoming_metrics.get("vocalEnergyRatio", 0.0))
        combined_presence = float(outgoing_metrics.get("presenceEnergyRatio", 0.0) + incoming_metrics.get("presenceEnergyRatio", 0.0))
        combined_high = float(outgoing_metrics.get("highEnergyRatio", 0.0) + incoming_metrics.get("highEnergyRatio", 0.0))
        combined_centroid = 0.5 * (float(outgoing_metrics.get("avgCentroidHz", 0.0)) + float(incoming_metrics.get("avgCentroidHz", 0.0)))
        combined_rolloff = 0.5 * (float(outgoing_metrics.get("avgRolloffHz", 0.0)) + float(incoming_metrics.get("avgRolloffHz", 0.0)))
        combined_transient = 0.5 * (float(outgoing_metrics.get("transientDensity", 0.0)) + float(incoming_metrics.get("transientDensity", 0.0)))
        combined_flux = 0.5 * (float(outgoing_metrics.get("fluxMean", 0.0)) + float(incoming_metrics.get("fluxMean", 0.0)))
        combined_dynamic = 0.5 * (float(outgoing_metrics.get("dynamicRange", 0.0)) + float(incoming_metrics.get("dynamicRange", 0.0)))
        vocal_overlap_ratio = min(
            float(outgoing_metrics.get("vocalEnergyRatio", 0.0)),
            float(incoming_metrics.get("vocalEnergyRatio", 0.0)),
        )
        bass_overlap_ratio = min(
            float(outgoing_metrics.get("bassEnergyRatio", 0.0)),
            float(incoming_metrics.get("bassEnergyRatio", 0.0)),
        )
        transient_mismatch = abs(
            float(outgoing_metrics.get("transientDensity", 0.0)) - float(incoming_metrics.get("transientDensity", 0.0))
        )
        telemetry.append(
            {
                "elapsed": round(time.monotonic() - started_at, 3),
                "progress": round(progress, 4),
                "from_volume": round(outgoing_volume, 4),
                "to_volume": round(incoming_volume, 4),
                "from_rms": outgoing_rms,
                "to_rms": incoming_rms,
                "combined_rms": combined_rms,
                "smoothed_combined_rms": smoothed_combined,
                "target_rms": baseline_rms,
                "from_bass_ratio": float(outgoing_metrics.get("bassEnergyRatio", 0.0)),
                "to_bass_ratio": float(incoming_metrics.get("bassEnergyRatio", 0.0)),
                "combined_bass_ratio": combined_bass / max(1e-6, baseline_bass),
                "combined_sub_ratio": combined_sub / max(1e-6, baseline_sub),
                "combined_vocal_ratio": combined_vocal,
                "combined_presence_ratio": combined_presence,
                "combined_high_ratio": combined_high,
                "combined_centroid_ratio": combined_centroid / max(1e-6, baseline_centroid),
                "combined_rolloff_ratio": combined_rolloff / max(1e-6, baseline_rolloff),
                "combined_transient_ratio": combined_transient / max(1e-6, baseline_transient),
                "combined_flux_ratio": combined_flux / max(1e-6, baseline_flux),
                "combined_dynamic_ratio": combined_dynamic / max(1e-6, baseline_dynamic),
                "from_crest_factor": float(outgoing_metrics.get("crestFactor", 0.0)),
                "to_crest_factor": float(incoming_metrics.get("crestFactor", 0.0)),
                "vocal_overlap_ratio": vocal_overlap_ratio,
                "bass_overlap_ratio": bass_overlap_ratio,
                "transient_mismatch": transient_mismatch,
                "router_gain": adjusted_gain,
                "low_gain": low_gain,
            }
        )
        if index < total_steps:
            remainder = max(0.0, step_seconds - settle)
            if remainder > 0:
                time.sleep(remainder)

    final_from = reapply_role_controls(from_role, volume=0.0, muted=True, port=port)
    final_to = reapply_role_controls(to_role, volume=1.0, muted=False, port=port)
    write_router_control(gain=base_gain, low_gain=1.0)
    graph = save_transition_graph(telemetry, baseline_rms)
    summary = summarize_transition_telemetry(telemetry, baseline_rms)
    return {
        "from": {"role": from_role, "status": final_from},
        "to": {"role": to_role, "status": final_to},
        "preset": "bass-swap",
        "seconds": total_seconds,
        "steps": total_steps,
        "telemetry": {
            "points": len(telemetry),
            "target_rms": baseline_rms,
            "summary": summary,
            "json": str(graph["json"]),
            "png": str(graph["png"]),
        },
    }


def volume_ramp_script(*, mode: str, side: str, duration_ms: int, start_at_ms: int) -> str:
    if mode not in {"crossfade", "staged"}:
        raise SystemExit(f"Unsupported fade mode: {mode}")
    if side not in {"from", "to"}:
        raise SystemExit(f"Unsupported fade side: {side}")
    return f"""
(() => {{
  const element = document.querySelector('audio,video');
  if (!element) return {{ ok: false, error: 'no-media-element' }};
  const side = {side!r};
  const mode = {mode!r};
  const durationMs = Math.max(0, {duration_ms});
  const startAtMs = {start_at_ms};
  window.__codexVolumeRampCancel && window.__codexVolumeRampCancel();
  let stopped = false;
  function stop() {{
    stopped = true;
  }}
  window.__codexVolumeRampCancel = stop;
  function clamp(v) {{
    return Math.max(0, Math.min(1, v));
  }}
  function targetVolume(progress) {{
    progress = clamp(progress);
    if (mode === 'crossfade') {{
      if (side === 'from') return Math.cos(progress * Math.PI / 2);
      return Math.sin(progress * Math.PI / 2);
    }}
    if (side === 'from') {{
      if (progress < 0.35) return 1;
      const tail = (progress - 0.35) / 0.65;
      return clamp(1 - tail);
    }}
    if (progress < 0.2) return progress / 0.2 * 0.65;
    if (progress < 0.55) return 0.65 + ((progress - 0.2) / 0.35) * 0.35;
    return 1;
  }}
  const begin = () => {{
    const startedAt = performance.now();
    const timer = setInterval(() => {{
      if (stopped) {{
        clearInterval(timer);
        return;
      }}
      const progress = durationMs <= 0 ? 1 : Math.min(1, (performance.now() - startedAt) / durationMs);
      element.volume = targetVolume(progress);
      if (progress >= 1) {{
        clearInterval(timer);
        element.volume = side === 'from' ? 0 : 1;
      }}
    }}, 16);
  }};
  const delayMs = Math.max(0, startAtMs - Date.now());
  if (delayMs <= 1) {{
    begin();
  }} else {{
    setTimeout(begin, delayMs);
  }}
  return {{ ok: true, scheduled: true, mode, side, durationMs, startAtMs }};
}})()
""".strip()


def write_router_control(
    *,
    gain: float | None = None,
    low_gain: float | None = None,
    mid_gain: float | None = None,
    high_gain: float | None = None,
    control_path: Path = ROUTER_CONTROL,
) -> None:
    control_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = control_path.with_suffix(control_path.suffix + ".lock")
    with lock_path.open("w") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        payload = safe_read_json(control_path)
        if gain is not None:
            payload["gain"] = gain
        if low_gain is not None:
            payload["low_gain"] = low_gain
        if mid_gain is not None:
            payload["mid_gain"] = mid_gain
        if high_gain is not None:
            payload["high_gain"] = high_gain
        atomic_write_json(control_path, payload)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def read_router_control(control_path: Path = ROUTER_CONTROL) -> dict[str, float]:
    payload = safe_read_json(control_path)
    return {key: float(value) for key, value in payload.items() if isinstance(value, (int, float))}


def summarize_transition_telemetry(telemetry: list[dict[str, float]], baseline_rms: float) -> dict[str, float]:
    combined = [float(point.get("combined_rms", 0.0)) for point in telemetry if point.get("combined_rms") is not None]
    if not combined:
        return {
            "target_rms": float(baseline_rms),
            "min_ratio": 0.0,
            "max_ratio": 0.0,
            "avg_ratio": 0.0,
            "mean_abs_error_ratio": 0.0,
            "dip_penalty": 0.0,
            "overshoot_penalty": 0.0,
            "reward_score": 0.0,
        }
    target = max(1e-6, float(baseline_rms))
    ratios = [value / target for value in combined]
    errors = [abs(ratio - 1.0) for ratio in ratios]
    mean_abs_error = sum(errors) / len(errors)
    dip_penalty = sum(max(0.0, 1.0 - ratio) for ratio in ratios) / len(ratios)
    overshoot_penalty = sum(max(0.0, ratio - 1.0) for ratio in ratios) / len(ratios)
    bass_ratios = [float(point.get("combined_bass_ratio", 1.0)) for point in telemetry]
    centroid_ratios = [float(point.get("combined_centroid_ratio", 1.0)) for point in telemetry]
    transient_ratios = [float(point.get("combined_transient_ratio", 1.0)) for point in telemetry]
    dynamic_ratios = [float(point.get("combined_dynamic_ratio", 1.0)) for point in telemetry]
    vocal_overlap = [float(point.get("vocal_overlap_ratio", 0.0)) for point in telemetry]
    bass_overlap = [float(point.get("bass_overlap_ratio", 0.0)) for point in telemetry]
    transient_mismatch = [float(point.get("transient_mismatch", 0.0)) for point in telemetry]
    bass_dip_penalty = sum(max(0.0, 1.0 - ratio) for ratio in bass_ratios) / len(bass_ratios)
    centroid_drift_penalty = sum(abs(ratio - 1.0) for ratio in centroid_ratios) / len(centroid_ratios)
    transient_drift_penalty = sum(abs(ratio - 1.0) for ratio in transient_ratios) / len(transient_ratios)
    dynamic_drift_penalty = sum(abs(ratio - 1.0) for ratio in dynamic_ratios) / len(dynamic_ratios)
    vocal_overlap_penalty = sum(vocal_overlap) / len(vocal_overlap)
    bass_overlap_penalty = sum(bass_overlap) / len(bass_overlap)
    transient_mismatch_penalty = sum(transient_mismatch) / len(transient_mismatch)
    reward = max(
        0.0,
        100.0
        - 28.0 * mean_abs_error
        - 24.0 * dip_penalty
        - 14.0 * overshoot_penalty
        - 12.0 * bass_dip_penalty
        - 8.0 * centroid_drift_penalty
        - 6.0 * transient_drift_penalty
        - 5.0 * dynamic_drift_penalty
        - 12.0 * vocal_overlap_penalty
        - 10.0 * bass_overlap_penalty
        - 6.0 * transient_mismatch_penalty
    )
    return {
        "target_rms": target,
        "min_ratio": min(ratios),
        "max_ratio": max(ratios),
        "avg_ratio": sum(ratios) / len(ratios),
        "mean_abs_error_ratio": mean_abs_error,
        "dip_penalty": dip_penalty,
        "overshoot_penalty": overshoot_penalty,
        "bass_dip_penalty": bass_dip_penalty,
        "centroid_drift_penalty": centroid_drift_penalty,
        "transient_drift_penalty": transient_drift_penalty,
        "dynamic_drift_penalty": dynamic_drift_penalty,
        "vocal_overlap_penalty": vocal_overlap_penalty,
        "bass_overlap_penalty": bass_overlap_penalty,
        "transient_mismatch_penalty": transient_mismatch_penalty,
        "min_bass_ratio": min(bass_ratios),
        "max_bass_ratio": max(bass_ratios),
        "reward_score": reward,
    }


def save_transition_graph(telemetry: list[dict[str, float]], baseline_rms: float) -> dict[str, Path]:
    TRANSITION_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"transition_{time.strftime('%Y%m%d_%H%M%S')}"
    json_path = TRANSITION_DIR / f"{stem}.json"
    png_path = TRANSITION_DIR / f"{stem}.png"
    summary = summarize_transition_telemetry(telemetry, baseline_rms)
    json_path.write_text(json.dumps({"target_rms": baseline_rms, "summary": summary, "telemetry": telemetry}, indent=2) + "\n")

    if telemetry:
        try:
            import matplotlib.pyplot as plt  # type: ignore
        except Exception:
            script = f"""
import json
from pathlib import Path
import matplotlib.pyplot as plt
payload = json.loads(Path({str(json_path)!r}).read_text())
telemetry = payload.get("telemetry", [])
if not telemetry:
    raise SystemExit(0)
xs = [point["elapsed"] for point in telemetry]
combined = [point["combined_rms"] for point in telemetry]
target = [point["target_rms"] for point in telemetry]
gain = [point["router_gain"] for point in telemetry]
from_vol = [point["from_volume"] for point in telemetry]
to_vol = [point["to_volume"] for point in telemetry]
ratios = [c / max(1e-6, t) for c, t in zip(combined, target)]
low = [point.get("combined_bass_ratio", 0.0) for point in telemetry]
vocal = [point.get("vocal_overlap_ratio", 0.0) for point in telemetry]
centroid = [point.get("combined_centroid_ratio", 0.0) for point in telemetry]
transient = [point.get("combined_transient_ratio", 0.0) for point in telemetry]
fig, axes = plt.subplots(5, 1, figsize=(9, 12), sharex=True)
axes[0].plot(xs, combined, label="combined_rms", color="#0b84f3", linewidth=2)
axes[0].plot(xs, target, label="target_rms", color="#f39c12", linestyle="--", linewidth=1.5)
axes[0].set_ylabel("RMS")
axes[0].legend(loc="upper right")
axes[0].grid(alpha=0.25)
axes[1].plot(xs, ratios, label="combined/target", color="#16a085", linewidth=2)
axes[1].axhline(1.0, color="#e67e22", linestyle="--", linewidth=1.2)
axes[1].set_ylabel("Ratio")
axes[1].legend(loc="upper right")
axes[1].grid(alpha=0.25)
axes[2].plot(xs, low, label="bass_ratio", color="#2c3e50", linewidth=2)
axes[2].plot(xs, vocal, label="vocal_overlap", color="#c0392b", linewidth=1.6)
axes[2].plot(xs, centroid, label="centroid_ratio", color="#2980b9", linewidth=1.6)
axes[2].axhline(1.0, color="#7f8c8d", linestyle="--", linewidth=1.0)
axes[2].set_ylabel("Tone")
axes[2].legend(loc="upper right")
axes[2].grid(alpha=0.25)
axes[3].plot(xs, transient, label="transient_ratio", color="#16a085", linewidth=2)
axes[3].axhline(1.0, color="#7f8c8d", linestyle="--", linewidth=1.0)
axes[3].set_ylabel("Transient")
axes[3].legend(loc="upper right")
axes[3].grid(alpha=0.25)
axes[4].plot(xs, from_vol, label="from_volume", color="#e74c3c", linewidth=2)
axes[4].plot(xs, to_vol, label="to_volume", color="#27ae60", linewidth=2)
axes[4].plot(xs, gain, label="router_gain", color="#8e44ad", linewidth=1.5)
axes[4].set_ylabel("Control")
axes[4].set_xlabel("Seconds")
axes[4].legend(loc="upper right")
axes[4].grid(alpha=0.25)
fig.tight_layout()
fig.savefig({str(png_path)!r}, dpi=160)
plt.close(fig)
"""
            try:
                subprocess.run(["python3", "-c", script], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except Exception:
                return {"json": json_path, "png": png_path}
            return {"json": json_path, "png": png_path}
        xs = [point["elapsed"] for point in telemetry]
        combined = [point["combined_rms"] for point in telemetry]
        target = [point["target_rms"] for point in telemetry]
        gain = [point["router_gain"] for point in telemetry]
        from_vol = [point["from_volume"] for point in telemetry]
        to_vol = [point["to_volume"] for point in telemetry]
        ratios = [c / max(1e-6, t) for c, t in zip(combined, target)]

        low = [point.get("combined_bass_ratio", 0.0) for point in telemetry]
        vocal = [point.get("vocal_overlap_ratio", 0.0) for point in telemetry]
        centroid = [point.get("combined_centroid_ratio", 0.0) for point in telemetry]
        transient = [point.get("combined_transient_ratio", 0.0) for point in telemetry]

        fig, axes = plt.subplots(5, 1, figsize=(9, 12), sharex=True)
        axes[0].plot(xs, combined, label="combined_rms", color="#0b84f3", linewidth=2)
        axes[0].plot(xs, target, label="target_rms", color="#f39c12", linestyle="--", linewidth=1.5)
        axes[0].set_ylabel("RMS")
        axes[0].legend(loc="upper right")
        axes[0].grid(alpha=0.25)

        axes[1].plot(xs, ratios, label="combined/target", color="#16a085", linewidth=2)
        axes[1].axhline(1.0, color="#e67e22", linestyle="--", linewidth=1.2)
        axes[1].set_ylabel("Ratio")
        axes[1].legend(loc="upper right")
        axes[1].grid(alpha=0.25)

        axes[2].plot(xs, low, label="bass_ratio", color="#2c3e50", linewidth=2)
        axes[2].plot(xs, vocal, label="vocal_overlap", color="#c0392b", linewidth=1.6)
        axes[2].plot(xs, centroid, label="centroid_ratio", color="#2980b9", linewidth=1.6)
        axes[2].axhline(1.0, color="#7f8c8d", linestyle="--", linewidth=1.0)
        axes[2].set_ylabel("Tone")
        axes[2].legend(loc="upper right")
        axes[2].grid(alpha=0.25)

        axes[3].plot(xs, transient, label="transient_ratio", color="#16a085", linewidth=2)
        axes[3].axhline(1.0, color="#7f8c8d", linestyle="--", linewidth=1.0)
        axes[3].set_ylabel("Transient")
        axes[3].legend(loc="upper right")
        axes[3].grid(alpha=0.25)

        axes[4].plot(xs, from_vol, label="from_volume", color="#e74c3c", linewidth=2)
        axes[4].plot(xs, to_vol, label="to_volume", color="#27ae60", linewidth=2)
        axes[4].plot(xs, gain, label="router_gain", color="#8e44ad", linewidth=1.5)
        axes[4].set_ylabel("Control")
        axes[4].set_xlabel("Seconds")
        axes[4].legend(loc="upper right")
        axes[4].grid(alpha=0.25)

        fig.tight_layout()
        fig.savefig(png_path, dpi=160)
        plt.close(fig)

    return {"json": json_path, "png": png_path}


def _metric_value(payload: dict[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def sample_role_metrics(
    role: str,
    *,
    seconds: float = 0.12,
    interval_ms: int = 20,
    port: int = PORT,
) -> dict[str, float]:
    cdp = ChromeCDP(port)
    page, _status = role_page_and_status(role, port=port)
    payload = cdp.eval(page, analyze_media_script(seconds, interval_ms=interval_ms))
    if not isinstance(payload, dict):
        return {}
    keys = [
        "avgRms",
        "rmsP95",
        "rmsP20",
        "avgPeak",
        "crestFactor",
        "avgZeroCrossingRate",
        "avgCentroidHz",
        "avgRolloffHz",
        "fluxMean",
        "transientDensity",
        "dynamicRange",
        "subEnergyRatio",
        "bassEnergyRatio",
        "lowMidEnergyRatio",
        "vocalEnergyRatio",
        "presenceEnergyRatio",
        "highEnergyRatio",
    ]
    metrics = {key: _metric_value(payload, key) for key in keys}
    bpm_payload = payload.get("bpm")
    if isinstance(bpm_payload, dict):
        metrics["bpm"] = _metric_value(bpm_payload, "bpm")
        metrics["bpmConfidence"] = _metric_value(bpm_payload, "confidence")
    return metrics


def sample_role_rms(role: str, *, seconds: float = 0.12, interval_ms: int = 20, port: int = PORT) -> float:
    return sample_role_metrics(role, seconds=seconds, interval_ms=interval_ms, port=port).get("avgRms", 0.0)


def read_router_pid(pidfile: Path = ROUTER_PIDFILE) -> int | None:
    if not pidfile.exists():
        return None
    try:
        raw = pidfile.read_text().strip()
        if raw.startswith("{"):
            payload = json.loads(raw)
            if isinstance(payload, dict):
                value = payload.get("pid")
                if isinstance(value, int):
                    return value
        return int(raw)
    except Exception:
        return None


def router_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_router_process(
    *,
    input_label: str,
    pair: str,
    blocksize: int,
    latency: str,
    gain: float,
    delay_seconds: float,
    low_gain: float,
    mid_gain: float,
    high_gain: float,
    pidfile: Path,
    logfile: Path,
    control_file: Path,
    stats_file: Path,
) -> dict[str, object]:
    stop_result = terminate_router(
        pidfile,
        script_name="scripts/djm_router.py",
        control_file=control_file,
        stats_file=stats_file,
    )
    if stop_result.get("ok") is False:
        raise SystemExit(stop_result["error"])

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "djm_router.py"),
        "route",
        "--input",
        input_label,
        "--pair",
        pair,
        "--blocksize",
        str(blocksize),
        "--latency",
        latency,
        "--gain",
        str(gain),
        "--delay-seconds",
        str(delay_seconds),
        "--low-gain",
        str(low_gain),
        "--mid-gain",
        str(mid_gain),
        "--high-gain",
        str(high_gain),
        "--control-file",
        str(control_file),
        "--stats-file",
        str(stats_file),
    ]
    rotate_log_if_needed(logfile)
    write_router_control(
        gain=gain,
        low_gain=low_gain,
        mid_gain=mid_gain,
        high_gain=high_gain,
        control_path=control_file,
    )
    with logfile.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(SCRIPT_DIR.parent),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    atomic_write_json(
        pidfile,
        {
            "pid": process.pid,
            "script": "scripts/djm_router.py",
            "started_at": time.time(),
            "command": cmd,
        },
    )
    return {"ok": True, "pid": process.pid, "log": str(logfile), "cmd": cmd, "replaced": stop_result}


def command_routed_start(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            start_router_process(
                input_label=ROUTED_LABEL,
                pair=args.pair,
                blocksize=args.blocksize,
                latency=args.latency,
                gain=args.gain,
                delay_seconds=args.delay_seconds,
                low_gain=args.low_gain,
                mid_gain=args.mid_gain,
                high_gain=args.high_gain,
                pidfile=ROUTER_PIDFILE,
                logfile=ROUTER_LOG,
                control_file=ROUTER_CONTROL,
                stats_file=ROUTER_STATS,
            ),
            indent=2,
        )
    )


def command_routed_stop(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            terminate_router(
                ROUTER_PIDFILE,
                script_name="scripts/djm_router.py",
                control_file=ROUTER_CONTROL,
                stats_file=ROUTER_STATS,
            ),
            indent=2,
        )
    )


def command_routed_status(args: argparse.Namespace) -> None:
    pid = read_router_pid(ROUTER_PIDFILE)
    payload = {
        "pid": pid,
        "running": router_running(pid),
        "log": str(ROUTER_LOG),
    }
    if ROUTER_LOG.exists():
        payload["log_tail"] = tail_text(ROUTER_LOG)
    if ROUTER_CONTROL.exists():
        payload["control"] = safe_read_json(ROUTER_CONTROL) or tail_text(ROUTER_CONTROL)
    if ROUTER_STATS.exists():
        payload["stats"] = safe_read_json(ROUTER_STATS) or tail_text(ROUTER_STATS)
    print(json.dumps(payload, indent=2))


def command_routed_set_fx(args: argparse.Namespace) -> None:
    write_router_control(
        gain=args.gain,
        low_gain=args.low_gain,
        mid_gain=args.mid_gain,
        high_gain=args.high_gain,
        control_path=ROUTER_CONTROL,
    )
    payload = {"ok": True}
    if ROUTER_CONTROL.exists():
        payload["control"] = safe_read_json(ROUTER_CONTROL)
    print(json.dumps(payload, indent=2))


def command_direct_relay_start(args: argparse.Namespace) -> None:
    payload = start_router_process(
        input_label=DIRECT_RELAY_LABEL,
        pair=args.pair,
        blocksize=args.blocksize,
        latency=args.latency,
        gain=args.gain,
        delay_seconds=args.delay_seconds,
        low_gain=1.0,
        mid_gain=1.0,
        high_gain=1.0,
        pidfile=DIRECT_ROUTER_PIDFILE,
        logfile=DIRECT_ROUTER_LOG,
        control_file=DIRECT_ROUTER_CONTROL,
        stats_file=DIRECT_ROUTER_STATS,
    )
    reapply_role_controls("direct", port=args.port)
    print(json.dumps(payload, indent=2))


def command_direct_relay_stop(args: argparse.Namespace) -> None:
    result = terminate_router(
        DIRECT_ROUTER_PIDFILE,
        script_name="scripts/djm_router.py",
        control_file=DIRECT_ROUTER_CONTROL,
        stats_file=DIRECT_ROUTER_STATS,
    )
    reapply_role_controls("direct", port=args.port)
    print(json.dumps(result, indent=2))


def command_direct_relay_status(args: argparse.Namespace) -> None:
    pid = read_router_pid(DIRECT_ROUTER_PIDFILE)
    payload = {
        "pid": pid,
        "running": router_running(pid),
        "log": str(DIRECT_ROUTER_LOG),
    }
    if DIRECT_ROUTER_LOG.exists():
        payload["log_tail"] = tail_text(DIRECT_ROUTER_LOG)
    if DIRECT_ROUTER_CONTROL.exists():
        payload["control"] = safe_read_json(DIRECT_ROUTER_CONTROL) or tail_text(DIRECT_ROUTER_CONTROL)
    if DIRECT_ROUTER_STATS.exists():
        payload["stats"] = safe_read_json(DIRECT_ROUTER_STATS) or tail_text(DIRECT_ROUTER_STATS)
    print(json.dumps(payload, indent=2))


def analyze_role(role: str, seconds: float, port: int = PORT) -> dict[str, object]:
    cdp = ChromeCDP(port)
    roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    page, status = roles[role]
    analysis = cdp.eval(page, analyze_media_script(seconds))
    return {"role": role, "analysis": analysis, "status": status}


def extract_bpm(analysis_payload: dict[str, object]) -> float | None:
    bpm_payload = analysis_payload.get("bpm")
    if isinstance(bpm_payload, dict):
        value = bpm_payload.get("bpm")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def normalize_bpm_pair(a: float | None, b: float | None) -> dict[str, float | None]:
    if a is None or b is None:
        return {"a": a, "b": b, "delta": None}
    candidates = []
    for ma in (0.5, 1.0, 2.0):
        for mb in (0.5, 1.0, 2.0):
            na = a * ma
            nb = b * mb
            delta = abs(na - nb)
            if 60 <= na <= 200 and 60 <= nb <= 200:
                candidates.append((delta, na, nb))
    if not candidates:
        delta = abs(a - b)
        return {"a": a, "b": b, "delta": delta}
    delta, na, nb = min(candidates, key=lambda item: item[0])
    return {"a": na, "b": nb, "delta": delta}


def extract_energy(analysis_payload: dict[str, object]) -> float:
    value = analysis_payload.get("rmsP95")
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def recommend_transition(direct_analysis: dict[str, object], routed_analysis: dict[str, object]) -> dict[str, object]:
    direct_bpm = extract_bpm(direct_analysis)
    routed_bpm = extract_bpm(routed_analysis)
    normalized = normalize_bpm_pair(direct_bpm, routed_bpm)
    direct_energy = extract_energy(direct_analysis)
    routed_energy = extract_energy(routed_analysis)
    energy_delta = abs(direct_energy - routed_energy)
    bpm_delta = normalized.get("delta")

    if bpm_delta is None:
        return {
            "transition": "manual",
            "reason": "missing BPM estimate",
        }
    if bpm_delta <= 3 and energy_delta <= 0.12:
        return {
            "transition": "bass-swap",
            "reason": "close BPM and similar energy; phrase bass swap is safer than a hard cut",
            "normalized_bpm": normalized,
            "energy_delta": energy_delta,
        }
    if bpm_delta <= 6:
        return {
            "transition": "loop-roll-swap",
            "reason": "moderate BPM gap; use creative cover transition",
            "normalized_bpm": normalized,
            "energy_delta": energy_delta,
        }
    return {
        "transition": "power-off-swap",
        "reason": "large BPM gap; abrupt reset transition is safer than blending",
        "normalized_bpm": normalized,
        "energy_delta": energy_delta,
    }


def command_analyze(args: argparse.Namespace) -> None:
    direct = analyze_role("direct", args.seconds, port=args.port)
    routed = analyze_role("routed", args.seconds, port=args.port)
    direct_analysis = direct.get("analysis") if isinstance(direct.get("analysis"), dict) else {}
    routed_analysis = routed.get("analysis") if isinstance(routed.get("analysis"), dict) else {}
    result = {
        "direct": direct,
        "routed": routed,
        "recommendation": recommend_transition(direct_analysis, routed_analysis),
    }
    print(json.dumps(result, indent=2))


def command_scan_bpm(args: argparse.Namespace) -> None:
    all_tracks, status = playlist_tracks_for_role(args.role, port=args.port)
    live_volume = status.get("volume") if isinstance(status, dict) else None
    if (
        not args.allow_live_disruption
        and isinstance(live_volume, list)
        and live_volume
        and isinstance(live_volume[0], (int, float))
        and float(live_volume[0]) > 1e-3
    ):
        raise SystemExit("scan-bpm refuses to disturb a live deck; park it silent first or pass --allow-live-disruption")
    start = max(0, args.start_index)
    stop = len(all_tracks) if args.limit is None else min(len(all_tracks), start + args.limit)
    target_tracks = all_tracks[start:stop]

    current_title = str(status.get("title", "")).replace(" | YouTube Music", "").strip()
    original_index = None
    for track in all_tracks:
        if isinstance(track, dict) and track.get("title") == current_title:
            original_index = int(track["index"])
            break

    results: list[dict[str, object]] = []
    for track in target_tracks:
        if not isinstance(track, dict):
            continue
        choose_role_track(args.role, index=int(track["index"]), settle_seconds=args.settle_seconds, port=args.port)
        analysis_payload = analyze_role(args.role, args.seconds, port=args.port)
        analysis = analysis_payload.get("analysis") if isinstance(analysis_payload.get("analysis"), dict) else {}
        bpm_payload = analysis.get("bpm") if isinstance(analysis, dict) else None
        results.append(
            {
                "index": track.get("index"),
                "title": track.get("title"),
                "subtitle": track.get("subtitle"),
                "url": track.get("url"),
                "bpm": bpm_payload,
                "energy": {
                    "rmsP95": analysis.get("rmsP95"),
                    "avgRms": analysis.get("avgRms"),
                    "avgCentroidHz": analysis.get("avgCentroidHz"),
                    "fluxMean": analysis.get("fluxMean"),
                },
            }
        )

    restored = None
    if original_index is not None:
        restored = choose_role_track(args.role, index=original_index, settle_seconds=args.settle_seconds, port=args.port)

    print(
        json.dumps(
            {
                "role": args.role,
                "playlist_count": len(all_tracks),
                "scanned_count": len(results),
                "start_index": start,
                "stop_index": stop,
                "results": results,
                "restored": restored,
            },
            indent=2,
        )
    )


def command_build_index(args: argparse.Namespace) -> None:
    all_tracks, status = playlist_tracks_for_role(args.role, port=args.port)
    live_volume = status.get("volume") if isinstance(status, dict) else None
    if (
        not args.allow_live_disruption
        and isinstance(live_volume, list)
        and live_volume
        and isinstance(live_volume[0], (int, float))
        and float(live_volume[0]) > 1e-3
    ):
        raise SystemExit("build-index refuses to disturb a live deck; park it silent first or pass --allow-live-disruption")
    start = max(0, args.start_index)
    stop = len(all_tracks) if args.limit is None else min(len(all_tracks), start + args.limit)
    target_tracks = all_tracks[start:stop]
    current_title = str(status.get("playerBarTitle") or status.get("title", "")).replace(" | YouTube Music", "").strip()
    original_index = None
    for track in all_tracks:
        if isinstance(track, dict) and track.get("title") == current_title:
            original_index = int(track["index"])
            break

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    for track in target_tracks:
        record = {
            "index": track.get("index"),
            "title": track.get("title"),
            "subtitle": track.get("subtitle"),
            "primary_artist": primary_artist(track),
            "url": track.get("url"),
            "online": None,
            "local_scan": None,
        }
        try:
            online = resolve_online_track_metadata(track)
        except Exception as exc:
            online = {"status": "error", "error": str(exc), "query": str(track.get("title") or "")}
        record["online"] = online

        need_local = args.scan_missing and (not isinstance(online, dict) or online.get("status") != "ok")
        if need_local:
            choose_role_track(args.role, index=int(track["index"]), settle_seconds=args.settle_seconds, port=args.port)
            analysis_payload = analyze_role(args.role, args.seconds, port=args.port)
            analysis = analysis_payload.get("analysis") if isinstance(analysis_payload.get("analysis"), dict) else {}
            record["local_scan"] = analysis
        if args.audio_metrics:
            record["audio_metrics"] = ensure_audio_metrics(record)

        records.append(record)
        if args.pause_between > 0:
            time.sleep(args.pause_between)

    restored = None
    if original_index is not None and args.restore_original:
        restored = choose_role_track(args.role, index=original_index, settle_seconds=args.settle_seconds, port=args.port)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "role": args.role,
        "playlist_count": len(all_tracks),
        "start_index": start,
        "stop_index": stop,
        "records": records,
        "restored": restored,
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output_path),
                "record_count": len(records),
                "online_ok": sum(1 for record in records if isinstance(record.get("online"), dict) and record["online"].get("status") == "ok"),
                "online_missing": sum(1 for record in records if isinstance(record.get("online"), dict) and record["online"].get("status") != "ok"),
                "audio_metrics_cached": sum(1 for record in records if isinstance(record.get("audio_metrics"), dict)),
            },
            indent=2,
        )
    )


def command_recommend_next(args: argparse.Namespace) -> None:
    payload = load_index(Path(args.index_path))
    print(
        json.dumps(
            recommend_candidates(
                payload,
                from_role=args.from_role,
                to_role=args.to_role,
                port=args.port,
                limit=args.limit,
            ),
            indent=2,
        )
    )


def command_choose_best_next(args: argparse.Namespace) -> None:
    payload = load_index(Path(args.index_path))
    recommendation = recommend_candidates(
        payload,
        from_role=args.from_role,
        to_role=args.to_role,
        port=args.port,
        limit=max(args.limit, 1),
    )
    suggestions = recommendation.get("recommendations") or []
    if not suggestions:
        raise SystemExit("No recommendations available")
    best = suggestions[0]
    if float(best["score"]) < args.min_score:
        raise SystemExit(
            f"Best candidate score {best['score']:.2f} is below min-score {args.min_score:.2f}"
        )
    chosen_record = next(record for record in payload["records"] if int(record.get("index", -1)) == int(best["index"]))
    chosen = choose_role_track(
        args.to_role,
        index=int(best["index"]),
        settle_seconds=args.settle_seconds,
        port=args.port,
    )
    if args.auto_cue:
        analyze_track_structure(chosen_record)
    cue_seconds = recommended_entry_seconds(chosen_record)
    cue_result = None
    if args.auto_cue:
        cdp = ChromeCDP(args.port)
        page, _status = role_page_and_status(args.to_role, port=args.port)
        cue_result = cdp.eval(page, transport_script(action="seek-abs", seconds=cue_seconds))
        time.sleep(args.settle_seconds)
        readiness = ensure_role_playing(args.to_role, port=args.port, timeout=max(2.5, args.settle_seconds + 1.0))
        if isinstance(cue_result, dict):
            cue_result["readiness"] = readiness
    print(
        json.dumps(
            {
                "recommendation": recommendation["from_track"],
                "chosen_candidate": best,
                "suggested_entry_seconds": cue_seconds,
                "choose_result": chosen,
                "cue_result": cue_result,
            },
            indent=2,
        )
    )


def resolve_track_record(
    *,
    role: str,
    title: str | None,
    index: int | None,
    port: int,
    index_path: str | None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object] | None]:
    index_payload = None
    if index_path:
        path = Path(index_path)
        if path.exists():
            index_payload = load_index(path)
    if title is None and index is None:
        record, status = current_track_record_for_role(role, port=port, index_payload=index_payload)
        return record, status, index_payload

    tracks, status = playlist_tracks_for_role(role, port=port)
    match = None
    if index is not None:
        for track in tracks:
            if int(track.get("index", -1)) == int(index):
                match = dict(track)
                break
    else:
        desired = normalize_title(title or "")
        best: tuple[float, dict[str, object]] | None = None
        for track in tracks:
            score = title_match_score(desired, str(track.get("title") or ""))
            if best is None or score > best[0]:
                best = (score, dict(track))
        if best and best[0] >= 0.68:
            match = best[1]
    if match is None:
        raise SystemExit("Could not resolve the requested playlist track")
    if index_payload:
        merged = match_index_record(index_payload, match)
        if merged:
            resolved = dict(merged)
            for key, value in match.items():
                if resolved.get(key) in (None, "", []):
                    resolved[key] = value
            match = resolved
    return match, status, index_payload


def command_stem_prep(args: argparse.Namespace) -> None:
    record, status, _index_payload = resolve_track_record(
        role=args.role,
        title=args.title,
        index=args.index,
        port=args.port,
        index_path=args.index_path,
    )
    metadata = analyze_prepared_stems(record)
    print(
        json.dumps(
            {
                "role": args.role,
                "live_status": status,
                "track": record,
                "stem_metadata": metadata,
            },
            indent=2,
        )
    )


def command_stem_status(args: argparse.Namespace) -> None:
    record, status, _index_payload = resolve_track_record(
        role=args.role,
        title=args.title,
        index=args.index,
        port=args.port,
        index_path=args.index_path,
    )
    metadata = stem_metadata_for_record(record)
    print(
        json.dumps(
            {
                "role": args.role,
                "live_status": status,
                "track": record,
                "source_audio": str(stem_source_path(record)),
                "preview_audio": str(stem_preview_path(record)),
                "stems": {name: str(path) for name, path in stem_paths(record).items()},
                "stem_metadata": metadata,
            },
            indent=2,
        )
    )


def command_audio_metrics(args: argparse.Namespace) -> None:
    record, status, _index_payload = resolve_track_record(
        role=args.role,
        title=args.title,
        index=args.index,
        port=args.port,
        index_path=args.index_path,
    )
    payload = ensure_audio_metrics(record, force=args.force)
    print(
        json.dumps(
            {
                "role": args.role,
                "live_status": status,
                "track": record,
                "audio_metrics": payload,
            },
            indent=2,
        )
    )


def command_visualize_track_analysis(args: argparse.Namespace) -> None:
    import librosa
    import matplotlib.pyplot as plt
    import numpy as np
    from scipy import signal as scipy_signal

    record, status, _index_payload = resolve_track_record(
        role=args.role,
        title=args.title,
        index=args.index,
        port=args.port,
        index_path=args.index_path,
    )
    structure = analyze_track_structure(record, force=args.force)
    vocal_cues = ensure_vocal_cues(record, prepare=args.prepare_vocals) if args.prepare_vocals else vocal_cue_metadata_for_record(record)
    source = ensure_track_audio(record)
    audio, sample_rate = librosa.load(str(source), sr=22050, mono=True)
    hop_length = 512
    frame_length = 2048
    times = np.arange(audio.size, dtype=np.float32) / float(sample_rate)
    rms = librosa.feature.rms(y=audio, frame_length=frame_length, hop_length=hop_length)[0]
    onset_env = librosa.onset.onset_strength(y=audio, sr=sample_rate, hop_length=hop_length)
    centroid = librosa.feature.spectral_centroid(y=audio, sr=sample_rate, hop_length=hop_length)[0]
    frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=sample_rate, hop_length=hop_length)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sample_rate, hop_length=hop_length)
    beat_times = librosa.frames_to_time(beat_frames, sr=sample_rate, hop_length=hop_length)

    band_sos = scipy_signal.butter(4, [35.0, 180.0], btype="bandpass", fs=sample_rate, output="sos")
    low_band = scipy_signal.sosfiltfilt(band_sos, audio).astype("float32", copy=False)
    low_rms = librosa.feature.rms(y=low_band, frame_length=frame_length, hop_length=hop_length)[0]

    analysis = structure.get("analysis") if isinstance(structure.get("analysis"), dict) else {}
    outgoing = analysis.get("top_outgoing_windows") if isinstance(analysis, dict) else []
    incoming = analysis.get("top_incoming_windows") if isinstance(analysis, dict) else []
    outgoing = outgoing if isinstance(outgoing, list) else []
    incoming = incoming if isinstance(incoming, list) else []

    output = Path(args.output) if args.output else structure_plot_path(record)
    output.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(5, 1, figsize=(14, 11), sharex=True)
    axes[0].plot(times, audio, color="#34495e", linewidth=0.6)
    axes[0].set_ylabel("Wave")
    axes[0].set_title(str(record.get("title") or "Track analysis"))
    axes[0].grid(alpha=0.15)

    axes[1].plot(frame_times, rms, label="RMS", color="#0b84f3", linewidth=1.3)
    axes[1].plot(frame_times, low_rms, label="Low RMS", color="#8e44ad", linewidth=1.1)
    axes[1].set_ylabel("Energy")
    axes[1].legend(loc="upper right")
    axes[1].grid(alpha=0.2)

    axes[2].plot(frame_times, onset_env, label="Onset", color="#16a085", linewidth=1.2)
    axes[2].set_ylabel("Onsets")
    axes[2].legend(loc="upper right")
    axes[2].grid(alpha=0.2)

    axes[3].plot(frame_times, centroid, label="Centroid", color="#e67e22", linewidth=1.2)
    axes[3].set_ylabel("Centroid")
    axes[3].legend(loc="upper right")
    axes[3].grid(alpha=0.2)

    axes[4].plot(frame_times, np.interp(frame_times, frame_times[:len(low_rms)], low_rms[:len(frame_times)] if len(low_rms) >= len(frame_times) else np.pad(low_rms, (0, max(0, len(frame_times) - len(low_rms))), mode="edge")), alpha=0)
    axes[4].set_ylabel("Markers")
    axes[4].set_xlabel("Seconds")
    axes[4].grid(alpha=0.2)

    for axis in axes:
        for beat_time in beat_times:
            axis.axvline(float(beat_time), color="#95a5a6", linewidth=0.45, alpha=0.14)

    for window in outgoing[:4]:
        boundary = window.get("boundary_seconds")
        if isinstance(boundary, (int, float)):
            for axis in axes:
                axis.axvline(float(boundary), color="#c0392b", linewidth=1.2, alpha=0.55)
            axes[4].text(float(boundary), 0.75, f"out:{window.get('label')}", rotation=90, color="#c0392b", fontsize=8, va="bottom")

    for window in incoming[:4]:
        boundary = window.get("boundary_seconds")
        if isinstance(boundary, (int, float)):
            for axis in axes:
                axis.axvline(float(boundary), color="#27ae60", linewidth=1.2, alpha=0.55)
            axes[4].text(float(boundary), 0.15, f"in:{window.get('label')}", rotation=90, color="#27ae60", fontsize=8, va="bottom")

    if isinstance(vocal_cues, dict) and bool(vocal_cues.get("available")):
        analysis_payload = vocal_cues.get("analysis") if isinstance(vocal_cues.get("analysis"), dict) else {}
        top_words = analysis_payload.get("top_wordplay_cues") if isinstance(analysis_payload, dict) else []
        top_words = top_words if isinstance(top_words, list) else []
        for cue in top_words[:10]:
            start = cue.get("start")
            text = cue.get("text")
            if isinstance(start, (int, float)):
                for axis in axes:
                    axis.axvline(float(start), color="#f1c40f", linewidth=0.9, alpha=0.35)
                axes[4].text(float(start), 0.45, str(text or "word"), rotation=90, color="#b7950b", fontsize=7, va="bottom")

    fig.tight_layout()
    fig.savefig(output, dpi=160)
    plt.close(fig)
    print(
        json.dumps(
            {
                "role": args.role,
                "live_status": status,
                "track": record,
                "output": str(output),
                "tempo_bpm": float(np.asarray(tempo).reshape(-1)[0]) if np.asarray(tempo).size else None,
                "beat_count": int(len(beat_times)),
                "outgoing_windows": outgoing[:4],
                "incoming_windows": incoming[:4],
                "vocal_cues_available": bool(isinstance(vocal_cues, dict) and vocal_cues.get("available")),
            },
            indent=2,
        )
    )


def command_vocal_cues(args: argparse.Namespace) -> None:
    record, status, _index_payload = resolve_track_record(
        role=args.role,
        title=args.title,
        index=args.index,
        port=args.port,
        index_path=args.index_path,
    )
    payload = ensure_vocal_cues(record, force=args.force, prepare=args.prepare)
    print(
        json.dumps(
            {
                "role": args.role,
                "live_status": status,
                "track": record,
                "vocal_cues": payload,
            },
            indent=2,
        )
    )


def command_detect_transition_windows(args: argparse.Namespace) -> None:
    payload = load_index(Path(args.index_path))
    roles = resolve_roles(args.port)
    if args.from_role not in roles or args.to_role not in roles:
        raise SystemExit("Both roles must be live")
    source_record, _source_status = current_track_record_for_role(args.from_role, port=args.port, index_payload=payload)
    target_record, _target_status = current_track_record_for_role(args.to_role, port=args.port, index_payload=payload)
    if args.prepare_stems:
        analyze_prepared_stems(target_record)
    detection = detect_transition_windows_between(source_record, target_record, prepare_vocals=args.prepare_vocals)
    print(json.dumps(detection, indent=2))


def command_prewarm(args: argparse.Namespace) -> None:
    payload = load_index(Path(args.index_path))
    source_record, source_status = current_track_record_for_role(args.from_role, port=args.port, index_payload=payload)
    recommendation = recommend_candidates(
        payload,
        from_role=args.from_role,
        to_role=args.to_role,
        port=args.port,
        limit=max(1, args.limit),
    )
    prepared: list[dict[str, object]] = []
    analyze_track_structure(source_record)
    source_audio_metrics = ensure_audio_metrics(source_record)
    if args.prepare_stems:
        analyze_prepared_stems(source_record)
    source_vocal_cues = ensure_vocal_cues(source_record, prepare=args.prepare_vocals) if args.prepare_vocals else None
    for candidate in recommendation.get("recommendations", []):
        if not isinstance(candidate, dict):
            continue
        index_value = candidate.get("index")
        if not isinstance(index_value, int):
            continue
        record = next((item for item in payload["records"] if isinstance(item, dict) and int(item.get("index", -1)) == index_value), None)
        if not isinstance(record, dict):
            continue
        structure = analyze_track_structure(record)
        audio_metrics = ensure_audio_metrics(record)
        stems_ready = False
        if args.prepare_stems:
            analyze_prepared_stems(record)
            stems_ready = True
        vocal_ready = bool(ensure_vocal_cues(record, prepare=True).get("available")) if args.prepare_vocals else False
        audio_metric_payload = audio_metrics.get("metrics") if isinstance(audio_metrics, dict) else {}
        prepared.append(
            {
                "title": record.get("title"),
                "index": record.get("index"),
                "suggested_entry_seconds": recommended_entry_seconds(record),
                "structure_duration_seconds": ((structure.get("analysis") or {}) if isinstance(structure, dict) else {}).get("duration_seconds"),
                "audio_bpm": audio_metric_payload.get("bpm") if isinstance(audio_metric_payload, dict) else None,
                "audio_key": audio_metric_payload.get("key") if isinstance(audio_metric_payload, dict) else None,
                "stems_prepared": stems_ready,
                "vocal_cues_prepared": vocal_ready,
                "score": candidate.get("score"),
            }
        )
    print(
        json.dumps(
            {
                "source_track": source_record,
                "source_status": source_status,
                "source_audio_metrics": source_audio_metrics.get("metrics") if isinstance(source_audio_metrics, dict) else None,
                "source_vocal_cues": source_vocal_cues.get("analysis") if isinstance(source_vocal_cues, dict) else None,
                "prepared_candidates": prepared,
            },
            indent=2,
        )
    )


def command_build_transition_dataset(args: argparse.Namespace) -> None:
    payload = load_index(Path(args.index_path))
    records = [item for item in payload.get("records", []) if isinstance(item, dict)]
    if args.limit:
        records = records[: max(1, int(args.limit))]
    output_path = Path(args.output or transition_dataset_path(args.name))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pair_limit = max(1, int(args.pair_limit))
    written = 0
    with output_path.open("w", encoding="utf-8") as handle:
        for source_index, source_record in enumerate(records):
            for target_record in records[source_index + 1: source_index + 1 + pair_limit]:
                detection = detect_transition_windows_between(
                    source_record,
                    target_record,
                    prepare_vocals=args.prepare_vocals,
                )
                top_pairing = None
                pairings = detection.get("pairings")
                if isinstance(pairings, list) and pairings:
                    top_pairing = pairings[0]
                row = {
                    "version": TRANSITION_DATASET_VERSION,
                    "source_title": source_record.get("title"),
                    "target_title": target_record.get("title"),
                    "source_key": stem_track_key(source_record),
                    "target_key": stem_track_key(target_record),
                    "top_pairing": top_pairing,
                    "normalized_bpm": detection.get("normalized_bpm"),
                    "key_reason": detection.get("key_reason"),
                    "vocal_matches": detection.get("vocal_matches"),
                }
                handle.write(json.dumps(row) + "\n")
                written += 1
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output_path),
                "rows_written": written,
                "limit": len(records),
                "pair_limit": pair_limit,
            },
            indent=2,
        )
    )


def command_stem_render(args: argparse.Namespace) -> None:
    record, status, _index_payload = resolve_track_record(
        role=args.role,
        title=args.title,
        index=args.index,
        port=args.port,
        index_path=args.index_path,
    )
    preset_gains = {
        "instrumental": {"vocals": 0.0, "drums": 1.0, "bass": 1.0, "other": 1.0},
        "drums-bass": {"vocals": 0.0, "drums": 1.0, "bass": 1.0, "other": 0.35},
        "drums-only": {"vocals": 0.0, "drums": 1.0, "bass": 0.0, "other": 0.0},
        "acapella": {"vocals": 1.0, "drums": 0.0, "bass": 0.0, "other": 0.0},
        "no-bass": {"vocals": 1.0, "drums": 1.0, "bass": 0.0, "other": 1.0},
    }[args.preset]
    output = render_stem_mix(
        record,
        vocals=args.vocals if args.vocals is not None else preset_gains["vocals"],
        drums=args.drums if args.drums is not None else preset_gains["drums"],
        bass=args.bass if args.bass is not None else preset_gains["bass"],
        other=args.other if args.other is not None else preset_gains["other"],
    )
    metadata = stem_metadata_for_record(record) or analyze_prepared_stems(record)
    print(
        json.dumps(
            {
                "role": args.role,
                "live_status": status,
                "track": record,
                "preset": args.preset,
                "output": str(output),
                "suggested_entry_seconds": recommended_entry_seconds(record),
                "stem_metadata": metadata,
            },
            indent=2,
        )
    )


def command_stretch_render(args: argparse.Namespace) -> None:
    record, status, _index_payload = resolve_track_record(
        role=args.role,
        title=args.title,
        index=args.index,
        port=args.port,
        index_path=args.index_path,
    )
    output = render_rubberband_preview(
        record,
        tempo_ratio=args.tempo_ratio,
        pitch_semitones=args.pitch_semitones,
        use_preview=not args.full_source,
    )
    print(
        json.dumps(
            {
                "role": args.role,
                "live_status": status,
                "track": record,
                "output": str(output),
                "tempo_ratio": args.tempo_ratio,
                "pitch_semitones": args.pitch_semitones,
                "used_preview": not args.full_source,
            },
            indent=2,
        )
    )


def phrase_wait_seconds(current_time: float, bpm: float, beats: int = 8) -> float:
    beat = 60.0 / bpm
    phrase = beat * beats
    remainder = current_time % phrase
    if remainder <= 1e-6:
        return phrase
    return phrase - remainder


def choose_live_transition_pairing(
    detection: dict[str, object],
    *,
    current_source_time: float,
    min_lead_seconds: float = 1.0,
    max_wait_seconds: float = 48.0,
) -> dict[str, object]:
    pairings = detection.get("pairings")
    if not isinstance(pairings, list) or not pairings:
        raise SystemExit("No transition pairings available")
    future_in_window: list[dict[str, object]] = []
    future_out_of_window: list[dict[str, object]] = []
    for pairing in pairings:
        if not isinstance(pairing, dict):
            continue
        boundary = pairing.get("source_boundary_seconds")
        if not isinstance(boundary, (int, float)):
            continue
        delta = float(boundary) - current_source_time
        if delta < min_lead_seconds:
            continue
        enriched = dict(pairing)
        enriched["wait_seconds"] = round(delta, 3)
        enriched["source_delta_seconds"] = round(delta, 3)
        if delta <= max_wait_seconds:
            future_in_window.append(enriched)
        else:
            future_out_of_window.append(enriched)
    if future_in_window:
        return max(future_in_window, key=lambda item: float(item.get("score", 0.0)))
    if future_out_of_window:
        next_wait = min(float(item.get("wait_seconds", 0.0)) for item in future_out_of_window)
        raise SystemExit(
            f"No transition boundary within max wait ({max_wait_seconds:.2f}s). "
            f"Next viable boundary is {next_wait:.2f}s away."
        )
    raise SystemExit("No future transition boundaries found")


def mapped_live_source_time(
    structure: dict[str, object],
    status: dict[str, object],
) -> tuple[float | None, dict[str, float | bool]]:
    analysis = structure.get("analysis") if isinstance(structure.get("analysis"), dict) else {}
    analyzed_duration = analysis.get("duration_seconds")
    duration_list = status.get("duration") if isinstance(status, dict) else None
    current_time_list = status.get("currentTime") if isinstance(status, dict) else None
    live_duration = float(duration_list[0]) if duration_list else None
    live_current_time = float(current_time_list[0]) if current_time_list else None
    if live_current_time is None or not isinstance(analyzed_duration, (int, float)):
        return None, {"usable": False}
    analyzed_duration = float(analyzed_duration)
    if live_duration and live_duration > 1e-3:
        ratio = analyzed_duration / live_duration
        if 0.85 <= ratio <= 1.15:
            return live_current_time * ratio, {
                "usable": True,
                "scale": ratio,
                "ratio": ratio,
                "live_duration": live_duration,
                "analyzed_duration": analyzed_duration,
            }
        return None, {
            "usable": False,
            "ratio": ratio,
            "live_duration": live_duration,
            "analyzed_duration": analyzed_duration,
        }
    return live_current_time, {
        "usable": True,
        "ratio": 1.0,
        "live_duration": 0.0,
        "analyzed_duration": analyzed_duration,
    }


def preset_args_for_pairing(pairing: dict[str, object], args: argparse.Namespace) -> argparse.Namespace:
    transition = str(pairing.get("execution_preset") or pairing.get("recommended_transition") or "transform-swap")
    source_label = str(pairing.get("source_label") or "")
    target_label = str(pairing.get("target_label") or "")
    overlap_risk = float(pairing.get("overlap_risk") or 0.0)
    section_match = float(pairing.get("section_match_score") or 0.0)
    confidence = float(pairing.get("confidence") or 0.0)

    if transition == "bass-swap":
        fade_seconds = 5.2 if source_label == "outro" and target_label == "intro" else 4.8
        fade_seconds += min(1.4, 1.1 * section_match)
        fade_seconds -= min(1.0, overlap_risk * 1.35)
        fade_seconds = max(4.4, min(6.8, fade_seconds))
        return argparse.Namespace(
            name="bass-swap",
            from_role=args.from_role,
            to_role=args.to_role,
            effect_seconds=0.0,
            lead_in=0.0,
            fade_seconds=fade_seconds,
            steps=max(10, min(14, args.steps)),
            loop_seconds=0.5,
            gate_ms=125,
            port=args.port,
        )
    if transition == "staged-swap":
        fade_seconds = 3.2 + 1.9 * section_match + 0.6 * confidence - 1.1 * overlap_risk
        fade_seconds = max(3.0, min(5.8, fade_seconds))
        return argparse.Namespace(
            name="staged-swap",
            from_role=args.from_role,
            to_role=args.to_role,
            effect_seconds=0.0,
            lead_in=0.0,
            fade_seconds=fade_seconds,
            steps=max(10, min(18, int(round(12 + 6 * confidence)))),
            loop_seconds=0.5,
            gate_ms=125,
            port=args.port,
        )
    if transition == "loop-roll-swap":
        effect_seconds = 0.95 + 0.5 * section_match - 0.3 * overlap_risk
        fade_seconds = 1.55 + 1.2 * section_match - 0.85 * overlap_risk
        return argparse.Namespace(
            name="loop-roll-swap",
            from_role=args.from_role,
            to_role=args.to_role,
            effect_seconds=max(0.8, min(1.5, effect_seconds)),
            lead_in=0.12 if overlap_risk >= 0.28 else 0.18,
            fade_seconds=max(1.35, min(2.6, fade_seconds)),
            steps=max(8, min(14, int(round(10 + 4 * confidence)))),
            loop_seconds=0.5,
            gate_ms=100,
            port=args.port,
        )
    if transition == "power-off-swap":
        return argparse.Namespace(
            name="transform-swap",
            from_role=args.from_role,
            to_role=args.to_role,
            effect_seconds=max(0.65, min(1.0, 0.75 + 0.25 * overlap_risk)),
            lead_in=0.08,
            fade_seconds=max(0.55, min(1.0, 0.8 - 0.15 * section_match + 0.2 * overlap_risk)),
            steps=max(8, min(12, int(round(8 + 3 * confidence)))),
            loop_seconds=0.5,
            gate_ms=90,
            port=args.port,
        )
    if transition == "simple-transition":
        return argparse.Namespace(
            name="simple-transition",
            from_role=args.from_role,
            to_role=args.to_role,
            mode="staged",
            seconds=max(1.0, min(2.8, 1.4 + 1.2 * confidence - 0.3 * overlap_risk)),
            port=args.port,
        )
    return argparse.Namespace(
        name="transform-swap",
        from_role=args.from_role,
        to_role=args.to_role,
        effect_seconds=max(0.65, min(1.0, 0.78 + 0.2 * overlap_risk)),
        lead_in=0.08,
        fade_seconds=max(0.55, min(1.0, 0.78 - 0.12 * section_match + 0.2 * overlap_risk)),
        steps=max(8, min(12, int(round(8 + 3 * confidence)))),
        loop_seconds=0.5,
        gate_ms=90,
        port=args.port,
    )


def command_assist(args: argparse.Namespace) -> None:
    index_payload = None
    index_path = Path(args.index_path)
    if index_path.exists():
        index_payload = load_index(index_path)
    analysis_from = analyze_role(args.from_role, args.analyze_seconds, port=args.port)
    analysis_to = analyze_role(args.to_role, args.analyze_seconds, port=args.port)
    from_analysis = analysis_from.get("analysis") if isinstance(analysis_from.get("analysis"), dict) else {}
    to_analysis = analysis_to.get("analysis") if isinstance(analysis_to.get("analysis"), dict) else {}
    from_bpm = ((from_analysis.get("bpm") or {}) if isinstance(from_analysis, dict) else {})
    to_bpm = ((to_analysis.get("bpm") or {}) if isinstance(to_analysis, dict) else {})
    source_record = None
    target_record = None
    structure_plan = None
    preset_namespace = None
    cue_result = None
    execution_payload = None
    plan_source = "analysis-only"
    if index_payload:
        try:
            source_record, source_status = current_track_record_for_role(args.from_role, port=args.port, index_payload=index_payload)
            target_record, _target_status = current_track_record_for_role(args.to_role, port=args.port, index_payload=index_payload)
            detection = detect_transition_windows_between(source_record, target_record, prepare_vocals=getattr(args, "prepare_vocals", False))
            mapped_time, timing_meta = mapped_live_source_time(detection.get("source_structure") if isinstance(detection, dict) else {}, source_status)
            if mapped_time is not None:
                structure_plan = choose_live_transition_pairing(
                    detection,
                    current_source_time=mapped_time,
                    min_lead_seconds=max(0.75, float(args.min_structure_lead)),
                    max_wait_seconds=max(8.0, float(args.max_structure_wait)),
                )
                if isinstance(structure_plan, dict):
                    structure_plan["timing_meta"] = timing_meta
                    preset_namespace = preset_args_for_pairing(structure_plan, args)
                    plan_source = "structure"
            else:
                structure_plan = {"timing_meta": timing_meta, "warning": "live duration does not match analyzed structure closely enough"}
            target_entry_seconds = structure_plan.get("target_entry_seconds") if isinstance(structure_plan, dict) else None
            if isinstance(target_entry_seconds, (int, float)):
                cdp = ChromeCDP(args.port)
                page, _status = role_page_and_status(args.to_role, port=args.port)
                pre_cue_status = park_role_silent(args.to_role, port=args.port)
                cue_eval = cdp.eval(page, transport_script(action="seek-abs", seconds=float(target_entry_seconds)))
                time.sleep(min(1.0, args.analyze_seconds / 3))
                readiness = ensure_role_playing(args.to_role, port=args.port, timeout=3.5)
                parked_status = park_role_silent(args.to_role, port=args.port)
                cue_result = {
                    "pre_cue_status": pre_cue_status,
                    "cue": cue_eval,
                    "readiness": readiness,
                    "parked_status": parked_status,
                }
        except Exception as exc:
            structure_plan = {"error": str(exc), "fallback": "analysis-only"}

    recommendation = recommend_transition(
        from_analysis if args.from_role == "direct" else to_analysis,
        to_analysis if args.to_role == "routed" else from_analysis,
    )

    wait_seconds = 0.0
    from_status = analysis_from.get("status") or {}
    if structure_plan and isinstance(structure_plan, dict) and structure_plan.get("wait_seconds") is not None:
        wait_seconds = float(structure_plan["wait_seconds"])
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    elif args.wait_phrase and isinstance(from_bpm, dict) and from_bpm.get("bpm"):
        current_time_list = from_status.get("currentTime") if isinstance(from_status, dict) else None
        current_time = float(current_time_list[0]) if current_time_list else 0.0
        wait_seconds = min(16.0, phrase_wait_seconds(current_time, float(from_bpm["bpm"]), beats=args.phrase_beats))
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    if args.to_role == "routed":
        write_router_control(low_gain=args.incoming_low_gain, mid_gain=1.0, high_gain=1.05)
    chosen_mode = args.mode
    if args.mode == "auto":
        chosen_mode = "staged" if recommendation["transition"] == "staged" else "crossfade"

    if preset_namespace is not None:
        if getattr(preset_namespace, "name", "") == "simple-transition":
            execution_payload = run_simple_transition(preset_namespace)
        else:
            execution_payload = run_preset(preset_namespace)
        if args.to_role == "routed":
            write_router_control(low_gain=1.0)
        print(
            json.dumps(
                {
                    "ok": True,
                    "wait_seconds": wait_seconds,
                    "from_bpm": from_bpm,
                    "to_bpm": to_bpm,
                    "recommendation": recommendation,
                    "structure_plan": structure_plan,
                    "plan_source": plan_source,
                    "cue_result": cue_result,
                    "source_track": source_record,
                    "target_track": target_record,
                    "execution": execution_payload,
                },
                indent=2,
            )
        )
        return

    if recommendation["transition"] == "loop-roll-swap":
        execution_payload = run_preset(
            argparse.Namespace(
                name="loop-roll-swap",
                from_role=args.from_role,
                to_role=args.to_role,
                effect_seconds=2.0,
                lead_in=0.6,
                fade_seconds=max(3.0, min(6.0, args.seconds)),
                steps=max(16, min(32, args.steps)),
                loop_seconds=0.5,
                gate_ms=125,
                port=args.port,
            )
        )
    elif recommendation["transition"] == "bass-swap":
        execution_payload = run_preset(
            argparse.Namespace(
                name="bass-swap",
                from_role=args.from_role,
                to_role=args.to_role,
                effect_seconds=0.0,
                lead_in=0.0,
                fade_seconds=max(5.0, min(8.0, args.seconds)),
                steps=max(10, min(18, args.steps)),
                loop_seconds=0.5,
                gate_ms=125,
                port=args.port,
            )
        )
    elif recommendation["transition"] == "power-off-swap":
        execution_payload = run_preset(
            argparse.Namespace(
                name="power-off-swap",
                from_role=args.from_role,
                to_role=args.to_role,
                effect_seconds=2.0,
                lead_in=0.5,
                fade_seconds=max(2.5, min(5.0, args.seconds)),
                steps=max(16, min(24, args.steps)),
                loop_seconds=0.5,
                gate_ms=125,
                port=args.port,
            )
        )
    else:
        execution_payload = run_fade(
            argparse.Namespace(
                from_role=args.from_role,
                to_role=args.to_role,
                mode=chosen_mode,
                seconds=args.seconds,
                steps=args.steps,
                port=args.port,
            )
        )
    if args.to_role == "routed":
        write_router_control(low_gain=1.0)
    print(
        json.dumps(
            {
                "ok": True,
                "wait_seconds": wait_seconds,
                "from_bpm": from_bpm,
                "to_bpm": to_bpm,
                "recommendation": recommendation,
                "structure_plan": structure_plan,
                "plan_source": plan_source,
                "cue_result": cue_result,
                "execution": execution_payload,
            },
            indent=2,
        )
    )


def effect_script(effect: str, *, seconds: float, loop_seconds: float, gate_ms: int, start_at_ms: int) -> str:
    if effect == "loop-roll":
        return f"""
(() => {{
  const element = document.querySelector('audio,video');
  if (!element) return {{ ok: false, error: 'no-media-element' }};
  const durationMs = {seconds * 1000!r};
  const loopSeconds = {loop_seconds!r};
  const startAtMs = {start_at_ms!r};
  const begin = () => {{
    const startTime = element.currentTime;
    const startedAt = performance.now();
    window.__codexEffectTimer && clearInterval(window.__codexEffectTimer);
    window.__codexEffectFinish && clearTimeout(window.__codexEffectFinish);
    window.__codexEffectTimer = setInterval(() => {{
      const elapsed = (performance.now() - startedAt) / 1000;
      const loopEnd = startTime + loopSeconds;
      if (element.currentTime >= loopEnd || element.currentTime < startTime) {{
        element.currentTime = startTime;
      }}
      if (elapsed >= durationMs / 1000) {{
        clearInterval(window.__codexEffectTimer);
      }}
    }}, 25);
    window.__codexEffectFinish = setTimeout(() => {{
      const elapsed = (performance.now() - startedAt) / 1000;
      clearInterval(window.__codexEffectTimer);
      element.currentTime = startTime + elapsed;
    }}, durationMs);
  }};
  setTimeout(begin, Math.max(0, startAtMs - Date.now()));
  return {{ ok: true, scheduled: true, effect: 'loop-roll', durationMs, startAtMs }};
}})()
""".strip()
    if effect == "power-off":
        return f"""
(() => {{
  const element = document.querySelector('audio,video');
  if (!element) return {{ ok: false, error: 'no-media-element' }};
  const durationMs = {seconds * 1000!r};
  const startAtMs = {start_at_ms!r};
  const begin = () => {{
    const startedAt = performance.now();
    const startRate = element.playbackRate || 1;
    window.__codexEffectTimer && clearInterval(window.__codexEffectTimer);
    window.__codexEffectFinish && clearTimeout(window.__codexEffectFinish);
    window.__codexEffectTimer = setInterval(() => {{
      const progress = Math.min(1, (performance.now() - startedAt) / durationMs);
      const rate = Math.max(0.1, startRate * (1 - 0.85 * progress));
      element.playbackRate = rate;
      if (progress >= 1) clearInterval(window.__codexEffectTimer);
    }}, 30);
    window.__codexEffectFinish = setTimeout(() => {{
      clearInterval(window.__codexEffectTimer);
      element.playbackRate = startRate;
    }}, durationMs + 20);
  }};
  setTimeout(begin, Math.max(0, startAtMs - Date.now()));
  return {{ ok: true, scheduled: true, effect: 'power-off', durationMs, startAtMs }};
}})()
""".strip()
    if effect == "transform":
        return f"""
(() => {{
  const element = document.querySelector('audio,video');
  if (!element) return {{ ok: false, error: 'no-media-element' }};
  const durationMs = {seconds * 1000!r};
  const gateMs = {gate_ms!r};
  const startAtMs = {start_at_ms!r};
  const begin = () => {{
    const startVolume = element.volume;
    let on = true;
    window.__codexEffectTimer && clearInterval(window.__codexEffectTimer);
    window.__codexEffectFinish && clearTimeout(window.__codexEffectFinish);
    window.__codexEffectTimer = setInterval(() => {{
      on = !on;
      element.volume = on ? startVolume : 0;
    }}, gateMs);
    window.__codexEffectFinish = setTimeout(() => {{
      clearInterval(window.__codexEffectTimer);
      element.volume = startVolume;
    }}, durationMs);
  }};
  setTimeout(begin, Math.max(0, startAtMs - Date.now()));
  return {{ ok: true, scheduled: true, effect: 'transform', durationMs, gateMs, startAtMs }};
}})()
""".strip()
    raise SystemExit(f"Unsupported effect: {effect}")


def run_effect(
    role: str,
    *,
    effect: str,
    seconds: float,
    loop_seconds: float,
    gate_ms: int,
    start_at_ms: int,
    port: int = PORT,
) -> dict[str, object]:
    cdp = ChromeCDP(port)
    roles = resolve_roles(port)
    if role not in roles:
        raise SystemExit(f"Role not found: {role}")
    page, status = roles[role]
    result = cdp.eval(
        page,
        effect_script(
            effect,
            seconds=seconds,
            loop_seconds=loop_seconds,
            gate_ms=gate_ms,
            start_at_ms=start_at_ms,
        ),
    )
    return {"role": role, "effect": result, "status": status}


def command_effect(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            run_effect(
                args.role,
                effect=args.effect,
                seconds=args.seconds,
                loop_seconds=args.loop_seconds,
                gate_ms=args.gate_ms,
                start_at_ms=int(time.time() * 1000 + 100),
                port=args.port,
            ),
            indent=2,
        )
    )


def run_preset(args: argparse.Namespace) -> dict[str, object]:
    start = time.perf_counter()
    roles = resolve_roles(args.port)
    cdp = ChromeCDP(args.port)
    queue_from = ensure_queue_locked(args.from_role, port=args.port, cdp=cdp, roles=roles)
    queue_to = ensure_queue_locked(args.to_role, port=args.port, cdp=cdp, roles=roles)
    readiness = ensure_role_playing(args.to_role, port=args.port, timeout=3.0)
    effect_start_at_ms = int(time.time() * 1000 + 180)
    fade_start_at_ms = effect_start_at_ms + int(max(0.0, args.lead_in) * 1000)
    if args.name == "bass-swap":
        payload = run_bass_swap_transition(
            args.from_role,
            args.to_role,
            seconds=args.fade_seconds,
            steps=args.steps,
            port=args.port,
        )
        payload["debug"] = {
            "queue_from": queue_from,
            "queue_to": queue_to,
            "incoming_readiness": readiness,
            "elapsed_seconds": round(time.perf_counter() - start, 3),
        }
        return payload
    if args.name == "staged-swap":
        fade_payload = run_fade(
            argparse.Namespace(
                from_role=args.from_role,
                to_role=args.to_role,
                mode="staged",
                seconds=max(1.5, float(args.fade_seconds)),
                steps=max(8, int(args.steps)),
                start_at_ms=fade_start_at_ms,
                port=args.port,
            )
        )
        payload = {
            "preset": args.name,
            "fade": fade_payload,
            "debug": {
                "queue_from": queue_from,
                "queue_to": queue_to,
                "incoming_readiness": readiness,
                "elapsed_seconds": round(time.perf_counter() - start, 3),
            },
        }
        return payload
    if args.name == "loop-roll-swap":
        effect_result = run_effect(
            args.from_role,
            effect="loop-roll",
            seconds=args.effect_seconds,
            loop_seconds=args.loop_seconds,
            gate_ms=args.gate_ms,
            start_at_ms=effect_start_at_ms,
            port=args.port,
        )
        fade_payload = run_fade(
            argparse.Namespace(
                from_role=args.from_role,
                to_role=args.to_role,
                mode="crossfade",
                seconds=args.fade_seconds,
                steps=args.steps,
                start_at_ms=fade_start_at_ms,
                port=args.port,
            )
        )
        payload = {
            "preset": args.name,
            "effect": effect_result,
            "fade": fade_payload,
            "debug": {
                "queue_from": queue_from,
                "queue_to": queue_to,
                "incoming_readiness": readiness,
                "elapsed_seconds": round(time.perf_counter() - start, 3),
            },
        }
        return payload
    if args.name == "power-off-swap":
        effect_result = run_effect(
            args.from_role,
            effect="power-off",
            seconds=args.effect_seconds,
            loop_seconds=args.loop_seconds,
            gate_ms=args.gate_ms,
            start_at_ms=effect_start_at_ms,
            port=args.port,
        )
        fade_payload = run_fade(
            argparse.Namespace(
                from_role=args.from_role,
                to_role=args.to_role,
                mode="crossfade",
                seconds=args.fade_seconds,
                steps=args.steps,
                start_at_ms=fade_start_at_ms,
                port=args.port,
            )
        )
        payload = {
            "preset": args.name,
            "effect": effect_result,
            "fade": fade_payload,
            "debug": {
                "queue_from": queue_from,
                "queue_to": queue_to,
                "incoming_readiness": readiness,
                "elapsed_seconds": round(time.perf_counter() - start, 3),
            },
        }
        return payload
    if args.name == "transform-swap":
        effect_result = run_effect(
            args.from_role,
            effect="transform",
            seconds=args.effect_seconds,
            loop_seconds=args.loop_seconds,
            gate_ms=args.gate_ms,
            start_at_ms=effect_start_at_ms,
            port=args.port,
        )
        fade_payload = run_fade(
            argparse.Namespace(
                from_role=args.from_role,
                to_role=args.to_role,
                mode="crossfade",
                seconds=max(0.35, float(args.fade_seconds)),
                steps=max(6, int(args.steps)),
                start_at_ms=fade_start_at_ms,
                port=args.port,
            )
        )
        payload = {
            "preset": args.name,
            "effect": effect_result,
            "fade": fade_payload,
            "debug": {
                "queue_from": queue_from,
                "queue_to": queue_to,
                "incoming_readiness": readiness,
                "elapsed_seconds": round(time.perf_counter() - start, 3),
            },
        }
        return payload
    raise SystemExit(f"Unsupported preset: {args.name}")


def command_preset(args: argparse.Namespace) -> None:
    print(json.dumps(run_preset(args), indent=2))


def command_watch(args: argparse.Namespace) -> None:
    start = time.monotonic()
    stream = bool(getattr(args, "stream", False))
    samples: list[dict[str, object]] = []
    while True:
        elapsed = time.monotonic() - start
        snapshot: dict[str, object] = {"elapsed": round(elapsed, 3)}
        roles = resolve_roles(args.port)
        for role, (_page, status) in roles.items():
            snapshot[role] = {
                "title": status.get("title"),
                "currentTime": status.get("currentTime"),
                "volume": status.get("volume"),
                "playbackRate": status.get("playbackRate"),
                "paused": status.get("paused"),
            }
        if stream:
            print(json.dumps(snapshot), flush=True)
        else:
            samples.append(snapshot)
        if elapsed >= args.seconds:
            break
        time.sleep(args.interval)
    if not stream:
        print(json.dumps(samples, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.add_argument("--port", type=int, default=PORT)
    status.set_defaults(func=command_status)

    doctor = sub.add_parser("doctor", help="Dump browser/router state for debugging")
    doctor.add_argument("--port", type=int, default=PORT)
    doctor.set_defaults(func=command_doctor)

    hot = sub.add_parser("hot", help="Set one role hot and the other silent")
    hot.add_argument("role", choices=["direct", "routed"])
    hot.add_argument("--port", type=int, default=PORT)
    hot.set_defaults(func=command_hot)

    lock_queue = sub.add_parser("lock-queue", help="Force repeat/shuffle into the stable queue state")
    lock_queue.add_argument("--role", choices=["direct", "routed"], required=True)
    lock_queue.add_argument("--port", type=int, default=PORT)
    lock_queue.set_defaults(func=command_lock_queue)

    playlist = sub.add_parser("playlist", help="List playlist tracks visible in one live role")
    playlist.add_argument("--role", choices=["direct", "routed"], required=True)
    playlist.add_argument("--port", type=int, default=PORT)
    playlist.set_defaults(func=command_playlist)

    choose = sub.add_parser("choose", help="Choose a playlist track in one live role by title or index")
    choose.add_argument("--role", choices=["direct", "routed"], required=True)
    choose.add_argument("--title", help="Case-insensitive title substring")
    choose.add_argument("--index", type=int, help="Visible playlist index")
    choose.add_argument("--settle-seconds", type=float, default=1.5)
    choose.add_argument("--port", type=int, default=PORT)
    choose.set_defaults(func=command_choose)

    transport = sub.add_parser("transport", help="Play/pause/next/prev/seek on one live role")
    transport.add_argument("--role", choices=["direct", "routed"], required=True)
    transport.add_argument("--action", choices=["play", "pause", "next", "prev", "seek-abs", "seek-rel"], required=True)
    transport.add_argument("--seconds", type=float, help="Used for seek-abs/seek-rel")
    transport.add_argument("--settle-seconds", type=float, default=0.8)
    transport.add_argument("--port", type=int, default=PORT)
    transport.set_defaults(func=command_transport)

    fade = sub.add_parser("fade", help="Fade between the live direct/routed tabs by role")
    fade.add_argument("--from-role", choices=["direct", "routed"], required=True)
    fade.add_argument("--to-role", choices=["direct", "routed"], required=True)
    fade.add_argument("--mode", choices=["staged", "crossfade"], default="staged")
    fade.add_argument("--seconds", type=float, default=12.0)
    fade.add_argument("--steps", type=int, default=48)
    fade.add_argument("--port", type=int, default=PORT)
    fade.set_defaults(func=command_fade)

    simple_transition = sub.add_parser("simple-transition", help="Do a dumb reliable fade with minimal logic")
    simple_transition.add_argument("--from-role", choices=["direct", "routed"])
    simple_transition.add_argument("--to-role", choices=["direct", "routed"])
    simple_transition.add_argument("--mode", choices=["staged", "crossfade"], default="crossfade")
    simple_transition.add_argument("--seconds", type=float, default=2.2)
    simple_transition.add_argument("--steps", type=int, default=24)
    simple_transition.add_argument("--ready-timeout", type=float, default=3.0)
    simple_transition.add_argument("--port", type=int, default=PORT)
    simple_transition.set_defaults(func=command_simple_transition)

    routed_start = sub.add_parser("routed-start", help="Start the routed BlackHole -> DJM leg with FX")
    routed_start.add_argument("--pair", default="5/6")
    routed_start.add_argument("--blocksize", type=int, default=0)
    routed_start.add_argument("--latency", choices=["low", "high"], default="high")
    routed_start.add_argument("--gain", type=float, default=1.35)
    routed_start.add_argument("--delay-seconds", type=float, default=0.0)
    routed_start.add_argument("--low-gain", type=float, default=1.0)
    routed_start.add_argument("--mid-gain", type=float, default=1.0)
    routed_start.add_argument("--high-gain", type=float, default=1.0)
    routed_start.set_defaults(func=command_routed_start)

    routed_stop = sub.add_parser("routed-stop")
    routed_stop.set_defaults(func=command_routed_stop)

    routed_status = sub.add_parser("routed-status")
    routed_status.set_defaults(func=command_routed_status)

    routed_set = sub.add_parser("routed-set-fx", help="Update live gain/EQ on the routed leg")
    routed_set.add_argument("--gain", type=float)
    routed_set.add_argument("--low-gain", type=float)
    routed_set.add_argument("--mid-gain", type=float)
    routed_set.add_argument("--high-gain", type=float)
    routed_set.set_defaults(func=command_routed_set_fx)

    direct_relay_start = sub.add_parser("direct-relay-start", help="Start the matched-latency direct leg via BlackHole 16ch -> DJM")
    direct_relay_start.add_argument("--pair", default="1/2")
    direct_relay_start.add_argument("--blocksize", type=int, default=0)
    direct_relay_start.add_argument("--latency", choices=["low", "high"], default="high")
    direct_relay_start.add_argument("--gain", type=float, default=1.0)
    direct_relay_start.add_argument("--delay-seconds", type=float, default=0.0)
    direct_relay_start.add_argument("--port", type=int, default=PORT)
    direct_relay_start.set_defaults(func=command_direct_relay_start)

    direct_relay_stop = sub.add_parser("direct-relay-stop")
    direct_relay_stop.add_argument("--port", type=int, default=PORT)
    direct_relay_stop.set_defaults(func=command_direct_relay_stop)

    direct_relay_status = sub.add_parser("direct-relay-status")
    direct_relay_status.set_defaults(func=command_direct_relay_status)

    analyze = sub.add_parser("analyze", help="Analyze current direct/routed roles")
    analyze.add_argument("--seconds", type=float, default=6.0)
    analyze.add_argument("--port", type=int, default=PORT)
    analyze.set_defaults(func=command_analyze)

    scan_bpm = sub.add_parser("scan-bpm", help="Scan visible playlist tracks for rough BPM on one role")
    scan_bpm.add_argument("--role", choices=["direct", "routed"], required=True)
    scan_bpm.add_argument("--start-index", type=int, default=0)
    scan_bpm.add_argument("--limit", type=int, help="How many tracks to scan from the visible playlist")
    scan_bpm.add_argument("--seconds", type=float, default=5.0, help="Analyze this many seconds per track")
    scan_bpm.add_argument("--settle-seconds", type=float, default=1.5)
    scan_bpm.add_argument("--allow-live-disruption", action="store_true")
    scan_bpm.add_argument("--port", type=int, default=PORT)
    scan_bpm.set_defaults(func=command_scan_bpm)

    build_index = sub.add_parser("build-index", help="Build a saved playlist index with online BPM metadata")
    build_index.add_argument("--role", choices=["direct", "routed"], required=True)
    build_index.add_argument("--output", default=str(DEFAULT_INDEX_PATH))
    build_index.add_argument("--start-index", type=int, default=0)
    build_index.add_argument("--limit", type=int, help="How many playlist rows to index")
    build_index.add_argument("--scan-missing", action="store_true", help="Run local BPM scan when online lookup fails")
    build_index.add_argument("--audio-metrics", action="store_true", help="Download audio and cache local aubio/keyfinder metrics")
    build_index.add_argument("--seconds", type=float, default=5.0, help="Analyze this many seconds for local fallback scans")
    build_index.add_argument("--settle-seconds", type=float, default=1.5)
    build_index.add_argument("--pause-between", type=float, default=0.0)
    build_index.add_argument("--restore-original", action="store_true", default=True)
    build_index.add_argument("--allow-live-disruption", action="store_true")
    build_index.add_argument("--port", type=int, default=PORT)
    build_index.set_defaults(func=command_build_index)

    recommend_next = sub.add_parser("recommend-next", help="Recommend better next songs from the saved playlist index")
    recommend_next.add_argument("--from-role", choices=["direct", "routed"], required=True)
    recommend_next.add_argument("--to-role", choices=["direct", "routed"], required=True)
    recommend_next.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    recommend_next.add_argument("--limit", type=int, default=5)
    recommend_next.add_argument("--port", type=int, default=PORT)
    recommend_next.set_defaults(func=command_recommend_next)

    choose_best = sub.add_parser("choose-best-next", help="Switch the target role to the best recommended next song")
    choose_best.add_argument("--from-role", choices=["direct", "routed"], required=True)
    choose_best.add_argument("--to-role", choices=["direct", "routed"], required=True)
    choose_best.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    choose_best.add_argument("--limit", type=int, default=5)
    choose_best.add_argument("--min-score", type=float, default=45.0)
    choose_best.add_argument("--auto-cue", action="store_true")
    choose_best.add_argument("--settle-seconds", type=float, default=1.5)
    choose_best.add_argument("--port", type=int, default=PORT)
    choose_best.set_defaults(func=command_choose_best_next)

    stem_prep = sub.add_parser("stem-prep", help="Download, separate, and analyze stems for a playlist/current track")
    stem_prep.add_argument("--role", choices=["direct", "routed"], required=True)
    stem_prep.add_argument("--title", help="Prep a specific visible playlist title without changing the live deck")
    stem_prep.add_argument("--index", type=int, help="Prep a specific visible playlist index without changing the live deck")
    stem_prep.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    stem_prep.add_argument("--port", type=int, default=PORT)
    stem_prep.set_defaults(func=command_stem_prep)

    stem_status = sub.add_parser("stem-status", help="Show cached stem assets and metadata for a playlist/current track")
    stem_status.add_argument("--role", choices=["direct", "routed"], required=True)
    stem_status.add_argument("--title")
    stem_status.add_argument("--index", type=int)
    stem_status.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    stem_status.add_argument("--port", type=int, default=PORT)
    stem_status.set_defaults(func=command_stem_status)

    audio_metrics = sub.add_parser("audio-metrics", help="Cache local BPM/key/onset metrics for a playlist/current track")
    audio_metrics.add_argument("--role", choices=["direct", "routed"], required=True)
    audio_metrics.add_argument("--title")
    audio_metrics.add_argument("--index", type=int)
    audio_metrics.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    audio_metrics.add_argument("--force", action="store_true")
    audio_metrics.add_argument("--port", type=int, default=PORT)
    audio_metrics.set_defaults(func=command_audio_metrics)

    visualize_analysis = sub.add_parser("visualize-track-analysis", help="Render waveform/energy/onset/section overlays for a track")
    visualize_analysis.add_argument("--role", choices=["direct", "routed"], required=True)
    visualize_analysis.add_argument("--title")
    visualize_analysis.add_argument("--index", type=int)
    visualize_analysis.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    visualize_analysis.add_argument("--prepare-vocals", action="store_true")
    visualize_analysis.add_argument("--force", action="store_true")
    visualize_analysis.add_argument("--output")
    visualize_analysis.add_argument("--port", type=int, default=PORT)
    visualize_analysis.set_defaults(func=command_visualize_track_analysis)

    vocal_cues = sub.add_parser("vocal-cues", help="Prepare or inspect cached vocal-word timing cues for a playlist/current track")
    vocal_cues.add_argument("--role", choices=["direct", "routed"], required=True)
    vocal_cues.add_argument("--title")
    vocal_cues.add_argument("--index", type=int)
    vocal_cues.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    vocal_cues.add_argument("--prepare", action="store_true", help="Generate cues with faster-whisper if available")
    vocal_cues.add_argument("--force", action="store_true")
    vocal_cues.add_argument("--port", type=int, default=PORT)
    vocal_cues.set_defaults(func=command_vocal_cues)

    detect_windows = sub.add_parser("detect-transition-windows", help="Detect structural transition windows between the live tracks")
    detect_windows.add_argument("--from-role", choices=["direct", "routed"], required=True)
    detect_windows.add_argument("--to-role", choices=["direct", "routed"], required=True)
    detect_windows.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    detect_windows.add_argument("--prepare-stems", action="store_true", help="Run stem prep on the target track first for better entry scoring")
    detect_windows.add_argument("--prepare-vocals", action="store_true", help="Generate vocal-word timing cues if faster-whisper is available")
    detect_windows.add_argument("--port", type=int, default=PORT)
    detect_windows.set_defaults(func=command_detect_transition_windows)

    prewarm = sub.add_parser("prewarm", help="Precompute structure/stem analysis for the hot deck and likely next songs")
    prewarm.add_argument("--from-role", choices=["direct", "routed"], required=True)
    prewarm.add_argument("--to-role", choices=["direct", "routed"], required=True)
    prewarm.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    prewarm.add_argument("--limit", type=int, default=4)
    prewarm.add_argument("--prepare-stems", action="store_true")
    prewarm.add_argument("--prepare-vocals", action="store_true")
    prewarm.add_argument("--port", type=int, default=PORT)
    prewarm.set_defaults(func=command_prewarm)

    transition_dataset = sub.add_parser("build-transition-dataset", help="Export offline transition candidate rows for review or model training")
    transition_dataset.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    transition_dataset.add_argument("--output")
    transition_dataset.add_argument("--name", default="transition_candidates")
    transition_dataset.add_argument("--limit", type=int, default=12)
    transition_dataset.add_argument("--pair-limit", type=int, default=3)
    transition_dataset.add_argument("--prepare-vocals", action="store_true")
    transition_dataset.set_defaults(func=command_build_transition_dataset)

    stem_render = sub.add_parser("stem-render", help="Render a stem mix preset such as instrumental or drums-bass")
    stem_render.add_argument("--role", choices=["direct", "routed"], required=True)
    stem_render.add_argument("--title")
    stem_render.add_argument("--index", type=int)
    stem_render.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    stem_render.add_argument("--preset", choices=["instrumental", "drums-bass", "drums-only", "acapella", "no-bass"], default="instrumental")
    stem_render.add_argument("--vocals", type=float)
    stem_render.add_argument("--drums", type=float)
    stem_render.add_argument("--bass", type=float)
    stem_render.add_argument("--other", type=float)
    stem_render.add_argument("--port", type=int, default=PORT)
    stem_render.set_defaults(func=command_stem_render)

    stretch_render = sub.add_parser("stretch-render", help="Render an offline stretched/pitched preview with rubberband")
    stretch_render.add_argument("--role", choices=["direct", "routed"], required=True)
    stretch_render.add_argument("--title")
    stretch_render.add_argument("--index", type=int)
    stretch_render.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    stretch_render.add_argument("--tempo-ratio", type=float, default=1.0)
    stretch_render.add_argument("--pitch-semitones", type=float, default=0.0)
    stretch_render.add_argument("--full-source", action="store_true")
    stretch_render.add_argument("--port", type=int, default=PORT)
    stretch_render.set_defaults(func=command_stretch_render)

    assist = sub.add_parser("assist", help="Analyze, optionally wait for a phrase boundary, then mix")
    assist.add_argument("--from-role", choices=["direct", "routed"], required=True)
    assist.add_argument("--to-role", choices=["direct", "routed"], required=True)
    assist.add_argument("--mode", choices=["auto", "staged", "crossfade"], default="auto")
    assist.add_argument("--seconds", type=float, default=12.0)
    assist.add_argument("--steps", type=int, default=48)
    assist.add_argument("--analyze-seconds", type=float, default=6.0)
    assist.add_argument("--wait-phrase", action="store_true")
    assist.add_argument("--phrase-beats", type=int, default=8)
    assist.add_argument("--incoming-low-gain", type=float, default=0.45)
    assist.add_argument("--index-path", default=str(DEFAULT_INDEX_PATH))
    assist.add_argument("--min-structure-lead", type=float, default=1.0)
    assist.add_argument("--max-structure-wait", type=float, default=48.0)
    assist.add_argument("--prepare-vocals", action="store_true")
    assist.add_argument("--port", type=int, default=PORT)
    assist.set_defaults(func=command_assist)

    effect = sub.add_parser("effect", help="Apply a creative transition effect to one live role")
    effect.add_argument("--role", choices=["direct", "routed"], required=True)
    effect.add_argument("--effect", choices=["loop-roll", "power-off", "transform"], required=True)
    effect.add_argument("--seconds", type=float, default=2.0)
    effect.add_argument("--loop-seconds", type=float, default=0.5)
    effect.add_argument("--gate-ms", type=int, default=125)
    effect.add_argument("--port", type=int, default=PORT)
    effect.set_defaults(func=command_effect)

    preset = sub.add_parser("preset", help="Run a creative transition preset")
    preset.add_argument("--name", choices=["loop-roll-swap", "power-off-swap", "transform-swap", "bass-swap", "staged-swap"], required=True)
    preset.add_argument("--from-role", choices=["direct", "routed"], required=True)
    preset.add_argument("--to-role", choices=["direct", "routed"], required=True)
    preset.add_argument("--effect-seconds", type=float, default=1.25)
    preset.add_argument("--lead-in", type=float, default=0.2)
    preset.add_argument("--fade-seconds", type=float, default=1.8)
    preset.add_argument("--steps", type=int, default=8)
    preset.add_argument("--loop-seconds", type=float, default=0.5)
    preset.add_argument("--gate-ms", type=int, default=125)
    preset.add_argument("--port", type=int, default=PORT)
    preset.set_defaults(func=command_preset)

    watch = sub.add_parser("watch", help="Log direct/routed tab state over time")
    watch.add_argument("--seconds", type=float, default=10.0)
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--stream", action="store_true")
    watch.add_argument("--port", type=int, default=PORT)
    watch.set_defaults(func=command_watch)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
