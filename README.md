# specimfx10-capture

Capture service for the **Specim FX10e** push-broom hyperspectral camera on Linux. It drives the camera over GigE Vision
through [Aravis](https://github.com/AravisProject/aravis), takes dark and white references, records scans as ENVI BIL
cubes with a JSON sidecar, serves a false-colour preview, and exposes all of it over a small HTTP API. A watchdog keeps
it running. Apache-2.0.

What it is not: a motion controller. A push-broom camera images one line at a time and only forms an image while
something moves it across the scene. This service takes the frames and records the positions you post; the carrier
(rail, slider, gantry, vehicle) is yours. `examples/consumer.py` shows the sequence with a stand-in carrier.

If push-broom imaging is new to you, read [docs/push-broom.md](docs/push-broom.md) first.

## Requirements

- Linux, Python 3.10+, a GigE port on the same link as the camera (jumbo frames recommended).
- **Aravis with the `Aravis-0.10` introspection API** (the 0.9.x development releases and the 0.10 line; the code binds this typelib), built with GObject introspection, and PyGObject (`python3-gi`). Distribution packages of
  Aravis are often too old for the FX10e's stream settings; build from source into a user prefix. Build
  dependencies on Debian/Ubuntu: `build-essential pkg-config libglib2.0-dev libxml2-dev gobject-introspection
  libgirepository1.0-dev python3-gi` (the names differ on other distributions; Aravis's own README lists them).

  ```sh
  git clone https://github.com/AravisProject/aravis.git && cd aravis && git checkout 0.9.2
  pip install meson ninja
  meson setup build --prefix="$HOME/specimFX10/aravis-0.9.2" --buildtype=release \
      -Dviewer=disabled -Dusb=disabled -Dv4l2=disabled -Dtests=false -Ddocumentation=disabled \
      -Dintrospection=enabled -Dgst-plugin=disabled -Dgentl-producer=false -Dpacket-socket=enabled
  ninja -C build && ninja -C build install
  ```

  The service finds a build under `~/specimFX10/aravis-*` by itself (the newest version), or set `ARAVIS_PREFIX`.
- Addressing: the host's GigE interface needs an address in the camera's subnet (the camera's address is set with
  the vendor's tool, or both sides use link-local addresses); `arv-tool-0.10` from the Aravis build lists the cameras
  it can reach, and `python -m specimFX10.capture_cli info` prints the camera's identity through this package before
  the service is started.
- Kernel and link settings for a sustained 50 fps and more (as root, non-persistent; put them in `sysctl.d` and the
  interface configuration to keep them):

  ```sh
  sysctl -w net.core.rmem_max=33554432 net.core.wmem_max=33554432 net.core.netdev_max_backlog=5000
  ip link set <interface> mtu 9000
  ```

## Install and run

```sh
python3 -m venv --system-site-packages .venv     # exposes the system python3-gi to the venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m specimFX10.main --server     # config: ~/specimFX10/data/config.json (written on first run)
```

No camera at hand: `--fake` starts the service on a built-in fake camera with synthetic frames, and the whole API,
the references, the scans and the preview work. `--no-camera` starts it without connecting.

The check CLI runs against the camera without the service: `python -m specimFX10.capture_cli info|testpattern|dark|
scan|look|monitor|shutter [--fake]`. `testpattern` records the camera's ramp pattern and verifies the pixel layout end
to end; `look` prints statistics and writes a preview; `monitor` prints the light level once per second.

The watchdog (`python -m specimFX10.program_monitor --command "<python> -m specimFX10.main --server" --port 23000`)
restarts the service when it exits, reports `INACTIVE`, or stops answering; while the last status was `SCANNING` it
tolerates three missed polls before it acts. Run it from a systemd unit.

## Security

There is no authentication and CORS is open: the service is meant for a private link between the camera host and
the machine that drives the scan. On a shared network set `server_address` to `127.0.0.1` and put an authenticating
proxy in front, or firewall the port. `/config` accepts only the declared settings; records are read only below
`data_dir`; scan names are reduced to letters, digits, `_` and `-`.

## API

Prefix `/api/specimFX10`; JSON in and out, errors included (`{"error", "status"}`), except the preview (PNG) and the
sidecar route (the file itself). Port 23000 by default.

| Route | Purpose |
|---|---|
| `GET /status` | `{"status": "INSETUP" \| "ACTIVE" \| "INACTIVE" \| "SCANNING"}` for the watchdog |
| `GET /www/status_server`, `GET /www/restart_server` | service identity; restart by SIGINT |
| `GET /camera` | model, serial, firmware, region, binning, frame rate and its maximum, exposure, packet settings, corrections, temperatures, shutter, stream state, handler and stream statistics, which references exist |
| `GET/POST /config` | the settings (`camera.*` keys are re-applied to the camera when idle; refused with 409 during a scan) |
| `POST /stream/start`, `POST /stream/stop` | streaming for the preview; stop is refused during a scan |
| `POST /shutter` `{"state": "open" \| "closed"}` | the mechanical shutter |
| `POST /reference/dark` `{"frames"}` | closes the shutter, averages the frames, reopens; stored under `data/refs/` |
| `POST /reference/white` `{"frames", "columns": [c0, c1]}` | averages the frames over the white tile; the tile's column range is extended to the whole line |
| `POST /scan/start` `{"name", "max_lines", "max_seconds", "meta"}` | starts recording; ends by itself at either limit or on `scan/stop` |
| `GET /scan/status`, `POST /scan/stop` | progress; stop closes the cube and writes the sidecar |
| `POST /scan/position` `{"t", "x", "y", "z"}` | carrier positions, appended to the running scan's sidecar |
| `GET /records`, `GET /records/<date>/<id>` | the scans on disk; a scan's sidecar |
| `GET /preview.png?gain=` | false-colour strip of the latest lines (three bands, dark-subtracted when a dark exists) |

## Data

One folder per scan, `data/<YYYY-MM-DD>/<HHMMSS>[_name]/`:

- `cube.raw` + `cube.hdr`: ENVI BIL, uint16, little endian, `lines × bands × samples` (one camera frame is one line
  record, appended verbatim); the header carries the wavelengths when a wavelength file is configured, the frame rate,
  the exposure and the serial.
- `dark.raw/.hdr`, `white.raw/.hdr`: the references in force at the start of the scan, one line each, same layout.
- `scan.json`: camera info and configuration at the start, per-line frame ids and camera timestamps, achieved frame
  rate, handler and stream statistics (missing frames, resends), the posted positions, your `meta`.

Reflectance is computed later: `(raw − dark) / (white − dark)` per band and sample. The service stores raw data
only, on purpose. Read a cube back with the package's own reader, `from specimFX10.envi import read_bil` and
`cube, header = read_bil("data/<date>/<scan>/cube")`: a memory-mapped `uint16` array of shape `(lines, bands,
samples)` and the header as a dict; any ENVI-aware tool reads the same files.

## Procedures

**Wavelengths.** The camera does not expose its band-to-wavelength mapping as a GenICam node. Take the wavelength
file for your binning from the calibration media delivered with your unit (two columns per line: band centre and
FWHM in nm; the "unified" grid applies when the aberration correction is on, the default here), put its path in
`wavelengths_file`, and the ENVI headers carry it. Without it the cubes are still valid, just without wavelengths.

**Exposure and frame rate.** Set the exposure on the brightest expected target (the white tile under the working
illumination) so that the maximum stays below saturation (4095); `look` and `monitor` print the level. The frame
rate is bounded by the exposure (`frame_rate_max` in `/camera`); the camera's maximum at the default region is 163 fps.

**Scan speed.** Along-track pixel size = carrier speed ÷ frame rate. With about 0.3 mm across-track sampling at
480 mm working distance (38° lens), square pixels need ≤ 5 cm/s at 163 fps; 10 cm/s at 50 fps gives 2 mm rows. Move
at constant speed; acceleration ramps bunch the rows, so crop by the recorded positions. The camera has a trigger
input, but this service runs it free-running (trigger mode off) and offers no trigger setting.

**References.** Dark: shutter closed, same exposure and frame rate as the scan. White: the tile at the working
distance under the scan's illumination, same settings; give the tile's column range if it does not span the whole
line. Outdoors, take both per scan, not per day.

**Focus and orientation.** One line is hard to judge by eye; the preview stitches the last few hundred lines into a
strip, and moving anything under the camera makes it show an image. The carrier must move perpendicular to the slit.

**Corrections and region.** The service does not touch the camera's own corrections (aberration correction, flat
field) or its region of interest; it reads them back into `/camera` and the sidecar. Set them once with the vendor's
tool or `arv-tool` and they persist in the camera. The defaults this software was developed with: corrections on,
binning 2 in the spectral axis (448 rows to 224 bands), 12-bit `Mono12`, 1024 spatial pixels.

## Tests

`.venv/bin/python -m pytest` runs in a few seconds on the fake camera: the ENVI writer round trip, the settings, the
service's routes through Flask's test client (references and their validation, a scan with positions ending by its
limit, the records and the traversal refusals, the preview, the refusals during a scan, the shutdown of a running
scan), and the example consumer against a live server thread. No hardware is exercised by the tests; the camera
transport is proven by `capture_cli testpattern` on a real camera, which exits non-zero when the layout check fails.
Release 0.1.0 was validated on the fake camera; its camera-facing module is the code operated on the real camera
before the release review, with comment edits and a version-ordered library lookup only; the first real-camera run of
this release is noted in the changelog when it happens.

## Layout

```
specimFX10/    camera.py (Aravis, the only module that touches it) · acquisition.py (references, scans, preview)
               envi.py (BIL writer/reader) · server.py (Flask routes) · settings.py · main.py · capture_cli.py
               program_monitor.py (watchdog) · fake_camera.py (stand-in for development and tests)
examples/      consumer.py (the scan sequence with a stand-in carrier)
tests/         pytest, fake camera only
docs/          push-broom.md (how a line-scan camera forms an image)
```

## Acknowledgements

Aravis does the GigE Vision transport. Developed
within the OpenAgri project (Horizon Europe, GA No 101134083) by Smart Farm Robotix VCC.
