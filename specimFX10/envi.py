"""ENVI BIL writer for FX10 scan lines.

Layout: one camera frame (bands x samples, uint16) == one BIL line record, so frames are appended verbatim.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np


def write_header(path_hdr: str, samples: int, lines: int, bands: int, *, wavelengths: Optional[list] = None,
                 description: str = "", extra: Optional[dict] = None) -> None:
    lines_out = [
        "ENVI",
        f"description = {{{description}}}",
        f"samples = {samples}",
        f"lines = {lines}",
        f"bands = {bands}",
        "header offset = 0",
        "file type = ENVI Standard",
        "data type = 12",          # uint16
        "interleave = bil",
        "byte order = 0",          # little endian
        "sensor type = Specim FX10e",
    ]
    if wavelengths:
        lines_out.append("wavelength units = Nanometers")
        lines_out.append("wavelength = {" + ", ".join(f"{w:.3f}" for w in wavelengths) + "}")
    for k, v in (extra or {}).items():
        lines_out.append(f"{k} = {v}")
    with open(path_hdr, "w") as f:
        f.write("\n".join(lines_out) + "\n")


class BilWriter:
    """Appends frames to <base>.raw and finalises <base>.hdr on close."""

    def __init__(self, base: str, samples: int, bands: int, *, wavelengths=None, description="", extra=None):
        self.base, self.samples, self.bands = base, samples, bands
        self.wavelengths, self.description, self.extra = wavelengths, description, dict(extra or {})
        self.lines = 0
        os.makedirs(os.path.dirname(base) or ".", exist_ok=True)
        self._f = open(base + ".raw", "wb", buffering=4 * 1024 * 1024)

    def append(self, frame: np.ndarray) -> None:
        if frame.shape != (self.bands, self.samples) or frame.dtype != np.uint16:
            raise ValueError(f"frame {frame.shape} {frame.dtype} != ({self.bands}, {self.samples}) uint16")
        self._f.write(frame.tobytes(order="C"))
        self.lines += 1

    def close(self, extra: Optional[dict] = None) -> None:
        if self._f is None:
            return
        self._f.close(); self._f = None
        if extra:
            self.extra.update(extra)
        write_header(self.base + ".hdr", self.samples, self.lines, self.bands, wavelengths=self.wavelengths,
                     description=self.description, extra=self.extra)


def write_reference(base: str, mean: np.ndarray, *, wavelengths=None, description="", extra=None) -> None:
    """Write an averaged reference (bands x samples) as a 1-line BIL file, float32 -> stored as uint16 rounded."""
    arr = np.clip(np.rint(mean), 0, 65535).astype("<u2")
    os.makedirs(os.path.dirname(base) or ".", exist_ok=True)
    with open(base + ".raw", "wb") as f:
        f.write(arr.tobytes(order="C"))
    write_header(base + ".hdr", arr.shape[1], 1, arr.shape[0], wavelengths=wavelengths, description=description, extra=extra)


def read_bil(base: str) -> tuple[np.ndarray, dict]:
    """Read <base>.hdr/.raw -> array[lines, bands, samples] (memmap) and the parsed header."""
    hdr = {}
    with open(base + ".hdr") as f:
        txt = f.read()
    import re
    for m in re.finditer(r"^\s*([\w ]+?)\s*=\s*(\{.*?\}|.*?)\s*$", txt, re.S | re.M):
        hdr[m.group(1).strip()] = m.group(2).strip()
    samples, lines, bands = int(hdr["samples"]), int(hdr["lines"]), int(hdr["bands"])
    data = np.memmap(base + ".raw", dtype="<u2", mode="r", shape=(lines, bands, samples))
    return data, hdr
