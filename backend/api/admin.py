import time
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.account_pool import AccountPool, Account

router = APIRouter()


def verify_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split("Bearer ", 1)[1]

    from backend.core.config import API_KEYS, settings as backend_settings
    if token != backend_settings.ADMIN_KEY and token not in API_KEYS:
        raise HTTPException(status_code=403, detail="Forbidden: Admin Key Mismatch")
    return token


class UserCreate(BaseModel):
    name: str
    quota: int = 1000000


@router.get("/status", dependencies=[Depends(verify_admin)])
async def get_system_status(request: Request):
    pool = request.app.state.account_pool
    engine = getattr(request.app.state, "gateway_engine", request.app.state.browser_engine)
    browser_engine = getattr(request.app.state, "browser_engine", None)
    httpx_engine = getattr(request.app.state, "httpx_engine", None)

    if hasattr(engine, "status"):
        engine_info = engine.status()
    elif hasattr(engine, "_pages") and hasattr(engine, "pool_size"):
        free_pages = engine._pages.qsize()
        in_use = engine.pool_size - free_pages
        engine_info = {
            "started": engine._started,
            "mode": "browser",
            "pool_size": engine.pool_size,
            "free_pages": free_pages,
            "queue": in_use if in_use > 0 else 0,
        }
    else:
        engine_info = {
            "started": getattr(engine, "_started", False),
            "mode": "httpx",
            "pool_size": 0,
            "free_pages": 0,
            "queue": 0,
        }

    browser_info = {
        "started": getattr(browser_engine, "_started", False),
        "pool_size": getattr(browser_engine, "pool_size", 0),
        "free_pages": browser_engine._pages.qsize() if getattr(browser_engine, "_pages", None) is not None else 0,
        "queue": max(0, getattr(browser_engine, "pool_size", 0) - (browser_engine._pages.qsize() if getattr(browser_engine, "_pages", None) is not None else 0)),
    } if browser_engine else {"started": False, "pool_size": 0, "free_pages": 0, "queue": 0}

    httpx_info = {
        "started": getattr(httpx_engine, "_started", False),
        "mode": "httpx",
    } if httpx_engine else {"started": False, "mode": "httpx"}

    return {
        "accounts": pool.status(),
        "engine_mode": settings.ENGINE_MODE,
        "browser_engine": browser_info,
        "httpx_engine": httpx_info,
        "hybrid_engine": engine_info if settings.ENGINE_MODE == "hybrid" else None,
    }


@router.get("/users", dependencies=[Depends(verify_admin)])
async def list_users(request: Request):
    db: AsyncJsonDB = request.app.state.users_db
    return {"users": await db.get()}


@router.post("/users", dependencies=[Depends(verify_admin)])
async def create_user(user: UserCreate, request: Request):
    import uuid
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    new_user = {
        "id": f"sk-{uuid.uuid4().hex}",
        "name": user.name,
        "quota": user.quota,
        "used_tokens": 0,
    }
    data.append(new_user)
    await db.save(data)
    return new_user


@router.post("/accounts", dependencies=[Depends(verify_admin)])
async def add_account(request: Request):
    import time
    from backend.services.qwen_client import QwenClient

    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, detail="Invalid JSON body")

    token = data.get("token", "")
    if not token:
        raise HTTPException(400, detail="token is required")

    acc = Account(
        email=data.get("email", f"manual_{int(time.time())}@qwen"),
        password=data.get("password", ""),
        token=token,
        cookies=data.get("cookies", ""),
        username=data.get("username", ""),
    )

    is_valid = await client.verify_token(token)
    if not is_valid:
        acc.valid = False
        acc.status_code = "auth_error"
        acc.last_error = "Token ??????"
        return {"ok": False, "error": acc.last_error, "message": acc.last_error}

    acc.valid = True
    acc.activation_pending = False
    acc.status_code = "valid"
    acc.last_error = ""
    await pool.add(acc)
    return {"ok": True, "email": acc.email, "message": "????????"}


@router.get("/accounts", dependencies=[Depends(verify_admin)])
async def list_accounts(request: Request):
    pool: AccountPool = request.app.state.account_pool
    accounts = []
    for a in pool.accounts:
        item = a.to_dict()
        item["valid"] = a.valid
        item["inflight"] = a.inflight
        item["rate_limited_until"] = a.rate_limited_until
        item["status_code"] = a.get_status_code()
        item["status_text"] = a.get_status_text()
        item["last_error"] = a.last_error
        accounts.append(item)
    return {"accounts": accounts}


