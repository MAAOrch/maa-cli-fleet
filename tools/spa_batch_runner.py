#!/usr/bin/env python3
"""
MAS-aware concurrent batch runner for CN Stronghold Protocol (卫戍协议：规程) badge farming.

Reads `config/spa_cn_batch.json` and runs `tools/spa_badge_runner.py` across up to N concurrent
emulators (default 3), automatically yielding if AUTO-MAS has active MAA.exe instances or if a
target emulator is already running externally.

Writes live progress to `E:\maa-cli-fleet\logs\spa_batch_status.json`.
"""

import argparse
import concurrent.futures
import datetime
import json
import subprocess
import sys
import threading
import time
from pathlib import Path


def now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_mas_maa_running() -> bool:
    """Return True if any GUI MAA.exe (used by AUTO-MAS) is currently running."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MAA.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return "MAA.exe" in r.stdout
    except Exception:
        return False


def is_emu_running(mumu_mgr: str, emu: int) -> bool:
    try:
        r = subprocess.run(
            [mumu_mgr, "info", "-v", str(emu)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        info = json.loads(r.stdout)
        return bool(info.get("is_process_started") or info.get("is_android_started"))
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Batch StrongholdProtocol badge runner")
    ap.add_argument("--config", default=r"E:\maa-cli-fleet\config\spa_cn_batch.json")
    ap.add_argument("--runner", default=r"E:\maa-cli-fleet\tools\spa_badge_runner.py")
    ap.add_argument("--python", default=r"C:\Program Files\Python312\python.exe")
    ap.add_argument("--mumu-mgr", default=r"E:\MuMu Player 12\nx_main\MuMuManager.exe")
    ap.add_argument("--log-dir", default=r"E:\maa-cli-fleet\logs")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--mode", default="badge", choices=["badge", "score"])
    ap.add_argument("--target", type=int, default=3, help="3=Gold, 6=Rainbow")
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--attempts", type=int, default=0)
    ap.add_argument("--max-minutes", type=int, default=240)
    ap.add_argument("--skip-emus", default="3", help="Comma-separated emu indices to skip (default: 3 tutorial-locked)")
    ap.add_argument("--only-emus", default="", help="Optional comma-separated emu indices to run")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    accounts = cfg["accounts"]
    skip_set = {x.strip() for x in args.skip_emus.split(",") if x.strip()}
    only_set = {x.strip() for x in args.only_emus.split(",") if x.strip()}

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    status_path = log_dir / "spa_batch_status.json"

    status_lock = threading.Lock()
    state = {
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "params": {
            "concurrency": args.concurrency,
            "mode": args.mode,
            "target": args.target,
            "runs": args.runs,
            "attempts": args.attempts,
        },
        "accounts": {},
    }

    queue = []
    for acc in accounts:
        emu_str = str(acc["emu"])
        if only_set and emu_str not in only_set:
            continue
        if emu_str in skip_set:
            state["accounts"][emu_str] = {
                "name": acc["name"],
                "client": acc["client"],
                "emu": int(emu_str),
                "status": "skipped_tutorial_locked" if emu_str == "3" else "skipped",
                "updated_at": now_iso(),
            }
            continue
        state["accounts"][emu_str] = {
            "name": acc["name"],
            "client": acc["client"],
            "emu": int(emu_str),
            "status": "pending",
            "updated_at": now_iso(),
        }
        queue.append(acc)

    def save_status():
        with status_lock:
            state["updated_at"] = now_iso()
            tmp = status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(status_path)

    save_status()
    print(f"[batch] Starting {len(queue)} accounts with concurrency={args.concurrency}, target={args.target}, runs={args.runs}", flush=True)

    # Stagger launches slightly so MuMu/ADB don't contend on cold boot
    launch_gate = threading.Lock()
    last_launch = [0.0]

    def run_one(acc: dict):
        emu = int(acc["emu"])
        emu_str = str(emu)
        name = acc["name"]
        client = acc["client"]
        log_file = log_dir / f"spa_emu{emu:02d}.log"

        # Wait if AUTO-MAS GUI is active or this emu is already occupied
        while is_mas_maa_running() or is_emu_running(args.mumu_mgr, emu):
            with status_lock:
                state["accounts"][emu_str]["status"] = "waiting_mas_idle"
            save_status()
            print(f"[batch][emu={emu} {name}] Waiting for AUTO-MAS / emu idle...", flush=True)
            time.sleep(30)

        with launch_gate:
            gap = time.time() - last_launch[0]
            if gap < 15:
                time.sleep(15 - gap)
            last_launch[0] = time.time()

        with status_lock:
            state["accounts"][emu_str].update({
                "status": "running",
                "started_at": now_iso(),
                "log_file": str(log_file),
            })
        save_status()
        print(f"[batch][emu={emu} {name}] Launching ({client}) -> {log_file}", flush=True)

        cmd = [
            args.python, "-u", args.runner,
            "--emu", str(emu),
            "--client", client,
            "--mode", args.mode,
            "--target", str(args.target),
            "--runs", str(args.runs),
            "--attempts", str(args.attempts),
            "--max-minutes", str(args.max_minutes),
        ]
        with open(log_file, "w", encoding="utf-8", errors="replace") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
            rc = proc.returncode

        if rc == 0:
            st = "completed"
        elif rc == 10:
            st = "tutorial_locked"
        elif rc == 11:
            st = "timeout"
        else:
            st = f"failed_rc_{rc}"

        with status_lock:
            state["accounts"][emu_str].update({
                "status": st,
                "exit_code": rc,
                "finished_at": now_iso(),
            })
        save_status()
        print(f"[batch][emu={emu} {name}] Finished status={st} rc={rc}", flush=True)
        return emu, name, st, rc

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(run_one, acc) for acc in queue]
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
            except Exception as e:
                print(f"[batch] Worker exception: {e}", flush=True)

    with status_lock:
        state["finished_at"] = now_iso()
    save_status()
    print(f"[batch] All queued accounts finished! Status: {status_path}", flush=True)


if __name__ == "__main__":
    main()
