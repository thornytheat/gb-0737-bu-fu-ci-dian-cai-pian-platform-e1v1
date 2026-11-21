from fastapi import APIRouter, Request, Response

from app.modules.m8_shifts.archiving import publish, read_active, read_history, replay

router = APIRouter(prefix="/api", tags=["分班作业"])


@router.post("/runs/{run_id}/shift-sheets", status_code=201)
def release_shift_sheet(run_id: str, request: Request, response: Response):
    manifest, reused = publish(request.app.state.archive, run_id)
    if reused:
        response.status_code = 200
    return {"manifest": manifest, "reused": reused}


@router.get("/runs/{run_id}/shift-sheets/active")
def active_shift_sheet(run_id: str, request: Request):
    document = read_active(request.app.state.archive, run_id)
    if document is None:
        return {"document": None}
    return {"document": document}


@router.get("/runs/{run_id}/shift-sheets")
def shift_sheet_history(run_id: str, request: Request):
    return {"sheets": read_history(request.app.state.archive, run_id)}


@router.post("/shift-sheets/{sheet_id}/rerun")
def rerun_shift_sheet(sheet_id: str, request: Request):
    manifest, time = replay(request.app.state.archive, sheet_id)
    return {"manifest": manifest, "verified": True, "replayed_at": time}
