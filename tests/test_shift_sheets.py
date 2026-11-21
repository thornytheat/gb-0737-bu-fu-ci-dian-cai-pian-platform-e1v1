import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from fastapi.testclient import TestClient

from app.manifest import canonical


def submit_run(client, sample="dirty", kerf=5):
    reply = client.post("/api/runs", json={"sample": sample, "kerf_mm": kerf})
    assert reply.status_code == 201
    return reply.json()["manifest"]["id"]


def release(client, run_id):
    return client.post(f"/api/runs/{run_id}/shift-sheets")


def test_publish_creates_versioned_sheet_and_preview_matches(client):
    run_id = submit_run(client)
    preview = client.get("/api/shifts").json()["data"]
    reply = release(client, run_id)
    assert reply.status_code == 201 and reply.json()["reused"] is False
    manifest = reply.json()["manifest"]
    assert manifest["version"] == 1 and manifest["run"] == run_id
    active = client.get(f"/api/runs/{run_id}/shift-sheets/active").json()["document"]
    assert active["manifest"]["id"] == manifest["id"]
    assert canonical(active["sheet"]) == canonical(preview)
    history = client.get(f"/api/runs/{run_id}/shift-sheets").json()["sheets"]
    assert [item["manifest"]["version"] for item in history] == [1]


def test_rerun_same_job_does_not_add_another_set_of_pieces(client):
    run_id = submit_run(client)
    first = release(client, run_id).json()["manifest"]
    again = release(client, run_id)
    assert again.status_code == 200 and again.json() == {"manifest": first, "reused": True}
    assert len(client.get(f"/api/runs/{run_id}/shift-sheets").json()["sheets"]) == 1
    reply = client.post(f"/api/shift-sheets/{first['id']}/rerun")
    assert reply.status_code == 200 and reply.json()["verified"] is True
    with closing(sqlite3.connect(client.app.state.archive.path)) as db:
        assert [row[0] for row in db.execute("SELECT action FROM shift_events ORDER BY seq")] == ["published", "replayed"]


def test_each_piece_and_kerf_belongs_to_one_shift_in_published_sheet(client):
    run_id = submit_run(client)
    sheet = release(client, run_id).json()
    active = client.get(f"/api/runs/{run_id}/shift-sheets/active").json()["document"]["sheet"]
    seen = []
    for shift in active["shifts"]:
        for piece in shift["pieces"]:
            seen.append((piece["id"], piece["shift"]))
    assert len(seen) == sheet["manifest"]["piece_count"]
    assert len({piece_id for piece_id, _ in seen}) == len(seen)
    # 跨班只发生在刀口：交接点两侧恰好是一片的刀口终点和下一片的起点。
    for handoff in active["handoffs"]:
        pieces = {piece["id"]: piece for shift in active["shifts"] for piece in shift["pieces"]}
        assert pieces[handoff["shift_one_ends"]]["kerf_end"] == handoff["handoff_mm"]
        assert pieces[handoff["shift_two_starts"]]["start"] == handoff["handoff_mm"]


def test_failed_release_keeps_previous_sheet_active(client, settings):
    run_id = submit_run(client)
    first = release(client, run_id).json()["manifest"]
    with closing(sqlite3.connect(settings.database)) as db, db:
        db.execute("""CREATE TRIGGER refuse_sheet BEFORE INSERT ON shift_events
                      BEGIN SELECT RAISE(ABORT, 'event storage unavailable'); END;""")
    # 相同内容走复用路径不受影响。
    assert release(client, run_id).json()["reused"] is True
    # 用另一刀口归档出新批次，其发布在事件插入时失败，事务整体回滚。
    other = submit_run(client, kerf=6)
    with _SilentClient(client.app) as safe:
        failed = safe.post(f"/api/runs/{other}/shift-sheets")
    assert failed.status_code == 500
    with closing(sqlite3.connect(settings.database)) as db:
        assert db.execute("SELECT COUNT(*) FROM shift_sheets").fetchone()[0] == 1
        assert db.execute("SELECT id, active FROM shift_sheets").fetchone() == (first["id"], 1)
    assert client.get(f"/api/runs/{run_id}/shift-sheets/active").json()["document"]["manifest"]["id"] == first["id"]


