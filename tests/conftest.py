import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from specimFX10.camera import CameraConfig          # noqa: E402
from specimFX10.fake_camera import FakeFX10Camera   # noqa: E402
from specimFX10.settings import ServiceSettings     # noqa: E402
from specimFX10.server import Server                # noqa: E402


@pytest.fixture
def settings(tmp_path):
    s = ServiceSettings()
    s.data_dir = str(tmp_path / "data")
    s.path = str(tmp_path / "config.json")
    s.wavelengths_file = str(tmp_path / "wavelengths.wls")
    with open(s.wavelengths_file, "w") as f:
        for i in range(224):
            f.write(f"{400.0 + i * 2.4:.3f} {5.5:.3f}\n")
    s.camera = CameraConfig(frame_rate=100.0, exposure_us=2000.0, binning_vertical=2)
    s.reference_frames = 10
    return s


@pytest.fixture
def server(settings):
    srv = Server(settings, camera=FakeFX10Camera(settings.camera))
    srv.connect()
    yield srv
    srv.cam.close()


@pytest.fixture
def client(server):
    server.app.config["TESTING"] = True
    return server.app.test_client()
