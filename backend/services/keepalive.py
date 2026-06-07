import asyncio
import logging
from typing import Optional

import httpx

from backend.core.config import get_keepalive_config

log = logging.getLogger("qwen2api.keepalive")


class KeepAliveService:
    def __init__(self, config_db):
        self.config_db = config_db
        self._task: Optional[asyncio.Task] = None
        self._url = ""
        self._interval = 0
        self.last_status_code: int | None = None
        self.last_error = ""

    async def _run(self, url: str, interval: int) -> None:
        self._url = url
        self._interval = interval
        log.info("[KeepAlive] 保活任务启动，URL=%s，间隔=%ss", url, interval)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            while True:
                try:
                    response = await client.get(url, timeout=30.0)
                    self.last_status_code = response.status_code
                    self.last_error = ""
                    log.info("[KeepAlive] GET %s -> %s", url, response.status_code)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)
                    log.warning("[KeepAlive] GET %s 失败: %s", url, exc)

                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise

    async def start(self) -> None:
        if self._task and not self._task.done():
            return

        config = await get_keepalive_config(self.config_db)
        url = config["keepalive_url"]
        interval = config["keepalive_interval"]
        if not url:
            log.debug("[KeepAlive] 未配置保活 URL，保活服务不启动")
            return

        self._task = asyncio.create_task(self._run(url, interval), name="qwen2api_keepalive")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            log.info("[KeepAlive] 保活服务已停止")
        self._task = None
        self._url = ""
        self._interval = 0

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "url": self._url,
            "interval": self._interval,
            "last_status_code": self.last_status_code,
            "last_error": self.last_error,
        }
