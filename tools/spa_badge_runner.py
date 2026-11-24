#!/usr/bin/env python3
r"""
Headless Stronghold Protocol (卫戍协议：规程) badge/score runner using MaaCore.dll v6.23+.

Features:
- Calls MaaCore.dll v6.23+ directly via ctypes (supports native `StrongholdProtocol` task).
- Auto-launches MuMu emulator & Arknights client (Official / Bilibili).
- Runs `MAAOrch@SPA@EnterCN` nav (auto-abandons old saves, switches to 个人模拟 tab, verifies 极速模拟).
- Detects `入门协议` lock (`rc=10`) so locked accounts never loop.
- Built-in stall watchdog (`--stall-minutes`, default 12): aborts cleanly (`rc=13`) if no task
  progress occurs for N minutes so the batch orchestrator can reboot the emulator and resume.
- Forces UTF-8 output and logs strategy/win/loss progress cleanly.

Exit codes:
  0  = completed cleanly (target wins reached or all strategies already at target badge)
  5  = ADB connect failed
  6  = StrongholdProtocol task rejected
  7  = AsstStart failed
  10 = tutorial_locked (未过入门协议)
  11 = max_minutes hard timeout
  12 = StrongholdProtocolTaskPlugin SubTaskError (e.g. screen lost >30s)
  13 = stall watchdog triggered (no progress for --stall-minutes)
  14 = consecutive losses circuit-breaker triggered
  15 = paused/yielded for AUTO-MAS lock (E:\fleet-pause\locks\*.lock)
  16 = nav gate failed (个人模拟 / 极速模拟 not confirmed; refused to start StrongholdProtocol)
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ASST_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)

MSG_NAMES = {
    0: "InternalError",
    1: "InitFailed",
    2: "ConnectionInfo",
    3: "AllTasksCompleted",
    4: "AsyncCallInfo",
    5: "Destroyed",
    10000: "TaskChainError",
    10001: "TaskChainStart",
    10002: "TaskChainCompleted",
    10003: "TaskChainExtraInfo",
    10004: "TaskChainStopped",
    20000: "SubTaskError",
    20001: "SubTaskStart",
    20002: "SubTaskCompleted",
    20003: "SubTaskExtraInfo",
    20004: "SubTaskStopped",
}


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


def run_mumu(mumu_mgr: str, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [mumu_mgr] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def ensure_emulator_started(mumu_mgr: str, emu: int, boot_timeout: int = 120) -> str:
    """Start MuMu emulator `emu` if not running and return ADB address `127.0.0.1:<port>`."""
    def get_info():
        r = run_mumu(mumu_mgr, ["info", "-v", str(emu)], timeout=20)
        try:
            return json.loads(r.stdout)
        except Exception:
            return {}

    info = get_info()
    if not info.get("is_android_started"):
        print(f"[emu={emu}] Launching emulator...", flush=True)
        run_mumu(mumu_mgr, ["control", "-v", str(emu), "launch"], timeout=30)
        deadline = time.time() + boot_timeout
        while time.time() < deadline:
            time.sleep(3)
            info = get_info()
            if info.get("is_android_started") and info.get("adb_port"):
                break
        else:
            raise RuntimeError(f"Emulator {emu} did not finish booting within {boot_timeout}s")
        time.sleep(5)

    host = info.get("adb_host_ip") or "127.0.0.1"
    port = info.get("adb_port")
    if not port:
        raise RuntimeError(f"Emulator {emu} started but adb_port missing: {info}")
    addr = f"{host}:{port}"
    print(f"[emu={emu}] Ready at {addr} (name={info.get('name', '')})", flush=True)
    return addr


def main():
    ap = argparse.ArgumentParser(description="Run MAA v6.23+ StrongholdProtocol headlessly via MaaCore.dll")
    ap.add_argument("--emu", type=int, required=True, help="MuMu emulator index")
    ap.add_argument("--client", choices=["Official", "Bilibili"], required=True, help="Game client type")
    ap.add_argument("--mode", choices=["badge", "score"], default="badge", help="StrongholdProtocol mode")
    ap.add_argument("--theme", default="Regulation", help="Theme: Regulation (S3) or Alliance (S2)")
    ap.add_argument("--target", type=int, default=3, help="Badge target clears per strategy: 3=Gold, 6=Rainbow")
    ap.add_argument("--runs", type=int, default=60, help="Target wins in this run (1-999)")
    ap.add_argument("--attempts", type=int, default=0, help="Max attempts (0=unlimited)")
    ap.add_argument("--maa-dir", default=r"E:\maa-cli-fleet\maa_v623", help="Directory containing MaaCore.dll & resource/")
    ap.add_argument("--state-root", default=r"E:\maa-cli-fleet\data_v623", help="Root dir for per-emu isolated state dirs")
    ap.add_argument("--mumu-mgr", default=r"E:\MuMu Player 12\nx_main\MuMuManager.exe", help="MuMuManager.exe path")
    ap.add_argument("--adb", default=r"E:\MuMu Player 12\nx_main\adb.exe", help="adb.exe path")
    ap.add_argument("--skip-launch", action="store_true", help="Do not launch emulator; use --addr or query info")
    ap.add_argument("--addr", default="", help="Override ADB address (e.g. 127.0.0.1:16480)")
    ap.add_argument("--skip-nav", action="store_true", help="Skip StartUp + EnterCN nav (already on SPA lobby)")
    ap.add_argument("--no-shutdown", action="store_true", help="Keep emulator running after task completes")
    ap.add_argument("--max-minutes", type=int, default=300, help="Hard safety timeout in minutes")
    ap.add_argument("--stall-minutes", type=int, default=12, help="Abort with rc=13 if no task progress for N minutes")
    ap.add_argument("--max-consecutive-losses", type=int, default=4, help="Circuit-breaker: abort with rc=14 after N consecutive losses (0=disable)")
    ap.add_argument("--pause-locks-dir", default=r"E:\fleet-pause\locks", help="AUTO-MAS fleet-pause locks dir (yield with rc=15 when fresh *.lock exists)")
    ap.add_argument("--skip-theme-check", action="store_true", help="Skip auto-switching to default theme 卫戍协议终端")
    ap.add_argument("--gpu", type=int, default=-1, help="GPU ID for OCR (-1 = CPU OCR)")
    args = ap.parse_args()

    maa_dir = Path(args.maa_dir)
    dll_path = maa_dir / "MaaCore.dll"
    if not dll_path.exists():
        print(f"ERROR: MaaCore.dll not found at {dll_path}", file=sys.stderr)
        sys.exit(2)

    state_dir = Path(args.state_root) / f"state_spa_{args.emu:02d}"
    state_dir.mkdir(parents=True, exist_ok=True)

    addr = args.addr
    if not addr:
        if args.skip_launch:
            r = run_mumu(args.mumu_mgr, ["info", "-v", str(args.emu)], timeout=20)
            info = json.loads(r.stdout)
            addr = f"{info.get('adb_host_ip', '127.0.0.1')}:{info['adb_port']}"
        else:
            addr = ensure_emulator_started(args.mumu_mgr, args.emu)

    os.add_dll_directory(str(maa_dir))
    dll = ctypes.CDLL(str(dll_path))

    dll.AsstGetVersion.restype = ctypes.c_char_p
    dll.AsstSetUserDir.argtypes = [ctypes.c_char_p]
    dll.AsstSetUserDir.restype = ctypes.c_bool
    dll.AsstLoadResource.argtypes = [ctypes.c_char_p]
    dll.AsstLoadResource.restype = ctypes.c_bool
    dll.AsstSetStaticOption.argtypes = [ctypes.c_int, ctypes.c_char_p]
    dll.AsstSetStaticOption.restype = ctypes.c_bool
    dll.AsstCreateEx.argtypes = [ASST_CALLBACK, ctypes.c_void_p]
    dll.AsstCreateEx.restype = ctypes.c_void_p
    dll.AsstDestroy.argtypes = [ctypes.c_void_p]
    dll.AsstConnect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    dll.AsstConnect.restype = ctypes.c_bool
    dll.AsstAppendTask.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    dll.AsstAppendTask.restype = ctypes.c_int
    dll.AsstStart.argtypes = [ctypes.c_void_p]
    dll.AsstStart.restype = ctypes.c_bool
    dll.AsstStop.argtypes = [ctypes.c_void_p]
    dll.AsstStop.restype = ctypes.c_bool
    dll.AsstRunning.argtypes = [ctypes.c_void_p]
    dll.AsstRunning.restype = ctypes.c_bool

    ver = dll.AsstGetVersion().decode("utf-8", errors="replace")
    print(f"[emu={args.emu}] MaaCore {ver} | state={state_dir}", flush=True)

    dll.AsstSetUserDir(str(state_dir).encode("utf-8"))
    if args.gpu >= 0:
        dll.AsstSetStaticOption(2, str(args.gpu).encode("utf-8"))

    if not dll.AsstLoadResource(str(maa_dir).encode("utf-8")):
        print("ERROR: AsstLoadResource failed", file=sys.stderr)
        sys.exit(3)

    events = {
        "all_done": False,
        "tutorial_locked": False,
        "solo_confirmed": False,
        "theme_tid": 0,
        "loss_abort": False,
        "errors": [],
        "last_task": "",
        "last_activity": time.time(),
        "wins": 0,
        "losses": 0,
        "attempted": 0,
        "consecutive_losses": 0,
        "strategy": "",
        "strategy_passed": 0,
        "strategy_target": args.target,
    }
    progress_file = state_dir / "progress.json"

    def write_progress():
        try:
            payload = {
                "emu": args.emu,
                "wins": events["wins"],
                "losses": events["losses"],
                "attempted": events["attempted"],
                "consecutive_losses": events["consecutive_losses"],
                "strategy": events["strategy"],
                "strategy_passed": events["strategy_passed"],
                "strategy_target": events["strategy_target"],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = progress_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(progress_file)
        except Exception:
            pass

    @ASST_CALLBACK
    def on_msg(msg: int, details_bytes: bytes, _arg):
        name = MSG_NAMES.get(msg, f"Msg({msg})")
        raw = details_bytes.decode("utf-8", errors="replace") if details_bytes else ""
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"raw": raw}

        what = data.get("what", "")
        sub = data.get("details", {}).get("task", "") or data.get("taskchain", "") or what

        if msg in (10001, 10002, 20001, 20002, 20003):
            events["last_activity"] = time.time()

        if "TutorialLocked" in raw:
            events["tutorial_locked"] = True

        # Track whether 个人模拟 / 极速模拟 was confirmed AFTER the final nav/theme step.
        if msg == 10001 and events["theme_tid"] and data.get("taskid") == events["theme_tid"]:
            events["solo_confirmed"] = False
        if msg == 20002 and ("ArrivedQuickly" in raw or "ArrivedQuicklyOcr" in raw):
            events["solo_confirmed"] = True

        if what == "StrongholdStrategy":
            d = data.get("details", {})
            events["strategy"] = str(d.get("name", ""))
            events["strategy_passed"] = int(d.get("passed", 0) or 0)
            events["strategy_target"] = int(d.get("target", args.target) or args.target)
            write_progress()
            print(
                f"[emu={args.emu}] [PROGRESS] Strategy={events['strategy']} passed={events['strategy_passed']}/{events['strategy_target']}",
                flush=True,
            )
        elif what == "StrongholdSettlement":
            d = data.get("details", {})
            cleared = bool(d.get("cleared"))
            events["attempted"] = int(d.get("attempted", events["attempted"] + 1) or 0)
            if cleared:
                events["wins"] = int(d.get("progress", events["wins"] + 1) or 0)
                events["consecutive_losses"] = 0
            else:
                events["losses"] += 1
                events["consecutive_losses"] += 1
            if d.get("strategy"):
                events["strategy"] = str(d.get("strategy"))
            write_progress()
            print(
                f"[emu={args.emu}] [SETTLEMENT] strategy={events['strategy']} cleared={cleared} wins={events['wins']}/{d.get('total', args.runs)} attempted={events['attempted']} streak_loss={events['consecutive_losses']}",
                flush=True,
            )
            if args.max_consecutive_losses > 0 and events["consecutive_losses"] >= args.max_consecutive_losses:
                events["loss_abort"] = True
        elif msg in (0, 1, 10000, 20000):
            events["errors"].append((name, raw[:300]))
            print(f"[emu={args.emu}] [{name}] {raw[:300]}", flush=True)
        elif msg in (3, 10001, 10002, 10004, 20003) or "Arrived" in raw or "GiveUp" in raw or "ClickSolo" in raw or "Theme" in raw:
            print(f"[emu={args.emu}] [{name}] {raw[:300]}", flush=True)

        if msg == 3:
            events["all_done"] = True
        if sub:
            events["last_task"] = sub

    handle = dll.AsstCreateEx(on_msg, None)
    if not handle:
        print("ERROR: AsstCreateEx failed", file=sys.stderr)
        sys.exit(4)

    exit_code = 0
    try:
        print(f"[emu={args.emu}] Connecting ADB {addr}...", flush=True)
        if not dll.AsstConnect(
            handle,
            args.adb.encode("utf-8"),
            addr.encode("utf-8"),
            b"General",
        ):
            print(f"ERROR: AsstConnect failed for {addr}", file=sys.stderr)
            exit_code = 5
            return

        if not args.skip_nav:
            pkg = "com.hypergryph.arknights" if args.client == "Official" else "com.hypergryph.arknights.bilibili"
            print(f"[emu={args.emu}] Launching package {pkg} via adb...", flush=True)
            subprocess.run(
                [args.adb, "-s", addr, "shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
                capture_output=True,
                timeout=15,
            )
            tid1 = dll.AsstAppendTask(
                handle,
                b"StartUp",
                json.dumps({"enable": True, "client_type": args.client, "start_game_enabled": True}).encode("utf-8"),
            )
            tid2 = dll.AsstAppendTask(
                handle,
                b"Custom",
                json.dumps({"enable": True, "task_names": ["MAAOrch@SPA@EnterCN"]}).encode("utf-8"),
            )
            tid_theme = 0
            if not args.skip_theme_check:
                tid_theme = dll.AsstAppendTask(
                    handle,
                    b"Custom",
                    json.dumps({"enable": True, "task_names": ["MAAOrch@SPA@EnsureDefaultTheme"]}).encode("utf-8"),
                )
            events["theme_tid"] = tid_theme
            print(f"[emu={args.emu}] Phase 1 (Nav+Theme): StartUp={tid1}, Nav={tid2}, DefaultTheme={tid_theme}", flush=True)
            if not dll.AsstStart(handle):
                print("ERROR: AsstStart (Phase 1) failed", file=sys.stderr)
                exit_code = 7
                return

            events["last_activity"] = time.time()
            nav_deadline = time.time() + min(args.max_minutes * 60, 8 * 60)
            while time.time() < nav_deadline:
                time.sleep(2)
                if events["tutorial_locked"]:
                    print(f"[emu={args.emu}] STOP: Account has not completed 入门协议 (极速模拟 locked).", flush=True)
                    dll.AsstStop(handle)
                    exit_code = 10
                    return
                mas_lock = has_active_mas_lock(args.pause_locks_dir)
                if mas_lock:
                    print(f"[emu={args.emu}] MAS PAUSE LOCK detected ({mas_lock}) during nav, yielding...", flush=True)
                    dll.AsstStop(handle)
                    exit_code = 15
                    return
                if events["all_done"] or not dll.AsstRunning(handle):
                    break
            else:
                print(f"[emu={args.emu}] NAV TIMEOUT (8m), aborting before StrongholdProtocol...", flush=True)
                dll.AsstStop(handle)
                exit_code = 16
                return

            if not events["solo_confirmed"]:
                print(
                    f"[emu={args.emu}] NAV GATE FAILED: 个人模拟/极速模拟 not confirmed (last_task={events['last_task']}); refusing to start StrongholdProtocol!",
                    flush=True,
                )
                exit_code = 16
                return
            print(f"[emu={args.emu}] NAV GATE PASSED: 个人模拟/极速模拟 confirmed.", flush=True)
            events["all_done"] = False

        sp_params = {"enable": True, "mode": args.mode}
        if args.mode == "badge":
            sp_params.update({
                "theme": args.theme,
                "target": args.target,
                "runs": args.runs,
                "attempts": args.attempts,
            })
        tid3 = dll.AsstAppendTask(
            handle,
            b"StrongholdProtocol",
            json.dumps(sp_params).encode("utf-8"),
        )
        print(f"[emu={args.emu}] Queued StrongholdProtocol={tid3} params={sp_params}", flush=True)
        if tid3 == 0:
            print("ERROR: AsstAppendTask(StrongholdProtocol) returned 0 (invalid params or unsupported core)", file=sys.stderr)
            exit_code = 6
            return

        if not dll.AsstStart(handle):
            print("ERROR: AsstStart failed", file=sys.stderr)
            exit_code = 7
            return

        events["last_activity"] = time.time()
        deadline = time.time() + args.max_minutes * 60
        stall_sec = args.stall_minutes * 60

        while time.time() < deadline:
            time.sleep(2)
            if events["tutorial_locked"]:
                print(f"[emu={args.emu}] STOP: Account has not completed 入门协议 (极速模拟 locked).", flush=True)
                dll.AsstStop(handle)
                exit_code = 10
                break
            if events["loss_abort"]:
                print(
                    f"[emu={args.emu}] CIRCUIT BREAKER: {events['consecutive_losses']} consecutive losses (wins={events['wins']}), aborting to avoid spinning.",
                    flush=True,
                )
                dll.AsstStop(handle)
                exit_code = 14
                break
            mas_lock = has_active_mas_lock(args.pause_locks_dir)
            if mas_lock:
                print(
                    f"[emu={args.emu}] MAS PAUSE LOCK detected ({mas_lock}), yielding emulator cleanly for AUTO-MAS...",
                    flush=True,
                )
                dll.AsstStop(handle)
                exit_code = 15
                break
            if events["all_done"] or not dll.AsstRunning(handle):
                has_sp_err = any("StrongholdProtocolTaskPlugin" in raw for _, raw in events["errors"])
                print(
                    f"[emu={args.emu}] Completed! last_task={events['last_task']} wins={events['wins']} losses={events['losses']} errors={len(events['errors'])} sp_err={has_sp_err}",
                    flush=True,
                )
                if has_sp_err:
                    exit_code = 12
                break
            if stall_sec > 0 and (time.time() - events["last_activity"]) > stall_sec:
                print(
                    f"[emu={args.emu}] STALL WATCHDOG: no progress for {args.stall_minutes}m (last_task={events['last_task']}), aborting for retry...",
                    flush=True,
                )
                dll.AsstStop(handle)
                exit_code = 13
                break
        else:
            print(f"[emu={args.emu}] Timeout after {args.max_minutes}m, stopping...", flush=True)
            dll.AsstStop(handle)
            exit_code = 11
    finally:
        dll.AsstDestroy(handle)
        if not args.no_shutdown:
            print(f"[emu={args.emu}] Shutting down emulator...", flush=True)
            run_mumu(args.mumu_mgr, ["control", "-v", str(args.emu), "shutdown"], timeout=30)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