def test_replay_rejects_drift_without_rewriting_sheet(client):
    run_id = submit_run(client)
    manifest = release(client, run_id).json()["manifest"]
    before = client.get(f"/api/runs/{run_id}/shift-sheets/active").content
    document = client.get(f"/api/runs/{run_id}").json()
    document["snapshot"]["changeovers"]["placements"][0]["start"] += 10
    with closing(sqlite3.connect(client.app.state.archive.path)) as db, db:
        db.execute("UPDATE runs SET document=? WHERE id=?", (canonical(document), run_id))
    reply = client.post(f"/api/shift-sheets/{manifest['id']}/rerun")
    assert (reply.status_code, reply.json()["error"]["code"]) == (409, "SHEET_STALE")
    assert client.get(f"/api/runs/{run_id}/shift-sheets/active").content == before


def test_changed_plan_publishes_new_version_but_keeps_the_old_one(client):
    run_id = submit_run(client)
    first = release(client, run_id).json()["manifest"]
    document = client.get(f"/api/runs/{run_id}").json()
    document["snapshot"]["changeovers"]["placements"][0]["source"] = "dirty-orders.csv:2#review"
    with closing(sqlite3.connect(client.app.state.archive.path)) as db, db:
        db.execute("UPDATE runs SET document=? WHERE id=?", (canonical(document), run_id))
    second = release(client, run_id)
    assert second.status_code == 201 and second.json()["reused"] is False
    assert second.json()["manifest"]["version"] == 2 and second.json()["manifest"]["id"] != first["id"]
    history = client.get(f"/api/runs/{run_id}/shift-sheets").json()["sheets"]
    assert [item["manifest"]["version"] for item in history] == [1, 2]
    active = client.get(f"/api/runs/{run_id}/shift-sheets/active").json()["document"]
    assert active["manifest"]["id"] == second.json()["manifest"]["id"]


def test_concurrent_releases_collapse_to_one_sheet(client):
    run_id = submit_run(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: release(client, run_id), range(4)))
    assert {reply.status_code for reply in replies} <= {200, 201}
    assert len({reply.json()["manifest"]["id"] for reply in replies}) == 1
    assert len(client.get(f"/api/runs/{run_id}/shift-sheets").json()["sheets"]) == 1


def test_unknown_run_and_sheet_return_404(client):
    assert release(client, "missing-run").status_code == 404
    assert client.get("/api/runs/missing-run/shift-sheets/active").status_code == 404
    assert client.post("/api/shift-sheets/missing-sheet/rerun").status_code == 404


def test_archived_run_without_shift_stage_is_served_by_rebuild(client):
    run_id = submit_run(client)
    # 模拟旧批次快照没有 shifts 阶段：归档读取仍能重建分班。
    document = client.get(f"/api/runs/{run_id}").json()
    del document["snapshot"]["shifts"]
    with closing(sqlite3.connect(client.app.state.archive.path)) as db, db:
        db.execute("UPDATE runs SET document=? WHERE id=?", (canonical(document), run_id))
    reply = client.get(f"/api/shifts?run_id={run_id}")
    assert reply.status_code == 200 and reply.json()["data"]["piece_count"] == 8


class _SilentClient:
    """与 test_slips 相同的异常抑制手法，用于验证 500 而不是抛出测试异常。"""

    def __init__(self, app):
        self.client = TestClient(app, raise_server_exceptions=False)

    def __enter__(self):
        return self.client

    def __exit__(self, *args):
        return False
    def __init__(self, app):
        self.client = TestClient(app, raise_server_exceptions=False)

    def __enter__(self):
        return self.client

    def __exit__(self, *args):
        return False
