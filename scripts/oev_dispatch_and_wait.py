#!/usr/bin/env python3
"""Dispatch the proven OEV RunPod workflow, wait for it, and retry infra failures.

Retries are deliberately narrow: only a failure in the RunPod allocation/preflight
step is retried. Build/test/render failures are returned immediately so bad code is
not hidden by expensive automatic redispatches.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def api(method: str, path: str, *, token: str, data: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    url = f"https://api.github.com{path}"
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"GitHub API {method} {path} -> {exc.code}: {detail}") from exc


def list_run_ids(repo: str, workflow: str, ref: str, token: str) -> set[int]:
    query = urllib.parse.urlencode({"branch": ref, "event": "workflow_dispatch", "per_page": 50})
    payload = api("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs?{query}", token=token)
    return {int(run["id"]) for run in payload.get("workflow_runs", [])}


def find_new_run(repo: str, workflow: str, ref: str, before: set[int], token: str, timeout_s: int = 120) -> int:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ids = list_run_ids(repo, workflow, ref, token)
        new = ids - before
        if new:
            return max(new)
        time.sleep(3)
    raise RuntimeError("workflow dispatch returned but no new run appeared within 120s")


def wait_run(repo: str, run_id: int, token: str, timeout_s: int) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run = api("GET", f"/repos/{repo}/actions/runs/{run_id}", token=token)
        status = run.get("status")
        conclusion = run.get("conclusion")
        print(f"OEV run {run_id}: status={status} conclusion={conclusion}", flush=True)
        if status == "completed":
            return run
        time.sleep(15)
    raise RuntimeError(f"run {run_id} did not complete within {timeout_s}s")


def failed_in_launch_step(repo: str, run_id: int, token: str) -> bool:
    payload = api("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", token=token)
    for job in payload.get("jobs", []):
        for step in job.get("steps") or []:
            name = str(step.get("name") or "")
            if name.startswith("Launch RunPod GPU pod") and step.get("conclusion") == "failure":
                return True
    return False


def dispatch_once(repo: str, workflow: str, ref: str, inputs: dict[str, str], token: str) -> int:
    before = list_run_ids(repo, workflow, ref, token)
    api("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", token=token, data={"ref": ref, "inputs": inputs})
    return find_new_run(repo, workflow, ref, before, token)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--inputs-json", required=True)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout-s", type=int, default=7200)
    parser.add_argument("--fallback-datacenter", default="")
    parser.add_argument("--github-output")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY are required")
    inputs = json.loads(args.inputs_json)
    if not isinstance(inputs, dict):
        raise SystemExit("--inputs-json must decode to an object")
    inputs = {str(k): str(v) for k, v in inputs.items()}

    primary_datacenter = inputs.get("datacenter", "")
    fallback_datacenter = args.fallback_datacenter.strip()

    final_run = 0
    for attempt in range(1, args.max_attempts + 1):
        attempt_inputs = dict(inputs)
        if attempt > 1 and fallback_datacenter and fallback_datacenter != primary_datacenter:
            attempt_inputs["datacenter"] = fallback_datacenter
        datacenter = attempt_inputs.get("datacenter", "unspecified")
        print(
            f"Dispatch attempt {attempt}/{args.max_attempts} for {args.ref} datacenter={datacenter}",
            flush=True,
        )
        run_id = dispatch_once(repo, args.workflow, args.ref, attempt_inputs, token)
        final_run = run_id
        run = wait_run(repo, run_id, token, args.timeout_s)
        if run.get("conclusion") == "success":
            print(f"OEV_RUN_ID={run_id}")
            if args.github_output:
                with open(args.github_output, "a", encoding="utf-8") as fh:
                    fh.write(f"run_id={run_id}\n")
                    fh.write(f"attempts={attempt}\n")
                    fh.write(f"datacenter={datacenter}\n")
            return 0

        launch_failed = failed_in_launch_step(repo, run_id, token)
        if launch_failed and attempt < args.max_attempts:
            next_datacenter = (
                fallback_datacenter
                if fallback_datacenter and fallback_datacenter != primary_datacenter
                else datacenter
            )
            print(
                f"Run {run_id} failed in RunPod allocation/preflight; retrying without changing code "
                f"(next datacenter={next_datacenter}).",
                flush=True,
            )
            continue
        if launch_failed:
            print(
                f"Run {run_id} failed in RunPod allocation/preflight; retry budget exhausted after "
                f"{args.max_attempts} attempts.",
                flush=True,
            )
        else:
            print(f"Run {run_id} failed outside the retryable RunPod allocation step; not retrying.", flush=True)
        break

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"run_id={final_run}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
