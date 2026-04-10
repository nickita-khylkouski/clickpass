#!/usr/bin/env python3
"""List and monitor Pioneer/CoreMIDI ports for reverse engineering."""

from __future__ import annotations

import argparse
import sys
import time

import mido


def list_ports() -> int:
    inputs = mido.get_input_names()
    outputs = mido.get_output_names()
    print("MIDI inputs:")
    if inputs:
        for name in inputs:
            print(f"  {name}")
    else:
        print("  <none>")
    print("MIDI outputs:")
    if outputs:
        for name in outputs:
            print(f"  {name}")
    else:
        print("  <none>")
    return 0


def resolve_port(name: str | None) -> str:
    inputs = mido.get_input_names()
    if not inputs:
        raise SystemExit("No MIDI input ports are visible")
    if name:
        if name in inputs:
            return name
        raise SystemExit(f"MIDI input port not found: {name}")
    preferred = [port for port in inputs if "Pioneer" in port or "CDJ" in port or "DJM" in port]
    if not preferred:
        raise SystemExit("No Pioneer/CDJ/DJM MIDI input port is visible; pass --port explicitly if you want a non-Pioneer port")
    if len(preferred) > 1:
        raise SystemExit(f"Multiple Pioneer-family MIDI ports are visible; pass --port explicitly: {preferred}")
    return preferred[0]


def monitor(port_name: str, timeout: float | None) -> int:
    print(f"Monitoring MIDI input: {port_name}")
    print("Move the target control now. Ctrl-C stops capture.")
    start = time.monotonic()
    count = 0
    with mido.open_input(port_name) as port:
        while True:
            pending = port.iter_pending()
            saw = False
            for msg in pending:
                saw = True
                count += 1
                stamp = time.monotonic() - start
                print(f"{stamp:8.3f}s  {msg!s}")
            if timeout is not None and (time.monotonic() - start) >= timeout:
                break
            if not saw:
                time.sleep(0.01)
    print(f"Captured {count} MIDI messages")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List or monitor CoreMIDI ports for Pioneer DJ hardware."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List visible MIDI ports")

    mon = sub.add_parser("monitor", help="Monitor one MIDI input port")
    mon.add_argument(
        "--port",
        help="Exact MIDI input port name. Defaults to the first Pioneer/CDJ/DJM port.",
    )
    mon.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Optional capture timeout in seconds",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "list":
        return list_ports()
    if args.cmd == "monitor":
        return monitor(resolve_port(args.port), args.seconds)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
