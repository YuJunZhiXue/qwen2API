from fastapi import APIRouter, Body, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from backend.core.config import (
    KEEPALIVE_MAX_INTERVAL,
    KEEPALIVE_MIN_INTERVAL,
    get_keepalive_config,
    settings,
    update_keepalive_config,
)
from backend.core.database import AsyncJsonDB
from backend.core.account_pool import AccountPool, Account
import secrets

router = APIRouter()

def verify_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split("Bearer ")[1]

    from backend.core.config import API_KEYS, settings as backend_settings

    # 允许使用默认管理员 Key (ADMIN_KEY) 或者任何已生成的 API_KEYS 作为管理凭证
    if token != backend_settings.ADMIN_KEY and token not in API_KEYS:
        raise HTTPException(status_code=403, detail="Forbidden: Admin Key Mismatch")
    return token

class UserCreate(BaseModel):
    name: str
    quota: int = 1000000

class User(BaseModel):
    id: str
    name: str
    quota: int
    used_tokens: int


class ApiKeyCreate(BaseModel):
    mode: str = "auto"
    key: str = ""

@router.get("/status", dependencies=[Depends(verify_admin)])
async def get_system_status(request: Request):
    pool = request.app.state.account_pool

    # 账号层细粒度 inflight / 状态
    per_account = []
    for acc in getattr(pool, "accounts", []):
        per_account.append({
            "email": acc.email,
            "status": acc.get_status_code(),
            "inflight": getattr(acc, "inflight", 0),
            "max_inflight": getattr(pool, "max_inflight_per_account", 0),
            "consecutive_failures": getattr(acc, "consecutive_failures", 0),
            "rate_limit_strikes": getattr(acc, "rate_limit_strikes", 0),
            "last_request_finished": getattr(acc, "last_request_finished", 0),
        })

    # chat_id 预热池指标（若已启用）
    chat_id_pool_stats = None
    cp = getattr(request.app.state, "chat_id_pool", None)
    if cp is not None:
        try:
            per_account_pool: dict[str, int] = {}
            for acc in getattr(pool, "accounts", []):
                per_account_pool[acc.email] = await cp.size(acc.email)
            chat_id_pool_stats = {
                "total_cached": await cp.total_size(),
                "target_per_account": cp._target,
                "ttl_seconds": cp._ttl,
                "per_account": per_account_pool,
            }
        except Exception:
            chat_id_pool_stats = {"error": "snapshot failed"}

    # 向运行时拿全局任务计数 / asyncio 状态
    import asyncio
    try:
        tasks = asyncio.all_tasks()
        running_tasks = sum(1 for t in tasks if not t.done())
    except Exception:
        running_tasks = -1

    return {
        "accounts": pool.status(),
        "per_account": per_account,
        "chat_id_pool": chat_id_pool_stats,
        "runtime": {
            "asyncio_running_tasks": running_tasks,
        },
        "request_runtime": {
            "mode": "direct_http",
            "browser_required_for_requests": False,
            "description": "普通请求直连 HTTP，不经过浏览器",
        },
        "browser_automation": {
            "mode": "on_demand_registration_only",
            "description": "仅注册/激活/刷新 Token 时按需启动真实浏览器",
        }
    }

@router.get("/users", dependencies=[Depends(verify_admin)])
async def list_users(request: Request):
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    return {"users": data}

@router.post("/users", dependencies=[Depends(verify_admin)])
async def create_user(user: UserCreate, request: Request):
    import uuid
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    new_user = {
        "id": f"sk-{uuid.uuid4().hex}",
        "name": user.name,
        "quota": user.quota,
        "used_tokens": 0
    }
    data.append(new_user)
    await db.save(data)
    return new_user

@router.post("/accounts", dependencies=[Depends(verify_admin)])
async def add_account(request: Request):
    import time
    from backend.core.account_pool import Account, AccountPool
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
        username=data.get("username", "")
    )

    is_valid = await client.verify_token(token)
    if not is_valid:
        return {"ok": False, "error": "Invalid token (验证失败，请确认Token有效)"}

    await pool.add(acc)
    return {"ok": True, "email": acc.email}


@router.get("/accounts", dependencies=[Depends(verify_admin)])
async def list_accounts(request: Request):
    pool: AccountPool = request.app.state.account_pool
    # 模拟原始 FastAPI 序列化，包含运行时状态
    accs = []
    for a in pool.accounts:
        d = a.to_dict()
        d["valid"] = a.valid
        d["inflight"] = a.inflight
        d["rate_limited_until"] = a.rate_limited_until
        accs.append(d)
    return {"accounts": accs}

