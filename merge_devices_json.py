#!/usr/bin/env python3
"""Merge multiple Junos device-mapping JSON files into a single devices.json.

Expected input format (same as devices.json):
  {
    "router_name": {"ip": "x.x.x.x", "port": 22, "username": "...", "auth": {...}},
    ...
  }

This script is safe even if one of the input files is the same path as the
output file (e.g., devices.json). It reads all inputs first, then writes the
output atomically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Conflict:
    device_name: str
    first_source: str
    later_source: str


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Input file not found: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON in {path}: {e}")


def _load_devices_map(path: Path) -> dict[str, dict[str, Any]]:
    data = _load_json(path)
    if not isinstance(data, dict):
        raise SystemExit(
            f"Unexpected JSON format in {path}. Expected an object/dict at top-level, got {type(data).__name__}."
        )

    # Best-effort type normalization (jmcp expects dict values)
    out: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise SystemExit(f"Invalid device name key in {path}: expected string key, got {type(key).__name__}")
        if not isinstance(value, dict):
            raise SystemExit(
                f"Invalid device config for '{key}' in {path}: expected object/dict, got {type(value).__name__}"
            )
        out[key] = value
    return out


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")

    os.replace(tmp, path)


def _maybe_backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(path.name + f".bak_{ts}")
    os.replace(path, backup)
    return backup


def _validate_devices_map(devices: dict[str, dict[str, Any]]) -> None:
    try:
        from utils.config import validate_all_devices  # type: ignore

        validate_all_devices(devices)
    except ImportError:
        # Validation is optional; if utils isn't importable for some reason, skip.
        return


def merge_devices(
    input_paths: list[Path],
    *,
    prefer: str,
) -> tuple[dict[str, dict[str, Any]], list[Conflict]]:
    merged: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    conflicts: list[Conflict] = []

    for path in input_paths:
        devices = _load_devices_map(path)
        for name, cfg in devices.items():
            if name in merged and merged[name] != cfg:
                conflicts.append(Conflict(device_name=name, first_source=sources[name], later_source=str(path)))
                if prefer == "first":
                    continue
            merged[name] = cfg
            sources[name] = str(path)

    return merged, conflicts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Merge 3 device-mapping JSON files into devices.json",
    )
    parser.add_argument("input1", type=Path, help="First input JSON file")
    parser.add_argument("input2", type=Path, help="Second input JSON file")
    parser.add_argument("input3", type=Path, help="Third input JSON file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("devices.json"),
        help="Output JSON file to write (default: ./devices.json)",
    )
    parser.add_argument(
        "--prefer",
        choices=["last", "first"],
        default="last",
        help="When the same device exists in multiple inputs with different configs: keep the last one (default) or the first one.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a backup of the output file if it already exists.",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip validating the merged devices against utils.config.validate_all_devices.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and merge inputs, print summary, but do not write output.",
    )

    args = parser.parse_args(argv)

    input_paths = [args.input1, args.input2, args.input3]
    merged, conflicts = merge_devices(input_paths, prefer=args.prefer)

    if not args.no_validate:
        _validate_devices_map(merged)

    if conflicts:
        print(f"Conflicts: {len(conflicts)} (prefer={args.prefer})", file=sys.stderr)
        for c in conflicts[:25]:
            print(
                f"  - {c.device_name}: {c.first_source} -> {c.later_source}",
                file=sys.stderr,
            )
        if len(conflicts) > 25:
            print(f"  ... ({len(conflicts) - 25} more)", file=sys.stderr)

    print(f"Inputs: {', '.join(str(p) for p in input_paths)}")
    print(f"Devices merged: {len(merged)}")

    if args.dry_run:
        print("Dry-run: not writing output")
        return 0

    backup = None
    if not args.no_backup and args.output.exists():
        backup = _maybe_backup(args.output)

    _atomic_write_json(args.output, merged)

    if backup:
        print(f"Wrote {args.output} (backup: {backup})")
    else:
        print(f"Wrote {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
