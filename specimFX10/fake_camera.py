"""A camera stand-in with the FX10Camera interface, for development and tests without hardware or Aravis.

Frames are synthetic: bands x samples uint16 in the 12-bit range, a smooth spectrum times a spatial profile plus
noise; near-dark when the shutter is closed; a ramp across the samples when TestPattern is "Ramp". Frame rate and
region follow the configuration, so timing, the ENVI layout and the whole service API behave as with the camera.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from .camera import CameraConfig, Frame, ShutterState, F_SHUTTER_CLOSE, F_SHUTTER_OPEN, F_TEMPS

SENSOR_ROWS = 448          # spectral rows before vertical binning
SAMPLES = 1024             # spatial pixels per line
DARK_LEVEL = 40.0          # counts with the shutter closed
FULL_SCALE = 4095          # 12-bit


class FakeFX10Camera:
    def __init__(self, cfg: CameraConfig, seed: int = 0):
        self.cfg = cfg
        self.shutter = ShutterState.UNKNOWN
        self._open = False
        self._features: dict = {
            "TriggerMode": "Off", "AcquisitionMode": "Continuous", "ExposureMode": "Timed",
            "EnAcquisitionFrameRate": True, "BinningHorizontal": cfg.binning_horizontal,
            "BinningVertical": cfg.binning_vertical, "GevSCPD": cfg.packet_delay_ns, "GevSCPSPacketSize": 8228,
            "DeviceFirmwareVersion": "fake", "Correction_Mode": "Flat", "AberCorrection_Enable": True,
            "DigitalGain": 1.0, "LinLog_Mode": "Off", "TestPattern": "Off",
        }
        for f in F_TEMPS:
            self._features[f] = 35.0
        self._lock = threading.RLock()
        self._acq_thread: Optional[threading.Thread] = None
        self._acq_stop = threading.Event()
        self._rng = np.random.default_rng(seed)
        self._frame_id = 0
        self.stats = {"frames": 0, "incomplete": 0, "last_frame_id": -1, "missing_frames": 0,
                      "handle_time_ms_max": 0.0, "handle_time_ms_avg": 0.0}

    # ---------- connection ----------
    @staticmethod
    def discover() -> list[dict]:
        return [{"id": "Fake-FX10e-1", "model": "FX10e", "serial": "000000000000", "address": "127.0.0.1", "vendor": "Specim"}]

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self.stop_acquisition()
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    # ---------- features ----------
    def get(self, name: str):
        if name not in self._features:
            raise KeyError(name)
        return self._features[name]

    def set(self, name: str, value) -> None:
        if name == F_SHUTTER_CLOSE:
            self.shutter = ShutterState.CLOSED
            return
        if name == F_SHUTTER_OPEN:
            self.shutter = ShutterState.OPEN
            return
        self._features[name] = value

    def temperatures(self) -> dict:
        return {f.replace("Temperature_", "").lower(): round(float(self._features[f]), 2) for f in F_TEMPS}

    def _region(self) -> tuple:
        return 0, 0, SAMPLES // max(int(self._features["BinningHorizontal"]), 1), SENSOR_ROWS // max(int(self._features["BinningVertical"]), 1)

    def info(self) -> dict:
        x, y, w, h = self._region()
        return {
            "model": "FX10e (fake)", "serial": "000000000000", "firmware": self._features["DeviceFirmwareVersion"],
            "region": {"x": x, "y": y, "width": w, "height": h},
            "binning": {"h": self._features["BinningHorizontal"], "v": self._features["BinningVertical"]},
            "pixel_format": self.cfg.pixel_format,
            "frame_rate": float(self.cfg.frame_rate), "frame_rate_max": 163.0,
            "exposure_us": float(self.cfg.exposure_us),
            "packet_size": self._features["GevSCPSPacketSize"], "packet_delay": self._features["GevSCPD"],
            "corrections": {k: self._features[k] for k in ("Correction_Mode", "AberCorrection_Enable", "DigitalGain", "LinLog_Mode")},
            "temperatures": self.temperatures(), "shutter": self.shutter,
        }

    # ---------- configuration ----------
    def configure(self) -> dict:
        with self._lock:
            self.stop_acquisition()
            self._features["BinningHorizontal"] = self.cfg.binning_horizontal
            self._features["BinningVertical"] = self.cfg.binning_vertical
            self._features["GevSCPD"] = self.cfg.packet_delay_ns
            self.shutter_open()
            return self.info()

    # ---------- shutter ----------
    def shutter_close(self, settle_s: float = 0.0) -> None:
        self.set(F_SHUTTER_CLOSE, 204)

    def shutter_open(self, settle_s: float = 0.0) -> None:
        self.set(F_SHUTTER_OPEN, 200)

    # ---------- frames ----------
    def _frame(self, h: int, w: int) -> np.ndarray:
        if self._features.get("TestPattern") == "Ramp":
            ramp = np.linspace(0, FULL_SCALE, w, dtype=np.float32)
            return np.repeat(ramp[None, :], h, axis=0).astype(np.uint16)
        noise = self._rng.normal(0.0, 3.0, size=(h, w)).astype(np.float32)
        if self.shutter == ShutterState.CLOSED:
            return np.clip(DARK_LEVEL + noise, 0, FULL_SCALE).astype(np.uint16)
        spectrum = 0.3 + 0.7 * np.exp(-((np.arange(h) - h * 0.55) / (h * 0.35)) ** 2)      # one hump across the bands
        profile = 0.6 + 0.4 * np.cos(np.linspace(-1.2, 1.2, w))                              # optics brighter in the middle
        scale = min(float(self.cfg.exposure_us) / 2000.0, 1.0) * 0.6 * FULL_SCALE
        frame = DARK_LEVEL + scale * spectrum[:, None] * profile[None, :] + noise
        return np.clip(frame, 0, FULL_SCALE).astype(np.uint16)

    def start_acquisition(self, on_frame: Callable[[Frame], None]) -> None:
        with self._lock:
            if self._acq_thread is not None:
                raise RuntimeError("already acquiring")
            self.stats.update({"frames": 0, "incomplete": 0, "last_frame_id": -1, "missing_frames": 0,
                               "handle_time_ms_max": 0.0, "handle_time_ms_avg": 0.0})
            self._acq_stop.clear()
            self._acq_thread = threading.Thread(target=self._acq_loop, args=(on_frame,), name="fake-fx10-acq", daemon=True)
            self._acq_thread.start()

    def stop_acquisition(self) -> None:
        with self._lock:
            t = self._acq_thread
            if t is None:
                return
            self._acq_stop.set()
            t.join(timeout=5.0)
            self._acq_thread = None

    def _acq_loop(self, on_frame: Callable[[Frame], None]) -> None:
        period = 1.0 / max(float(self.cfg.frame_rate), 1.0)
        _, _, w, h = self._region()
        st = self.stats
        n, tsum = 0, 0.0
        next_t = time.perf_counter()
        while not self._acq_stop.is_set():
            next_t += period
            t0 = time.perf_counter()
            self._frame_id += 1
            fid = self._frame_id
            st["last_frame_id"] = fid
            try:
                on_frame(Frame(self._frame(h, w), fid, int(time.time() * 1e9), time.time()))
            except Exception:                       # like the real camera: a failing handler never kills the stream
                logging.getLogger("specimFX10.fake").exception("frame handler failed")
            dt = (time.perf_counter() - t0) * 1000.0
            n += 1; tsum += dt
            st["frames"] = n
            st["handle_time_ms_max"] = max(st["handle_time_ms_max"], dt)
            st["handle_time_ms_avg"] = tsum / n
            delay = next_t - time.perf_counter()
            if delay > 0:
                self._acq_stop.wait(delay)

    def stream_statistics(self) -> dict:
        if self._acq_thread is None:
            return {}
        return {"completed_buffers": self.stats["frames"], "failed_buffers": 0, "underruns": 0}
