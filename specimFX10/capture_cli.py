"""Command-line checks against the camera (no motion control). Add --fake to run them without hardware.

  python -m specimFX10.capture_cli info
  python -m specimFX10.capture_cli testpattern            # Ramp pattern round-trip: validates unpacking + BIL layout
  python -m specimFX10.capture_cli dark  [--frames 40]
  python -m specimFX10.capture_cli scan  --seconds 3 [--fps 50] [--name bench]
  python -m specimFX10.capture_cli look                   # what does the camera see right now? (stats + preview png)
  python -m specimFX10.capture_cli monitor --seconds 120  # print light level once per second (for on-site checks)
  python -m specimFX10.capture_cli shutter --cycles 3     # pulse the shutter closed/open, print level after each

Exit status: 0 = the command ran and, for testpattern, the layout check passed; 1 = a check failed;
2 = the camera delivered no frames within the wait (network, packet size, firewall).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import numpy as np

from .camera import FX10Camera, CameraConfig
from .acquisition import Acquisition
from .envi import read_bil

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("specimFX10.cli")

FRAME_WAIT_S = 5.0


class NoFrames(RuntimeError):
    pass


def stats(a: np.ndarray) -> dict:
    return {"min": int(a.min()), "max": int(a.max()), "mean": round(float(a.mean()), 1),
            "p50": int(np.percentile(a, 50)), "p99": int(np.percentile(a, 99)), "saturated_frac": float((a >= 4095).mean())}


def _wait(pred, timeout_s: float, what: str) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if pred():
            return
        time.sleep(0.05)
    raise NoFrames(f"{what} within {timeout_s:.0f} s")


def _frame(acq: Acquisition, timeout_s: float = FRAME_WAIT_S) -> np.ndarray:
    _wait(lambda: acq.last_frame is not None, timeout_s, "no frame from the camera")
    return acq.last_frame.data


def _finished_scan(acq: Acquisition, timeout_s: float):
    _wait(lambda: acq.mode != "scan", timeout_s, "the scan did not finish")
    return acq.scan


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["info", "testpattern", "dark", "scan", "look", "monitor", "shutter"])
    p.add_argument("--cycles", type=int, default=3)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--data-dir", default=os.path.expanduser("~/specimFX10/data"))
    p.add_argument("--fps", type=float, default=50.0)
    p.add_argument("--exposure", type=float, default=2000.0)
    p.add_argument("--binv", type=int, default=2)
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--frames", type=int, default=40)
    p.add_argument("--name", default="")
    p.add_argument("--fake", action="store_true", help="built-in fake camera instead of Aravis")
    a = p.parse_args(argv)

    cfg = CameraConfig(frame_rate=a.fps, exposure_us=a.exposure, binning_vertical=a.binv)
    if a.fake:
        from .fake_camera import FakeFX10Camera
        cam = FakeFX10Camera(cfg)
    else:
        cam = FX10Camera(cfg)
    cam.open()
    info = cam.configure()
    acq = Acquisition(cam, a.data_dir)
    try:
        if a.cmd == "info":
            print(json.dumps(info, indent=1)); return 0

        if a.cmd == "testpattern":
            cam.set("TestPattern", "Ramp")
            try:
                acq.start_stream(); _frame(acq)
                acq.start_scan(name="testpattern", max_lines=20)
                s = _finished_scan(acq, 20 / max(a.fps, 1) + FRAME_WAIT_S)
            finally:
                cam.set("TestPattern", "Off")
            data, hdr = read_bil(os.path.join(s.folder, "cube"))
            f0 = np.asarray(data[0])                      # [bands, samples]
            row = f0[0].astype(int)
            d = np.diff(row)
            checks = {
                "hdr_interleave_bil": hdr.get("interleave") == "bil",
                "shape": tuple(data.shape) == (20, info["region"]["height"], info["region"]["width"]),
                "ramp_rises_across_the_line": bool((d >= 0).all() and row[-1] > row[0]),
                "ramp_reaches_full_scale": int(f0.max()) == 4095,
                "rows_identical": bool((f0 == f0[0]).all()),
                "frames_identical": bool((np.asarray(data) == f0).all()),
            }
            report = {"folder": s.folder, "shape": list(data.shape), "row0_first10": row[:10].tolist(),
                      "row0_last5": row[-5:].tolist(), "unique_diffs": sorted(set(d.tolist()))[:6],
                      "checks": checks, "handler": cam.stats}
            print(json.dumps(report, indent=1))
            return 0 if all(checks.values()) else 1

        if a.cmd == "dark":
            r = acq.capture_dark(n=a.frames, folder=os.path.join(a.data_dir, "refs"))
            print(json.dumps({"dark": r, "shutter": cam.shutter, "handler": cam.stats}, indent=1)); return 0

        if a.cmd == "look":
            acq.start_stream(); fr = _frame(acq)
            spec = fr.mean(axis=1)                        # mean spectrum over the line
            prof = fr.mean(axis=0)                        # mean spatial profile
            out = {"frame": stats(fr), "shutter": cam.shutter,
                   "spectrum_mean_by_band_every_16": [round(float(x), 1) for x in spec[::16]],
                   "spatial_profile_every_64": [round(float(x), 1) for x in prof[::64]],
                   "temperatures": cam.temperatures()}
            rgb = acq.preview_rgb(gain=4.0)
            if rgb is not None:
                try:
                    from PIL import Image
                    os.makedirs(a.data_dir, exist_ok=True)
                    pth = os.path.join(a.data_dir, "look.png"); Image.fromarray(rgb).save(pth); out["preview_png"] = pth
                except ImportError:
                    out["preview_png"] = "PIL not installed"
            print(json.dumps(out, indent=1)); return 0

        if a.cmd == "monitor":
            acq.start_stream(); _frame(acq)
            print(f"monitoring {a.seconds:.0f}s at exposure {a.exposure:.0f} us, {a.fps:.0f} fps (Ctrl-C to stop)", flush=True)
            t_end = time.time() + a.seconds
            while time.time() < t_end:
                d = acq.last_frame.data
                print(f"{time.strftime('%H:%M:%S')}  mean {d.mean():7.1f}  max {int(d.max()):5d}  p99 {int(np.percentile(d, 99)):5d}"
                      f"  sat {(d >= 4095).mean()*100:5.1f}%  center-col {d[:, d.shape[1]//2].mean():7.1f}", flush=True)
                time.sleep(a.interval)
            return 0

        if a.cmd == "shutter":
            acq.start_stream(); _frame(acq)
            def level():
                time.sleep(0.8); d = acq.last_frame.data; return d.mean(), int(d.max())
            for i in range(a.cycles):
                cam.shutter_close(); m, mx = level(); print(f"cycle {i+1}: CLOSED  mean {m:7.1f} max {mx}", flush=True)
                time.sleep(a.interval)
                cam.shutter_open();  m, mx = level(); print(f"cycle {i+1}: OPEN    mean {m:7.1f} max {mx}", flush=True)
                time.sleep(a.interval)
            print("shutter left OPEN"); return 0

        if a.cmd == "scan":
            acq.start_stream(); _frame(acq)
            acq.start_scan(name=a.name, max_seconds=a.seconds)
            s = _finished_scan(acq, a.seconds + FRAME_WAIT_S)
            data, hdr = read_bil(os.path.join(s.folder, "cube"))
            j = json.load(open(os.path.join(s.folder, "scan.json")))
            ids = np.array(j["line_frame_ids"]); ts = np.array(j["line_cam_timestamps_ns"]) / 1e9
            dt = np.diff(ts) * 1000 if len(ts) > 1 else np.array([0.0])
            out = {"folder": s.folder, "lines": s.lines, "duration_s": round(j["duration_s"], 3), "achieved_fps": round(j["achieved_fps"], 2),
                   "frame_id_gaps": int((np.diff(ids) != 1).sum()) if len(ids) > 1 else 0,
                   "cam_dt_ms": {"min": round(float(dt.min()), 3), "max": round(float(dt.max()), 3), "mean": round(float(dt.mean()), 3)},
                   "handler": j["handler_stats"], "stream": j["stream_stats"], "file_MB": round(os.path.getsize(os.path.join(s.folder, "cube.raw")) / 1e6, 1),
                   "frame0": stats(np.asarray(data[0])), "temperatures": cam.temperatures()}
            print(json.dumps(out, indent=1)); return 0
    except (NoFrames, TimeoutError) as e:      # TimeoutError: a reference did not get its frames
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        try:
            acq.stop_scan()
        finally:
            cam.close()


if __name__ == "__main__":
    sys.exit(main())
