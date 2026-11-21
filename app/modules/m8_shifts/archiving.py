import json

from app.archive import save_sheet, sheet_event
from app.manifest import canonical, hash_payload, timestamp
from app.modules.m8_shifts.service import shifts
from app.reasons import FabricError


def build_sheet(run_document):
    snapshot = run_document["snapshot"]
    return shifts(snapshot["zones"]["rolls"], snapshot["fit"], snapshot["changeovers"])


def sheet_identity(run_id, sheet):
    # 作业单内容决定身份：同一批次重跑相同分班得到同一编号，不会新增另一套裁片。
    return hash_payload({"run": run_id, "kind": "shift-sheet", "sheet": canonical(sheet)})


def next_version(db, run_id):
    row = db.execute("SELECT COALESCE(MAX(version), 0) AS version FROM shift_sheets WHERE run=?", (run_id,)).fetchone()
    return row["version"] + 1


def load_run(db, run_id):
    row = db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise FabricError("RUN_UNKNOWN", 404)
    return json.loads(row["document"])


def publish(archive, run_id):
    with archive.write() as db:
        run_document = load_run(db, run_id)
        sheet = build_sheet(run_document)
        sheet_id = sheet_identity(run_id, sheet)
        existing = db.execute("SELECT document FROM shift_sheets WHERE id=?", (sheet_id,)).fetchone()
        if existing:
            return json.loads(existing["document"])["manifest"], True
        document = {
            "manifest": {"id": sheet_id, "run": run_id, "version": next_version(db, run_id),
                         "created_at": timestamp(), "sheet_sha256": hash_payload(sheet),
                         "piece_count": sheet["piece_count"], "split_roll_count": sheet["split_roll_count"],
                         "total_changes": sheet["total_changes"]},
            "sheet": sheet,
        }
        # 发布在同一事务内：任一步失败随事务回滚，active 旧版作业单保持不变。
        save_sheet(db, document)
        return document["manifest"], False


def replay(archive, sheet_id):
    with archive.write() as db:
        document = archive.shift_sheet(db, sheet_id)
        run_document = load_run(db, document["manifest"]["run"])
        rebuilt = build_sheet(run_document)
        digest = document["manifest"]["sheet_sha256"]
        if hash_payload(rebuilt) != digest or hash_payload(document["sheet"]) != digest:
            raise FabricError("SHEET_STALE", 409)
        now = timestamp()
        sheet_event(db, sheet_id, now, "replayed")
        return document["manifest"], now


def read_active(archive, run_id):
    with archive.reader() as db:
        load_run(db, run_id)
        return archive.active_shift_sheet(db, run_id)


def read_history(archive, run_id):
    with archive.reader() as db:
        load_run(db, run_id)
        return archive.shift_history(db, run_id)