@router.post("/accounts/register-verify", dependencies=[Depends(verify_admin)])
async def verify_register_secret(request: Request):
    """验证注册解锁密码，正确则前端显示一键注册按钮。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    secret = body.get("secret", "")
    expected = settings.REGISTER_SECRET
    if not expected:
        return {"ok": False, "error": "register secret not configured"}
    return {"ok": (secret == expected)}


@router.post("/accounts/register", dependencies=[Depends(verify_admin)])
async def register_new_account(request: Request):
    import logging
    from backend.core.config import resolve_model
    from backend.services.auth_resolver import activate_account as activate_logic, register_qwen_account
    from backend.services.qwen_client import QwenClient

    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client
    log = logging.getLogger("backend.api.admin")

    client_ip = request.client.host if request.client else "127.0.0.1"
    log.info(f"[??] ?????????? IP?{client_ip}")

    if len(pool.accounts) >= 100:
        return {"ok": False, "error": "??????????????"}

    try:
        acc = await register_qwen_account()
        if not acc:
            return {"ok": False, "error": "??????????????????"}

        activated_during_register = False
        readiness_error = ""
        chat_id = None
        try:
            chat_id = await client.create_chat(acc.token, resolve_model("qwen"))
        except Exception as e:
            readiness_error = str(e)
        finally:
            if chat_id:
                try:
                    await client.delete_chat(acc.token, chat_id)
                except Exception:
                    pass

        if readiness_error:
            err_lower = readiness_error.lower()
            if any(k in err_lower for k in ("pending activation", "please check your email", "not activated")):
                log.warning(f"[Register] {acc.email} registered but not ready yet: {readiness_error}")
                acc.valid = False
                acc.activation_pending = True
                acc.status_code = "pending_activation"
                acc.last_error = "???????????"
                activated_during_register = await activate_logic(acc)
                if not activated_during_register:
                    await pool.add(acc)
                    log.info(f"[Register] Pending account added to pool for manual activation: {acc.email}")
                    return {
                        "ok": True,
                        "email": acc.email,
                        "activation_pending": True,
                        "message": "???????????",
                        "error": acc.last_error,
                    }
            elif any(k in err_lower for k in ("unauthorized", "forbidden", "401", "403", "token", "login")):
                log.warning(f"[Register] {acc.email} rejected by upstream right after registration: {readiness_error}")
                return {
                    "ok": False,
                    "email": acc.email,
                    "error": "???????????????????????",
                }
            else:
                log.warning(f"[Register] readiness check skipped for {acc.email}: {readiness_error}")

        acc.valid = True
        acc.activation_pending = False
        acc.status_code = "valid"
        acc.last_error = ""
        await pool.add(acc)
        message = "?????????????"
        if activated_during_register:
            message = "??????????????????"
        return {"ok": True, "email": acc.email, "message": message}
    except Exception as e:
        return {"ok": False, "error": f"????: {str(e)}"}


@router.post("/verify", dependencies=[Depends(verify_admin)])
async def verify_all_accounts(request: Request):
    """?????????"""
    import asyncio
    import logging
    from backend.services.qwen_client import QwenClient
    from backend.core.config import settings as backend_settings

    log = logging.getLogger("qwen2api.admin")
    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client

    concurrency = max(1, min(len(pool.accounts) or 1, max(2, backend_settings.BROWSER_POOL_SIZE)))
    sem = asyncio.Semaphore(concurrency)

    async def verify_one(acc: Account):
        async with sem:
            is_valid = await client.verify_token(acc.token)
            refreshed = False
            if not is_valid and acc.password:
                log.info(f"[??] {acc.email} Token ?????????...")
                refreshed = await client.auth_resolver.refresh_token(acc)
                is_valid = refreshed or is_valid

            acc.valid = is_valid
            if is_valid:
                acc.activation_pending = False
                acc.status_code = "valid"
                acc.last_error = ""
            elif acc.activation_pending:
                acc.status_code = "pending_activation"
            elif acc.get_status_code() != "rate_limited":
                acc.status_code = acc.status_code or "auth_error"
                if not acc.last_error:
                    acc.last_error = "???????????"

            return {
                "email": acc.email,
                "valid": is_valid,
                "refreshed": refreshed,
                "status_code": acc.get_status_code(),
                "status_text": acc.get_status_text(),
                "error": acc.last_error,
            }

    results = await asyncio.gather(*(verify_one(acc) for acc in pool.accounts))
    await pool.save()
    return {"ok": True, "results": results, "concurrency": concurrency}


@router.post("/accounts/{email}/activate", dependencies=[Depends(verify_admin)])
async def activate_account(email: str, request: Request):
    """?????????"""
    from backend.services.auth_resolver import activate_account as activate_logic

    pool: AccountPool = request.app.state.account_pool
    acc = next((a for a in pool.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    started_at = float(getattr(acc, "_activation_started_at", 0) or 0)
    if getattr(acc, "_is_activating", False):
        if started_at and (time.time() - started_at) < 90:
            return {"ok": True, "pending": True, "message": "账号正在激活中，请稍后刷新"}
        setattr(acc, "_is_activating", False)
        setattr(acc, "_activation_started_at", 0)

    try:
        setattr(acc, "_is_activating", True)
        success = await activate_logic(acc)
        if success:
            acc.valid = True
            acc.activation_pending = False
            acc.status_code = "valid"
            acc.last_error = ""
            await pool.add(acc)
            return {"ok": True, "message": "??????"}

        if acc.activation_pending:
            acc.status_code = "pending_activation"
        elif acc.status_code not in ("banned", "rate_limited"):
            acc.status_code = acc.status_code or "auth_error"
        if not acc.last_error:
            acc.last_error = "????????????"
        await pool.save()
        return {"ok": False, "error": acc.last_error, "message": acc.last_error}
    finally:
        setattr(acc, "_is_activating", False)


@router.post("/accounts/{email}/verify", dependencies=[Depends(verify_admin)])
async def verify_account(email: str, request: Request):
    """?????????"""
    import logging
    from backend.services.qwen_client import QwenClient

    log = logging.getLogger("qwen2api.admin")
    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client

    acc = next((a for a in pool.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    is_valid = await client.verify_token(acc.token)
    refreshed = False
    if not is_valid and acc.password:
        log.info(f"[??] {acc.email} Token ?????????...")
        refreshed = await client.auth_resolver.refresh_token(acc)
        is_valid = refreshed or is_valid

    acc.valid = is_valid
    if is_valid:
        acc.activation_pending = False
        acc.status_code = "valid"
        acc.last_error = ""
    elif acc.activation_pending:
        acc.status_code = "pending_activation"
        if not acc.last_error:
            acc.last_error = "??????"
    elif acc.get_status_code() != "rate_limited":
        acc.status_code = acc.status_code or "auth_error"
        if not acc.last_error:
            acc.last_error = "???????????"

    await pool.save()
    return {
        "email": acc.email,
        "valid": is_valid,
        "refreshed": refreshed,
        "status_code": acc.get_status_code(),
        "status_text": acc.get_status_text(),
        "error": acc.last_error,
    }


@router.delete("/accounts/{email}", dependencies=[Depends(verify_admin)])
async def delete_account(email: str, request: Request):
    pool: AccountPool = request.app.state.account_pool
    await pool.remove(email)
    return {"ok": True}


@router.get("/settings", dependencies=[Depends(verify_admin)])
async def get_settings():
    from backend.core.config import MODEL_MAP
    from backend.core.config import settings as backend_settings

    return {
        "version": "2.0.0",
        "max_inflight_per_account": backend_settings.MAX_INFLIGHT_PER_ACCOUNT,
        "engine_mode": backend_settings.ENGINE_MODE,
        "model_aliases": {k: v for k, v in MODEL_MAP.items()},
    }


@router.put("/settings", dependencies=[Depends(verify_admin)])
async def update_settings(data: dict, request: Request):
    from backend.core.config import MODEL_MAP
    if "max_inflight_per_account" in data:
        value = int(data["max_inflight_per_account"])
        settings.MAX_INFLIGHT_PER_ACCOUNT = value
        request.app.state.account_pool.max_inflight = value
    if "engine_mode" in data and data["engine_mode"] in ("httpx", "browser", "hybrid"):
        settings.ENGINE_MODE = data["engine_mode"]
    if "model_aliases" in data:
        MODEL_MAP.clear()
        MODEL_MAP.update(data["model_aliases"])
    return {"ok": True}


@router.get("/keys", dependencies=[Depends(verify_admin)])
async def get_keys():
    from backend.core.config import API_KEYS
    return {"keys": list(API_KEYS)}


@router.post("/keys", dependencies=[Depends(verify_admin)])
async def generate_key():
    import uuid
    from backend.core.config import API_KEYS, save_api_keys
    new_key = f"sk-qwen-{uuid.uuid4().hex[:20]}"
    API_KEYS.add(new_key)
    save_api_keys(API_KEYS)
    return {"ok": True, "key": new_key}


@router.delete("/keys/{key}", dependencies=[Depends(verify_admin)])
async def delete_key(key: str):
    from backend.core.config import API_KEYS, save_api_keys
    if key in API_KEYS:
        API_KEYS.remove(key)
        save_api_keys(API_KEYS)
    return {"ok": True}
