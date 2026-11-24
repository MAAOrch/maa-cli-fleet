#!/usr/bin/env python3
"""
End-to-end automated test for AUTO-MAS fleet-pause lock yield & auto-resume.

Verifies:
1. has_active_mas_lock(): empty dir -> False, fresh q01.lock -> True, stale (>180s) -> False.
2. Full spa_batch_runner.py lifecycle:
   - Starts a simulated worker for emu=99;
   - While worker is running, creates a fresh `q01.lock` in an isolated temp locks dir;
   - Worker detects lock via `has_active_mas_lock` and yields with rc=15;
   - Batch runner transitions status to `paused_for_mas` / `waiting_mas_idle` without burning retries;
   - Removes `q01.lock`;
   - Batch runner automatically resumes emu=99 and finishes with `status == "completed"`, `retries == 0`, `runs == 2` (yielded once, resumed once).
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import spa_badge_runner
import spa_batch_runner


def test_lock_detection():
    with tempfile.TemporaryDirectory() as td:
        assert spa_badge_runner.has_active_mas_lock(td) == ""
        assert spa_batch_runner.has_active_mas_lock(td) == ""

        lock = Path(td) / "q01.lock"
        lock.write_text("q01 2026-11-24 21:00:00", encoding="utf-8")
        assert spa_badge_runner.has_active_mas_lock(td) == "q01.lock"
        assert spa_batch_runner.has_active_mas_lock(td) == "q01.lock"

        # Stale lock (5 minutes old > 180s) must be ignored
        old_ts = time.time() - 300
        os.utime(lock, (old_ts, old_ts))
        assert spa_badge_runner.has_active_mas_lock(td) == ""
        assert spa_batch_runner.has_active_mas_lock(td) == ""
    print("PASS: test_lock_detection (empty / fresh / stale)")


def test_batch_yield_and_resume():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        locks_dir = tmp / "locks"
        locks_dir.mkdir()
        log_dir = tmp / "logs"
        log_dir.mkdir()
        cfg_path = tmp / "batch.json"
        cfg_path.write_text(
            json.dumps({"accounts": [{"id": "t99", "name": "测试号", "client": "Official", "emu": "99"}]}),
            encoding="utf-8",
        )
        marker = tmp / "runs.txt"

        # Simulated spa_badge_runner that uses the real has_active_mas_lock() helper
        fake_runner = tmp / "fake_badge_runner.py"
        fake_runner.write_text(
            f"""import argparse, sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'tools')!r})
import spa_badge_runner

ap = argparse.ArgumentParser()
ap.add_argument('--pause-locks-dir', default='')
args, _ = ap.parse_known_args()

marker = Path({str(marker)!r})
run_idx = len(marker.read_text(encoding='utf-8').splitlines()) + 1 if marker.exists() else 1
with marker.open('a', encoding='utf-8') as f:
    f.write(f'start_{{run_idx}}\\n')
print(f'fake worker run {{run_idx}} started', flush=True)

if run_idx == 1:
    # Wait up to 10s for MAS lock to appear, then yield with rc=15
    for _ in range(50):
        lk = spa_badge_runner.has_active_mas_lock(args.pause_locks_dir)
        if lk:
            print(f'yielded on lock {{lk}}', flush=True)
            sys.exit(15)
        time.sleep(0.2)
    print('ERROR: lock never appeared', flush=True)
    sys.exit(99)
else:
    # Resumed after MAS finished -> succeed cleanly
    print('resumed and completed!', flush=True)
    sys.exit(0)
""",
            encoding="utf-8",
        )

        # Background thread simulates AUTO-MAS creating q01.lock, holding it for 2.5s, then releasing
        saw_paused = [False]

        def mas_simulator():
            # Wait until run_1 has started
            for _ in range(50):
                if marker.exists() and "start_1" in marker.read_text(encoding="utf-8"):
                    break
                time.sleep(0.1)
            lock = locks_dir / "q01.lock"
            lock.write_text("q01 test", encoding="utf-8")
            # Wait while lock is held and verify batch status reflects pause/wait
            for _ in range(30):
                st_file = log_dir / "spa_batch_status.json"
                if st_file.exists():
                    try:
                        st = json.loads(st_file.read_text(encoding="utf-8"))
                        acc_st = st.get("accounts", {}).get("99", {}).get("status", "")
                        if acc_st in ("paused_for_mas", "waiting_mas_idle"):
                            saw_paused[0] = True
                            break
                    except Exception:
                        pass
                time.sleep(0.15)
            time.sleep(0.5)
            lock.unlink(missing_ok=True)

        t = threading.Thread(target=mas_simulator, daemon=True)
        t.start()

        cmd = [
            sys.executable, "-u", str(ROOT / "tools" / "spa_batch_runner.py"),
            "--config", str(cfg_path),
            "--runner", str(fake_runner),
            "--python", sys.executable,
            "--log-dir", str(log_dir),
            "--concurrency", "1",
            "--pause-locks-dir", str(locks_dir),
            "--mas-poll-sec", "1",
            "--skip-emu-check",
            "--skip-emus", "",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        t.join(timeout=5)
        assert r.returncode == 0, f"batch rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
        assert saw_paused[0], f"never observed paused_for_mas/waiting_mas_idle! stdout={r.stdout}"

        runs = marker.read_text(encoding="utf-8").splitlines()
        assert runs == ["start_1", "start_2"], f"unexpected runs: {runs}"

        final_status = json.loads((log_dir / "spa_batch_status.json").read_text(encoding="utf-8"))
        acc = final_status["accounts"]["99"]
        assert acc["status"] == "completed", f"unexpected final status: {acc}"
        assert acc["retries"] == 0, f"yield should not burn retries, got retries={acc['retries']}"
        assert acc["exit_code"] == 0
    print("PASS: test_batch_yield_and_resume (yield rc=15 -> paused_for_mas -> auto-resume -> completed, retries=0)")


if __name__ == "__main__":
    test_lock_detection()
    test_batch_yield_and_resume()
    print("ALL MAS-YIELD TESTS PASSED!")
