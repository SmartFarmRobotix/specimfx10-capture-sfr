"""Entry point: python -m specimFX10.main --config-file data/config.json --server [--port N]"""
from __future__ import annotations

import argparse
import logging
import sys

from .settings import ServiceSettings, DEFAULT_CONFIG_PATH


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config-file", default=DEFAULT_CONFIG_PATH)
    p.add_argument("--server", action="store_true", help="start the Flask service")
    p.add_argument("--port", type=int, default=None, help="override server port")
    p.add_argument("--no-camera", action="store_true", help="start the service without connecting (replay/dev)")
    p.add_argument("--fake", action="store_true", help="use the built-in fake camera (no hardware, no Aravis)")
    a = p.parse_args(argv)

    settings = ServiceSettings.load(a.config_file)
    if a.port:
        settings.server_port = a.port
    logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log = logging.getLogger("specimFX10")
    log.info("specimFX10 starting; config %s; data %s; port %d", a.config_file, settings.data_dir, settings.server_port)
    settings.save()   # write defaults on first run so they can be seen and edited

    if not a.server:
        p.print_help(); return 0

    from .server import Server
    camera = None
    if a.fake:
        from .fake_camera import FakeFX10Camera
        camera = FakeFX10Camera(settings.camera)
    srv = Server(settings, camera=camera)
    if not a.no_camera:
        try:
            srv.connect()
        except Exception as e:
            log.error("camera connect failed: %s", e)
            srv.state = "INACTIVE"
    import signal

    def _term(*_):                 # the watchdog restarts with SIGTERM: leave through the finally below
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _term)
    try:
        srv.run()
    finally:
        srv.shutdown()    # a running scan is finalised before the camera is released
    return 0


if __name__ == "__main__":
    sys.exit(main())