@router.post("/accounts/register", dependencies=[Depends(verify_admin)])
async def register_new_account(request: Request):
    """一键调用浏览器无头注册新千问账号"""
    import logging
    from backend.services.auth_resolver import register_qwen_account
    from backend.core.account_pool import AccountPool
    pool: AccountPool = request.app.state.account_pool

    log = logging.getLogger("backend.api.admin")

    client_ip = request.client.host if request.client else "127.0.0.1"
    log.info(f"[注册] 管理员触发注册，来源IP: {client_ip}")

    # 简单的频率限制保护
    current = len(pool.accounts)
    if current >= 100:
        return {"ok": False, "error": "账号池已满，请先清理死号"}

    try:
        acc = await register_qwen_account()
        if acc:
            await pool.add(acc)
            log.info(f"[注册] 注册成功: {acc.email}（当前账号数: {len(pool.accounts)}/100）")
            return {"ok": True, "email": acc.email, "message": "新账号注册成功并已入池"}
        return {"ok": False, "error": "自动化注册失败，可能遇到风控或页面元素改变"}
    except Exception as e:
        return {"ok": False, "error": f"注册发生异常: {str(e)}"}

@router.post("/verify", dependencies=[Depends(verify_admin)])
async def verify_all_accounts(request: Request):
    """逐个到 chat.qwen.ai 官网验证账号；token 失效时自动刷新。"""
    from backend.core.account_pool import AccountPool
    from backend.services.qwen_client import QwenClient

    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client

    results = []
    for acc in pool.accounts:
        results.append(await client.verify_account(acc))

    summary = {
        "total": len(results),
        "valid": sum(1 for item in results if item.get("valid")),
        "refreshed": sum(1 for item in results if item.get("refreshed")),
        "banned": sum(1 for item in results if item.get("status_code") == "banned"),
        "failed": sum(1 for item in results if not item.get("valid")),
    }
    return {"ok": True, "results": results, "summary": summary, "concurrency": 1}

