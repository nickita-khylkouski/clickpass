#!/usr/bin/env python3
"""
Direct USB control for the Pioneer DJM-900NXS utility endpoint.

This reproduces the vendor control transfers used by the official macOS
DJM-900nexus Setting Utility. It does not control analog master/booth/trim
knobs; the Pioneer app does not expose those over this USB path.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import subprocess
import sys
from typing import Dict, List, Tuple

import usb.core
import usb.util


VENDOR_ID = 0x08E4
PRODUCT_ID = 0x0158
USB_LOCK_PATH = "/tmp/djm900ctl.lock"

INPUT_SWITCH_REQUEST = {
    "bmRequestType": 0xC0,
    "bRequest": 0,
    "wValue": 0x0000,
    "wIndex": 0x8002,
    "length": 5,
}

USB_OUTPUT_ROUTE_REQUEST = {
    "bmRequestType": 0x40,
    "bRequest": 3,
    "wIndex": 0x8002,
}

USB_OUTPUT_LEVEL_REQUEST = {
    "bmRequestType": 0x40,
    "bRequest": 3,
    "wIndex": 0x8003,
}


INPUT_SWITCH_VALUES = {
    0x00: "CD/LINE",
    0x01: "LINE",
    0x03: "PHONO",
    0x04: "USB",
}

LEVEL_TO_BYTE = {
    "-5": 0x03,
    "-10": 0x02,
    "-15": 0x01,
    "-19": 0x00,
}

PAIR_LABELS = {
    1: "USB1/2",
    2: "USB3/4",
    3: "USB5/6",
    4: "USB7/8",
}

# These values come from the DJM-900NXS macOS Setting Utility's __convTable and
# the app's string resources.
PAIR_ROUTE_OPTIONS: Dict[int, List[Tuple[str, int]]] = {
    1: [
        ("phono", 0x03),
        ("cdline", 0x00),
        ("digital", 0x02),
        ("postfader", 0x06),
        ("crossfader_a", 0x07),
        ("crossfader_b", 0x08),
        ("mic", 0x09),
        ("rec_out", 0x0A),
    ],
    2: [
        ("cdline", 0x00),
        ("line", 0x01),
        ("digital", 0x02),
        ("postfader", 0x06),
        ("crossfader_a", 0x07),
        ("crossfader_b", 0x08),
        ("mic", 0x09),
        ("rec_out", 0x0A),
    ],
    3: [
        ("cdline", 0x00),
        ("line", 0x01),
        ("digital", 0x02),
        ("postfader", 0x06),
        ("crossfader_a", 0x07),
        ("crossfader_b", 0x08),
        ("mic", 0x09),
        ("rec_out", 0x0A),
    ],
    4: [
        ("phono", 0x03),
        ("cdline", 0x00),
        ("digital", 0x02),
        ("postfader", 0x06),
        ("crossfader_a", 0x07),
        ("crossfader_b", 0x08),
        ("mic", 0x09),
        ("rec_out", 0x0A),
    ],
}


class DJM900Error(RuntimeError):
    pass


@contextlib.contextmanager
def usb_lock():
    with open(USB_LOCK_PATH, "w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def get_device():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        raise DJM900Error("DJM-900NXS not found over USB")
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    try:
        if hasattr(dev, "is_kernel_driver_active") and dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError:
        pass
    return dev


def release_device(dev) -> None:
    try:
        usb.util.release_interface(dev, 0)
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)


def read_input_switches(dev) -> List[int]:
    req = INPUT_SWITCH_REQUEST
    response = dev.ctrl_transfer(
        req["bmRequestType"],
        req["bRequest"],
        req["wValue"],
        req["wIndex"],
        req["length"],
    )
    return list(response)


def set_usb_output_level(dev, db: str) -> None:
    if db not in LEVEL_TO_BYTE:
        raise DJM900Error(f"Unsupported level {db}; choose from {', '.join(LEVEL_TO_BYTE)}")
    w_value = LEVEL_TO_BYTE[db] << 8
    req = USB_OUTPUT_LEVEL_REQUEST
    dev.ctrl_transfer(req["bmRequestType"], req["bRequest"], w_value, req["wIndex"], b"")


def set_usb_output_route(dev, pair: int, route: str) -> None:
    if pair not in PAIR_ROUTE_OPTIONS:
        raise DJM900Error(f"Unsupported USB pair {pair}; choose 1-4")
    route_map = dict(PAIR_ROUTE_OPTIONS[pair])
    if route not in route_map:
        supported = ", ".join(name for name, _ in PAIR_ROUTE_OPTIONS[pair])
        raise DJM900Error(f"Unsupported route for {PAIR_LABELS[pair]}: {route}. Supported: {supported}")
    route_value = route_map[route]
    w_value = ((pair & 0xFF) << 8) | route_value
    req = USB_OUTPUT_ROUTE_REQUEST
    dev.ctrl_transfer(req["bmRequestType"], req["bRequest"], w_value, req["wIndex"], b"")


def print_status(dev) -> None:
    raw = read_input_switches(dev)
    print(f"raw_switch_bytes: {raw}")
    for channel, value in enumerate(raw[1:], start=1):
        print(f"CH{channel}: {INPUT_SWITCH_VALUES.get(value, f'UNKNOWN({value})')}")

    print("")
    print("USB output routing options exposed by Pioneer utility:")
    for pair in range(1, 5):
        options = ", ".join(name for name, _ in PAIR_ROUTE_OPTIONS[pair])
        print(f"{PAIR_LABELS[pair]}: {options}")

    print("")
    print("USB output level options exposed by Pioneer utility: -5, -10, -15, -19 dB")


def play_test_sound() -> None:
    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Direct USB control for DJM-900NXS utility settings. "
            "This controls the same USB settings as the Pioneer app, not the "
            "analog master/booth/trim knobs."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Read the live CH1-CH4 input selector switch positions")

    level = sub.add_parser(
        "set-usb-output-level",
        help="Set USB output level used for mixer -> computer capture",
    )
    level.add_argument("db", choices=sorted(LEVEL_TO_BYTE.keys(), key=int), help="Level in dB")

    route = sub.add_parser(
        "set-usb-output-route",
        help="Set mixer -> computer USB output routing for a stereo pair",
    )
    route.add_argument("pair", type=int, choices=[1, 2, 3, 4], help="1=USB1/2, 2=USB3/4, 3=USB5/6, 4=USB7/8")
    route.add_argument(
        "route",
        help="Route name for that pair. Run `status` to see supported values per pair.",
    )

    sub.add_parser("play-test-sound", help="Set macOS output volume to 100 and play a short test sound")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "play-test-sound":
            play_test_sound()
            return 0

        with usb_lock():
            dev = get_device()
            try:
                if args.command == "status":
                    print_status(dev)
                    return 0
                if args.command == "set-usb-output-level":
                    set_usb_output_level(dev, args.db)
                    print(f"set USB output level to {args.db} dB")
                    return 0
                if args.command == "set-usb-output-route":
                    set_usb_output_route(dev, args.pair, args.route)
                    print(f"set {PAIR_LABELS[args.pair]} route to {args.route}")
                    return 0
            finally:
                release_device(dev)
    except DJM900Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except usb.core.USBError as exc:
        print(f"usb error: {exc}", file=sys.stderr)
        return 3
    except subprocess.CalledProcessError as exc:
        print(f"subprocess failed: {exc}", file=sys.stderr)
        return exc.returncode or 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
