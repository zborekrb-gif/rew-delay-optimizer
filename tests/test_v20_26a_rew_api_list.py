from pathlib import Path

from rew_api_client import REWApiClient


class FakeClient(REWApiClient):
    def __init__(self, payload, details=None):
        self.base_url = "http://127.0.0.1:4735"
        self.timeout = 1.0
        self.payload = payload
        self.details = details or {}

    def get_json(self, path, query=None):
        if path == "/measurements":
            return self.payload
        if path.startswith("/measurements/"):
            key = path.split("/")[2]
            return self.details[key]
        raise AssertionError(path)


def summary(title="L main", uuid="abc"):
    result = {"title": title, "startFreq": 20.0, "endFreq": 250.0}
    if uuid is not None:
        result["uuid"] = uuid
    return result


def test_official_summary_list():
    rows = FakeClient([summary()]).list_measurements()
    assert len(rows) == 1
    assert rows[0].title == "L main"
    assert rows[0].api_reference == "abc"


def test_wrapped_summary_list():
    rows = FakeClient({"measurements": [summary("R sub", "def")]}).list_measurements()
    assert [row.title for row in rows] == ["R sub"]


def test_mapping_of_indices_to_summaries():
    rows = FakeClient({"1": summary("L main", "a"), "2": summary("L sub", "b")}).list_measurements()
    assert [row.list_index for row in rows] == [1, 2]
    assert [row.title for row in rows] == ["L main", "L sub"]


def test_scalar_list_fetches_details_by_index():
    client = FakeClient(["first", "second"], {"1": summary("L main", "a"), "2": summary("R main", "b")})
    rows = client.list_measurements()
    assert [row.title for row in rows] == ["L main", "R main"]


def test_missing_uuid_is_kept_and_uses_index():
    rows = FakeClient([summary("Pomiar historyczny", None)], {"1": summary("Pomiar historyczny", None)}).list_measurements()
    assert len(rows) == 1
    assert rows[0].title == "Pomiar historyczny"
    assert rows[0].uuid == "rew-index-1"
    assert rows[0].api_reference == "1"


def test_gui_worker_does_not_read_tk_variables():
    source = Path(__file__).resolve().parents[1].joinpath("rew_api_gui.py").read_text(encoding="utf-8")
    refresh_worker = source.split("def worker(url: str)", 1)[1].split("threading.Thread", 1)[0]
    assert "self.url_var.get()" not in refresh_worker
    assert "lambda error=exc" in source
