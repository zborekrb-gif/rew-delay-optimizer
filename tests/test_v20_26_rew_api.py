from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

import numpy as np

from rew_api_client import (
    REWApiClient,
    REWApiMeasurement,
    decode_frequency_response,
    encode_float32_base64,
    normalise_api_url,
    read_snapshot,
    is_no_smoothing_label,
    verify_snapshot_response_integrity,
)
from rew_mdat import import_mdat_measurements, scan_mdat_project


def response_payload(start: float = 20.0, ppo: int = 12, points: int = 24, offset: float = 0.0):
    magnitudes = np.linspace(70.0 + offset, 80.0 + offset, points)
    phases = np.linspace(-90.0, 45.0, points)
    return {
        "smoothing": "None",
        "startFreq": start,
        "ppo": ppo,
        "magnitudes": encode_float32_base64(magnitudes),
        "phases": encode_float32_base64(phases),
    }


class FakeREWHandler(BaseHTTPRequestHandler):
    summaries = [
        {"uuid": "u-lm", "title": "90_BW48_L_MAIN_D30.00", "startFreq": 20, "endFreq": 80, "rewVersion": "V5.40 beta"},
        {"uuid": "u-ls", "title": "90_BW48_L_SUB_D20.40", "startFreq": 20, "endFreq": 80, "rewVersion": "V5.40 beta"},
        {"uuid": "u-rm", "title": "90_BW48_R_MAIN_D30.00", "startFreq": 20, "endFreq": 80, "rewVersion": "V5.40 beta"},
        {"uuid": "u-rs", "title": "90_BW48_R_SUB_D19.10", "startFreq": 20, "endFreq": 80, "rewVersion": "V5.40 beta"},
    ]

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/measurements":
            self._send(self.summaries)
            return
        if path == "/measurements/frequency-response/smoothing-choices":
            self._send(["None", "1/48", "1/24", "1/12"])
            return
        if path.endswith("/frequency-response"):
            query = parse_qs(urlparse(self.path).query)
            assert "ppo" not in query
            assert query.get("smoothing") == ["None"]
            uuid = path.split("/")[2]
            offset = {"u-lm": 0.0, "u-ls": 1.0, "u-rm": 2.0, "u-rs": 3.0}[uuid]
            payload = response_payload(offset=offset)
            payload.pop("ppo")
            payload["freqStep"] = 1.0
            self._send(payload)
            return
        if path.endswith("/group-delay"):
            self._send({"smoothing": "None", "startFreq": 20.0, "ppo": 12, "magnitudes": encode_float32_base64([0.01] * 24)})
            return
        if path.endswith("/impulse-response"):
            self._send({
                "startTime": -0.01,
                "sampleInterval": 1.0 / 48000.0,
                "sampleRate": 48000.0,
                "timingReference": "Acoustic timing reference",
                "unit": "Percent",
                "data": encode_float32_base64([0.0, 1.0, 0.5, 0.0]),
            })
            return
        self.send_error(404)

    def _send(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class REWApiTests(unittest.TestCase):
    def test_decode_log_frequency_response(self):
        decoded = decode_frequency_response(response_payload(start=16.0, ppo=4, points=5))
        np.testing.assert_allclose(decoded["frequency_hz"], [16.0, 16.0 * 2 ** 0.25, 16.0 * 2 ** 0.5, 16.0 * 2 ** 0.75, 32.0])
        self.assertEqual(decoded["axis_kind"], "log")

    def test_decode_linear_frequency_response(self):
        payload = response_payload(points=4)
        payload.pop("ppo")
        payload["freqStep"] = 5.0
        decoded = decode_frequency_response(payload)
        np.testing.assert_allclose(decoded["frequency_hz"], [20, 25, 30, 35])
        self.assertEqual(decoded["axis_kind"], "linear")

    def test_local_url_guard(self):
        self.assertEqual(normalise_api_url("localhost:4800"), "http://localhost:4800")
        with self.assertRaises(ValueError):
            normalise_api_url("http://192.168.1.25:4735")

    def test_duplicate_uuid_uses_current_list_index(self):
        original = list(FakeREWHandler.summaries)
        FakeREWHandler.summaries = [
            {"uuid": "same", "title": "L_MAIN"},
            {"uuid": "same", "title": "R_MAIN"},
        ]
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeREWHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = REWApiClient(f"http://127.0.0.1:{server.server_port}")
            items = client.list_measurements()
            self.assertEqual([x.api_reference for x in items], ["1", "2"])
            self.assertTrue(all(x.duplicate_uuid for x in items))
        finally:
            server.shutdown()
            server.server_close()
            FakeREWHandler.summaries = original

    def test_snapshot_includes_time_metadata(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeREWHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = REWApiClient(f"http://127.0.0.1:{server.server_port}")
            with TemporaryDirectory() as temp:
                out = client.create_snapshot(
                    client.list_measurements()[:1],
                    Path(temp) / "time",
                    include_group_delay=True,
                    include_impulse=True,
                )
                snapshot = read_snapshot(out)
                record = snapshot["measurements"][0]
                self.assertEqual(snapshot["formatVersion"], 3)
                self.assertEqual(record["groupDelayInfo"]["unit"], "seconds")
                self.assertEqual(record["impulseInfo"]["sampleRateHz"], 48000.0)
                self.assertFalse(record["impulseInfo"]["normalisedRequested"])
        finally:
            server.shutdown()
            server.server_close()

    def test_api_snapshot_round_trip_and_offline_compatibility(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeREWHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            client = REWApiClient(base_url)
            measurements = client.list_measurements()
            self.assertEqual(len(measurements), 4)
            with TemporaryDirectory() as temp:
                out = client.create_snapshot(measurements, Path(temp) / "raw", include_group_delay=True)
                self.assertTrue(out.name.endswith(".rewapi.json"))
                snapshot = read_snapshot(out)
                self.assertEqual(snapshot["formatVersion"], 3)
                self.assertTrue(snapshot["acquisition"]["nativeAxisRequested"])
                self.assertIsNone(snapshot["acquisition"]["ppoRequested"])
                ok, problems = verify_snapshot_response_integrity(snapshot)
                self.assertTrue(ok, problems)
                scan = scan_mdat_project(out)
                self.assertTrue(scan["is_rew_api_snapshot"])
                self.assertEqual(scan["missing_required"], [])
                project = import_mdat_measurements(out)
                self.assertEqual(set(project.measurements_by_role), {"L main", "L sub", "R main", "R sub"})
                self.assertEqual(project.points, 24)
                self.assertAlmostEqual(project.measurements_by_role["R sub"].spl_db[0], 73.0, places=5)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()


class REWApiRawConfirmationTests(unittest.TestCase):
    def _verified_state(self):
        return {
            "schemaVersion": 1,
            "userConfirmed": True,
            "raw": True,
            "diracOff": True,
            "crossoverOff": True,
            "peqOff": True,
            "delaysZero": True,
            "polarityNormal": True,
            "relFilter120": True,
            "timingReferenceConsistent": True,
            "microphoneStationary": True,
            "mainGainDb": 0.0,
            "subGainDb": -6.0,
        }

    def test_measurement_state_verification(self):
        from rew_api_client import verify_measurement_state
        ok, missing = verify_measurement_state(self._verified_state())
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        broken = self._verified_state()
        broken["peqOff"] = False
        ok, missing = verify_measurement_state(broken)
        self.assertFalse(ok)
        self.assertIn("peqOff", missing)

    def test_api_snapshot_confirmation_unlocks_export_gate(self):
        from rew_acoustic_crossover_designer import _audit_input_for_acoustic_design, _audit_is_verified_raw
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeREWHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = REWApiClient(f"http://127.0.0.1:{server.server_port}")
            measurements = client.list_measurements()
            with TemporaryDirectory() as temp:
                out = client.create_snapshot(
                    measurements,
                    Path(temp) / "api_raw",
                    measurement_state=self._verified_state(),
                )
                payload = read_snapshot(out)
                self.assertEqual(payload["measurementState"]["subGainDb"], -6.0)
                project = import_mdat_measurements(out)
                status, decision, note = _audit_input_for_acoustic_design(out, project.role_to_name)
                self.assertEqual(status, "RAW_VERIFIED_BY_API_NATIVE")
                self.assertTrue(_audit_is_verified_raw(status, decision))
                self.assertIn("sub -6.00 dB", note)
        finally:
            server.shutdown()
            server.server_close()

    def test_api_snapshot_without_confirmation_stays_diagnostic(self):
        from rew_acoustic_crossover_designer import _audit_input_for_acoustic_design, _audit_is_verified_raw
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeREWHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = REWApiClient(f"http://127.0.0.1:{server.server_port}")
            with TemporaryDirectory() as temp:
                out = client.create_snapshot(client.list_measurements(), Path(temp) / "api_raw")
                project = import_mdat_measurements(out)
                status, decision, note = _audit_input_for_acoustic_design(out, project.role_to_name)
                self.assertEqual(status, "RAW_API_NIEPOTWIERDZONY")
                self.assertFalse(_audit_is_verified_raw(status, decision))
                self.assertIn("Braki", note)
        finally:
            server.shutdown()
            server.server_close()


def test_no_smoothing_labels():
    assert is_no_smoothing_label("None")
    assert is_no_smoothing_label("Bez wygładzania")
    assert not is_no_smoothing_label("1/12")


def test_integrity_rejects_old_snapshot():
    ok, problems = verify_snapshot_response_integrity({"measurements": []})
    assert not ok
    assert problems
