import json
import os

from specimFX10.settings import ServiceSettings


def test_load_wavelength_file_two_columns_and_blank_lines(tmp_path):
    p = tmp_path / "w.wls"
    p.write_text("400.5 5.4\n\n402.9 5.4\n# a comment line is skipped\n405.3\n")
    s = ServiceSettings(wavelengths_file=str(p))
    centres, fwhm = s.load_wls()
    assert centres == [400.5, 402.9, 405.3] and fwhm == [5.4, 5.4, 0.0]
    assert s.load_wavelengths() == centres


def test_missing_wavelength_file_is_none(tmp_path):
    assert ServiceSettings(wavelengths_file=str(tmp_path / "none.wls")).load_wls() is None


def test_load_ignores_unknown_keys_and_empty_wavelength_path(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"server_port": 24000, "wavelengths_file": "", "bogus": 1, "#note": "x",
                               "camera": {"frame_rate": 12.5, "bogus": 2}}))
    s = ServiceSettings.load(str(cfg))
    assert s.server_port == 24000 and s.camera.frame_rate == 12.5
    assert s.wavelengths_file == ServiceSettings().wavelengths_file
    assert not hasattr(s, "bogus") and not hasattr(s.camera, "bogus")


def test_save_with_a_bare_file_name_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = ServiceSettings.load("config.json")
    s.save()
    assert os.path.exists(tmp_path / "config.json")


def test_update_returns_changed_and_ignored(tmp_path):
    s = ServiceSettings()
    changed, ignored = s.update({"reference_frames": 40, "debug": True, "path": "/x", "camera": {"exposure_us": 3000.0, "nope": 1}})
    assert changed == ["debug", "camera.exposure_us"] and ignored == ["path", "camera.nope"]
    assert s.camera.exposure_us == 3000.0
