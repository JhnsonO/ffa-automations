#!/usr/bin/env python3
"""Small, dependency-free helper for the OEV agent test harness."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

DEFAULT_CONFIG = pathlib.Path("oev/harness_profiles.json")
DEFAULT_REQUEST = pathlib.Path("oev/harness_request.json")
REQUIRED_DISPATCH_KEYS = {
    "datacenter",
    "sample_id",
    "duration_s",
    "sample_set_id",
    "model_variant",
    "lookahead",
    "cluster_alpha_override",
}


def load_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schema_version") != 1:
        raise ValueError("oev harness schema_version must be 1")
    if not isinstance(config.get("baseline_workflow"), str) or not config["baseline_workflow"]:
        raise ValueError("baseline_workflow must be a non-empty string")
    profiles = config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("profiles must be a non-empty object")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ValueError(f"profile {name!r} must be an object")
        dispatch = profile.get("dispatch")
        if not isinstance(dispatch, dict):
            raise ValueError(f"profile {name!r} dispatch must be an object")
        missing = REQUIRED_DISPATCH_KEYS - set(dispatch)
        if missing:
            raise ValueError(f"profile {name!r} dispatch missing: {sorted(missing)}")
        fps = profile.get("fps")
        if not isinstance(fps, (int, float)) or fps <= 0:
            raise ValueError(f"profile {name!r} fps must be > 0")
        ball_class_id = profile.get("ball_class_id")
        if not isinstance(ball_class_id, int) or ball_class_id < 0:
            raise ValueError(f"profile {name!r} ball_class_id must be >= 0")
        retries = profile.get("capacity_retry_max", 1)
        if not isinstance(retries, int) or retries < 1 or retries > 5:
            raise ValueError(f"profile {name!r} capacity_retry_max must be 1..5")
        windows = profile.get("review_windows", [])
        if not isinstance(windows, list):
            raise ValueError(f"profile {name!r} review_windows must be a list")
        for window in windows:
            if not isinstance(window, dict):
                raise ValueError(f"profile {name!r} has a non-object review window")
            if float(window.get("end_s", -1)) <= float(window.get("start_s", -1)):
                raise ValueError(f"profile {name!r} has invalid review window {window!r}")


def resolve_profile(config: dict[str, Any], name: str) -> dict[str, Any]:
    validate_config(config)
    try:
        profile = config["profiles"][name]
    except KeyError as exc:
        raise ValueError(f"unknown profile {name!r}; choose from {sorted(config['profiles'])}") from exc
    return profile


def resolved_request(
    config: dict[str, Any],
    request: dict[str, Any],
    *,
    default_ref: str = "",
    manual_profile: str = "",
    manual_ref: str = "",
    manual_reference_run: str = "",
    force_mode: str = "",
) -> dict[str, Any]:
    mode = force_mode or str(request.get("mode") or "check")
    if mode not in {"check", "run"}:
        raise ValueError("request mode must be 'check' or 'run'")
    profile_name = manual_profile or str(request.get("profile") or "sample_02_180_quality")
    profile = resolve_profile(config, profile_name)
    experiment_ref = manual_ref or str(request.get("experiment_ref") or "") or default_ref
    if not experiment_ref:
        raise ValueError("experiment_ref resolved to empty")
    override = manual_reference_run.strip() if manual_reference_run else ""
    if override:
        reference_run_id = int(override)
    else:
        raw = request.get("reference_run_id_override")
        reference_run_id = int(raw) if raw not in (None, "") else int(profile.get("reference_run_id") or 0)
    return {
        "mode": mode,
        "profile_name": profile_name,
        "experiment_ref": experiment_ref,
        "reference_run_id": reference_run_id,
        "baseline_workflow": config["baseline_workflow"],
        "fps": float(profile["fps"]),
        "ball_class_id": int(profile["ball_class_id"]),
        "capacity_retry_max": int(profile.get("capacity_retry_max", 1)),
        "dispatch": profile["dispatch"],
        "review_windows": profile.get("review_windows", []),
    }


def write_github_output(data: dict[str, Any]) -> None:
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"))
        elif isinstance(value, bool):
            value = "true" if value else "false"
        print(f"{key}={value}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    parser.add_argument("--request", type=pathlib.Path, default=DEFAULT_REQUEST)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate")

    profile_p = sub.add_parser("profile")
    profile_p.add_argument("--name", required=True)
    profile_p.add_argument("--format", choices=("json", "github-output"), default="json")

    request_p = sub.add_parser("resolve-request")
    request_p.add_argument("--default-ref", default="")
    request_p.add_argument("--manual-profile", default="")
    request_p.add_argument("--manual-ref", default="")
    request_p.add_argument("--manual-reference-run", default="")
    request_p.add_argument("--force-mode", choices=("", "check", "run"), default="")
    request_p.add_argument("--format", choices=("json", "github-output"), default="json")

    args = parser.parse_args()
    config = load_json(args.config)

    if args.command == "validate":
        validate_config(config)
        request = load_json(args.request)
        mode = request.get("mode", "check")
        if mode not in {"check", "run"}:
            raise ValueError("request mode must be check or run")
        if str(request.get("profile") or "") not in config["profiles"]:
            raise ValueError("request profile does not exist in harness_profiles.json")
        print("OEV_HARNESS_CONFIG=PASS")
        return 0

    if args.command == "profile":
        profile = resolve_profile(config, args.name)
        data = {
            "profile_name": args.name,
            "baseline_workflow": config["baseline_workflow"],
            "reference_run_id": int(profile.get("reference_run_id") or 0),
            "fps": float(profile["fps"]),
            "ball_class_id": int(profile["ball_class_id"]),
            "capacity_retry_max": int(profile.get("capacity_retry_max", 1)),
            "dispatch": profile["dispatch"],
            "review_windows": profile.get("review_windows", []),
        }
    else:
        request = load_json(args.request)
        data = resolved_request(
            config,
            request,
            default_ref=args.default_ref,
            manual_profile=args.manual_profile,
            manual_ref=args.manual_ref,
            manual_reference_run=args.manual_reference_run,
            force_mode=args.force_mode,
        )

    if args.format == "github-output":
        write_github_output(data)
    else:
        json.dump(data, sys.stdout, indent=2, sort_keys=True)
        print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"OEV harness error: {exc}", file=sys.stderr)
        raise SystemExit(2)
