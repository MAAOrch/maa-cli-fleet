#!/usr/bin/env python3
"""
Headless Stronghold Protocol (卫戍协议：规程) badge/score runner using MaaCore.dll v6.23+.

Why direct MaaCore.dll ctypes instead of maa-cli?
  maa-cli v0.7.5's TOML parser does not yet include the `StrongholdProtocol` task variant
  added in MAA v6.23.0/v6.23.1 (PR #19325). Calling MaaCore.dll directly via ctypes runs
  the exact official `StrongholdProtocol` badge-farming engine headlessly per emulator,
  with isolated state directories and clean callback logging.

Usage (on MAS host):
  python tools/spa_badge_runner.py --emu 4 --client Official --mode badge --target 3 --runs 60
  python tools/spa_badge_runner.py --emu 4 --client Official --mode badge --target 3 --runs 1 --attempts 1 --no-shutdown
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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
        # Give Android UI a few seconds to settle
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
    ap.add_argument("--max-minutes", type=int, default=240, help="Hard safety timeout in minutes")
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

    # Signatures
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
        # AsstStaticOptionKey_UseGpu = 2 (legacy) / keep default CPU unless specified
        dll.AsstSetStaticOption(2, str(args.gpu).encode("utf-8"))

    if not dll.AsstLoadResource(str(maa_dir).encode("utf-8")):
        print("ERROR: AsstLoadResource failed", file=sys.stderr)
        sys.exit(3)

    events = {"all_done": False, "tutorial_locked": False, "errors": [], "last_task": ""}

    @ASST_CALLBACK
    def on_msg(msg: int, details_bytes: bytes, _arg):
        name = MSG_NAMES.get(msg, f"Msg({msg})")
        raw = details_bytes.decode("utf-8", errors="replace") if details_bytes else ""
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"raw": raw}
        sub = data.get("details", {}).get("task", "") or data.get("taskchain", "") or data.get("what", "")
        if "TutorialLocked" in raw:
            events["tutorial_locked"] = True
        if msg in (0, 1, 10000, 20000):
            events["errors"].append((name, raw[:300]))
            print(f"[emu={args.emu}] [{name}] {raw[:300]}", flush=True)
        elif msg in (3, 10001, 10002, 10004, 20003) or "Stronghold" in raw or "Arrived" in raw:
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
            print(f"[emu={args.emu}] Queued StartUp={tid1}, Nav={tid2}", flush=True)

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

        deadline = time.time() + args.max_minutes * 60
        while time.time() < deadline:
            time.sleep(2)
            if events["tutorial_locked"]:
                print(f"[emu={args.emu}] STOP: Account has not completed 入门协议 (极速模拟 locked).", flush=True)
                dll.AsstStop(handle)
                exit_code = 10
                break
            if events["all_done"] or not dll.AsstRunning(handle):
                has_sp_err = any("StrongholdProtocolTaskPlugin" in raw for _, raw in events["errors"])
                print(f"[emu={args.emu}] Completed! last_task={events['last_task']} errors={len(events['errors'])} sp_err={has_sp_err}", flush=True)
                if has_sp_err:
                    exit_code = 12
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
