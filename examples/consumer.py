"""Reference consumer: one scan through the service API while something moves the camera.

The service does no motion control. This example shows the sequence and the one thing you have to provide: a
carrier (rail, slider, gantry, vehicle) that moves the camera across the scene at a constant speed and reports
its position. `Carrier` below is a stand-in that only simulates time and position. Replace it with a class that
talks to your own motion controller and keep the rest.

Run against a service started with `python -m specimFX10.main --server --fake` (no hardware):

    python examples/consumer.py --base http://127.0.0.1:23000 --speed 5 --length 20
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from typing import Optional

import requests


class ServiceClient:
    """Minimal HTTP client for the capture service."""

    def __init__(self, base: str = "http://127.0.0.1:23000", timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.api = self.base + "/api/specimFX10"
        self.timeout = timeout

    def _get(self, path: str):
        url = self.base + path if path.startswith("/") else self.api + "/" + path
        r = requests.get(url, timeout=self.timeout); r.raise_for_status(); return r.json()

    def _post(self, path: str, body: Optional[dict] = None, timeout: Optional[float] = None):
        r = requests.post(self.api + "/" + path, json=body or {}, timeout=timeout or self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text.strip()[:200]}")
        return r.json()

    def status(self) -> str: return self._get("/status")["status"]
    def camera(self) -> dict: return self._get("camera")
    def stream_start(self) -> dict: return self._post("stream/start")
    def dark(self, frames: int = 40) -> dict: return self._post("reference/dark", {"frames": frames}, timeout=120)
    def white(self, frames: int = 40, columns: Optional[list] = None) -> dict:
        body = {"frames": frames}
        if columns: body["columns"] = list(columns)
        return self._post("reference/white", body, timeout=120)
    def scan_start(self, name: str = "", max_seconds: float = 0, meta: Optional[dict] = None) -> dict:
        return self._post("scan/start", {"name": name, "max_seconds": max_seconds, "meta": meta or {}})
    def scan_position(self, x=None, y=None, z=None) -> dict:
        return self._post("scan/position", {"t": time.time(), "x": x, "y": y, "z": z})
    def scan_stop(self) -> dict: return self._post("scan/stop")
    def record(self, record_id: str) -> dict: return self._get("records/" + record_id)


class Carrier:
    """Stand-in for your motion controller: moves one axis at a constant speed and reports the position.

    Replace `move_to` and `position` with calls to your own hardware. The scan only needs two things from it:
    a move at constant speed from the start to the end of the scan, and the current position on request.
    """

    def __init__(self):
        self._pos = {"x": 0.0, "y": 0.0, "z": 0.0}

    def position(self) -> dict:
        return dict(self._pos)

    def move_to(self, axis: str, target_cm: float, speed_cm_s: float) -> None:
        start = self._pos[axis]
        dist = target_cm - start
        duration = abs(dist) / max(speed_cm_s, 0.1)
        t0 = time.time()
        while True:
            f = min((time.time() - t0) / duration, 1.0) if duration > 0 else 1.0
            self._pos[axis] = start + dist * f
            if f >= 1.0:
                return
            time.sleep(0.05)


def run_scan(cam: ServiceClient, carrier: Carrier, *, axis: str = "x", start_cm: float = 0.0, length_cm: float = 20.0,
             speed_cm_s: float = 5.0, dark_frames: int = 40, white_frames: int = 0, white_columns: Optional[list] = None,
             name: str = "scan", poll_s: float = 0.1) -> dict:
    """Preconditions -> references -> scan while the carrier moves -> record."""
    if cam.status() != "ACTIVE":
        raise RuntimeError(f"service is {cam.status()}, not ACTIVE")
    result = {"camera": {k: cam.camera()[k] for k in ("serial", "frame_rate", "exposure_us")}}
    cam.stream_start()
    if dark_frames:
        result["dark"] = cam.dark(dark_frames)                      # shutter closed inside the camera, then reopened
    if white_frames:
        result["white"] = cam.white(white_frames, white_columns)     # the white tile must be in the scene now
    carrier.move_to(axis, start_cm, speed_cm_s=10.0)                 # positioning move, speed does not matter
    travel_s = length_cm / max(speed_cm_s, 0.1)
    cam.scan_start(name=name, max_seconds=travel_s + 10.0, meta={"axis": axis, "speed_cm_s": speed_cm_s, "length_cm": length_cm})
    stop = threading.Event()

    def poll_positions():
        while not stop.is_set():
            p = carrier.position()
            try:
                cam.scan_position(x=p.get("x"), y=p.get("y"), z=p.get("z"))
            except Exception:
                pass
            stop.wait(poll_s)

    poller = threading.Thread(target=poll_positions, daemon=True)
    poller.start()
    try:
        carrier.move_to(axis, start_cm + length_cm, speed_cm_s=speed_cm_s)   # the scan move, at the chosen speed
    finally:
        stop.set(); poller.join(timeout=2.0)
        final = cam.scan_stop()
    result["scan"] = final.get("scan", {})
    rid = result["scan"].get("record_id")
    if rid:
        rec = cam.record(rid)
        result["record"] = {k: rec.get(k) for k in ("lines", "duration_s", "achieved_fps")}
        result["record"]["positions"] = len(rec.get("positions", []))
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default="http://127.0.0.1:23000")
    p.add_argument("--axis", default="x")
    p.add_argument("--start", type=float, default=0.0, help="start position [cm]")
    p.add_argument("--length", type=float, default=20.0, help="scan length [cm]")
    p.add_argument("--speed", type=float, default=5.0, help="scan speed [cm/s]")
    p.add_argument("--dark-frames", type=int, default=40)
    p.add_argument("--white-frames", type=int, default=0)
    p.add_argument("--name", default="scan")
    a = p.parse_args(argv)
    out = run_scan(ServiceClient(a.base), Carrier(), axis=a.axis, start_cm=a.start, length_cm=a.length,
                   speed_cm_s=a.speed, dark_frames=a.dark_frames, white_frames=a.white_frames, name=a.name)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
