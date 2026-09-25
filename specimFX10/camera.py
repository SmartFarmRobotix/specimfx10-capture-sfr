"""Thin wrapper around Aravis for the Specim FX10e.

Only this module talks to Aravis. Everything else sees numpy frames and plain Python values.
Design rule: Aravis does the GigE transport in C; Python only handles complete frames,
never single pixels.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable, Optional

import numpy as np

try:
    import gi
except ImportError:
    gi = None


def _register_aravis_prefix() -> Optional[str]:
    """Make a user-prefix Aravis build visible to PyGObject without environment variables.
    Looks at $ARAVIS_PREFIX, then ~/specimFX10/aravis-*/ (newest)."""
    import glob
    import os
    import re

    def _version_key(path: str) -> tuple:      # aravis-0.10.0 sorts after aravis-0.9.2, not before
        return tuple(int(x) for x in re.findall(r"\d+", os.path.basename(path)))

    cands = [os.environ["ARAVIS_PREFIX"]] if os.environ.get("ARAVIS_PREFIX") else []
    cands += sorted(glob.glob(os.path.expanduser("~/specimFX10/aravis-*")), key=_version_key, reverse=True)
    if gi is None:
        return None
    for prefix in cands:
        libdirs = glob.glob(os.path.join(prefix, "lib*", "girepository-1.0")) + \
                  glob.glob(os.path.join(prefix, "lib*", "*", "girepository-1.0"))
        if libdirs:
            gi.require_version("GIRepository", "2.0")
            from gi.repository import GIRepository
            GIRepository.Repository.prepend_search_path(libdirs[0])
            GIRepository.Repository.prepend_library_path(os.path.dirname(libdirs[0]))
            return prefix
    return None


ARAVIS_PREFIX = _register_aravis_prefix()
try:
    if gi is None:
        raise ImportError("PyGObject (gi) not installed")
    gi.require_version("Aravis", "0.10")
    from gi.repository import Aravis  # noqa: E402
except (ImportError, ValueError) as _e:   # no Aravis on this machine: config/ENVI code still importable, camera use fails at open()
    Aravis = None
    _ARAVIS_IMPORT_ERROR = _e

log = logging.getLogger("specimFX10.camera")

# FX10e GenICam feature names this module relies on (from the camera's own GenICam description, `arv-tool features`)
F_SHUTTER_CLOSE = "MotorShutter_PulseFwd"   # writing a pulse length (200..204) closes the shutter
F_SHUTTER_OPEN = "MotorShutter_PulseRev"    # writing a pulse length (200..204) opens the shutter
F_TEMPS = ("Temperature_Sensor", "Temperature_Proc", "Temperature_FPGA", "Temperature_Interface", "Temperature_Phy")


@dataclass
class CameraConfig:
    serial: str = ""                 # "" = first Specim camera found
    binning_vertical: int = 2        # 448 sensor rows -> 224 bands
    binning_horizontal: int = 1
    pixel_format: str = "Mono12"     # 12-bit in 16-bit little-endian words: no unpacking needed
    frame_rate: float = 50.0
    exposure_us: float = 2000.0
    packet_delay_ns: int = 0
    auto_packet_size: bool = True    # negotiate the largest packet size that gets through (MTU 9000 -> 8228)
    socket_buffer_mb: int = 16
    n_buffers: int = 64


@dataclass
class Frame:
    """One camera frame = one scan line: array[bands, spatial] uint16."""
    data: np.ndarray
    frame_id: int
    cam_timestamp_ns: int
    sys_time: float


class ShutterState:
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class FX10Camera:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.cam: Optional[Aravis.Camera] = None
        self.dev: Optional[Aravis.Device] = None
        self.stream: Optional[Aravis.Stream] = None
        self.shutter = ShutterState.UNKNOWN
        self._lock = threading.RLock()
        self._acq_thread: Optional[threading.Thread] = None
        self._acq_stop = threading.Event()
        self.stats = {"frames": 0, "incomplete": 0, "last_frame_id": -1, "missing_frames": 0,
                      "handle_time_ms_max": 0.0, "handle_time_ms_avg": 0.0}

    # ---------- connection ----------
    @staticmethod
    def discover() -> list[dict]:
        Aravis.update_device_list()
        out = []
        for i in range(Aravis.get_n_devices()):
            out.append({"id": Aravis.get_device_id(i), "model": Aravis.get_device_model(i),
                        "serial": Aravis.get_device_serial_nbr(i), "address": Aravis.get_device_address(i),
                        "vendor": Aravis.get_device_vendor(i)})
        return out

    def open(self) -> None:
        if Aravis is None:
            raise RuntimeError(f"Aravis bindings not available on this machine: {_ARAVIS_IMPORT_ERROR}")
        with self._lock:
            devices = self.discover()
            wanted = [d for d in devices if d["vendor"] == "Specim" and (not self.cfg.serial or d["serial"] == self.cfg.serial)]
            if not wanted:
                raise RuntimeError(f"No Specim camera found (serial={self.cfg.serial!r}); seen: {devices}")
            self.cam = Aravis.Camera.new(wanted[0]["id"])
            self.dev = self.cam.get_device()
            log.info("opened %s (%s) at %s", wanted[0]["id"], self.cam.get_device_serial_number(), wanted[0]["address"])

    def close(self) -> None:
        with self._lock:
            self.stop_acquisition()
            self.stream = None
            self.dev = None
            self.cam = None

    @property
    def is_open(self) -> bool:
        return self.cam is not None

    # ---------- features ----------
    def get(self, name: str):
        node = self.dev.get_feature(name)
        if node is None:
            raise KeyError(name)
        t = type(node).__name__
        if isinstance(node, Aravis.GcEnumeration):
            return node.get_string_value()
        if isinstance(node, Aravis.GcBoolean):
            return node.get_value()
        if isinstance(node, Aravis.GcFloat):
            return node.get_value()
        if isinstance(node, Aravis.GcInteger):
            return node.get_value()
        if isinstance(node, Aravis.GcString):
            return node.get_value()
        raise TypeError(f"{name}: unsupported node type {t}")

    def set(self, name: str, value) -> None:
        node = self.dev.get_feature(name)
        if node is None:
            raise KeyError(name)
        if isinstance(node, Aravis.GcEnumeration):
            node.set_string_value(str(value))
        elif isinstance(node, Aravis.GcBoolean):
            node.set_value(bool(value))
        elif isinstance(node, Aravis.GcFloat):
            node.set_value(float(value))
        elif isinstance(node, Aravis.GcInteger):
            node.set_value(int(value))
        elif isinstance(node, Aravis.GcString):
            node.set_value(str(value))
        elif isinstance(node, Aravis.GcCommand):
            node.execute()
        else:
            raise TypeError(name)

    def temperatures(self) -> dict:
        out = {}
        for f in F_TEMPS:
            try:
                out[f.replace("Temperature_", "").lower()] = round(float(self.get(f)), 2)
            except Exception:
                pass
        return out

    def info(self) -> dict:
        c = self.cam
        x, y, w, h = c.get_region()
        return {
            "model": c.get_model_name(), "serial": c.get_device_serial_number(),
            "firmware": self.get("DeviceFirmwareVersion"),
            "region": {"x": x, "y": y, "width": w, "height": h},
            "binning": {"h": self.get("BinningHorizontal"), "v": self.get("BinningVertical")},
            "pixel_format": c.get_pixel_format_as_string(),
            "frame_rate": c.get_frame_rate(), "frame_rate_max": c.get_frame_rate_bounds()[1],
            "exposure_us": c.get_exposure_time(),
            "packet_size": self.get("GevSCPSPacketSize"), "packet_delay": self.get("GevSCPD"),
            "corrections": {"Correction_Mode": self.get("Correction_Mode"), "AberCorrection_Enable": self.get("AberCorrection_Enable"),
                            "DigitalGain": self.get("DigitalGain"), "LinLog_Mode": self.get("LinLog_Mode")},
            "temperatures": self.temperatures(), "shutter": self.shutter,
        }

    # ---------- configuration ----------
    def configure(self) -> dict:
        """Apply CameraConfig. Returns the resulting info()."""
        c, cfg = self.cam, self.cfg
        with self._lock:
            self.stop_acquisition()
            self.set("TriggerMode", "Off")
            self.set("AcquisitionMode", "Continuous")
            self.set("ExposureMode", "Timed")
            self.set("EnAcquisitionFrameRate", True)
            self.set("BinningHorizontal", cfg.binning_horizontal)
            self.set("BinningVertical", cfg.binning_vertical)
            c.set_pixel_format_from_string(cfg.pixel_format)
            c.set_exposure_time(cfg.exposure_us)
            c.set_frame_rate(cfg.frame_rate)
            self.set("GevSCPD", cfg.packet_delay_ns)
            if cfg.auto_packet_size:
                try:
                    c.gv_auto_packet_size()
                except Exception as e:  # falls back to whatever the camera has
                    log.warning("auto packet size failed: %s", e)
            # the shutter blade position is unknown after power-up (it can come up closed): always start open
            self.shutter_open()
            return self.info()

    # ---------- shutter ----------
    def shutter_close(self, settle_s: float = 0.5) -> None:
        self.set(F_SHUTTER_CLOSE, 204)
        time.sleep(settle_s)
        self.shutter = ShutterState.CLOSED

    def shutter_open(self, settle_s: float = 0.5) -> None:
        self.set(F_SHUTTER_OPEN, 200)
        time.sleep(settle_s)
        self.shutter = ShutterState.OPEN

    # ---------- acquisition ----------
    def start_acquisition(self, on_frame: Callable[[Frame], None]) -> None:
        """Start streaming; on_frame is called from the acquisition thread for every complete frame."""
        with self._lock:
            if self._acq_thread is not None:
                raise RuntimeError("already acquiring")
            c = self.cam
            self.stream = c.create_stream(None, None)
            if self.stream is None:
                raise RuntimeError("could not create stream (firewall / interface?)")
            try:
                self.stream.set_property("socket-buffer", Aravis.GvStreamSocketBuffer.FIXED)
                self.stream.set_property("socket-buffer-size", self.cfg.socket_buffer_mb * 1024 * 1024)
                self.stream.set_property("packet-resend", Aravis.GvStreamPacketResend.ALWAYS)
            except Exception as e:
                log.warning("stream property not set: %s", e)
            payload = c.get_payload()
            for _ in range(self.cfg.n_buffers):
                self.stream.push_buffer(Aravis.Buffer.new_allocate(payload))
            _, _, self._w, self._h = c.get_region()
            self.stats.update({"frames": 0, "incomplete": 0, "last_frame_id": -1, "missing_frames": 0,
                               "handle_time_ms_max": 0.0, "handle_time_ms_avg": 0.0})
            self._acq_stop.clear()
            self._acq_thread = threading.Thread(target=self._acq_loop, args=(on_frame,), name="fx10-acq", daemon=True)
            c.start_acquisition()
            self._acq_thread.start()

    def stop_acquisition(self) -> None:
        with self._lock:
            t = self._acq_thread
            if t is None:
                return
            self._acq_stop.set()
            t.join(timeout=5.0)
            self._acq_thread = None
            try:
                self.cam.stop_acquisition()
            except Exception as e:
                log.warning("stop_acquisition: %s", e)
            self.stream = None

    def _acq_loop(self, on_frame: Callable[[Frame], None]) -> None:
        stream, st = self.stream, self.stats
        h, w = self._h, self._w
        n, tsum = 0, 0.0
        while not self._acq_stop.is_set():
            buf = stream.timeout_pop_buffer(200_000)  # 200 ms
            if buf is None:
                continue
            try:
                if buf.get_status() != Aravis.BufferStatus.SUCCESS:
                    st["incomplete"] += 1
                    continue
                t0 = time.perf_counter()
                arr = np.frombuffer(buf.get_data(), dtype="<u2", count=h * w).reshape(h, w)
                fid = buf.get_frame_id()
                if st["last_frame_id"] >= 0 and fid > st["last_frame_id"] + 1:
                    st["missing_frames"] += fid - st["last_frame_id"] - 1
                st["last_frame_id"] = fid
                on_frame(Frame(arr, fid, buf.get_timestamp(), time.time()))
                dt = (time.perf_counter() - t0) * 1000.0
                n += 1; tsum += dt
                st["frames"] = n
                if dt > st["handle_time_ms_max"]:
                    st["handle_time_ms_max"] = dt
                st["handle_time_ms_avg"] = tsum / n
            except Exception:
                log.exception("frame handler failed")
            finally:
                stream.push_buffer(buf)

    def stream_statistics(self) -> dict:
        if self.stream is None:
            return {}
        try:
            st = self.stream.get_statistics()          # 0.8: (completed, failures, underruns); 0.9: may return fewer
            out = dict(zip(("completed_buffers", "failed_buffers", "underruns"), st))
            for k in ("n_received_packets", "n_missing_packets", "n_resend_requests", "n_resent_packets", "n_ignored_packets"):
                try:
                    out[k] = self.stream.get_info_int64_by_name(k)
                except Exception:
                    pass
            return out
        except Exception as e:
            return {"error": str(e)}
