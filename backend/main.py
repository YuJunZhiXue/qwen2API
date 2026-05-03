import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
import os
import sys

# 将项目根目录加入到 sys.path，解决直接运行 main.py 时找不到 backend 模块的问题
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.account_pool import AccountPool
from backend.core.session_affinity import SessionAffinityStore
from backend.core.upstream_file_cache import UpstreamFileCache
from backend.core.session_lock import SessionLockRegistry
from backend.core.request_logging import configure_logging, request_context
from backend.services.qwen_client import QwenClient
from backend.services.file_store import LocalFileStore
from backend.services.context_offload import ContextOffloader
from backend.services.upstream_file_uploader import UpstreamFileUploader
import backend.api.models as models
from backend.api import admin, v1_chat, probes, anthropic, gemini, embeddings, images, files_api
from backend.services.garbage_collector import garbage_collect_chats
from backend.services.context_cleanup import context_cleanup_loop

configure_logging(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
log = logging.getLogger("qwen2api")


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise

@asynccontextmanager
async def lifespan(app: FastAPI):
    with request_context(surface="startup"):
        log.info("正在启动 qwen2API v2.0 企业网关...")

        # 初始化数据存储 (带锁 JSON)
        app.state.accounts_db = AsyncJsonDB(settings.ACCOUNTS_FILE, default_data=[])
        app.state.users_db = AsyncJsonDB(settings.USERS_FILE, default_data=[])
        app.state.captures_db = AsyncJsonDB(settings.CAPTURES_FILE, default_data=[])
        app.state.session_affinity_db = AsyncJsonDB(settings.CONTEXT_AFFINITY_FILE, default_data=[])
        app.state.context_cache_db = AsyncJsonDB(settings.CONTEXT_CACHE_FILE, default_data=[])
        app.state.uploaded_files_db = AsyncJsonDB(settings.UPLOADED_FILES_FILE, default_data=[])

        # 初始化组件
        app.state.account_pool = AccountPool(app.state.accounts_db, max_inflight=settings.MAX_INFLIGHT_PER_ACCOUNT)
        app.state.qwen_client = QwenClient(app.state.account_pool)
        app.state.qwen_executor = app.state.qwen_client.executor
        app.state.file_store = LocalFileStore(settings.CONTEXT_GENERATED_DIR, app.state.uploaded_files_db)
        app.state.session_affinity = SessionAffinityStore(app.state.session_affinity_db)
        app.state.upstream_file_cache = UpstreamFileCache(app.state.context_cache_db)
        app.state.context_offloader = ContextOffloader(settings)
        app.state.upstream_file_uploader = UpstreamFileUploader(app.state.qwen_client, settings)
        app.state.session_locks = SessionLockRegistry()

        # 加载账号并启动后台清理任务
        await app.state.account_pool.load()
        await app.state.file_store.load()
        await app.state.session_affinity.load()
        await app.state.upstream_file_cache.load()
        asyncio.create_task(garbage_collect_chats(app))
        asyncio.create_task(context_cleanup_loop(app))

        # 启动 chat_id 预热池（省上游 /chats/new 握手 500ms~6s）
        from backend.services.chat_id_pool import ChatIdPool
        app.state.chat_id_pool = ChatIdPool(app.state.qwen_client, target_per_account=5, ttl_seconds=600, default_model="qwen3.6-plus")
        app.state.qwen_executor.chat_id_pool = app.state.chat_id_pool  # 让 executor 直接访问
        await app.state.chat_id_pool.start()

    yield

    with request_context(surface="shutdown"):
        log.info("正在关闭网关服务...")
        # 关闭 chat_id 池
        pool = getattr(app.state, "chat_id_pool", None)
        if pool:
            await pool.stop()
        # 关闭 HTTP 连接池
        await app.state.qwen_client._http_client.aclose()
        log.info("HTTP 连接池已关闭")

app = FastAPI(title="qwen2API Enterprise Gateway", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由
app.include_router(v1_chat.router, tags=["OpenAI Compatible"])
app.include_router(models.router, tags=["Models"])
app.include_router(anthropic.router, tags=["Claude Compatible"])
app.include_router(gemini.router, tags=["Gemini Compatible"])
app.include_router(embeddings.router, tags=["Embeddings"])
app.include_router(images.router, tags=["Images"])
app.include_router(files_api.router, tags=["Files"])
app.include_router(probes.router, tags=["Probes"])
app.include_router(admin.router, prefix="/api/admin", tags=["Dashboard Admin"])

@app.get("/api", tags=["System"])
async def root():
    return {
        "status": "qwen2API Enterprise Gateway is running",
        "docs": "/docs",
        "version": "2.0.0"
    }

# 托管前端构建产物
FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.exists(FRONTEND_DIST):
    app.mount("/", SPAStaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    log.warning(f"未找到前端构建目录: {FRONTEND_DIST}，WebUI 将不可用。")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=settings.PORT, workers=1)