@router.post("/accounts/{email}/activate", dependencies=[Depends(verify_admin)])
async def activate_account(email: str, request: Request):
    """单独激活某个账号"""
    from backend.services.auth_resolver import activate_account as activate_logic
    from backend.core.account_pool import AccountPool

    pool: AccountPool = request.app.state.account_pool
    acc = next((a for a in pool.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    # 防止并发点击：检查一个运行时标志
    if getattr(acc, "_is_activating", False):
        return {"ok": False, "error": "该账号正在激活中，请勿重复点击"}

    try:
        setattr(acc, "_is_activating", True)
        success = await activate_logic(acc)
        if success:
            acc.valid = True
            acc.activation_pending = False
            await pool.add(acc) # 这会触发覆盖保存
            return {"ok": True, "message": "账号激活成功"}
        return {"ok": False, "error": "未能找到激活链接或获取Token"}
    finally:
        setattr(acc, "_is_activating", False)

@router.post("/accounts/{email}/verify", dependencies=[Depends(verify_admin)])
async def verify_account(email: str, request: Request):
    """单独到 chat.qwen.ai 官网验证账号；token 失效时自动刷新。"""
    from backend.services.qwen_client import QwenClient
    from backend.core.account_pool import AccountPool

    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client

    acc = next((a for a in pool.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    return await client.verify_account(acc)

@router.delete("/accounts/{email}", dependencies=[Depends(verify_admin)])
async def delete_account(email: str, request: Request):
    from backend.core.account_pool import AccountPool
    pool: AccountPool = request.app.state.account_pool
    acc = next((a for a in pool.accounts if a.email == email), None)
    if acc and getattr(acc, "source", "") == "env":
        raise HTTPException(status_code=400, detail="环境变量注入账号不能在面板删除，请移除对应环境变量后重启服务")
    await pool.remove(email)
    return {"ok": True}

@router.get("/settings", dependencies=[Depends(verify_admin)])
async def get_settings(request: Request):
    from backend.core.config import MODEL_MAP
    from backend.core.config import settings as backend_settings

    safe_map = {k: v for k, v in MODEL_MAP.items()}
    pool = getattr(request.app.state, "chat_id_pool", None)
    acc_pool = getattr(request.app.state, "account_pool", None)
    keepalive_config = await get_keepalive_config(request.app.state.config_db)
    keepalive_service = getattr(request.app.state, "keepalive_service", None)
    return {
        "version": "2.0.0",
        "max_inflight_per_account": backend_settings.MAX_INFLIGHT_PER_ACCOUNT,
        "global_max_inflight": getattr(acc_pool, "global_max_inflight", 0),
        "max_queue_size": getattr(acc_pool, "max_queue_size", 0),
        "account_ready_set_threshold": backend_settings.ACCOUNT_READY_SET_THRESHOLD,
        "account_ready_set_enabled": getattr(acc_pool, "ready_set_enabled", False),
        "chat_id_pool_target": pool.target if pool else 0,
        "chat_id_pool_ttl_seconds": pool.ttl if pool else 0,
        "chat_id_pool_max_concurrency": pool.max_concurrency if pool else 0,
        "keepalive_url": keepalive_config["keepalive_url"],
        "keepalive_interval": keepalive_config["keepalive_interval"],
        "keepalive_env_locked": keepalive_config["env_locked"],
        "keepalive_running": keepalive_service.is_running if keepalive_service else False,
        "keepalive_status": keepalive_service.status() if keepalive_service else {},
        "model_aliases": safe_map,
    }

@router.put("/settings", dependencies=[Depends(verify_admin)])
async def update_settings(data: dict, request: Request):
    from backend.core.config import MODEL_MAP
    if "max_inflight_per_account" in data:
        try:
            val = int(data["max_inflight_per_account"])
            settings.MAX_INFLIGHT_PER_ACCOUNT = val
            pool = getattr(request.app.state, "account_pool", None)
            if pool is not None and hasattr(pool, "set_max_inflight"):
                pool.set_max_inflight(val)
        except (TypeError, ValueError):
            pass
    if "account_ready_set_threshold" in data:
        try:
            val = max(1, int(data["account_ready_set_threshold"]))
            settings.ACCOUNT_READY_SET_THRESHOLD = val
            pool = getattr(request.app.state, "account_pool", None)
            if pool is not None and hasattr(pool, "_reset_concurrency_limits"):
                pool._reset_concurrency_limits()
        except (TypeError, ValueError):
            pass
    if "global_max_inflight" in data:
        try:
            val = int(data["global_max_inflight"])
            pool = getattr(request.app.state, "account_pool", None)
            if pool is not None and val > 0:
                pool.global_max_inflight = val
        except (TypeError, ValueError):
            pass
    if "chat_id_pool_target" in data or "chat_id_pool_ttl_seconds" in data or "chat_id_pool_max_concurrency" in data:
        cp = getattr(request.app.state, "chat_id_pool", None)
        if cp is not None:
            await cp.apply_config(
                target=data.get("chat_id_pool_target"),
                ttl_seconds=data.get("chat_id_pool_ttl_seconds"),
                max_concurrency=data.get("chat_id_pool_max_concurrency"),
            )
    if "keepalive_url" in data or "keepalive_interval" in data:
        if "keepalive_url" in data and not isinstance(data["keepalive_url"], str):
            raise HTTPException(status_code=400, detail="保活 URL 必须是字符串")
        if "keepalive_interval" in data:
            try:
                interval = int(data["keepalive_interval"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="保活间隔必须是有效整数")
            if interval < KEEPALIVE_MIN_INTERVAL or interval > KEEPALIVE_MAX_INTERVAL:
                raise HTTPException(status_code=400, detail="保活间隔必须在 5 - 86400 秒之间")
            data["keepalive_interval"] = interval
        await update_keepalive_config(request.app.state.config_db, data)
        keepalive_service = getattr(request.app.state, "keepalive_service", None)
        if keepalive_service is not None:
            await keepalive_service.restart()
    if "model_aliases" in data:
        MODEL_MAP.clear()
        MODEL_MAP.update(data["model_aliases"])
    return {"ok": True}

@router.get("/keys", dependencies=[Depends(verify_admin)])
async def get_keys():
    from backend.core.config import list_api_key_items

    items = list_api_key_items()
    return {"keys": [item["key"] for item in items], "items": items}

@router.post("/keys", dependencies=[Depends(verify_admin)])
async def create_key(payload: ApiKeyCreate | None = Body(default=None)):
    from backend.core.config import API_KEYS, add_api_key

    mode = (payload.mode if payload else "auto").strip().lower()
    if mode == "custom":
        new_key = (payload.key if payload else "").strip()
        if not new_key:
            raise HTTPException(status_code=400, detail="自定义 Key 不能为空")
        if any(ch.isspace() for ch in new_key):
            raise HTTPException(status_code=400, detail="自定义 Key 不能包含空白字符")
    else:
        new_key = f"sk-{secrets.token_hex(24)}"

    if new_key in API_KEYS or not add_api_key(new_key):
        raise HTTPException(status_code=409, detail="API Key 已存在")
    return {"ok": True, "key": new_key}

@router.delete("/keys/{key}", dependencies=[Depends(verify_admin)])
async def delete_key(key: str):
    from backend.core.config import remove_api_key

    result = remove_api_key(key)
    if result == "env":
        raise HTTPException(status_code=400, detail="环境变量注入 Key 不能在面板删除，请移除对应环境变量后重启服务")
    return {"ok": True}
