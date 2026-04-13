from fastapi import APIRouter, Request, Depends, HTTPException
from backend.api.admin import verify_admin
from backend.core.database import AsyncJsonDB

router = APIRouter()

@router.get("/healthz")
async def healthz():
    return {"status": "ok"}

@router.get("/readyz")
async def readyz(request: Request):
    gateway_engine = getattr(request.app.state, "gateway_engine", None)
    browser_engine = getattr(request.app.state, "browser_engine", None)
    if gateway_engine and getattr(gateway_engine, "_started", False):
        if getattr(getattr(request.app.state, "gateway_engine", None), "__class__", type("", (), {})).__name__ == "HybridEngine":
            if browser_engine and getattr(browser_engine, "_started", False):
                return {"status": "ready"}
            raise HTTPException(status_code=503, detail="browser engine not ready")
        return {"status": "ready"}
    raise HTTPException(status_code=503, detail="gateway not ready")

@router.get("/admin/dev/captures", dependencies=[Depends(verify_admin)])
async def get_captures(request: Request):
    db: AsyncJsonDB = request.app.state.captures_db
    return {"captures": await db.get()}

@router.delete("/admin/dev/captures", dependencies=[Depends(verify_admin)])
async def clear_captures(request: Request):
    db: AsyncJsonDB = request.app.state.captures_db
    await db.save([])
    return {"status": "cleared"}
