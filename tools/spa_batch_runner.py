#!/usr/bin/env python3
r"""
MAS-aware concurrent batch runner with per-account hot control (zero-restart) and auto-recovery
for CN Stronghold Protocol (卫戍协议：规程) badge farming.

Features:
- Smart per-emulator AUTO-MAS coexistence with host-wide emulator cap (`SPA + MAS <= concurrency`).
- Zero-restart hot control via `<log_dir>/spa_batch_control.json` (polled every 2s):
  * Add an emu to `paused_emus` -> stops ONLY that single emu's runner & frees its slot; other accounts keep running untouched.
  * Remove an emu from `paused_emus` (or add to `resume_emus`) -> dynamically queues/starts that single emu without restarting the batch.
  * Change `concurrency` -> dynamically adjusts the live concurrency cap without interrupting running accounts.
- Automatic crash/stall recovery (`--max-retries`) with clean emulator reboot.
"""

import argparse
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


def get_running_emus(mumu_mgr: str) -> set[int]:
    """Return set of all currently running MuMu emulator indices."""
    try:
        r = subprocess.run(
            [mumu_mgr, "info", "-v", "all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        data = json.loads(r.stdout)
        return {
            int(k)
            for k, v in data.items()
            if isinstance(v, dict) and (v.get("is_process_started") or v.get("is_android_started"))
        }
    except Exception:
        return set()


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


def kill_proc_tree(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Batch StrongholdProtocol badge runner with zero-restart hot control")
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
    ap.add_argument("--mas-strict-yield", action="store_true", help="Pause ALL SPA slots on any AUTO-MAS lock (legacy global pause)")
    ap.add_argument("--mas-poll-sec", type=int, default=30, help="Seconds to sleep while waiting for AUTO-MAS to finish")
    ap.add_argument("--skip-emu-check", action="store_true", help="Skip MuMuManager check (for unit/integration tests)")
    ap.add_argument("--skip-emus", default="3,22", help="Comma-separated emu indices to skip")
    ap.add_argument("--only-emus", default="", help="Optional comma-separated emu indices to run")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    accounts = cfg["accounts"]
    skip_set = {int(x.strip()) for x in args.skip_emus.split(",") if x.strip().isdigit()}
    only_set = {int(x.strip()) for x in args.only_emus.split(",") if x.strip().isdigit()}

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    status_path = log_dir / "spa_batch_status.json"
    control_path = log_dir / "spa_batch_control.json"

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

    accounts_by_emu: dict[int, dict] = {}
    ordered_emus: list[int] = []
    paused_emus: set[int] = set()
    retries_by_emu: dict[int, int] = {}

    for acc in accounts:
        emu = int(acc["emu"])
        emu_str = str(emu)
        accounts_by_emu[emu] = acc
        ordered_emus.append(emu)
        retries_by_emu[emu] = 0

        if (only_set and emu not in only_set) or (emu in skip_set):
            paused_emus.add(emu)
            st_init = "skipped_tutorial_locked" if emu in (3, 22) else "skipped"
            state["accounts"][emu_str] = {
                "name": acc["name"],
                "client": acc["client"],
                "emu": emu,
                "status": st_init,
                "retries": 0,
                "updated_at": now_iso(),
            }
        else:
            state["accounts"][emu_str] = {
                "name": acc["name"],
                "client": acc["client"],
                "emu": emu,
                "status": "pending",
                "retries": 0,
                "updated_at": now_iso(),
            }

    def save_status():
        with status_lock:
            state["updated_at"] = now_iso()
            tmp = status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(status_path)

    def write_control(conc: int, paused: set[int]):
        payload = {
            "concurrency": conc,
            "paused_emus": sorted(paused),
            "resume_emus": [],
            "updated_at": now_iso(),
        }
        tmp = control_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(control_path)

    current_concurrency = max(1, min(6, args.concurrency))
    write_control(current_concurrency, paused_emus)
    last_ctrl_mtime = control_path.stat().st_mtime
    save_status()

    active_count = sum(1 for e in ordered_emus if e not in paused_emus)
    print(
        f"[batch] Starting {active_count} active accounts (total={len(ordered_emus)}) with "
        f"concurrency={current_concurrency}, target={args.target}, runs={args.runs}, max_retries={args.max_retries}",
        flush=True,
    )

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    active_spa_emus: set[int] = set()
    active_procs: dict[int, subprocess.Popen] = {}
    user_stopped_emus: set[int] = set()
    next_eligible_at: dict[int, float] = {}
    last_launch_ts = 0.0

    def run_single_attempt(emu: int):
        nonlocal active_spa_emus
        acc = accounts_by_emu[emu]
        emu_str = str(emu)
        name = acc["name"]
        client = acc["client"]
        log_file = log_dir / f"spa_emu{emu:02d}.log"

        with status_lock:
            retries_by_emu[emu] += 1
            attempt_num = retries_by_emu[emu]
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
        if args.mas_strict_yield:
            cmd.append("--mas-strict-yield")

        mode_open = "w" if (attempt_num == 1 and not log_file.exists()) else "a"
        rc = -1
        try:
            with open(log_file, mode_open, encoding="utf-8", errors="replace") as lf:
                if attempt_num > 1:
                    lf.write(f"\n=== AUTO-RETRY #{attempt_num - 1} at {now_iso()} ===\n")
                    lf.flush()
                proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
                with status_lock:
                    active_procs[emu] = proc
                rc = proc.wait()
        finally:
            with status_lock:
                active_procs.pop(emu, None)
                active_spa_emus.discard(emu)
                was_user_stopped = emu in user_stopped_emus
                user_stopped_emus.discard(emu)

        if was_user_stopped:
            if not args.skip_emu_check:
                force_shutdown_emu(args.mumu_mgr, emu)
            with status_lock:
                state["accounts"][emu_str].update({
                    "status": "paused_by_user",
                    "exit_code": rc,
                    "finished_at": now_iso(),
                })
            save_status()
            print(f"[batch][emu={emu} {name}] Stopped cleanly by user hot-control (other accounts untouched).", flush=True)
            return

        if rc == 0:
            st = "completed"
        elif rc == 10:
            st = "tutorial_locked"
        elif rc == 14:
            st = "consecutive_losses"
        elif rc == 15:
            # Yielded cleanly for AUTO-MAS: do NOT burn retry budget; re-queue as paused_for_mas
            with status_lock:
                retries_by_emu[emu] = max(0, retries_by_emu[emu] - 1)
                state["accounts"][emu_str]["status"] = "paused_for_mas"
                next_eligible_at[emu] = time.time() + max(1, args.mas_poll_sec)
            save_status()
            print(f"[batch][emu={emu} {name}] Yielded for AUTO-MAS (rc=15); will auto-resume when MAS clears.", flush=True)
            return
        else:
            st = {11: "timeout", 12: "plugin_error", 13: "stalled", 16: "nav_not_solo"}.get(rc, f"failed_rc_{rc}")
            now_dt = datetime.datetime.now()
            in_daily_reset = (now_dt.hour == 3 and now_dt.minute >= 58) or (now_dt.hour == 4 and now_dt.minute <= 20)
            if in_daily_reset and rc in (12, 13):
                # CN 04:00 daily server sync ("数据已更新，即将进行同步"): reboot & relogin without burning retry budget
                print(
                    f"[batch][emu={emu} {name}] Hit 04:00 daily reset window ({st}, rc={rc}); relogging without consuming retry.",
                    flush=True,
                )
                if not args.skip_emu_check:
                    force_shutdown_emu(args.mumu_mgr, emu)
                with status_lock:
                    retries_by_emu[emu] = max(0, retries_by_emu[emu] - 1)
                    state["accounts"][emu_str]["status"] = "pending"
                    next_eligible_at[emu] = time.time() + (1 if args.skip_emu_check else 15)
                save_status()
                return
            print(f"[batch][emu={emu} {name}] Attempt {attempt_num} ended with {st} (rc={rc})", flush=True)
            if not args.skip_emu_check:
                force_shutdown_emu(args.mumu_mgr, emu)
            if attempt_num <= args.max_retries:
                with status_lock:
                    state["accounts"][emu_str]["status"] = "pending"
                    next_eligible_at[emu] = time.time() + (1 if args.skip_emu_check else 15)
                save_status()
                return

        with status_lock:
            state["accounts"][emu_str].update({
                "status": st,
                "exit_code": rc,
                "retries": max(0, retries_by_emu[emu] - 1),
                "finished_at": now_iso(),
            })
        save_status()
        print(f"[batch][emu={emu} {name}] Final status={st} rc={rc} retries={max(0, retries_by_emu[emu] - 1)}", flush=True)

    def sync_control_file():
        nonlocal current_concurrency, last_ctrl_mtime
        try:
            if not control_path.exists():
                return
            mt = control_path.stat().st_mtime
            if mt == last_ctrl_mtime:
                return
            last_ctrl_mtime = mt
            ctrl = json.loads(control_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[batch][ctrl] Failed to read control file: {e}", flush=True)
            return

        new_conc = int(ctrl.get("concurrency", current_concurrency) or current_concurrency)
        new_conc = max(1, min(6, new_conc))
        if new_conc != current_concurrency:
            print(f"[batch][ctrl] Hot-updated concurrency: {current_concurrency} -> {new_conc}", flush=True)
            current_concurrency = new_conc
            with status_lock:
                state["params"]["concurrency"] = new_conc
            save_status()

        new_paused = {int(x) for x in ctrl.get("paused_emus", []) if str(x).isdigit()}
        resume_list = {int(x) for x in ctrl.get("resume_emus", []) if str(x).isdigit()}
        new_paused -= resume_list

        # 1. Stop any newly paused running emulators (WITHOUT touching other accounts!)
        to_stop = new_paused - paused_emus
        for emu in sorted(to_stop):
            paused_emus.add(emu)
            emu_str = str(emu)
            proc = None
            with status_lock:
                if emu in active_procs:
                    user_stopped_emus.add(emu)
                    proc = active_procs[emu]
                elif emu_str in state["accounts"] and state["accounts"][emu_str]["status"] in ("pending", "waiting_mas_idle", "paused_for_mas"):
                    state["accounts"][emu_str]["status"] = "paused_by_user"
            if proc is not None:
                print(f"[batch][ctrl] Hot-stopping ONLY emu={emu} (pid={proc.pid})...", flush=True)
                kill_proc_tree(proc)
            save_status()

        # 2. Resume / hot-add any unpaused or explicitly resumed accounts
        to_resume = (paused_emus - new_paused) | resume_list
        if to_resume:
            for emu in sorted(to_resume):
                paused_emus.discard(emu)
                emu_str = str(emu)
                if emu not in accounts_by_emu:
                    continue
                with status_lock:
                    if emu in active_spa_emus:
                        continue
                    cur_st = state["accounts"][emu_str]["status"]
                    if cur_st != "running":
                        retries_by_emu[emu] = 0
                        next_eligible_at[emu] = 0.0
                        state["accounts"][emu_str]["status"] = "pending"
                        state["accounts"][emu_str].pop("finished_at", None)
                        print(f"[batch][ctrl] Hot-queued emu={emu} ({accounts_by_emu[emu]['name']}) into live batch!", flush=True)
            save_status()
            if resume_list:
                write_control(current_concurrency, paused_emus)
                last_ctrl_mtime = control_path.stat().st_mtime

    threads: list[threading.Thread] = []
    poll_interval = 0.5 if args.skip_emu_check else 2.0
    min_stagger = 0.0 if args.skip_emu_check else 15.0

    while True:
        sync_control_file()

        # Prune finished threads
        threads = [t for t in threads if t.is_alive()]

        # Find runnable accounts
        now_ts = time.time()
        candidates: list[int] = []
        has_future_work = False
        with status_lock:
            for emu in ordered_emus:
                if emu in paused_emus or emu in active_spa_emus:
                    continue
                st = state["accounts"][str(emu)]["status"]
                if st in ("pending", "waiting_mas_idle", "paused_for_mas"):
                    has_future_work = True
                    if now_ts >= next_eligible_at.get(emu, 0.0):
                        candidates.append(emu)

        if not has_future_work and not active_spa_emus and not threads:
            break

        if candidates:
            running_all = set() if args.skip_emu_check else get_running_emus(args.mumu_mgr)
            for emu in candidates:
                with status_lock:
                    active_cnt = len(active_spa_emus)
                    external_emus = running_all - active_spa_emus
                    total_used = active_cnt + len(external_emus)

                if args.mas_strict_yield:
                    blocked = (
                        (not args.skip_emu_check and is_mas_maa_running())
                        or bool(has_active_mas_lock(args.pause_locks_dir))
                        or (not args.skip_emu_check and emu in running_all)
                        or active_cnt >= current_concurrency
                    )
                elif args.skip_emu_check:
                    blocked = bool(has_active_mas_lock(args.pause_locks_dir)) or active_cnt >= current_concurrency
                else:
                    blocked = (emu in running_all) or (total_used >= current_concurrency)

                if blocked:
                    with status_lock:
                        if state["accounts"][str(emu)]["status"] == "pending":
                            state["accounts"][str(emu)]["status"] = "waiting_mas_idle"
                    save_status()
                    continue

                if min_stagger > 0 and (time.time() - last_launch_ts) < min_stagger:
                    break

                with status_lock:
                    active_spa_emus.add(emu)
                last_launch_ts = time.time()
                running_all.add(emu)
                t = threading.Thread(target=run_single_attempt, args=(emu,), daemon=True)
                t.start()
                threads.append(t)
                break  # launch at most 1 per loop tick so stagger & control checks stay responsive

        time.sleep(poll_interval)

    with status_lock:
        state["finished_at"] = now_iso()
    save_status()
    print(f"[batch] All queued accounts finished! Status: {status_path}", flush=True)


if __name__ == "__main__":
    main()
