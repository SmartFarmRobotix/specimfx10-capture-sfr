"""Flask service for the Specim FX10e.

  GET  /status                          -> watchdog: {"status": "INSETUP"|"ACTIVE"|"INACTIVE"|"SCANNING"}
  GET  /www/status_server               -> {"service": "specimFX10", ...}
  GET  /www/restart_server              -> finalise a running scan, then SIGINT self (the watchdog restarts)
  GET  /api/specimFX10/camera           -> camera info, temperatures, shutter, stream state, handler stats
  GET  /api/specimFX10/config           -> settings
  POST /api/specimFX10/config           -> partial update; camera keys re-applied to the camera when idle
  POST /api/specimFX10/stream/start|stop
  POST /api/specimFX10/reference/dark   {frames}
  POST /api/specimFX10/reference/white  {frames, columns:[c0,c1]}
  POST /api/specimFX10/scan/start       {name, max_lines, max_seconds, meta:{}}
  GET  /api/specimFX10/scan/status
  POST /api/specimFX10/scan/stop
  POST /api/specimFX10/scan/position    {t, x, y, z}  -> carrier positions appended to the running scan's sidecar
  GET  /api/specimFX10/records          -> list of scan folders
  GET  /api/specimFX10/records/<date>/<id> -> scan.json
  GET  /api/specimFX10/preview.png      ?gain=  -> false-colour strip of the latest lines
  POST /api/specimFX10/shutter          {"state": "open"|"closed"}

The service has no authentication and answers on every interface by default: run it on a private link, or bind it
to the loopback address and put a proxy in front (README, "Security").
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import threading
import time

from flask import Flask, jsonify, request, send_file, abort
from werkzeug.exceptions import HTTPException
from flask_cors import CORS

from . import __version__
from .acquisition import Acquisition
from .camera import FX10Camera
from .settings import ServiceSettings

log = logging.getLogger("specimFX10.server")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# camera keys that change the transport only, not the frames: a change to them keeps the references valid
_TRANSPORT_KEYS = {"camera.serial", "camera.packet_delay_ns", "camera.auto_packet_size", "camera.socket_buffer_mb", "camera.n_buffers"}


def _int(body: dict, key: str, default: int, minimum: int = 0) -> int:
    raw = body.get(key, default)
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        abort(400, f"{key} must be an integer")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        abort(400, f"{key} must be an integer")
    if v < minimum:
        abort(400, f"{key} must be at least {minimum}")
    return v


def _float(body: dict, key: str, default: float, minimum: float = 0.0) -> float:
    try:
        v = float(body.get(key, default))
    except (TypeError, ValueError):
        abort(400, f"{key} must be a number")
    if v < minimum:
        abort(400, f"{key} must be at least {minimum}")
    return v


class Server:
    def __init__(self, settings: ServiceSettings, camera=None):
        """camera: an object with the FX10Camera interface; None = the real Aravis-backed camera.
        specimFX10.fake_camera.FakeFX10Camera serves for development and tests without hardware."""
        self.settings = settings
        self.app = Flask("specimFX10")

        @self.app.errorhandler(HTTPException)
        def _json_error(e):          # the API answers its errors as JSON too, not as Flask's HTML pages
            return jsonify({"error": e.description, "status": e.code}), e.code
        CORS(self.app)
        self.cam = camera if camera is not None else FX10Camera(settings.camera)
        self.acq: Acquisition | None = None
        self.state = "INSETUP"
        self.lock = threading.Lock()          # serialises long actions (references, config apply)
        self._routes()

    # ---------- lifecycle ----------
    def connect(self) -> None:
        self.cam.open()
        info = self.cam.configure()
        wls = self.settings.load_wls() or (None, None)
        self.acq = Acquisition(self.cam, self.settings.data_dir, wavelengths=wls[0], fwhm=wls[1],
                               preview_lines=self.settings.preview_lines)
        if wls[0] is None:
            log.warning("no wavelength file at %s: ENVI headers will lack wavelengths", self.settings.wavelengths_file)
        elif len(wls[0]) != info["region"]["height"]:
            log.warning("wavelength file has %d entries but the camera delivers %d bands: headers will lack wavelengths",
                        len(wls[0]), info["region"]["height"])
        self.state = "ACTIVE"
        log.info("camera connected: %s", info["serial"])

    def shutdown(self) -> None:
        """Finalise a running scan (cube, header, sidecar), then release the camera."""
        if self.acq is not None:
            try:
                self.acq.stop_scan()
            except Exception:
                log.exception("could not finalise the running scan")
        self.cam.close()

    def run(self) -> None:
        self.app.run(host=self.settings.server_address, port=self.settings.server_port, debug=False, threaded=True)

    # ---------- helpers ----------
    def _status(self) -> str:
        if self.acq and self.acq.mode == "scan":
            return "SCANNING"
        return self.state

    def _require_cam(self):
        if not self.cam.is_open or self.acq is None:
            abort(503, "camera not connected")

    def _body(self) -> dict:
        if not request.data or not request.data.strip():
            return {}                                   # an empty body is fine: every field has a default
        b = request.get_json(silent=True)
        if b is None:
            abort(400, "body is not valid JSON")
        if not isinstance(b, dict):
            abort(400, "body must be a JSON object")
        return b

    # ---------- routes ----------
    def _routes(self):
        app, P = self.app, "/api/specimFX10"

        @app.route("/status")
        def status():
            return jsonify({"status": self._status()})

        @app.route("/www/status_server")
        def www_status():
            return jsonify({"service": "specimFX10", "status": "OK" if self.state == "ACTIVE" else self.state,
                            "timestamp": time.time(), "version": __version__})

        @app.route("/www/restart_server")
        def restart():
            def kill():
                time.sleep(0.5)
                self.shutdown()
                os.kill(os.getpid(), signal.SIGINT)
            threading.Thread(target=kill, daemon=True).start()
            return jsonify({"service": "specimFX10", "status": "Restarting", "timestamp": time.time()})

        @app.route(P + "/camera")
        def camera():
            self._require_cam()
            info = self.cam.info()
            info.update({"stream": self.acq.mode, "handler": self.cam.stats, "stream_stats": self.cam.stream_statistics(),
                         "references": {"dark": self.acq.dark is not None, "white": self.acq.white is not None}})
            return jsonify(info)

        @app.route(P + "/config", methods=["GET"])
        def config_get():
            return jsonify(self.settings.as_dict())

        @app.route(P + "/config", methods=["POST"])
        def config_set():
            data = self._body()
            with self.lock:
                if self.acq and self.acq.mode in ("scan", "reference"):
                    abort(409, f"{self.acq.mode} in progress")
                previous = self.settings.as_dict()
                changed, ignored = self.settings.update(data)
                applied = False
                if any(k.startswith("camera.") for k in changed) and self.cam.is_open and self.acq is not None:
                    was_streaming = self.acq.mode != "idle"
                    if was_streaming:
                        self.acq.stop_stream()
                    try:
                        self.cam.configure()
                    except Exception as e:
                        self.settings.update(previous)              # the camera refused: keep the settings that worked
                        try:
                            self.cam.configure()
                        finally:
                            if was_streaming:
                                self.acq.start_stream()
                        abort(409, f"camera refused the new configuration: {e}")
                    # any change to what the camera delivers makes the preview and the references stale
                    if any(k.startswith("camera.") and k not in _TRANSPORT_KEYS for k in changed):
                        self.acq.reset_after_reconfigure()
                    if was_streaming:
                        self.acq.start_stream()
                    applied = True
                self.settings.save()
                if "wavelengths_file" in changed and self.acq:
                    wls = self.settings.load_wls() or (None, None)
                    self.acq.wavelengths, self.acq.fwhm = wls
                restart_needed = [k for k in changed if k in ("server_address", "server_port", "data_dir", "preview_lines", "debug", "camera.serial")]
            return jsonify({"changed": changed, "ignored": ignored, "camera_reconfigured": applied,
                            "restart_needed_for": restart_needed, "config": self.settings.as_dict()})

        @app.route(P + "/stream/start", methods=["POST"])
        def stream_start():
            self._require_cam(); self.acq.start_stream(); return jsonify({"stream": self.acq.mode})

        @app.route(P + "/stream/stop", methods=["POST"])
        def stream_stop():
            self._require_cam()
            try:
                self.acq.stop_stream()
            except RuntimeError as e:
                abort(409, str(e))
            return jsonify({"stream": self.acq.mode})

        @app.route(P + "/shutter", methods=["POST"])
        def shutter():
            self._require_cam()
            if self.acq.mode in ("scan", "reference"):
                abort(409, f"{self.acq.mode} in progress")
            st = self._body().get("state")
            if st == "open":
                self.cam.shutter_open()
            elif st == "closed":
                self.cam.shutter_close()
            else:
                abort(400, "state must be open|closed")
            return jsonify({"shutter": self.cam.shutter})

        @app.route(P + "/reference/dark", methods=["POST"])
        def ref_dark():
            self._require_cam()
            n = _int(self._body(), "frames", self.settings.reference_frames, minimum=1)
            with self.lock:
                try:
                    r = self.acq.capture_dark(n=n, folder=os.path.join(self.settings.data_dir, "refs"))
                except ValueError as e:
                    abort(400, str(e))
                except (RuntimeError, TimeoutError) as e:
                    abort(409, str(e))
            return jsonify(r)

        @app.route(P + "/reference/white", methods=["POST"])
        def ref_white():
            self._require_cam()
            body = self._body()
            n = _int(body, "frames", self.settings.reference_frames, minimum=1)
            cols = body.get("columns", self.settings.white_columns)
            if cols is not None and (not isinstance(cols, (list, tuple)) or len(cols) != 2):
                abort(400, "columns must be [c0, c1]")
            with self.lock:
                try:
                    r = self.acq.capture_white(n=n, columns=tuple(cols) if cols else None,
                                               folder=os.path.join(self.settings.data_dir, "refs"))
                except ValueError as e:
                    abort(400, str(e))
                except (RuntimeError, TimeoutError) as e:
                    abort(409, str(e))
            return jsonify(r)

        @app.route(P + "/scan/start", methods=["POST"])
        def scan_start():
            self._require_cam()
            b = self._body()
            meta = b.get("meta")
            if meta is not None and not isinstance(meta, dict):
                abort(400, "meta must be an object")
            try:
                self.acq.start_scan(name=str(b.get("name", "")), max_lines=_int(b, "max_lines", 0),
                                    max_seconds=_float(b, "max_seconds", 0.0), meta=meta)
            except RuntimeError as e:
                abort(409, str(e))
            return jsonify(self._scan_status())

        @app.route(P + "/scan/status")
        def scan_status():
            self._require_cam(); return jsonify(self._scan_status())

        @app.route(P + "/scan/stop", methods=["POST"])
        def scan_stop():
            self._require_cam()
            self.acq.stop_scan()
            return jsonify(self._scan_status())

        @app.route(P + "/scan/position", methods=["POST"])
        def scan_position():
            self._require_cam()
            b = self._body()
            try:
                n = self.acq.add_position(_float(b, "t", time.time()), x=b.get("x"), y=b.get("y"), z=b.get("z"))
            except RuntimeError as e:
                abort(409, str(e))
            return jsonify({"positions": n})

        @app.route(P + "/records")
        def records():
            out = []
            root = self.settings.data_dir
            for date in sorted(os.listdir(root)) if os.path.isdir(root) else []:
                d = os.path.join(root, date)
                if not os.path.isdir(d) or date == "refs":
                    continue
                for rid in sorted(os.listdir(d)):
                    p = os.path.join(d, rid)
                    if os.path.exists(os.path.join(p, "cube.hdr")):
                        out.append({"record_id": f"{date}/{rid}", "folder": p,
                                    "size_MB": round(os.path.getsize(os.path.join(p, "cube.raw")) / 1e6, 1)})
            return jsonify(out)

        @app.route(P + "/records/<date>/<rid>")
        def record(date, rid):
            if not _DATE_RE.match(date) or not _RID_RE.match(rid):
                abort(404)
            root = os.path.realpath(self.settings.data_dir)
            p = os.path.realpath(os.path.join(root, date, rid, "scan.json"))
            if not p.startswith(root + os.sep) or not os.path.exists(p):
                abort(404)
            return send_file(p, mimetype="application/json")

        @app.route(P + "/preview.png")
        def preview():
            self._require_cam()
            try:
                gain = float(request.args.get("gain", 1.0))
            except ValueError:
                abort(400, "gain must be a number")
            bands = self.settings.preview_bands
            try:
                rgb = self.acq.preview_rgb(bands=tuple(bands) if bands else None, gain=gain)
            except ValueError as e:
                abort(400, str(e))
            if rgb is None:
                abort(404, "no frames yet")
            from PIL import Image
            buf = io.BytesIO(); Image.fromarray(rgb).save(buf, format="PNG"); buf.seek(0)
            return send_file(buf, mimetype="image/png")

    def _scan_status(self) -> dict:
        s = self.acq.scan
        d = {"state": self._status(), "stream": self.acq.mode, "handler": self.cam.stats}
        if s is not None:
            d["scan"] = {"record_id": s.record_id, "folder": s.folder, "lines": s.lines, "state": s.state, "error": s.error,
                         "positions": len(self.acq.positions),
                         "started": s.started, "stopped": s.stopped, "elapsed_s": round((s.stopped or time.time()) - s.started, 3)}
        return d
