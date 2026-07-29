import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator

import httpx
from playwright.async_api import async_playwright

from backend.core.account_pool import AccountPool
from backend.services.auth_resolver import BASE_URL, AuthResolver
from backend.upstream.payload_builder import build_chat_payload
from backend.upstream.qwen_executor import QwenExecutor
from backend.upstream.sse_consumer import parse_sse_chunk

log = logging.getLogger("qwen2api.client")

# Cookie 持久化文件
_COOKIE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "browser_cookies.json")

# 流式请求超时（真流式：只约束"连接"和"两次数据块之间的最大空闲"，
# 不再对整个响应设硬上限，避免长任务/agent 工作跑到一半被整体超时杀掉）。
_CHAT_STREAM_CONNECT_TIMEOUT = float(os.getenv("QWEN_STREAM_CONNECT_TIMEOUT", "30"))
_CHAT_STREAM_IDLE_TIMEOUT = float(os.getenv("QWEN_STREAM_IDLE_TIMEOUT", "180"))


class QwenClient:
    def __init__(self, account_pool: AccountPool):
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool) if account_pool is not None else None
        self.executor = QwenExecutor(self, account_pool)

        # httpx 用于非流式 API 调用（不会被 WAF 拦截）
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=10, keepalive_expiry=30.0)
        timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)
        self._http_client = httpx.AsyncClient(
            limits=limits, timeout=timeout, http2=False, follow_redirects=True,
        )

        # Playwright 用于流式聊天请求（httpx 会被 WAF 拦截）
        self._pw_playwright = None
        self._pw_browser = None
        self._pw_context = None
        self._pw_main_page = None  # 长期有效的已通过 WAF 挑战的主页面
        self._pw_init_lock = asyncio.Lock()
        self._waf_cleared = False  # 标记 WAF 是否已通过

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 关闭 HTTP 连接池前保存 cookie
        await self._save_cookies()
        if self._http_client:
            await self._http_client.aclose()
        if self._pw_main_page:
            await self._pw_main_page.close()
        if self._pw_context:
            await self._pw_context.close()
        if self._pw_browser:
            await self._pw_browser.close()
        if self._pw_playwright:
            await self._pw_playwright.stop()
        return False

    async def _ensure_browser(self):
        """延迟初始化 Playwright 浏览器（仅流式请求时使用）。
        
        环境变量 BROWSER_HEADED=1 时打开可见窗口，用于手动 captcha 求解。
        Cookie 自动持久化到 data/browser_cookies.json，重启后保留登录态。
        
        使用 asyncio.Lock 防止并发请求同时初始化浏览器。
        """
        if self._pw_context is not None:
            return self._pw_context
        
        async with self._pw_init_lock:
            # 双重检查：获取锁后再次检查是否已被其他协程初始化
            if self._pw_context is not None:
                return self._pw_context
            
            headed = os.getenv("BROWSER_HEADED", "").lower() in ("1", "true", "yes")
            self._pw_playwright = await async_playwright().start()
            self._pw_browser = await self._pw_playwright.chromium.launch(
                headless=not headed,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            import os as _os
            _proxy = _os.getenv("PLAYWRIGHT_PROXY") or _os.getenv("HTTP_PROXY") or _os.getenv("HTTPS_PROXY")
            _ctx_kwargs = {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            if _proxy:
                _ctx_kwargs["proxy"] = {"server": _proxy}
                log.info("[QwenClient] Playwright 代理已配置: %s", _proxy)
            self._pw_context = await self._pw_browser.new_context(**_ctx_kwargs)
            await self._pw_context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """)
            
            # 恢复持久化的 cookie
            await self._load_cookies()
            
            log.info(f"[QwenClient] Playwright 浏览器已初始化 (headed={headed})")
            if headed:
                log.info(f"[QwenClient] 浏览器窗口已打开，请在浏览器中手动登录 Qwen 并完成验证码")
                log.info(f"[QwenClient] 访问 {BASE_URL}/ 并设置 token 到 localStorage")
            return self._pw_context
    
    async def _load_cookies(self):
        """从磁盘加载持久化的 cookie。"""
        try:
            if os.path.exists(_COOKIE_FILE):
                with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                if cookies:
                    await self._pw_context.add_cookies(cookies)
                    log.info(f"[QwenClient] 已恢复 {len(cookies)} 个持久化 cookie")
        except Exception as e:
            log.warning(f"[QwenClient] 加载 cookie 失败: {e}")
    
    async def _save_cookies(self):
        """将当前 context 的 cookie 持久化到磁盘。"""
        try:
            if self._pw_context is None:
                return
            cookies = await self._pw_context.cookies()
            os.makedirs(os.path.dirname(_COOKIE_FILE), exist_ok=True)
            with open(_COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            log.info(f"[QwenClient] 已持久化 {len(cookies)} 个 cookie 到 {_COOKIE_FILE}")
        except Exception as e:
            log.warning(f"[QwenClient] 保存 cookie 失败: {e}")

    @staticmethod
    def _load_bx_pool() -> list[dict]:
        """加载 bx_pool.json（多 bx-ua 轮换池，降低风控概率）。"""
        import os, json as _json, time as _time
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            pool_path = os.path.join(project_root, "bx_pool.json")
            now = _time.time()
            cache = getattr(QwenClient, "_bx_pool_cache", None)
            if cache and now - cache["loaded_at"] < 60:
                return cache["pool"]
            if not os.path.exists(pool_path):
                QwenClient._bx_pool_cache = {"pool": [], "loaded_at": now}
                return []
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = _json.load(f)
            QwenClient._bx_pool_cache = {"pool": pool, "loaded_at": now}
            return pool
        except Exception:
            return []

    @staticmethod
    def _build_headers(token: str) -> dict[str, str]:
        """构建 httpx 请求头（含 bx 反爬头）。"""
        import os, uuid, random
        pool = QwenClient._load_bx_pool()
        entry = random.choice(pool) if pool else {}
        ua = entry.get("user_agent") or os.getenv(
            "QWEN_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        )
        bx_ua = entry.get("bx_ua") or os.getenv("QWEN_BX_UA", "")
        bx_umid = entry.get("bx_umidtoken") or os.getenv("QWEN_BX_UMIDTOKEN", "")
        bx_v = entry.get("bx_v") or os.getenv("QWEN_BX_V", "")
        version = entry.get("web_version") or os.getenv("QWEN_WEB_VERSION", "")
        timezone = entry.get("timezone") or os.getenv("QWEN_TIMEZONE", "")

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{BASE_URL}/",
            "Origin": BASE_URL,
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "source": "web",
            "X-Request-Id": str(uuid.uuid4()),
        }
        if bx_ua:
            headers["bx-ua"] = bx_ua
        if bx_umid:
            headers["bx-umidtoken"] = bx_umid
        if bx_v:
            headers["bx-v"] = bx_v
        if version:
            headers["Version"] = version
        if timezone:
            headers["Timezone"] = timezone
        # 注入浏览器 cookie，降低 WAF 拦截概率（与网页会话对齐）
        try:
            if os.path.exists(_COOKIE_FILE):
                with open(_COOKIE_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                if isinstance(cookies, list) and cookies:
                    cookie_str = "; ".join(
                        f"{c.get('name')}={c.get('value')}"
                        for c in cookies
                        if c.get("name") and c.get("value")
                    )
                    if cookie_str:
                        headers["Cookie"] = cookie_str
        except Exception:
            pass
        return headers

    # ========================================================================
    # 非流式 API 请求：使用 httpx（WAF 通常不拦截 GET/DELETE，POST 尝试后降级）
    # ========================================================================
    async def _request_json(self, method: str, path: str, token: str, body: dict | None = None, timeout: float = 30.0) -> dict:
        """发送非流式 API 请求。

        使用 httpx 发送所有非流式请求（create_chat, list_chats, delete_chat 等），
        旧的 page.evaluate(fetch) 方式已被 Qwen BaXia 拦截导致挂起。
        流式请求（chat/completions）走独立的 _browser_request_json。

        如果 httpx 返回 WAF 挑战页面，尝试浏览器备用路径。
        """
        try:
            resp = await self._http_client.request(
                method,
                f"{BASE_URL}{path}",
                headers=self._build_headers(token),
                json=body,
                timeout=timeout,
            )
            body_text = resp.text
            status = resp.status_code

            # 检测 WAF 拦截
            is_waf, waf_type = self._is_waf_challenge(body_text)
            if is_waf:
                log.warning("[QwenClient] httpx 请求被 WAF 拦截 (type=%s, status=%d, path=%s)", waf_type, status, path)
                # 如果浏览器已初始化且通过了 WAF，尝试用浏览器 API 重试
                if self._pw_context is not None and self._waf_cleared:
                    log.info("[QwenClient] 尝试通过浏览器 ApiRequest 重试...")
                    browser_result = await self._browser_request_json(
                        method, f"{BASE_URL}{path}", token, body=body, timeout=timeout
                    )
                    return browser_result
                # 否则返回 WAF 错误
                return {"status": status, "body": body_text, "waf": waf_type}

            return {"status": status, "body": body_text}
        except httpx.TimeoutException:
            log.warning("[QwenClient] httpx 请求超时 (path=%s, timeout=%s)", path, timeout)
            # 超时后尝试浏览器路径
            if self._pw_context is not None and self._waf_cleared:
                log.info("[QwenClient] httpx 超时，尝试通过浏览器 ApiRequest 重试...")
                browser_result = await self._browser_request_json(
                    method, f"{BASE_URL}{path}", token, body=body, timeout=timeout
                )
                return browser_result
            return {"status": 0, "body": f"httpx timeout ({timeout}s)"}
        except Exception as e:
            log.warning(f"[QwenClient] _request_json 异常: {e}")
            return {"status": 0, "body": str(e)}

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t") -> str:
        return await self.executor.create_chat(token, model, chat_type=chat_type)

    async def delete_chat(self, token: str, chat_id: str):
        await self._request_json("DELETE", f"/api/v2/chats/{chat_id}", token, timeout=20.0)

    async def list_chats(self, token: str, limit: int = 50) -> list[dict]:
        res = await self._request_json("GET", f"/api/v2/chats?limit={limit}", token, timeout=20.0)
        if res["status"] != 200:
            return []
        try:
            data = json.loads(res.get("body", "{}"))
        except Exception:
            return []
        chats = data.get("data", [])
        return chats if isinstance(chats, list) else []

    async def get_chat(self, token: str, chat_id: str) -> dict:
        res = await self._request_json("GET", f"/api/v2/chats/{chat_id}", token, timeout=30.0)
        if res["status"] != 200:
            raise Exception(f"get_chat HTTP {res['status']}: {res.get('body', '')[:200]}")
        try:
            return json.loads(res.get("body", "{}"))
        except Exception as e:
            raise Exception(f"get_chat parse error: {e}, body={res.get('body', '')[:200]}")

    async def get_task_status(self, token: str, task_id: str) -> dict:
        # 1) httpx
        res = await self._request_json("GET", f"/api/v1/tasks/status/{task_id}", token, timeout=30.0)
        body = res.get("body", "") or ""
        status = res.get("status", 0)
        is_waf, _ = self._is_waf_challenge(body)
        if status == 200 and not is_waf:
            try:
                return json.loads(body)
            except Exception as e:
                raise Exception(f"task status parse error: {e}, body={body[:200]}")

        # 2) curl_cffi + bx
        try:
            from curl_cffi.requests import AsyncSession
            headers = self._build_headers(token)
            async with AsyncSession(impersonate="chrome131", timeout=30.0) as session:
                resp = await session.get(
                    f"{BASE_URL}/api/v1/tasks/status/{task_id}",
                    headers=headers,
                )
                if resp.status_code == 200 and not self._is_waf_challenge(resp.text or "")[0]:
                    return resp.json()
                status = resp.status_code
                body = resp.text or body
        except Exception as e:
            log.warning("[QwenClient] get_task_status curl_cffi failed: %s", e)

        # 3) browser fallback
        br = await self._browser_request_json(
            "GET",
            f"{BASE_URL}/api/v1/tasks/status/{task_id}",
            token,
            timeout=30.0,
        )
        if br.get("status") == 200:
            try:
                return json.loads(br.get("body", "{}"))
            except Exception as e:
                raise Exception(f"task status parse error: {e}, body={br.get('body', '')[:200]}")

        raise Exception(f"task status HTTP {status or br.get('status', 0)}: {body[:200]}")

    async def complete_once(
        self,
        token: str,
        chat_id: str,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        chat_type: str = "t2t",
        size: str | None = None,
        img_url: str | None = None,
    ) -> dict:
        """完成一次生成（t2t/t2i/t2v）。

        上游 t2i/t2v 对 stream=false 常返回 Bad_Request；
        这里强制 stream=true，再把 SSE 聚合成 dict 供 images/videos 解析 URL/task_id。
        """
        payload = build_chat_payload(
            chat_id,
            model,
            content,
            has_custom_tools,
            files=files,
            chat_type=chat_type,
            size=size,
            img_url=img_url,
        )
        # t2i: stream=true 直接出图 URL
        # t2v/i2v: stream=false 才能拿到 task_id（stream=true 常空答）
        # t2t: stream=true
        prefer_stream = chat_type not in {"t2v", "i2v"}
        attempts = [True] if prefer_stream else [False, True]
        if prefer_stream:
            attempts = [True, False]

        body_text = ""
        status = 0
        last_err = ""
        for use_stream in attempts:
            payload["stream"] = use_stream
            payload["incremental_output"] = True
            # 1) curl_cffi
            curl_res = await self._stream_chat_via_curl_cffi(token, chat_id, payload, timeout=300.0)
            if curl_res and curl_res.get("status") == 200 and not self._is_waf_challenge(curl_res.get("body") or "")[0]:
                status = 200
                body_text = curl_res.get("body") or ""
            else:
                # 2) httpx
                res = await self._request_json(
                    "POST",
                    f"/api/v2/chat/completions?chat_id={chat_id}",
                    token,
                    payload,
                    timeout=300.0,
                )
                status = res.get("status", 0)
                body_text = res.get("body", "") or ""
                is_waf, _ = self._is_waf_challenge(body_text)
                if status != 200 or is_waf:
                    # 3) browser
                    br = await self._browser_request_json(
                        "POST",
                        f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}",
                        token,
                        body=payload,
                        timeout=300.0,
                    )
                    status = br.get("status", 0)
                    body_text = br.get("body", "") or ""

            if status != 200:
                last_err = f"HTTP {status}: {body_text[:200]}"
                continue

            # 判断是否有实质内容：task_id / 媒体 URL / 非空 answer
            has_task = ("task_id" in body_text) or ("taskId" in body_text)
            has_media = any(x in body_text for x in (".mp4", ".png", ".jpg", "cdn.qwenlm.ai", "/t2i/", "/t2v/"))
            empty_answer = (
                '"content": ""' in body_text
                and "task_id" not in body_text
                and "cdn.qwenlm.ai" not in body_text
            )
            # 对 t2v：空答则换下一种 stream 模式
            if chat_type in {"t2v", "i2v"} and not has_task and not has_media and empty_answer:
                last_err = "empty t2v answer without task_id"
                log.warning("[complete_once] t2v empty with stream=%s, try next mode", use_stream)
                continue
            break
        else:
            raise Exception(f"complete_once failed: {last_err or 'unknown'}")

        if status != 200:
            raise Exception(f"complete_once HTTP {status}: {body_text[:300]}")

        # SSE 文本：聚合成可解析结构
        events: list[dict] = []
        for line in body_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                events.append(json.loads(data_str))
            except Exception:
                events.append({"raw": data_str})

        if not events:
            try:
                return json.loads(body_text)
            except Exception:
                return {"raw_sse": body_text, "events": [], "success": True}

        return {"raw_sse": body_text, "events": events, "success": True}

    async def verify_token(self, token: str) -> bool:
        if not token:
            return False
        try:
            resp = await self._http_client.get(
                f"{BASE_URL}/api/v1/auths/",
                headers=self._build_headers(token),
                timeout=15.0,
            )
            if resp.status_code != 200:
                return False
            try:
                data = resp.json()
                return data.get("role") == "user"
            except Exception as e:
                log.warning(f"[verify_token] JSON 解析失败: {e}, status={resp.status_code}, text={resp.text[:100]}")
                if "aliyun_waf" in resp.text.lower() or "<!doctype" in resp.text.lower():
                    log.info("[verify_token] WAF 拦截页面，放行")
                    return True
                return False
        except Exception as e:
            log.warning(f"[verify_token] HTTP 异常: {e}")
            return False

    async def list_models(self, token: str) -> list:
        try:
            resp = await self._http_client.get(
                f"{BASE_URL}/api/models",
                headers=self._build_headers(token),
                timeout=10.0,
            )
            if resp.status_code != 200:
                return []
            try:
                return resp.json().get("data", [])
            except Exception as e:
                log.warning(f"[list_models] JSON 解析失败: {e}")
                return []
        except Exception:
            return []

    # ---- cached upstream-pool model list ----
    _UPSTREAM_MODELS_TTL = 300
    _upstream_models_cache: list[dict] = []
    _upstream_models_fetched_at: float = 0.0

    async def list_models_from_pool(self) -> list[dict]:
        now = time.time()
        if self._upstream_models_cache and (now - self._upstream_models_fetched_at) < self._UPSTREAM_MODELS_TTL:
            return self._upstream_models_cache
        if self.account_pool is None:
            return []
        acc = None
        try:
            acc = await self.account_pool.acquire_wait(timeout=5)
            if not acc:
                return []
            models = await self.list_models(acc.token)
            if models:
                QwenClient._upstream_models_cache = models
                QwenClient._upstream_models_fetched_at = now
            return models
        except Exception as e:
            log.warning(f"[list_models_from_pool] failed: {e}")
            return []
        finally:
            if acc is not None:
                self.account_pool.release(acc)

    def _build_payload(
        self,
        chat_id: str,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        chat_type: str = "t2t",
        size: str | None = None,
    ) -> dict:
        return build_chat_payload(
            chat_id,
            model,
            content,
            has_custom_tools,
            files=files,
            chat_type=chat_type,
            size=size,
        )

    def parse_sse_chunk(self, chunk: str) -> list[dict]:
        return parse_sse_chunk(chunk)

    async def stream(self, token: str, chat_id: str, model: str, content: str, has_custom_tools: bool = False, files: list[dict] | None = None):
        async for event in self.executor.stream(token, chat_id, model, content, has_custom_tools, files=files):
            yield event

    async def chat_stream_events_with_retry(
        self,
        model: str,
        content: str,
        has_custom_tools: bool = False,
        files: list[dict] | None = None,
        fixed_account=None,
        existing_chat_id: str | None = None,
    ):
        """转发到 executor.chat_stream_events_with_retry（带重试的流式聊天）。"""
        async for item in self.executor.chat_stream_events_with_retry(
            model, content,
            has_custom_tools=has_custom_tools,
            files=files,
            fixed_account=fixed_account,
            existing_chat_id=existing_chat_id,
        ):
            yield item

    # ========================================================================
    # WAF 检测
    # ========================================================================
    def _is_waf_challenge(self, text: str) -> tuple[bool, str]:
        """检测响应是否为 WAF 挑战页面。返回 (is_waf, challenge_type)。"""
        if not text:
            return False, ""
        if "FAIL_SYS_USER_VALIDATE" in text:
            return True, "punish"
        if "aliyun_waf_aa" in text or "aliyun_waf_bb" in text:
            return True, "aa_bb"
        if "captcha" in text.lower() and ("sliding" in text.lower() or "滑块" in text):
            return True, "captcha"
        if "<!doctype" in text[:50].lower() and "waf" in text.lower():
            return True, "unknown_html"
        return False, ""

    # ========================================================================
    # 浏览器主页面管理（WAF 挑战 + 会话保持）
    # ========================================================================
    async def _ensure_main_page(self):
        """确保有一个已加载 Qwen 主页面并完成 WAF 挑战的页面。
        
        这个方法保留一个长期有效的页面，用于：
        1. 完成 Aliyun WAF 的 JavaScript 挑战（通过页面导航）
        2. 维护浏览器 session/cookies 的有效性
        3. 在 headed 模式下等待用户手动完成验证码
        4. 作为流式 API 请求的浏览器上下文
        """
        if self._pw_main_page is not None:
            # 检查页面是否还活着
            try:
                await self._pw_main_page.evaluate("1")
                return self._pw_main_page
            except Exception:
                log.warning("[QwenClient] 主页面已失效，重新创建")
                self._pw_main_page = None
                self._waf_cleared = False
        
        ctx = await self._ensure_browser()
        self._pw_main_page = await ctx.new_page()
        
        headed = os.getenv("BROWSER_HEADED", "").lower() in ("1", "true", "yes")
        
        # 导航到 Qwen 主页，等待 DOM 加载
        log.info("[QwenClient] 正在导航到 %s 以完成 WAF 挑战...", BASE_URL)
        try:
            await self._pw_main_page.goto(
                f"{BASE_URL}/",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            log.info("[QwenClient] 主页面 DOM 已加载")
        except Exception as e:
            log.warning("[QwenClient] 主页面导航异常: %s", e)
        
        # 等待页面 JS 执行（包括 WAF 挑战和 BaXia 初始化）
        await self._pw_main_page.wait_for_timeout(8000)
        
        # WAF 清除等待（headed 模式下交互等待用户完成验证码）
        await self._ensure_waf_clearance(self._pw_main_page)
        
        # 保存更新后的 cookie
        await self._save_cookies()
        return self._pw_main_page

    async def _ensure_waf_clearance(self, page):
        """确保 WAF 挑战已被清除。

        在 headed 模式下，等待用户手动完成 WAF 验证码/登录。
        在 headless 模式下，检测当前状态并尝试重试。
        """
        headed = os.getenv("BROWSER_HEADED", "").lower() in ("1", "true", "yes")
        
        if headed:
            # === Headed 模式：交互等待用户完成 WAF 验证 ===
            log.info("=" * 60)
            log.info("[QwenClient] ===== Headed 模式：等待 WAF 验证完成 =====")
            log.info("[QwenClient] 请在打开的浏览器窗口中完成以下步骤：")
            log.info("[QwenClient] 1. 如果出现 WAF 滑块验证码 → 手动滑动完成")
            log.info("[QwenClient] 2. 如果出现 Aliyun WAF 页面 → 等待自动跳转")
            log.info("[QwenClient] 3. 页面加载完成后 → 登录 Qwen 账号（如需）")
            log.info("[QwenClient] 4. 确认可以看到 Qwen 聊天界面")
            log.info("[QwenClient] 完成后我会自动检测到并继续执行。")
            log.info("[QwenClient] 超时时间：5 分钟")
            log.info("=" * 60)
            
            start_time = time.time()
            max_wait = 300  # 5 分钟超时
            poll_interval = 3  # 每 3 秒检测一次
            
            while time.time() - start_time < max_wait:
                await asyncio.sleep(poll_interval)
                
                try:
                    # 检测 1：页面内容是否包含 WAF 关键词
                    html_preview = await page.evaluate("document.body?.innerText?.substring(0, 200) || ''")
                    
                    # 检测 2：检查是否已通过 WAF（AjaxRequest 是否存在）
                    api_request_available = await page.evaluate("typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.post === 'function'")
                    
                    # 检测 3：检查页面标题
                    title = await page.evaluate("document.title || ''")
                    
                    waf_detected_in_html = (
                        "aliyun_waf" in html_preview.lower() or
                        "滑块" in html_preview or
                        "captcha" in html_preview.lower() or
                        "验证" in html_preview
                    )
                    
                    if not waf_detected_in_html and api_request_available:
                        log.info("[QwenClient] WAF 验证已通过！ApiRequest 可用，页面加载完成。")
                        log.info(f"[QwenClient] 页面标题: {title}")
                        self._waf_cleared = True
                        return True
                    
                    elapsed = int(time.time() - start_time)
                    if elapsed % 15 == 0:  # 每 15 秒提醒一次
                        log.info(f"[QwenClient] 等待 WAF 验证完成... (已等待 {elapsed}s)")
                        if waf_detected_in_html:
                            log.info(f"[QwenClient] 仍检测到验证码/安全挑战，请完成浏览器中的验证")
                
                except Exception as e:
                    log.debug(f"[QwenClient] WAF 检测异常: {e}")
                    continue
            
            log.warning(f"[QwenClient] WAF 验证等待超时 ({max_wait}s)")
            # 超时后仍然继续，尝试使用当前状态
            return False
        
        else:
            # === Headless 模式：自动检测 ===
            try:
                api_request_available = await page.evaluate("typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.post === 'function'")
                if api_request_available:
                    self._waf_cleared = True
                    log.info("[QwenClient] Headless 模式：ApiRequest 可用，WAF 已通过")
                    return True
                else:
                    # 检查页面是否被 WAF 拦截
                    html = await page.evaluate("document.body?.innerHTML?.substring(0, 500) || ''")
                    is_waf, waf_type = self._is_waf_challenge(html)
                    if is_waf:
                        log.warning(f"[QwenClient] Headless 模式：页面被 WAF 拦截 (type={waf_type})")
                        log.warning(f"[QwenClient] 建议设置 BROWSER_HEADED=1 手动完成验证")
                    else:
                        log.warning(f"[QwenClient] Headless 模式：ApiRequest 不可用，页面可能未完全加载")
                    return False
            except Exception as e:
                log.warning(f"[QwenClient] Headless WAF 检测异常: {e}")
                return False

    # ========================================================================
    # 浏览器内 API 请求（通过 window.ApiRequest 绕过 BaXia）
    # ========================================================================
    async def _browser_request_json(self, method: str, url: str, token: str, body: dict | None = None, timeout: float = 120.0) -> dict:
        """通过浏览器 page.evaluate 使用 window.ApiRequest 发送请求。

        Qwen 前端的 window.ApiRequest 是官方 API 客户端，自动处理：
        - bx-* 安全头的注入（通过 BaXia 集成）
        - 请求签名和时间戳
        - 会话身份验证

        相比直接使用 window.fetch（被 BaXia 包裹导致挂起），ApiRequest 是受支持的途径。
        如果 ApiRequest 不可用，降级到 window.fetch。
        """
        main_page = await self._ensure_main_page()
        
        # 设置 token 到 localStorage（Qwen 前端需要）
        escaped_token = token.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        try:
            await main_page.evaluate(f"localStorage.setItem('token', '{escaped_token}')")
        except Exception:
            pass
        
        # 安全的字符串转义
        def _js_str(s):
            return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        
        safe_url = _js_str(url)
        safe_method = _js_str(method)
        safe_token = escaped_token
        
        body_json = ""
        if body is not None:
            body_json = json.dumps(body, ensure_ascii=False)
        
        timeout_ms = int(timeout * 1000)
        
        # 构建 JS：优先使用 window.ApiRequest，降级到 window.fetch
        # ApiRequest API（从运行时检测得出）：
        #   ApiRequest.post(url, bodyObj, opts)  → POST with object body
        #   ApiRequest.postBody(url, bodyStr, opts) → POST with string body
        #   ApiRequest.request(method, url, opts) → generic (no body)
        #   ApiRequest.requestBody(method, url, bodyStr, opts) → generic with string body
        has_body = body is not None
        if has_body:
            # 使用 JSON.parse/stringify 确保 body_json 在 JS 中可用
            request_js = f"""
(async () => {{
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), {timeout_ms});
    try {{
        const useApiRequest = typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.postBody === 'function';
        
        if (useApiRequest) {{
            // 使用 Qwen 官方 ApiRequest（自动处理 bx-* 安全头）
            // postBody 接受 (url, bodyString, opts) —— body 以字符串传递
            const resp = await window.ApiRequest.postBody(
                '{safe_url}',
                '{_js_str(body_json)}',
                {{
                    headers: {{
                        'Authorization': 'Bearer {safe_token}',
                        'Content-Type': 'application/json',
                    }},
                    signal: controller.signal,
                }}
            );
            clearTimeout(id);
            // ApiRequest 返回的响应可能有不同格式
            if (typeof resp.json === 'function') {{
                const data = await resp.json();
                return {{ status: resp.status || 200, body: JSON.stringify(data) }};
            }} else if (typeof resp.text === 'function') {{
                const text = await resp.text();
                return {{ status: resp.status || 200, body: text }};
            }} else {{
                return {{ status: 200, body: JSON.stringify(resp) }};
            }}
        }} else {{
            // 降级到 window.fetch（可能被 BaXia 拦截或挂起）
            const resp = await fetch('{safe_url}', {{
                method: '{safe_method}',
                headers: {{
                    'Authorization': 'Bearer {safe_token}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Origin': '{_js_str(BASE_URL)}',
                    'Referer': '{_js_str(BASE_URL)}/',
                    'source': 'web',
                }},
                body: '{_js_str(body_json)}',
                signal: controller.signal,
                credentials: 'include',
            }});
            clearTimeout(id);
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
    }} catch (e) {{
        clearTimeout(id);
        return {{ status: 0, body: e.toString() }};
    }}
}})()
"""
        else:
            # 没有 body 的请求（GET/DELETE）
            request_js = f"""
(async () => {{
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), {timeout_ms});
    try {{
        const useApiRequest = typeof window.ApiRequest !== 'undefined' && typeof window.ApiRequest.request === 'function';
        
        if (useApiRequest) {{
            const resp = await window.ApiRequest.request(
                '{safe_method}',
                '{safe_url}',
                {{
                    headers: {{
                        'Authorization': 'Bearer {safe_token}',
                    }},
                    signal: controller.signal,
                }}
            );
            clearTimeout(id);
            if (typeof resp.json === 'function') {{
                const data = await resp.json();
                return {{ status: resp.status || 200, body: JSON.stringify(data) }};
            }} else if (typeof resp.text === 'function') {{
                const text = await resp.text();
                return {{ status: resp.status || 200, body: text }};
            }} else {{
                return {{ status: 200, body: JSON.stringify(resp) }};
            }}
        }} else {{
            const resp = await fetch('{safe_url}', {{
                method: '{safe_method}',
                headers: {{
                    'Authorization': 'Bearer {safe_token}',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Origin': '{_js_str(BASE_URL)}',
                    'Referer': '{_js_str(BASE_URL)}/',
                    'source': 'web',
                }},
                signal: controller.signal,
                credentials: 'include',
            }});
            clearTimeout(id);
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
    }} catch (e) {{
        clearTimeout(id);
        return {{ status: 0, body: e.toString() }};
    }}
}})()
"""
        log.info("[QwenClient] browser_fetch: 发送 %s %s ... (useApiRequest=%s)", method, url.split('?')[0], "yes")
        try:
            result = await asyncio.wait_for(
                main_page.evaluate(request_js),
                timeout=timeout + 5.0,  # 额外 5 秒用于 JS 执行开销
            )
            status = result.get("status", 0)
            body_len = len(result.get("body", ""))
            log.info("[QwenClient] browser_fetch 完成: status=%d 长度=%d", status, body_len)
            if body_len > 0 and status != 0:
                log.info("[QwenClient] browser_fetch 响应前100: %s", result.get("body", "")[:100])
            return result
        except asyncio.TimeoutError:
            log.error(f"[QwenClient] browser_fetch 超时 ({timeout}s)")
            return {"status": 0, "body": f"browser fetch timeout ({timeout}s)"}
        except Exception as e:
            log.error(f"[QwenClient] browser_fetch 异常: {e}", exc_info=True)
            return {"status": 0, "body": str(e)}
        finally:
            await self._save_cookies()

    # ========================================================================
    #  流式聊天请求
    # ========================================================================
    async def _stream_chat_via_curl_cffi(self, token: str, chat_id: str, payload: dict, timeout: float = 120.0) -> dict | None:
        """优先走 curl_cffi(chrome131)+bx 头直连上游，避免 Playwright 超时。

        【缓冲模式】用于 t2i/t2v 等需要一次性拿到完整 SSE 再解析 task_id/URL 的场景。
        用 (connect, idle) 元组超时：curl 在 stream 关闭时以 LOW_SPEED 语义判定，
        即"整段响应期间持续无数据"才中断，而不是对总时长设硬上限，
        因此长任务不会被拦腰截断。
        """
        try:
            from curl_cffi.requests import AsyncSession
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 不可用: %s", e)
            return None

        headers = self._build_headers(token)
        headers["Accept"] = "text/event-stream, application/json"
        url = f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}"
        # 元组超时：(连接超时, 读取空闲超时)；stream 关闭 → curl 用 LOW_SPEED_TIME=connect+idle
        idle = max(timeout, _CHAT_STREAM_IDLE_TIMEOUT)
        tup_timeout = (_CHAT_STREAM_CONNECT_TIMEOUT, idle)
        try:
            async with AsyncSession(impersonate="chrome131", timeout=tup_timeout) as session:
                chunks: list[str] = []
                async with session.stream("POST", url, headers=headers, json=payload) as resp:
                    async for chunk in resp.aiter_content():
                        if chunk:
                            chunks.append(chunk.decode("utf-8", errors="replace"))
                    status_code = resp.status_code
                return {"status": status_code, "body": "".join(chunks)}
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 流式请求失败: %s", e)
            return None

    async def _stream_chat_via_curl_cffi_iter(self, token: str, chat_id: str, payload: dict):
        """【真流式模式】逐块产出上游 SSE，用于对话流式返回。

        与缓冲版的区别：数据一到就 yield，客户端 SSE 连接持续有数据流动，
        不会因等待完整响应而空闲断连；同时用 (connect, idle) 元组超时，
        只在"长时间完全无新数据"时才中断，长任务/agent 全程不被总时长上限杀掉。

        产出:
          {"status": int, "body": str}                # 首个错误/WAF 状态帧（非 200 时）
          {"chunk": str}                              # 正常 SSE 数据块
        """
        try:
            from curl_cffi.requests import AsyncSession
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 不可用: %s", e)
            yield {"status": 0, "body": f"curl_cffi unavailable: {e}"}
            return

        headers = self._build_headers(token)
        headers["Accept"] = "text/event-stream, application/json"
        url = f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}"
        tup_timeout = (_CHAT_STREAM_CONNECT_TIMEOUT, _CHAT_STREAM_IDLE_TIMEOUT)
        try:
            async with AsyncSession(impersonate="chrome131", timeout=tup_timeout) as session:
                async with session.stream("POST", url, headers=headers, json=payload) as resp:
                    status_code = resp.status_code
                    if status_code != 200:
                        body_chunks: list[str] = []
                        async for chunk in resp.aiter_content():
                            if chunk:
                                body_chunks.append(chunk.decode("utf-8", errors="replace"))
                        yield {"status": status_code, "body": "".join(body_chunks)[:2000]}
                        return
                    async for chunk in resp.aiter_content():
                        if chunk:
                            yield {"chunk": chunk.decode("utf-8", errors="replace")}
        except Exception as e:
            log.warning("[QwenClient] curl_cffi 真流式请求失败: %s", e)
            yield {"status": 0, "body": str(e)}

    async def stream_chat_once(self, token: str, chat_id: str, payload: dict) -> AsyncIterator[dict]:
        """流式聊天：优先 curl_cffi 真流式直连，失败再降级 Playwright ApiRequest。"""
        # 先尝试真流式：逐块透传，遇到首个数据块即视为成功；非 200/WAF 再降级浏览器。
        first_error: dict | None = None
        got_data = False
        yielded_any = False  # 是否已向下游 yield 过 chunk（用于只在首帧检测 WAF）
        buffer = ""
        async for item in self._stream_chat_via_curl_cffi_iter(token, chat_id, payload):
            if "chunk" in item:
                got_data = True
                buffer += item["chunk"]
                # 保持 SSE 事件边界（\n\n）切分后透传
                while "\n\n" in buffer:
                    msg, buffer = buffer.split("\n\n", 1)
                    # 尚未向下游发过任何数据时，若首帧命中 WAF 文本，转错误帧走降级
                    if not yielded_any and self._is_waf_challenge(msg)[0]:
                        first_error = {"status": 403, "body": msg[:300]}
                        got_data = False
                        break
                    yield {"chunk": msg + "\n\n"}
                    yielded_any = True
                if first_error is not None:
                    break
                continue
            # 状态帧（非 200 或 curl 失败）
            status0 = item.get("status", 0)
            body0 = item.get("body", "") or ""
            first_error = {"status": status0, "body": body0}
            break

        if got_data:
            # flush 残留 buffer
            if buffer.strip():
                yield {"chunk": buffer if buffer.endswith("\n\n") else buffer + "\n\n"}
            return

        # 未取得任何数据 → 判定是否降级浏览器
        use_browser = True
        if first_error is not None:
            is_waf0, waf0 = self._is_waf_challenge(first_error.get("body", "") or "")
            log.warning(
                "[QwenClient] curl_cffi 真流式未取得数据 status=%s waf=%s，降级浏览器",
                first_error.get("status"), waf0 or "-",
            )

        result = await self._browser_request_json(
            "POST",
            f"{BASE_URL}/api/v2/chat/completions?chat_id={chat_id}",
            token,
            body=payload,
            timeout=_CHAT_STREAM_IDLE_TIMEOUT,
        )

        status = result.get("status", 0)
        raw_text = result.get("body", "")

        # 检测 WAF 挑战
        is_waf, waf_type = self._is_waf_challenge(raw_text)
        if is_waf:
            log.warning("[QwenClient] 上游返回 WAF 挑战 (type=%s, status=%d)", waf_type, status)
            if waf_type == "punish":
                try:
                    data = json.loads(raw_text)
                    punish_url = data.get("data", {}).get("url", "")
                    if punish_url:
                        log.info("[QwenClient] 提取到 punish URL: %s", punish_url[:150])
                except Exception:
                    pass
            yield {"status": 403, "body": f"WAF challenge ({waf_type}): {raw_text[:300]}"}
            return

        if status != 200:
            log.warning("[QwenClient] 上游 HTTP %d: %.200s", status, raw_text)
            yield {"status": status, "body": raw_text[:500]}
            return

        # 浏览器降级路径是缓冲返回：按原始字节流逐块输出（保持 SSE 格式）
        buffer = ""
        for chunk_text in [raw_text[i:i+4096] for i in range(0, len(raw_text), 4096)]:
            buffer += chunk_text
            while "\n\n" in buffer:
                msg, buffer = buffer.split("\n\n", 1)
                yield {"chunk": msg + "\n\n"}
