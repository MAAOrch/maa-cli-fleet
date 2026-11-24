#!/usr/bin/env python3
r"""
MAS-aware concurrent batch runner with automatic crash/stall recovery for CN Stronghold Protocol
(卫戍协议：规程) badge farming.

Reads `config/spa_cn_batch.json` and runs `tools/spa_badge_runner.py` across up to N concurrent
emulators (default 3), automatically:
- yielding if AUTO-MAS has active MAA.exe instances or if a target emulator is already occupied;
- retrying transient failures (ADB disconnect, plugin 30s screen timeout rc=12, stall watchdog rc=13)
  up to `--max-retries` times with full emulator reboot (progress is preserved by the game);
- recording live UTF-8 status & retry counts to `E:\maa-cli-fleet\logs\spa_batch_status.json`.
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def has_active_mas_lock(locks_dir: str, max_age_sec: int = 180) -> str:
    """Return lock filename if a fresh AUTO-MAS pause lock (*.lock) exists, else empty string."""
    if not locks_dir:
        return ""
    p = Path(locks_dir)
    if not p.is_dir():
        return ""
    cutoff = time.time() - max_age_sec
    try:
        for f in p.glob("*.lock"):
            try:
                if f.stat().st_mtime >= cutoff:
                    return f.name
            except OSError:
                continue
    except OSError:
        return ""
    return ""


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


def force_shutdown_emu(mumu_mgr: str, emu: int):
    try:
        subprocess.run([mumu_mgr, "control", "-v", str(emu), "shutdown"], capture_output=True, timeout=30)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Batch StrongholdProtocol badge runner with auto-recovery")
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
    ap.add_argument("--max-minutes", type=int, default=300)
    ap.add_argument("--stall-minutes", type=int, default=12)
    ap.add_argument("--max-retries", type=int, default=3, help="Auto-retry count on transient failure/stall")
    ap.add_argument("--pause-locks-dir", default=r"E:\fleet-pause\locks", help="AUTO-MAS fleet-pause locks directory")
    ap.add_argument("--mas-poll-sec", type=int, default=30, help="Seconds to sleep while waiting for AUTO-MAS to finish")
    ap.add_argument("--skip-emu-check", action="store_true", help="Skip MuMuManager check (for unit/integration tests)")
    ap.add_argument("--skip-emus", default="3,22", help="Comma-separated emu indices to skip")
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
            "max_retries": args.max_retries,
            "stall_minutes": args.stall_minutes,
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
                "status": "skipped_tutorial_locked" if emu_str in ("3", "22") else "skipped",
                "updated_at": now_iso(),
            }
            continue
        state["accounts"][emu_str] = {
            "name": acc["name"],
            "client": acc["client"],
            "emu": int(emu_str),
            "status": "pending",
            "retries": 0,
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
    print(
        f"[batch] Starting {len(queue)} accounts with concurrency={args.concurrency}, "
        f"target={args.target}, runs={args.runs}, max_retries={args.max_retries}",
        flush=True,
    )

    launch_gate = threading.Lock()
    last_launch = [0.0]

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    def run_one(acc: dict):
        emu = int(acc["emu"])
        emu_str = str(emu)
        name = acc["name"]
        client = acc["client"]
        log_file = log_dir / f"spa_emu{emu:02d}.log"

        attempt_num = 0
        rc = -1
        st = "pending"

        while attempt_num <= args.max_retries:
            attempt_num += 1
            while (
                (not args.skip_emu_check and is_mas_maa_running())
                or bool(has_active_mas_lock(args.pause_locks_dir))
                or (not args.skip_emu_check and is_emu_running(args.mumu_mgr, emu))
            ):
                lock_name = has_active_mas_lock(args.pause_locks_dir)
                with status_lock:
                    state["accounts"][emu_str]["status"] = "waiting_mas_idle"
                save_status()
                print(
                    f"[batch][emu={emu} {name}] Waiting for AUTO-MAS / emu idle (lock={lock_name or 'none'})...",
                    flush=True,
                )
                time.sleep(max(1, args.mas_poll_sec))

            with launch_gate:
                gap = time.time() - last_launch[0]
                min_gap = 0 if args.skip_emu_check else 15
                if gap < min_gap:
                    time.sleep(min_gap - gap)
                last_launch[0] = time.time()

            with status_lock:
                state["accounts"][emu_str].update({
                    "status": "running" if attempt_num == 1 else f"retrying_{attempt_num - 1}",
                    "retries": attempt_num - 1,
                    "started_at": state["accounts"][emu_str].get("started_at") or now_iso(),
                    "last_attempt_at": now_iso(),
                    "log_file": str(log_file),
                })
            save_status()
            print(f"[batch][emu={emu} {name}] Attempt {attempt_num}/{args.max_retries + 1} ({client}) -> {log_file}", flush=True)

            cmd = [
                args.python, "-u", args.runner,
                "--emu", str(emu),
                "--client", client,
                "--mode", args.mode,
                "--target", str(args.target),
                "--runs", str(args.runs),
                "--attempts", str(args.attempts),
                "--max-minutes", str(args.max_minutes),
                "--stall-minutes", str(args.stall_minutes),
                "--pause-locks-dir", args.pause_locks_dir,
            ]
            mode_open = "w" if (attempt_num == 1 and not log_file.exists()) else "a"
            with open(log_file, mode_open, encoding="utf-8", errors="replace") as lf:
                if attempt_num > 1:
                    lf.write(f"\n=== AUTO-RETRY #{attempt_num - 1} at {now_iso()} ===\n")
                    lf.flush()
                proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
                rc = proc.returncode

            if rc == 0:
                st = "completed"
                break
            if rc == 10:
                st = "tutorial_locked"
                break
            if rc == 14:
                st = "consecutive_losses"
                break
            if rc == 15:
                # Yielded cleanly for AUTO-MAS: do NOT burn retry budget; wait until MAS finishes and resume!
                attempt_num -= 1
                with status_lock:
                    state["accounts"][emu_str]["status"] = "paused_for_mas"
                save_status()
                print(f"[batch][emu={emu} {name}] Yielded for AUTO-MAS (rc=15); will auto-resume when MAS clears.", flush=True)
                time.sleep(max(1, args.mas_poll_sec))
                continue

            # Transient failure (ADB / 30s screen lost / stall / timeout) -> clean reboot & retry
            st = {11: "timeout", 12: "plugin_error", 13: "stalled"}.get(rc, f"failed_rc_{rc}")
            print(f"[batch][emu={emu} {name}] Attempt {attempt_num} ended with {st} (rc={rc})", flush=True)
            if not args.skip_emu_check:
                force_shutdown_emu(args.mumu_mgr, emu)
            if attempt_num <= args.max_retries:
                time.sleep(1 if args.skip_emu_check else 15)

        with status_lock:
            state["accounts"][emu_str].update({
                "status": st,
                "exit_code": rc,
                "retries": attempt_num - 1,
                "finished_at": now_iso(),
            })
        save_status()
        print(f"[batch][emu={emu} {name}] Final status={st} rc={rc} retries={attempt_num - 1}", flush=True)
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
