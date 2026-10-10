import json
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer

import pytest

import dose_rules
from server import Handler, verified_dose_calculations

RULE = {"sku": "SP-01", "amount": 20, "unit": "ml", "per_volume": 16,
        "source_doc": "nhan.pdf", "source_page": "2", "status": "approved"}


@pytest.fixture
def db(tmp_path):
    with closing(sqlite3.connect(str(tmp_path / "w.sqlite3"))) as conn:
        conn.row_factory = sqlite3.Row
        dose_rules.ensure_schema(conn)
        yield conn


def test_import_is_candidate(db):
    result = dose_rules.import_rules(db, [dict(RULE)])  # status gửi lên bị bỏ qua
    assert result["imported"] == 1 and result["status"] == "candidate"
    rules = dose_rules.list_rules(db, "SP-01")["rules"]
    assert [r["status"] for r in rules] == ["candidate"]
    with pytest.raises(ValueError):
        dose_rules.import_rules(db, [{**RULE, "amount": 0}])


def test_only_approved_used(db):
    ids = dose_rules.import_rules(db, [dict(RULE), {**RULE, "amount": 99}])["rule_ids"]
    assert dose_rules.approved_directions(db, "SP-01") == []
    profile = dose_rules.profile_with_approved_rules(db, {}, "SP-01")
    assert verified_dose_calculations("bình 16 lít pha bao nhiêu", profile) == []
    dose_rules.review_rule(db, ids[0], "approve")
    dose_rules.review_rule(db, ids[1], "reject")
    profile = dose_rules.profile_with_approved_rules(db, {"huong_dan_su_dung": []}, "SP-01")
    rows = verified_dose_calculations("bình 16 lít pha bao nhiêu", profile)
    assert len(rows) == 1 and rows[0]["calculated_ml"] == 20 and rows[0]["don_vi"] == "ml"


def test_review_409_twice(db):
    rule_id = dose_rules.import_rules(db, [dict(RULE)])["rule_ids"][0]
    dose_rules.review_rule(db, rule_id, "approve")
    with pytest.raises(dose_rules.DoseRuleConflict):
        dose_rules.review_rule(db, rule_id, "reject")
    with pytest.raises(ValueError):
        dose_rules.review_rule(db, "missing", "approve")


class _Workspace:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        dose_rules.ensure_schema(conn)
        try:
            with conn:
                yield conn
        finally:
            conn.close()


class _Engine:
    def __init__(self, path):
        self.workspace = _Workspace(path)


def _call(port, method, path, body=None, origin=""):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_local_only_route_and_review_flow(tmp_path):
    Handler.engine = _Engine(tmp_path / "w.sqlite3")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, args=(0.1,), daemon=True).start()
    port = server.server_address[1]
    try:
        body = {"rules": [dict(RULE)]}
        assert _call(port, "POST", "/api/workspace/dose-rules/import", body,
                     origin="chrome-extension://abc")[0] == 403
        assert _call(port, "GET", "/api/workspace/dose-rules",
                     origin="chrome-extension://abc")[0] == 403
        status, imported = _call(port, "POST", "/api/workspace/dose-rules/import", body)
        assert status == 200 and imported["status"] == "candidate"
        rule_id = imported["rule_ids"][0]
        _, listed = _call(port, "GET", "/api/workspace/dose-rules?sku=SP-01")
        assert listed["rules"][0]["status"] == "candidate"
        url = f"/api/workspace/dose-rules/{rule_id}/review"
        assert _call(port, "POST", url, {"decision": "approve"})[0] == 200
        assert _call(port, "POST", url, {"decision": "approve"})[0] == 409
        assert _call(port, "POST", url, {"decision": "x"}, origin="chrome-extension://abc")[0] == 403
    finally:
        server.shutdown()
        server.server_close()
