#!/usr/bin/env python3
"""Integration test proving zero-restart per-account hot control in spa_batch_runner.py.

Verifies:
1. Two accounts (emu=30, emu=36) start concurrently; emu=4 starts skipped.
2. Adding emu=36 to spa_batch_control.json's paused_emus stops ONLY emu=36 (`paused_by_user`)
   while emu=30 continues running uninterrupted (marker shows 1 single run, never restarted!).
3. Adding emu=4 to resume_emus hot-launches emu=4 into the live batch and completes it
   without restarting emu=30.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


MOCK_RUNNER = r"""
import argparse, pathlib, sys, time
ap = argparse.ArgumentParser()
ap.add_argument("--emu", type=int, required=True)
ap.add_argument("--client", default="Official")
ap.add_argument("--mode", default="badge")
ap.add_argument("--target", type=int, default=3)
ap.add_argument("--runs", type=int, default=60)
ap.add_argument("--attempts", type=int, default=0)
ap.add_argument("--max-minutes", type=int, default=10)
ap.add_argument("--stall-minutes", type=int, default=5)
ap.add_argument("--pause-locks-dir", default="")
ap.add_argument("--mas-strict-yield", action="store_true")
args = ap.parse_args()

marker = pathlib.Path(args.pause_locks_dir).parent / f"runs_emu{args.emu}.txt"
with open(marker, "a", encoding="utf-8") as f:
    f.write("start\n")
if args.emu == 36:
    # Stay alive until hot-stopped by batch control
    for _ in range(100):
        time.sleep(0.2)
elif args.emu == 30:
    # Stay alive ~4s and finish cleanly (must NOT be restarted!)
    time.sleep(4.0)
else:
    # emu=4 hot-added mid-run, finishes quickly
    time.sleep(1.0)
with open(marker, "a", encoding="utf-8") as f:
    f.write("done\n")
sys.exit(0)
"""


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        locks = root / "locks"
        locks.mkdir()
        logs = root / "logs"
        logs.mkdir()
        mock_py = root / "mock_runner.py"
        mock_py.write_text(MOCK_RUNNER, encoding="utf-8")
        cfg_path = root / "batch.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "accounts": [
                        {"id": "a4", "name": "皮埃尔商店", "client": "Official", "emu": "4"},
                        {"id": "a30", "name": "笙笙wzh", "client": "Official", "emu": "30"},
                        {"id": "a36", "name": "y9Flipped", "client": "Official", "emu": "36"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "tools/spa_batch_runner.py",
                "--config", str(cfg_path),
                "--runner", str(mock_py),
                "--python", sys.executable,
                "--log-dir", str(logs),
                "--concurrency", "2",
                "--skip-emus", "4",
                "--skip-emu-check",
                "--pause-locks-dir", str(locks),
                "--mas-poll-sec", "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )

        ctrl_path = logs / "spa_batch_control.json"
        status_path = logs / "spa_batch_status.json"

        # Wait until both 30 and 36 are running
        deadline = time.time() + 10
        while time.time() < deadline:
            if status_path.exists():
                st = json.loads(status_path.read_text(encoding="utf-8"))
                if st["accounts"]["30"]["status"] == "running" and st["accounts"]["36"]["status"] == "running":
                    break
            time.sleep(0.2)
        else:
            proc.kill()
            raise AssertionError("emu=30 and emu=36 did not both enter running state")

        # Hot-stop ONLY emu=36 AND hot-start emu=4 in the same control update!
        time.sleep(0.1)
        ctrl = json.loads(ctrl_path.read_text(encoding="utf-8"))
        ctrl["paused_emus"] = [36]
        ctrl["resume_emus"] = [4]
        ctrl_path.write_text(json.dumps(ctrl, ensure_ascii=False), encoding="utf-8")

        out, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0, f"batch rc={proc.returncode}\n{out}"

        st = json.loads(status_path.read_text(encoding="utf-8"))
        assert st["accounts"]["36"]["status"] == "paused_by_user", st["accounts"]["36"]
        assert st["accounts"]["30"]["status"] == "completed", st["accounts"]["30"]
        assert st["accounts"]["4"]["status"] == "completed", st["accounts"]["4"]

        # Verify emu=30 ran ONCE without ever being restarted!
        r30 = (root / "runs_emu30.txt").read_text(encoding="utf-8").splitlines()
        assert r30 == ["start", "done"], f"emu=30 was interrupted/restarted: {r30}"
        r4 = (root / "runs_emu4.txt").read_text(encoding="utf-8").splitlines()
        assert r4 == ["start", "done"], f"emu=4 did not complete cleanly: {r4}"

        print("PASS: hot-stop emu=36 + hot-start emu=4 left emu=30 100% uninterrupted!")


if __name__ == "__main__":
    main()
