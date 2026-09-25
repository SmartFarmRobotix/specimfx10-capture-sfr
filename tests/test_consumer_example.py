"""The example consumer against a live service (Flask dev server in a thread) on the fake camera."""
import os
import sys
import threading
import time

import requests
from werkzeug.serving import make_server

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"))
import consumer  # noqa: E402


def test_example_scan_produces_a_record_with_positions(server):
    httpd = make_server("127.0.0.1", 0, server.app, threaded=True)   # the server picks its own free port
    port = httpd.server_port
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                requests.get(base + "/status", timeout=1); break
            except Exception:
                time.sleep(0.1)
        out = consumer.run_scan(consumer.ServiceClient(base), consumer.Carrier(), axis="x", start_cm=0.0,
                                length_cm=2.0, speed_cm_s=4.0, dark_frames=4, white_frames=4,
                                white_columns=[400, 600], name="example", poll_s=0.05)
    finally:
        httpd.shutdown()
    assert out["dark"]["frames"] == 4 and out["white"]["frames"] == 4
    assert out["scan"]["state"] == "done" and out["record"]["lines"] > 10
    assert out["record"]["positions"] >= 3                 # the carrier moved 0.5 s, polled every 50 ms
    assert out["record"]["achieved_fps"] > 20              # a loaded test host still delivers a fraction of 100 fps
