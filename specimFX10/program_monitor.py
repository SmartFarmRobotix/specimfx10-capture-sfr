"""Watchdog: spawns the service, polls GET /status, restarts it on failure.
shlex-split command (quoted args survive), capped back-off, tolerates three missed polls while the
last status was SCANNING before restarting,
stops cleanly on SIGTERM as well as SIGINT."""
from __future__ import annotations

import argparse
import shlex
import signal
import subprocess
import sys
import threading
import time

import requests


class ProgramMonitor:
    def __init__(self, command: str, port: int, timeout: float, poll_s: float = 10.0):
        self.command = shlex.split(command) + ["--port", str(port)]
        self.port, self.timeout, self.poll_s = port, timeout, poll_s
        self.proc: subprocess.Popen | None = None
        self.stop_flag = threading.Event()
        self.startup_wait, self.cleanup_wait, self.max_wait = 15.0, 5.0, 120.0
        self.scan_grace_polls = 3             # consecutive failed polls tolerated while the last status was SCANNING
        signal.signal(signal.SIGINT, self._exit)
        signal.signal(signal.SIGTERM, self._exit)

    def _log(self, msg: str) -> None:
        print(f"[WATCHDOG] {msg}", flush=True)

    def _spawn(self) -> None:
        self.proc = subprocess.Popen(self.command)
        self._log(f"started pid {self.proc.pid}: {' '.join(self.command)}")
        self.stop_flag.wait(self.startup_wait)

    def _restart(self) -> None:
        self._log(f"restarting (next startup wait {self.startup_wait:.0f}s)")
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait()
        self.stop_flag.wait(self.cleanup_wait)
        self.startup_wait = min(self.startup_wait * 1.2, self.max_wait)
        if not self.stop_flag.is_set():
            self._spawn()

    def run(self) -> None:
        self._spawn()
        url = f"http://127.0.0.1:{self.port}/status"
        last_status, failures = None, 0
        while not self.stop_flag.is_set():
            if self.proc.poll() is not None:
                self._log(f"process exited with {self.proc.returncode}")
                self._restart(); last_status, failures = None, 0; continue
            try:
                r = requests.get(url, timeout=self.timeout)
                status = r.json().get("status")
                failures = 0
                if status == "SCANNING":
                    pass                      # never interrupt a scan
                elif status == "INACTIVE":
                    self._log("service reports INACTIVE"); self._restart(); last_status = None; continue
                else:
                    self.startup_wait = 15.0  # healthy: reset back-off
                last_status = status
            except Exception as e:
                failures += 1
                # a scan keeps the service busy; one missed poll during a scan is not a reason to kill it
                if last_status == "SCANNING" and failures <= self.scan_grace_polls:
                    self._log(f"status check failed during a scan ({failures}/{self.scan_grace_polls}): {e}")
                else:
                    self._log(f"status check failed: {e}"); self._restart(); last_status, failures = None, 0; continue
            self.stop_flag.wait(self.poll_s)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _exit(self, signum, frame) -> None:
        self._log(f"signal {signum}, stopping")
        self.stop_flag.set()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--command", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--timeout", type=float, default=30.0)
    a = ap.parse_args()
    ProgramMonitor(a.command, a.port, a.timeout).run()
    sys.exit(0)
