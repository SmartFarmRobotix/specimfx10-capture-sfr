import os

import numpy as np

from specimFX10.envi import BilWriter, read_bil, write_reference


def test_bil_round_trip(tmp_path):
    base = str(tmp_path / "cube")
    w = BilWriter(base, samples=16, bands=8, wavelengths=[400.0 + i for i in range(8)], description="test",
                  extra={"fps": 50})
    frames = [np.full((8, 16), i, dtype=np.uint16) for i in range(5)]
    for fr in frames:
        w.append(fr)
    w.close(extra={"lines written": 5})
    data, hdr = read_bil(base)
    assert data.shape == (5, 8, 16)
    assert hdr["interleave"] == "bil" and hdr["data type"] == "12" and hdr["byte order"] == "0"
    assert hdr["samples"] == "16" and hdr["lines"] == "5" and hdr["bands"] == "8"
    assert hdr["wavelength units"] == "Nanometers" and hdr["wavelength"].startswith("{400.000, 401.000")
    assert hdr["fps"] == "50" and hdr["lines written"] == "5"
    assert (np.asarray(data[3]) == 3).all()
    assert os.path.getsize(base + ".raw") == 5 * 8 * 16 * 2


def test_writer_refuses_wrong_shape_or_dtype(tmp_path):
    w = BilWriter(str(tmp_path / "c"), samples=4, bands=2)
    try:
        w.append(np.zeros((2, 5), dtype=np.uint16))
        assert False, "shape must be checked"
    except ValueError:
        pass
    try:
        w.append(np.zeros((2, 4), dtype=np.float32))
        assert False, "dtype must be checked"
    except ValueError:
        pass
    w.close()


def test_reference_is_one_line_uint16(tmp_path):
    base = str(tmp_path / "dark")
    mean = np.full((8, 16), 1234.6, dtype=np.float32)
    write_reference(base, mean, wavelengths=[1.0] * 8, description="dark reference")
    data, hdr = read_bil(base)
    assert data.shape == (1, 8, 16)
    assert int(data[0, 0, 0]) == 1235
    assert hdr["description"] == "{dark reference}"
