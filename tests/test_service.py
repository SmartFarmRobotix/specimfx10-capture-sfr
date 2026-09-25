"""The service end to end through Flask's test client, on the fake camera: no hardware, no Aravis."""
import json
import os
import time

import numpy as np

from specimFX10.envi import read_bil

API = "/api/specimFX10"


def _wait(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _scan_done(client):
    return _wait(lambda: client.get(API + "/scan/status").get_json()["scan"]["state"] == "done")


def test_status_and_camera_after_connect(client, server):
    assert client.get("/status").get_json() == {"status": "ACTIVE"}
    info = client.get("/www/status_server").get_json()
    assert info["service"] == "specimFX10" and "version" in info and "host" not in info
    cam = client.get(API + "/camera").get_json()
    assert cam["region"] == {"x": 0, "y": 0, "width": 1024, "height": 224}
    assert cam["shutter"] == "open" and cam["stream"] == "idle"
    assert cam["references"] == {"dark": False, "white": False}


def test_config_update_reconfigures_the_camera(client, server, monkeypatch):
    calls = []
    real_configure = server.cam.configure
    monkeypatch.setattr(server.cam, "configure", lambda: (calls.append(1), real_configure())[1])
    client.post(API + "/reference/dark", json={"frames": 4})
    assert server.acq.dark is not None
    r = client.post(API + "/config", json={"camera": {"frame_rate": 60.0, "binning_vertical": 4}, "reference_frames": 5,
                                            "path": "/tmp/elsewhere", "save": 1, "camera_extra": 3}).get_json()
    assert set(r["changed"]) == {"camera.frame_rate", "camera.binning_vertical", "reference_frames"}
    assert set(r["ignored"]) == {"path", "save", "camera_extra"}
    assert r["camera_reconfigured"] is True and calls == [1]
    assert client.get(API + "/camera").get_json()["region"]["height"] == 112
    assert server.acq.dark is None, "a reference taken with the old geometry must not survive"
    assert json.load(open(server.settings.path))["camera"]["frame_rate"] == 60.0
    assert os.path.dirname(server.settings.path) != "/tmp"


def test_dark_reference_closes_and_reopens_the_shutter(client, server):
    r = client.post(API + "/reference/dark", json={"frames": 8}).get_json()
    assert r["frames"] == 8 and r["mean"] < 100 and r["max"] < 200
    assert server.cam.shutter == "open"
    assert client.get(API + "/camera").get_json()["references"]["dark"] is True
    data, hdr = read_bil(os.path.join(server.settings.data_dir, "refs", "dark"))
    assert data.shape == (1, 224, 1024)


def test_reference_payloads_are_validated(client):
    assert client.post(API + "/reference/dark", json={"frames": 0}).status_code == 400
    assert client.post(API + "/reference/dark", json={"frames": "many"}).status_code == 400
    assert client.post(API + "/reference/white", json={"frames": 2, "columns": [600, 400]}).status_code == 400
    assert client.post(API + "/reference/white", json={"frames": 2, "columns": [0, 5000]}).status_code == 400
    assert client.post(API + "/reference/white", json={"frames": 2, "columns": [1]}).status_code == 400


def test_white_reference_extends_the_tile_columns_over_the_line(client, server, monkeypatch):
    frame = np.full((224, 1024), 1000, dtype=np.uint16)
    frame[:, 400:600] = 3000                              # the tile is bright, the rest of the line is not
    monkeypatch.setattr(server.cam, "_frame", lambda h, w: frame.copy())
    r = client.post(API + "/reference/white", json={"frames": 4, "columns": [400, 600]}).get_json()
    assert r["columns"] == [400, 600] and abs(r["mean"] - 3000) < 1 and r["saturated_fraction"] == 0.0
    white = server.acq.white
    assert white.shape == (224, 1024)
    assert np.allclose(white, 3000, atol=1), "every column carries the tile's mean, not the line's own value"


def test_scan_writes_a_cube_and_keeps_positions_when_the_limit_ends_it(client, server):
    client.post(API + "/reference/dark", json={"frames": 4})
    s = client.post(API + "/scan/start", json={"name": "t", "max_lines": 30}).get_json()
    assert s["state"] == "SCANNING" and s["scan"]["state"] == "scanning"
    for i in range(3):
        assert client.post(API + "/scan/position", json={"x": i * 1.5, "y": 0, "z": 0}).status_code == 200
    assert _scan_done(client)                              # ended by max_lines, nobody called scan/stop
    st = client.get(API + "/scan/status").get_json()
    rid, folder = st["scan"]["record_id"], st["scan"]["folder"]
    assert st["scan"]["lines"] == 30 and st["state"] == "ACTIVE"
    data, hdr = read_bil(os.path.join(folder, "cube"))
    assert data.shape == (30, 224, 1024) and hdr["wavelength"].startswith("{400.000")
    scan = json.load(open(os.path.join(folder, "scan.json")))
    assert scan["lines"] == 30 and len(scan["line_frame_ids"]) == 30 and scan["state"] == "done"
    assert len(scan["positions"]) == 3 and scan["positions"][2]["x"] == 3.0
    assert os.path.exists(os.path.join(folder, "dark.hdr"))
    recs = client.get(API + "/records").get_json()
    assert [r["record_id"] for r in recs] == [rid]
    assert client.get(API + "/records/" + rid).get_json()["record_id"] == rid


def test_scan_names_are_sanitised_and_same_second_scans_do_not_collide(client, server, monkeypatch):
    import specimFX10.acquisition as acq_module
    fixed = time.localtime(1_800_000_000)
    monkeypatch.setattr(acq_module.time, "localtime", lambda *_: fixed)   # both scans get the same second
    a = client.post(API + "/scan/start", json={"name": "../../etc x", "max_lines": 2}).get_json()["scan"]
    assert _scan_done(client)
    b = client.post(API + "/scan/start", json={"name": "../../etc x", "max_lines": 2}).get_json()["scan"]
    assert _scan_done(client)
    assert a["record_id"].endswith("_etcx") and ".." not in a["folder"]
    assert os.path.realpath(a["folder"]).startswith(os.path.realpath(server.settings.data_dir))
    assert b["record_id"] == a["record_id"] + "-2" and os.path.exists(os.path.join(b["folder"], "cube.hdr"))


def test_malformed_json_is_refused_and_an_empty_body_is_fine(client):
    r = client.post(API + "/scan/start", data="{not json", content_type="application/json")
    assert r.status_code == 400
    assert client.get("/status").get_json() == {"status": "ACTIVE"}, "no scan was started by the bad body"
    assert client.post(API + "/stream/start").status_code == 200


def test_a_refused_camera_configuration_keeps_the_old_settings(client, server, monkeypatch):
    real = server.cam.configure
    calls = {"n": 0}

    def configure_once_failing():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("feature out of range")
        return real()

    monkeypatch.setattr(server.cam, "configure", configure_once_failing)
    r = client.post(API + "/config", json={"camera": {"frame_rate": 9999.0}})
    assert r.status_code == 409 and "refused" in r.get_data(as_text=True)
    assert server.settings.camera.frame_rate == 100.0
    assert json.load(open(server.settings.path))["camera"]["frame_rate"] == 100.0 if os.path.exists(server.settings.path) else True


def test_frames_are_not_appended_past_the_limit(client, server):
    client.post(API + "/scan/start", json={"max_lines": 7})
    assert _scan_done(client)
    st = client.get(API + "/scan/status").get_json()
    data, _ = read_bil(os.path.join(st["scan"]["folder"], "cube"))
    assert data.shape[0] == 7 and st["scan"]["lines"] == 7


def test_record_route_refuses_traversal(client, server, tmp_path):
    outside = tmp_path / "scan.json"
    outside.write_text("{}")
    assert client.get(API + "/records/../../scan").status_code == 404
    assert client.get(API + "/records/2026-01-01/..").status_code == 404
    assert client.get(API + "/records/2026-01-01/%2e%2e%2f%2e%2e").status_code == 404
    assert client.get(API + "/records/notadate/x").status_code == 404


def test_position_outside_a_scan_is_refused(client):
    assert client.post(API + "/scan/position", json={"x": 0}).status_code == 409


def test_operations_that_would_break_a_scan_are_refused(client, server):
    client.post(API + "/scan/start", json={"max_lines": 500})
    assert client.post(API + "/config", json={"camera": {"frame_rate": 10.0}}).status_code == 409
    assert client.post(API + "/shutter", json={"state": "closed"}).status_code == 409
    assert client.post(API + "/stream/stop").status_code == 409
    assert client.post(API + "/reference/dark", json={"frames": 2}).status_code == 409
    assert client.post(API + "/scan/start", json={"max_lines": 5}).status_code == 409
    client.post(API + "/scan/stop")
    assert client.get("/status").get_json() == {"status": "ACTIVE"}


def test_shutdown_finalises_a_running_scan(server):
    server.acq.start_scan(name="cut", max_lines=100000)
    _wait(lambda: server.acq.scan.lines > 3)
    server.shutdown()
    scan = json.load(open(os.path.join(server.acq.scan.folder, "scan.json")))
    assert scan["state"] == "done" and scan["lines"] > 3
    assert not server.cam.is_open


def test_preview_is_a_png_and_bands_are_validated(client, server):
    client.post(API + "/stream/start")
    assert _wait(lambda: client.get(API + "/camera").get_json()["handler"]["frames"] > 5)
    r = client.get(API + "/preview.png?gain=2")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(API + "/preview.png?gain=x").status_code == 400
    server.settings.preview_bands = [1, 2, 999]
    assert client.get(API + "/preview.png").status_code == 400


def test_test_pattern_ramp_round_trip(client, server):
    server.cam.set("TestPattern", "Ramp")
    client.post(API + "/scan/start", json={"name": "ramp", "max_lines": 3})
    assert _scan_done(client)
    st = client.get(API + "/scan/status").get_json()
    server.cam.set("TestPattern", "Off")
    data, _ = read_bil(os.path.join(st["scan"]["folder"], "cube"))
    row = np.asarray(data[0])[0].astype(int)
    assert row[0] == 0 and row[-1] == 4095 and (np.diff(row) >= 0).all()


def test_wavelengths_that_do_not_fit_the_band_count_are_left_out(client, server):
    server.acq.wavelengths = [500.0] * 10
    client.post(API + "/scan/start", json={"max_lines": 2})
    assert _scan_done(client)
    _, hdr = read_bil(os.path.join(client.get(API + "/scan/status").get_json()["scan"]["folder"], "cube"))
    assert "wavelength" not in hdr and hdr["bands"] == "224"
