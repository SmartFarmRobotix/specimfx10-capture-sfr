"""JSON-backed settings for the specimFX10 service."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict, fields
from typing import Optional

from .camera import CameraConfig

DEFAULT_CONFIG_PATH = os.path.expanduser("~/specimFX10/data/config.json")


@dataclass
class ServiceSettings:
    server_address: str = "0.0.0.0"       # every interface; "127.0.0.1" keeps the service local (README, "Security")
    server_port: int = 23000
    data_dir: str = os.path.expanduser("~/specimFX10/data")
    wavelengths_file: str = os.path.expanduser("~/specimFX10/wavelengths.wls")   # your unit's wavelength file (see README)
    white_columns: Optional[list] = None  # [c0, c1] where the white tile is in the line; set at the working distance
    reference_frames: int = 40
    preview_lines: int = 200              # frames kept for the preview strip; each full-region frame is 0.46 MB in memory
    preview_bands: Optional[list] = None  # [r, g, b] band indices; None = Specim default {179, 112, 45} (their radiometric headers)
    debug: bool = False
    camera: CameraConfig = field(default_factory=CameraConfig)

    _FIELDS = None      # filled lazily: the settable field names, never arbitrary attributes

    @classmethod
    def _settable(cls) -> tuple:
        if cls._FIELDS is None:
            cls._FIELDS = (tuple(f.name for f in fields(cls) if f.name != "camera"),
                           tuple(f.name for f in fields(CameraConfig)))
        return cls._FIELDS

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "ServiceSettings":
        s = cls()
        own, cam_fields = cls._settable()
        if os.path.exists(path):
            with open(path) as f:
                raw = json.load(f)
            cam = raw.pop("camera", {}) or {}
            for k, v in raw.items():
                if k.startswith("#"):
                    continue
                if k == "wavelengths_file" and not v:
                    continue                      # empty = keep the default path
                if k in own:
                    setattr(s, k, v)
            for k, v in cam.items():
                if k in cam_fields:
                    setattr(s.camera, k, v)
        s.path = path
        return s

    def save(self, path: Optional[str] = None) -> None:
        path = path or getattr(self, "path", DEFAULT_CONFIG_PATH)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.as_dict(), f, indent=2)

    def as_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "camera"}
        d["camera"] = asdict(self.camera)
        return d

    def update(self, data: dict) -> tuple:
        """Apply a partial update. Returns (changed keys, ignored keys); only declared fields are ever set.
        Camera keys go under 'camera'."""
        own, cam_fields = self._settable()
        changed, ignored = [], []
        cam = data.get("camera", {}) or {}
        if not isinstance(cam, dict):
            cam, ignored = {}, ["camera"]
        for k, v in data.items():
            if k == "camera":
                continue
            if k not in own:
                ignored.append(k); continue
            if k == "wavelengths_file" and not v:
                ignored.append(k); continue         # empty = keep the current path, as load() does
            if getattr(self, k) != v:
                setattr(self, k, v); changed.append(k)
        for k, v in cam.items():
            if k not in cam_fields:
                ignored.append("camera." + k); continue
            if getattr(self.camera, k) != v:
                setattr(self.camera, k, v); changed.append("camera." + k)
        return changed, ignored

    def load_wavelengths(self) -> Optional[list]:
        """Band centres [nm] from a wavelength file (two columns per line: centre, FWHM) or one number per line."""
        return (self.load_wls() or (None, None))[0]

    def load_wls(self) -> Optional[tuple]:
        """(centres, fwhm) from the configured wavelength file, or None."""
        p = self.wavelengths_file
        if not p or not os.path.exists(p):
            return None
        centres, fwhm = [], []
        for line in open(p):
            parts = line.split()
            if not parts:
                continue
            try:
                centres.append(float(parts[0])); fwhm.append(float(parts[1]) if len(parts) > 1 else 0.0)
            except ValueError:
                continue
        return (centres, fwhm) if centres else None
