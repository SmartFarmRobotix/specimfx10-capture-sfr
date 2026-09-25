"""Scan sessions and reference captures on top of FX10Camera. No motion control here: whatever moves the
camera posts its positions through the service API while a scan runs."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from . import __version__
from .camera import FX10Camera, Frame
from .envi import BilWriter, write_reference

log = logging.getLogger("specimFX10.acq")

_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def safe_name(name: str, max_len: int = 40) -> str:
    """A scan name is only ever a suffix of a folder name: letters, digits, '_' and '-'."""
    return _NAME_RE.sub("", str(name or ""))[:max_len]


@dataclass
class ScanInfo:
    record_id: str
    folder: str
    started: float
    stopped: float = 0.0
    lines: int = 0
    max_lines: int = 0
    max_seconds: float = 0.0
    state: str = "scanning"   # scanning | done | error
    error: str = ""


class Acquisition:
    """Owns the camera stream. Modes: idle | preview | scan | reference."""

    def __init__(self, cam: FX10Camera, data_dir: str, wavelengths: Optional[list] = None, preview_lines: int = 400,
                 fwhm: Optional[list] = None):
        self.cam = cam
        self.data_dir = data_dir
        self.wavelengths = wavelengths
        self.fwhm = fwhm
        self.lock = threading.Lock()                 # mode transitions, the writer, the scan record
        self._preview_lock = threading.Lock()        # the preview deque
        self.mode = "idle"
        self.scan: Optional[ScanInfo] = None
        self.positions: list = []                    # carrier positions posted during the running scan
        self._stop_requested = False
        self._writer: Optional[BilWriter] = None
        self._meta_lines: list = []
        self._ref_frames: list = []
        self._ref_target = 0
        self._ref_done = threading.Event()
        self.preview = deque(maxlen=preview_lines)   # recent frames for the preview strip
        self.last_frame: Optional[Frame] = None
        self.dark: Optional[np.ndarray] = None
        self.white: Optional[np.ndarray] = None

    # ---- frame sink ----
    def _on_frame(self, fr: Frame) -> None:
        self.last_frame = fr
        with self._preview_lock:
            self.preview.append(fr.data)
        mode = self.mode
        if mode == "scan":
            with self.lock:
                w, s = self._writer, self.scan
                if w is None or s is None or self.mode != "scan" or self._stop_requested:
                    return
                try:
                    w.append(fr.data)
                except Exception as e:                   # a bad frame ends the scan with an error, never silently
                    s.state, s.error = "error", str(e)
                    log.error("scan %s: %s", s.record_id, e)
                    threading.Thread(target=self.stop_scan, daemon=True).start()
                    return
                self._meta_lines.append((fr.frame_id, fr.cam_timestamp_ns, fr.sys_time))
                s.lines = w.lines
                if (s.max_lines and s.lines >= s.max_lines) or (s.max_seconds and fr.sys_time - s.started >= s.max_seconds):
                    self._stop_requested = True      # no further frame is appended; the stop thread closes the cube
                    threading.Thread(target=self.stop_scan, daemon=True).start()
        elif mode == "reference":
            if len(self._ref_frames) < self._ref_target:
                self._ref_frames.append(fr.data.astype(np.float32))
            if len(self._ref_frames) >= self._ref_target:
                self._ref_done.set()

    # ---- streaming ----
    def start_stream(self) -> None:
        with self.lock:
            if self.cam._acq_thread is None:
                self.cam.start_acquisition(self._on_frame)
            if self.mode == "idle":
                self.mode = "preview"

    def stop_stream(self) -> None:
        with self.lock:
            if self.mode in ("scan", "reference"):
                raise RuntimeError(f"{self.mode} in progress")
            self.cam.stop_acquisition()
            self.mode = "idle"

    def reset_after_reconfigure(self) -> None:
        """After the camera geometry or exposure changed: the preview frames and the references no longer apply."""
        with self._preview_lock:
            self.preview.clear()
        self.last_frame = None
        self.dark = None
        self.white = None

    # ---- references ----
    def _capture_reference(self, n: int, timeout_s: float) -> np.ndarray:
        with self.lock:
            if self.mode in ("scan", "reference"):
                raise RuntimeError(f"{self.mode} in progress")
            self._ref_frames, self._ref_target = [], n
            self._ref_done.clear()
            prev = self.mode
            self.mode = "reference"
        try:
            if not self._ref_done.wait(timeout_s):
                raise TimeoutError(f"got {len(self._ref_frames)}/{n} reference frames in {timeout_s}s")
            stack = np.stack(self._ref_frames)
            return stack.mean(axis=0), stack.std(axis=0)
        finally:
            with self.lock:
                self.mode = "preview" if prev != "idle" else prev
                self._ref_frames = []

    def capture_dark(self, n: int = 40, folder: Optional[str] = None) -> dict:
        """Close the camera's internal shutter, average n frames, reopen."""
        if n < 1:
            raise ValueError("frames must be at least 1")
        self.start_stream()
        self.cam.shutter_close()
        try:
            mean, std = self._capture_reference(n, timeout_s=n / max(self.cam.cfg.frame_rate, 1) + 5)
        finally:
            self.cam.shutter_open()
        self.dark = mean
        out = {"frames": n, "mean": float(mean.mean()), "std_mean": float(std.mean()), "max": float(mean.max())}
        if folder:
            write_reference(os.path.join(folder, "dark"), mean, wavelengths=self._wavelengths_for(mean.shape[0]),
                            description=f"dark reference, {n} frames averaged", extra={"reference stats": json.dumps(out)})
        return out

    def capture_white(self, n: int = 40, columns: Optional[tuple] = None, folder: Optional[str] = None) -> dict:
        """Average n frames over the white tile. columns=(c0,c1) marks where the tile is; the mean of that range is
        extended to the full line (tile shorter than the swath)."""
        if n < 1:
            raise ValueError("frames must be at least 1")
        self.start_stream()
        mean, std = self._capture_reference(n, timeout_s=n / max(self.cam.cfg.frame_rate, 1) + 5)
        full, region = mean, mean
        if columns:
            c0, c1 = int(columns[0]), int(columns[1])
            if not (0 <= c0 < c1 <= mean.shape[1]):
                raise ValueError(f"columns must satisfy 0 <= c0 < c1 <= {mean.shape[1]}, got {columns}")
            columns = (c0, c1)
            region = mean[:, c0:c1]                                   # statistics refer to the tile columns only
            profile = region.mean(axis=1, keepdims=True)
            full = np.repeat(profile, mean.shape[1], axis=1)
        self.white = full
        sat = float((region >= 4095).mean())
        out = {"frames": n, "columns": columns, "mean": float(region.mean()), "max": float(region.max()), "saturated_fraction": sat}
        if folder:
            write_reference(os.path.join(folder, "white"), full, wavelengths=self._wavelengths_for(full.shape[0]),
                            description=f"white reference, {n} frames averaged", extra={"reference stats": json.dumps(out)})
        return out

    # ---- scans ----
    def _wavelengths_for(self, bands: int) -> Optional[list]:
        """The configured wavelengths, only if they fit the current band count (a header must not lie)."""
        if self.wavelengths and len(self.wavelengths) == bands:
            return self.wavelengths
        if self.wavelengths:
            log.warning("wavelength file has %d entries, the camera delivers %d bands: header written without wavelengths",
                        len(self.wavelengths), bands)
        return None

    def add_position(self, t: float, x=None, y=None, z=None) -> int:
        with self.lock:
            if self.mode != "scan":
                raise RuntimeError("no scan in progress")
            self.positions.append({"t": float(t), "x": x, "y": y, "z": z})
            return len(self.positions)

    def start_scan(self, name: str = "", max_lines: int = 0, max_seconds: float = 0.0, meta: Optional[dict] = None) -> ScanInfo:
        with self.lock:
            if self.mode in ("scan", "reference"):
                raise RuntimeError(f"{self.mode} in progress")
            if self.cam._acq_thread is None:
                self.cam.start_acquisition(self._on_frame)
            ts = time.time()
            name = safe_name(name)
            rid = time.strftime("%Y-%m-%d/%H%M%S", time.localtime(ts)) + (f"_{name}" if name else "")
            folder = os.path.join(self.data_dir, rid)
            n = 1
            while os.path.exists(folder):                             # a second scan in the same second never overwrites
                n += 1
                folder = os.path.join(self.data_dir, f"{rid}-{n}")
            rid = os.path.relpath(folder, self.data_dir)
            os.makedirs(folder)
            info = self.cam.info()
            h, w = info["region"]["height"], info["region"]["width"]
            wls = self._wavelengths_for(h)
            fwhm = self.fwhm if (wls and self.fwhm and len(self.fwhm) == h) else None
            self._writer = BilWriter(os.path.join(folder, "cube"), samples=w, bands=h, wavelengths=wls,
                                     description=f"specimFX10 scan {rid}",
                                     extra={"acquisition time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
                                            "fps": info["frame_rate"], "exposure us": info["exposure_us"], "serial": info["serial"],
                                            **({"fwhm": "{" + ", ".join(f"{f:.3f}" for f in fwhm) + "}"} if fwhm else {})})
            self._meta_lines = []
            self.positions = []
            self._stop_requested = False
            self._scan_meta ={"camera": info, "config": asdict(self.cam.cfg), "software": {"specimFX10": __version__}, "user": meta or {}}
            self.scan = ScanInfo(record_id=rid, folder=folder, started=ts, max_lines=max_lines, max_seconds=max_seconds)
            if self.dark is not None:
                write_reference(os.path.join(folder, "dark"), self.dark, wavelengths=wls, description="dark reference (last captured)")
            if self.white is not None:
                write_reference(os.path.join(folder, "white"), self.white, wavelengths=wls, description="white reference (last captured)")
            self.mode = "scan"
            log.info("scan %s started", rid)
            return self.scan

    def stop_scan(self) -> Optional[ScanInfo]:
        with self.lock:
            if self.mode != "scan":
                return self.scan
            s = self.scan
            s.stopped = time.time()
            stats = dict(self.cam.stats)
            stream_stats = self.cam.stream_statistics()
            writer, self._writer = self._writer, None   # frames stop being appended before the cube is closed
            writer.close()
            final_state = "error" if s.state == "error" else "done"
            lines = self._meta_lines
            meta = dict(self._scan_meta)
            meta.update({
                "record_id": s.record_id, "started": s.started, "stopped": s.stopped, "lines": s.lines,
                "duration_s": s.stopped - s.started, "achieved_fps": s.lines / max(s.stopped - s.started, 1e-6),
                "handler_stats": stats, "stream_stats": stream_stats,
                "line_frame_ids": [l[0] for l in lines], "line_cam_timestamps_ns": [l[1] for l in lines],
                "line_sys_times": [round(l[2], 6) for l in lines],
                "positions": list(self.positions),   # carrier positions vs time, posted through the API
                "state": final_state, "error": s.error,
            })
            with open(os.path.join(s.folder, "scan.json"), "w") as f:
                json.dump(meta, f, indent=1)
            s.state = final_state      # only now: the cube, its header and the sidecar are complete on disk
            self.mode = "preview"
            log.info("scan %s stopped: %d lines in %.2fs", s.record_id, s.lines, s.stopped - s.started)
            return s

    # ---- preview ----
    def preview_rgb(self, bands: Optional[tuple] = None, gain: float = 1.0) -> Optional[np.ndarray]:
        """False-colour strip [lines, samples, 3] uint8 from the most recent frames (no per-pixel Python loops)."""
        with self._preview_lock:
            frames = list(self.preview)
        if not frames:
            return None
        stack = np.stack(frames)                             # [n, bands, samples]
        nb = stack.shape[1]
        if bands is None:
            bands = (179, 112, 45) if nb >= 180 else (int(nb * 5 / 6), int(nb * 3 / 6), int(nb * 1 / 6))   # Specim default bands (~880/700/520 nm)
        bands = tuple(int(b) for b in bands)
        if len(bands) != 3 or any(not 0 <= b < nb for b in bands):
            raise ValueError(f"preview bands must be three indices in [0, {nb})")
        rgb = stack[:, list(bands), :].transpose(0, 2, 1).astype(np.float32)   # [n, samples, 3]
        if self.dark is not None and self.dark.shape == stack.shape[1:]:
            rgb -= self.dark[list(bands), :].T[None]
        rgb *= (255.0 / 4095.0) * gain
        return np.clip(rgb, 0, 255).astype(np.uint8)
