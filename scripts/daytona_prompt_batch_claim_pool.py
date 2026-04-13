#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from daytona_agent_leases import heartbeat_launch
from daytona_agent_task_queue import (
    claim_next_task,
    connect_task_queue,
    mark_task_completed,
    mark_task_failed,
    queue_counts,
)


ROOT = Path(__file__).resolve().parent.parent
CODEX_REMOTE = ROOT / "daytona_codex_remote.py"
CLAUDE_REMOTE = ROOT / "daytona_claude_remote.py"


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sandbox-centric local claim-pool controller for generic Daytona prompt batches.")
    parser.add_argument("--queue-db", type=Path, required=True)
    parser.add_argument("--leases-db", type=Path, default=None)
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--controller-id", required=True)
    parser.add_argument("--runtime", default="codex")
    parser.add_argument("--sandbox-name", required=True)
    parser.add_argument("--worker-specs-file", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--tools", default="default")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--web-mode", choices=("exa", "mixed", "claude"), default="exa")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--memory", type=int, default=4)
    parser.add_argument("--disk", type=int, default=10)
    parser.add_argument("--auto-stop-interval", type=int, default=5)
    parser.add_argument("--auto-archive-interval", type=int, default=5)
    parser.add_argument("--auto-delete-interval", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--claim-stale-after-seconds", type=int, default=1800)
    parser.add_argument("--claim-max-attempts", type=int, default=2)
    return parser.parse_args()


def load_worker_specs(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise SystemExit(f"Worker specs file must be a non-empty JSON list: {path}")
    out = [dict(item) for item in payload if isinstance(item, dict)]
    if not out:
        raise SystemExit(f"No valid worker specs in {path}")
    return out


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def controller_state_path(args: argparse.Namespace) -> Path:
    return args.output_root.expanduser().resolve() / args.launch_id / "controllers" / f"{args.controller_id}.json"


def task_output_path(args: argparse.Namespace, task: dict[str, object]) -> Path:
    ref = str(task.get("output_ref") or "").strip()
    if ref:
        output_path = Path(ref).expanduser()
        if not output_path.is_absolute():
            output_path = args.output_root.expanduser().resolve() / args.launch_id / output_path
    else:
        output_path = args.output_root.expanduser().resolve() / args.launch_id / "results" / f"{task['task_id']}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def task_result_meta_path(args: argparse.Namespace, task_id: str) -> Path:
    path = args.output_root.expanduser().resolve() / args.launch_id / "results" / f"{task_id}.meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_remote_task(
    args: argparse.Namespace,
    *,
    task: dict[str, object],
    worker_spec: dict[str, object],
    log_path: Path,
) -> int:
    output_path = task_output_path(args, task)
    prompt = str(task["prompt_text"])
    runtime = str(args.runtime)
    workdir = str(task.get("workdir") or args.workdir or ("/root" if runtime == "codex" else ""))
    agent_mode = str(task.get("agent_mode") or "ask").strip() or "ask"
    session_alias = str(task.get("session_alias") or "").strip()
    session_id = str(task.get("session_id") or "").strip()
    create_session = bool(task.get("create_session") or False)
    create_session_flag = create_session and agent_mode == "ask"
    append_output = bool(task.get("append_output") or False)
    allowed_tools = str(task.get("allowed_tools") or "").strip()
    disallowed_tools = str(task.get("disallowed_tools") or "").strip()
    helper_python = os.environ.get("DAYTONA_PYTHON_BIN") or "python3"
    if runtime == "codex":
        cmd = [
            helper_python,
            "-u",
            str(CODEX_REMOTE),
            "--sandbox-name",
            str(args.sandbox_name),
            "--cpu",
            str(int(args.cpu)),
            "--memory",
            str(int(args.memory)),
            "--disk",
            str(int(args.disk)),
            "--auto-stop-interval",
            str(int(args.auto_stop_interval)),
            "--auto-archive-interval",
            str(int(args.auto_archive_interval)),
            "--auto-delete-interval",
            str(int(args.auto_delete_interval)),
            "--output-file",
            str(output_path),
            "--model",
            str(worker_spec.get("model") or args.model),
            "--workdir",
            workdir,
            prompt,
        ]
    elif runtime in {"wafer", "claude", "minimax"}:
        cmd = [
            helper_python,
            "-u",
            str(CLAUDE_REMOTE),
            "--runtime",
            runtime,
            "--sandbox-name",
            str(args.sandbox_name),
            "--cpu",
            str(int(args.cpu)),
            "--memory",
            str(int(args.memory)),
            "--disk",
            str(int(args.disk)),
            "--auto-stop-interval",
            str(int(args.auto_stop_interval)),
            "--auto-archive-interval",
            str(int(args.auto_archive_interval)),
            "--auto-delete-interval",
            str(int(args.auto_delete_interval)),
            "--output-file",
            str(output_path),
            "--model",
            str(worker_spec.get("model") or args.model),
            "--tools",
            str(args.tools),
            "--effort",
            str(args.effort),
            "--wafer-web-mode",
            str(args.web_mode),
            "--timeout-seconds",
            str(int(args.timeout_seconds)),
            "--workdir",
            workdir,
            "--run-id",
            str(worker_spec.get("run_id") or ""),
            "--agent-mode",
            agent_mode,
            prompt,
        ]
        if append_output:
            cmd.append("--append-output")
        if session_alias:
            cmd.extend(["--session-alias", session_alias])
        if session_id:
            cmd.extend(["--session-id", session_id])
        if create_session_flag:
            cmd.append("--create-session")
        if allowed_tools:
            cmd.extend(["--allowed-tools", allowed_tools])
        if disallowed_tools:
            cmd.extend(["--disallowed-tools", disallowed_tools])
    else:
        raise SystemExit(f"Unsupported runtime for prompt batch: {runtime}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            check=False,
            env=os.environ.copy(),
        )
    write_json(
        task_result_meta_path(args, str(task["task_id"])),
        {
            "launch_id": args.launch_id,
            "controller_id": args.controller_id,
            "worker_id": str(worker_spec["worker_id"]),
            "run_id": str(worker_spec["run_id"]),
            "sandbox_name": args.sandbox_name,
            "runtime": args.runtime,
            "model": str(worker_spec.get("model") or args.model),
            "task_id": str(task["task_id"]),
            "task_type": str(task.get("task_type") or "prompt"),
            "prompt_ref": str(task.get("prompt_ref") or ""),
            "input_ref": str(task.get("input_ref") or ""),
            "output_file": str(output_path),
            "workdir": workdir,
            "agent_mode": agent_mode,
            "session_alias": session_alias,
            "session_id": session_id,
            "create_session": create_session_flag,
            "append_output": append_output,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "return_code": int(proc.returncode or 0),
            "finished_at_utc": utc_now(),
        },
    )
    return int(proc.returncode or 0)


def worker_loop(
    *,
    args: argparse.Namespace,
    worker_spec: dict[str, object],
    queue_db: Path,
    controller_path: Path,
    controller_lock: threading.Lock,
    controller_metrics: dict[str, int],
) -> None:
    run_id = str(worker_spec["run_id"])
    worker_id = str(worker_spec["worker_id"])
    lane_slot = int(worker_spec["lane_slot"])
    log_path = Path(str(worker_spec["log"]))
    processed = 0
    failed = 0
    while True:
        if args.leases_db is not None:
            heartbeat_launch(args.leases_db.expanduser().resolve(), launch_id=args.launch_id, status="active")
        task = claim_next_task(
            queue_db,
            worker_id=worker_id,
            run_id=run_id,
            stale_after_seconds=int(args.claim_stale_after_seconds),
        )
        if task is None:
            break
        item_id = int(task["id"])
        with log_path.open("ab") as log_file:
            log_file.write(
                (
                    f"[claim] run_id={run_id} slot={lane_slot} task_id={task['task_id']} "
                    f"ordinal={int(task['ordinal'])}\n"
                ).encode("utf-8")
            )
            log_file.flush()
        returncode = run_remote_task(args, task=task, worker_spec=worker_spec, log_path=log_path)
        if returncode == 0:
            processed += 1
            mark_task_completed(queue_db, item_id=item_id, run_id=run_id)
            with controller_lock:
                controller_metrics["processed"] += 1
        else:
            failed += 1
            with controller_lock:
                controller_metrics["failed"] += 1
            requeue = int(task.get("attempts") or 1) < int(args.claim_max_attempts)
            mark_task_failed(
                queue_db,
                item_id=item_id,
                run_id=run_id,
                error_text=f"runner_rc={returncode}",
                requeue=requeue,
            )
        with controller_lock:
            write_json(
                controller_path,
                {
                    "status": "running",
                    "launch_id": args.launch_id,
                    "controller_id": args.controller_id,
                    "runtime": args.runtime,
                    "sandbox_name": args.sandbox_name,
                    "updated_at_utc": utc_now(),
                    "worker_count": controller_metrics["worker_count"],
                    "processed": controller_metrics["processed"],
                    "failed": controller_metrics["failed"],
                    "queue_counts": queue_counts(queue_db),
                },
            )
        time.sleep(0.1)


def main() -> int:
    args = parse_args()
    if str(args.runtime) not in {"codex", "wafer", "claude", "minimax"}:
        raise SystemExit("prompt-batch currently supports runtime=codex, wafer, claude, or minimax")
    queue_db = args.queue_db.expanduser().resolve()
    connect_task_queue(queue_db).close()
    worker_specs = load_worker_specs(args.worker_specs_file.expanduser().resolve())
    controller_path = controller_state_path(args)
    controller_lock = threading.Lock()
    controller_metrics = {"worker_count": len(worker_specs), "processed": 0, "failed": 0}
    write_json(
        controller_path,
        {
            "status": "running",
            "launch_id": args.launch_id,
            "controller_id": args.controller_id,
            "runtime": args.runtime,
            "sandbox_name": args.sandbox_name,
            "started_at_utc": utc_now(),
            "worker_count": len(worker_specs),
            "processed": 0,
            "failed": 0,
            "queue_counts": queue_counts(queue_db),
        },
    )
    if args.leases_db is not None:
        heartbeat_launch(args.leases_db.expanduser().resolve(), launch_id=args.launch_id, status="active")
    try:
        with ThreadPoolExecutor(max_workers=len(worker_specs), thread_name_prefix=f"prompt-pool-{args.runtime}") as executor:
            futures = [
                executor.submit(
                    worker_loop,
                    args=args,
                    worker_spec=worker_spec,
                    queue_db=queue_db,
                    controller_path=controller_path,
                    controller_lock=controller_lock,
                    controller_metrics=controller_metrics,
                )
                for worker_spec in worker_specs
            ]
            for future in futures:
                future.result()
    except KeyboardInterrupt:
        write_json(
            controller_path,
            {
                "status": "interrupted",
                "launch_id": args.launch_id,
                "controller_id": args.controller_id,
                "runtime": args.runtime,
                "sandbox_name": args.sandbox_name,
                "finished_at_utc": utc_now(),
                "worker_count": len(worker_specs),
                "processed": controller_metrics["processed"],
                "failed": controller_metrics["failed"],
                "queue_counts": queue_counts(queue_db),
            },
        )
        os.kill(os.getpid(), signal.SIGTERM)
        return 1
    write_json(
        controller_path,
        {
            "status": "finished",
            "launch_id": args.launch_id,
            "controller_id": args.controller_id,
            "runtime": args.runtime,
            "sandbox_name": args.sandbox_name,
            "finished_at_utc": utc_now(),
            "worker_count": len(worker_specs),
            "processed": controller_metrics["processed"],
            "failed": controller_metrics["failed"],
            "queue_counts": queue_counts(queue_db),
        },
    )
    print(
        f"[controller-finished] controller_id={args.controller_id} runtime={args.runtime} "
        f"workers={len(worker_specs)} processed={controller_metrics['processed']} failed={controller_metrics['failed']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
