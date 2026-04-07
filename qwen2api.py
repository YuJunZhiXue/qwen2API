#!/usr/bin/env python3
"""
qwen2api v2 — chat.qwen.ai → OpenAI / Anthropic / Gemini Compatible API
Usage: python qwen2api.py [--port 8080] [--api-key sk-xxx] [--workers 3]
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import string
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, AsyncGenerator

import argparse

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# ─── Logging ────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("qwen2api")
# Keep noisy libraries at INFO only
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)  # suppress WinError 10054 ConnectionReset noise
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)

# Suppress "Invalid HTTP request received" — caused by browsers auto-trying HTTPS on HTTP servers
class _InvalidHttpFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Invalid HTTP request" not in record.getMessage()

logging.getLogger("uvicorn.error").addFilter(_InvalidHttpFilter())

VERSION = "2.0.0"
BASE_URL = "https://chat.qwen.ai"
DEFAULT_MODEL = "qwen3.6-plus"
ACCOUNTS_FILE = Path("accounts.json")
CONFIG_FILE = Path("config.json")
MAX_RETRIES = 3
AUTH_FAIL_KEYWORDS = ("token", "unauthorized", "expired", "forbidden", "401", "403", "invalid", "login", "activation", "pending activation", "not activated")

# Globals
API_KEYS: set = set()
PORT = 8080
BROWSER_POOL_SIZE = 2
MAX_INFLIGHT_PER_ACCOUNT = 1
_last_conv_fp: str = ""  # fingerprint of the last conversation's first user message

# ─── Model Mapping ──────────────────────────────────────
# All "strong" aliases → qwen3.6-plus (latest/best)
# Only explicit "mini/flash/turbo/haiku" variants → smaller models
MODEL_MAP = {
    # OpenAI
    "gpt-4o":            "qwen3.6-plus",
    "gpt-4o-mini":       "qwen3.5-flash",
    "gpt-4-turbo":       "qwen3.6-plus",
    "gpt-4":             "qwen3.6-plus",
    "gpt-4.1":           "qwen3.6-plus",
    "gpt-4.1-mini":      "qwen3.5-flash",
    "gpt-3.5-turbo":     "qwen3.5-flash",
    "gpt-5":             "qwen3.6-plus",
    "o1":                "qwen3.6-plus",
    "o1-mini":           "qwen3.5-flash",
    "o3":                "qwen3.6-plus",
    "o3-mini":           "qwen3.5-flash",
    # Anthropic
    "claude-opus-4-6":   "qwen3.6-plus",
    "claude-sonnet-4-5": "qwen3.6-plus",
    "claude-3-opus":     "qwen3.6-plus",
    "claude-3.5-sonnet": "qwen3.6-plus",
    "claude-3-sonnet":   "qwen3.6-plus",
    "claude-3-haiku":    "qwen3.5-flash",
    # Gemini
    "gemini-2.5-pro":    "qwen3.6-plus",
    "gemini-2.5-flash":  "qwen3.5-flash",
    # Qwen aliases
    "qwen":              "qwen3.6-plus",
    "qwen-max":          "qwen3.6-plus",
    "qwen-plus":         "qwen3.6-plus",
    "qwen-turbo":        "qwen3.5-flash",
    # DeepSeek
    "deepseek-chat":     "qwen3.6-plus",
    "deepseek-reasoner": "qwen3.6-plus",
}


def resolve_model(name: str) -> str:
    return MODEL_MAP.get(name, name)


# ─── Config Persistence ────────────────────────────────
class Config:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.keys: list[str] = []
        self.model_aliases: dict = {}
        self.max_inflight: int = 1
        self.admin_key: str = "admin"
        self.load()

    def load(self):
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.keys = d.get("keys", [])
                self.model_aliases = d.get("model_aliases", {})
                self.max_inflight = d.get("max_inflight_per_account", 1)
                self.admin_key = d.get("admin_key", "admin")
            except Exception as e:
                log.error(f"Config load error: {e}")

    def save(self):
        d = {
            "keys": self.keys,
            "model_aliases": self.model_aliases,
            "max_inflight_per_account": self.max_inflight,
            "admin_key": self.admin_key,
        }
        self.path.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


RATE_LIMIT_KEYWORDS = ("rate limit", "rate_limit", "too many", "quota", "exceeded",
                       "throttle", "ratelimit", "slow down", "capacity", "overloaded")
RATE_LIMIT_COOLDOWN = 600  # 10 minutes before retrying a rate-limited account


def _is_rate_limit_error(error_msg: str) -> bool:
    msg = error_msg.lower()
    return any(kw in msg for kw in RATE_LIMIT_KEYWORDS)


# ─── Account ───────────────────────────────────────────
class Account:
    def __init__(self, email="", password="", token="", cookies="", username="",
                 activation_pending=False, **_):
        self.email = email
        self.password = password
        self.token = token
        self.cookies = cookies
        self.username = username
        self.activation_pending = activation_pending
        self.valid = not activation_pending  # 待激活账号启动时直接标为不可用
        self.last_used = 0.0
        self.inflight = 0
        self.rate_limited_until = 0.0
        self.transient_failures = 0

    def is_rate_limited(self) -> bool:
        return self.rate_limited_until > time.time()

    def is_available(self) -> bool:
        return self.valid and not self.is_rate_limited()

    def to_dict(self):
        return {"email": self.email, "password": self.password,
                "token": self.token, "cookies": self.cookies, "username": self.username,
                "activation_pending": self.activation_pending}


class AccountStore:
    def __init__(self, path: Path = ACCOUNTS_FILE):
        self.path = path
        self.accounts: list[Account] = []
        self.load()

    def load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.accounts = [Account(**d) for d in data]
                log.info(f"Loaded {len(self.accounts)} account(s)")
            except Exception as e:
                log.error(f"Failed to load accounts: {e}")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [a.to_dict() for a in self.accounts]
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def add(self, account: Account):
        self.accounts = [a for a in self.accounts if a.email != account.email]
        self.accounts.append(account)
        self.save()

    def remove(self, email: str):
        self.accounts = [a for a in self.accounts if a.email != email]
        self.save()


# ─── Account Pool (Concurrency-Aware) ──────────────────
class AccountPool:
    def __init__(self, store: AccountStore, max_inflight: int = 1):
        self.store = store
        self.max_inflight = max_inflight
        self._lock = asyncio.Lock()
        self._waiters: list[asyncio.Event] = []
        self._sticky_email: Optional[str] = None  # current primary account — stick to it until Qwen rejects it

    async def acquire(self, exclude: set = None) -> Optional[Account]:
        async with self._lock:
            available = [a for a in self.store.accounts
                         if a.is_available() and (not exclude or a.email not in exclude)]
            if not available:
                return None
            # Sticky: always prefer the current primary account
            if self._sticky_email:
                sticky = next((a for a in available if a.email == self._sticky_email), None)
                if sticky and sticky.inflight < self.max_inflight:
                    sticky.inflight += 1
                    sticky.last_used = time.time()
                    return sticky
            # No sticky or sticky excluded/at-capacity: pick least-busy, set as new primary
            available.sort(key=lambda a: a.inflight)
            best = available[0]
            if best.inflight >= self.max_inflight:
                return None  # All accounts at capacity
            best.inflight += 1
            best.last_used = time.time()
            self._sticky_email = best.email
            log.info(f"[Pool] 主账号切换为: {best.email}")
            return best

    async def acquire_wait(self, timeout: float = 60, exclude: set = None) -> Optional[Account]:
        """Try to acquire, wait if all accounts at capacity."""
        acc = await self.acquire(exclude)
        if acc:
            return acc
        # Wait for a release
        evt = asyncio.Event()
        self._waiters.append(evt)
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if evt in self._waiters:
                self._waiters.remove(evt)
        return await self.acquire(exclude)

    def release(self, acc: Account):
        acc.inflight = max(0, acc.inflight - 1)
        # Notify one waiter
        if self._waiters:
            evt = self._waiters.pop(0)
            evt.set()

    def mark_invalid(self, acc: Account):
        acc.valid = False
        if self._sticky_email == acc.email:
            self._sticky_email = None
            log.warning(f"[Pool] {acc.email} 认证失败已标记失效，下次请求切换到下一个账号")

    def mark_rate_limited(self, acc: Account, cooldown: int = RATE_LIMIT_COOLDOWN):
        """Qwen官方限速：暂停账号，切换到下一个。"""
        acc.rate_limited_until = time.time() + cooldown
        if self._sticky_email == acc.email:
            self._sticky_email = None
            log.warning(f"[Pool] {acc.email} Qwen官方限速，已切换到下一个账号，{cooldown}s后可恢复")

    def status(self):
        all_accs = self.store.accounts
        available = [a for a in all_accs if a.is_available()]
        rate_limited = [a for a in all_accs if a.valid and a.is_rate_limited()]
        invalid = [a for a in all_accs if not a.valid]
        in_use = sum(a.inflight for a in available)
        return {
            "total": len(all_accs),
            "valid": len(available),
            "rate_limited": len(rate_limited),
            "invalid": len(invalid),
            "in_use": in_use,
            "max_inflight_per_account": self.max_inflight,
            "waiting": len(self._waiters),
        }


# ─── Browser Engine ─────────────────────────────────────
JS_FETCH = """
async (args) => {
    const opts = {
        method: args.method,
        headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + args.token
        }
    };
    if (args.body) opts.body = JSON.stringify(args.body);
    const res = await fetch(args.url, opts);
    const text = await res.text();
    return { status: res.status, body: text };
}
"""

JS_STREAM_FULL = """
async (args) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 150000);  // 150s timeout
    try {
        const res = await fetch(args.url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + args.token
            },
            body: JSON.stringify(args.payload),
            signal: controller.signal
        });
        if (!res.ok) {
            const t = await res.text();
            clearTimeout(timer);
            return { status: res.status, body: t.substring(0, 2000) };
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let body = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            body += decoder.decode(value, { stream: true });
        }
        clearTimeout(timer);
        return { status: res.status, body: body };
    } catch(e) {
        clearTimeout(timer);
        return { status: 0, body: 'JS error: ' + e.message };
    }
}
"""

# Camoufox (Firefox) launch options.
# firefox_user_prefs disables GPU/SWGL renderer to avoid
# "RenderCompositorSWGL failed mapping default framebuffer" on headless servers.
_CAMOUFOX_OPTS = {
    "headless": True,
    "humanize": False,
    "i_know_what_im_doing": True,
    "firefox_user_prefs": {
        "layers.acceleration.disabled": True,
        "gfx.webrender.enabled": False,
        "gfx.webrender.all": False,
        "gfx.webrender.software": False,
        "gfx.canvas.azure.backends": "skia",
        "media.hardware-video-decoding.enabled": False,
    },
}


@asynccontextmanager
async def _new_browser():
    """Launch a camoufox (Firefox) browser."""
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(**_CAMOUFOX_OPTS) as browser:
        yield browser


class BrowserEngine:
    def __init__(self, pool_size: int = 3):
        self.pool_size = pool_size
        self._browser = None
        self._browser_cm = None       # camoufox context manager
        self._pages: asyncio.Queue = asyncio.Queue()
        self._started = False
        self._ready = asyncio.Event()   # set when engine is fully started

    async def start(self):
        if self._started:
            return
        try:
            await self._start_camoufox()
        except Exception as e:
            log.error(f"[Browser] camoufox failed ({type(e).__name__}): {e}")
        finally:
            self._ready.set()

    async def _start_camoufox(self):
        await self._ensure_browser_installed()
        from camoufox.async_api import AsyncCamoufox
        log.info("Starting browser engine (camoufox)...")
        self._browser_cm = AsyncCamoufox(**_CAMOUFOX_OPTS)
        self._browser = await self._browser_cm.__aenter__()
        await self._init_pages()
        self._started = True
        log.info("Browser engine started (camoufox)")

    async def _init_pages(self):
        for i in range(self.pool_size):
            page = await self._browser.new_page()
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            self._pages.put_nowait(page)
            log.info(f"  Page {i+1}/{self.pool_size} ready")

    @staticmethod
    async def _ensure_browser_installed():
        """Check if camoufox Firefox exe is present; skip download if found."""
        import sys, subprocess
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, "-m", "camoufox", "path"],
                    capture_output=True, text=True, timeout=10
                )
            )
            cache_dir = result.stdout.strip()
            if cache_dir:
                # Look for the actual executable inside the cache dir
                exe_name = "camoufox.exe" if os.name == "nt" else "camoufox"
                exe_path = os.path.join(cache_dir, exe_name)
                if os.path.exists(exe_path):
                    log.debug(f"[Browser] camoufox 已安装: {exe_path}")
                    return
                log.warning(f"[Browser] camoufox.exe 不在 {cache_dir}，请将解压内容移动到该目录")
        except Exception as e:
            log.debug(f"[Browser] camoufox path 检查失败: {e}")
        log.info("[Browser] 未检测到 camoufox Firefox，正在自动下载（首次使用约100MB，请稍候）...")
        try:
            loop = asyncio.get_event_loop()
            def _do_install():
                from camoufox.pkgman import CamoufoxFetcher
                CamoufoxFetcher().install()
            await loop.run_in_executor(None, _do_install)
            log.info("[Browser] camoufox Firefox 下载完成")
        except Exception as e:
            log.error(f"[Browser] 自动下载失败: {e}，请手动运行: python -m camoufox fetch")

    async def stop(self):
        self._started = False
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._browser_cm:
            try:
                await self._browser_cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        await asyncio.wait_for(self._ready.wait(), timeout=300)
        if not self._started:
            return {"status": 0, "body": "Browser engine failed to start"}
        page = await asyncio.wait_for(self._pages.get(), timeout=60)
        needs_refresh = False
        try:
            result = await page.evaluate(JS_FETCH, {
                "method": method, "url": path, "token": token, "body": body,
            })
            # Only refresh on JS-level errors (status=0 with JS error message), not HTTP errors
            if result.get("status") == 0 and result.get("body", "").startswith("JS error:"):
                needs_refresh = True
            return result
        except Exception as e:
            log.error(f"api_call error: {e}")
            needs_refresh = True
            return {"status": 0, "body": str(e)}
        finally:
            if needs_refresh:
                asyncio.create_task(self._refresh_page_and_return(page))
            else:
                self._pages.put_nowait(page)

    async def _refresh_page(self, page) -> None:
        """Reload a browser page to reset it after a JS error."""
        try:
            await asyncio.wait_for(
                page.goto(BASE_URL, wait_until="domcontentloaded"),
                timeout=20000,
            )
            log.info("[Browser] 页面已刷新（JS错误后重置）")
        except Exception as e:
            log.warning(f"[Browser] 页面刷新失败: {e}")

    async def fetch_chat(self, token: str, chat_id: str, payload: dict) -> dict:
        await asyncio.wait_for(self._ready.wait(), timeout=300)
        if not self._started:
            return {"status": 0, "body": "Browser engine failed to start"}
        page = await asyncio.wait_for(self._pages.get(), timeout=60)
        needs_refresh = False
        try:
            url = f'/api/v2/chat/completions?chat_id={chat_id}'
            log.info(f"[fetch_chat] model={payload.get('model')}")
            result = await asyncio.wait_for(
                page.evaluate(JS_STREAM_FULL, {"url": url, "token": token, "payload": payload}),
                timeout=180,
            )
            # Log first SSE chunk to debug model selection
            if result.get("status") == 200:
                first_line = result["body"][:300]
                log.info(f"[fetch_chat] SSE preview: {first_line!r}")
            elif result.get("status") == 0:
                # Browser-level failure (JS error / abort) — page may be in a bad state
                log.warning(f"[Browser] 页面JS错误: {result.get('body','')[:100]}, 将刷新页面")
                needs_refresh = True
            return result
        except asyncio.TimeoutError:
            needs_refresh = True
            return {"status": 0, "body": "Timeout"}
        except Exception as e:
            needs_refresh = True
            return {"status": 0, "body": str(e)}
        finally:
            if needs_refresh:
                asyncio.create_task(self._refresh_page_and_return(page))
            else:
                self._pages.put_nowait(page)

    async def _refresh_page_and_return(self, page) -> None:
        """Refresh a broken page and return it to the pool."""
        await self._refresh_page(page)
        self._pages.put_nowait(page)

    async def refresh_idle_pages(self) -> None:
        """Drain all idle pages from the pool, refresh them in background, then return them.
        Called when a conversation switch is detected (new client or new session)."""
        idle = []
        while True:
            try:
                idle.append(self._pages.get_nowait())
            except asyncio.QueueEmpty:
                break
        if idle:
            log.info(f"[Browser] 检测到对话切换，后台刷新 {len(idle)} 个空闲页面...")
            for page in idle:
                asyncio.create_task(self._refresh_page_and_return(page))


# ─── Qwen Client ────────────────────────────────────────
class QwenClient:
    def __init__(self, engine: BrowserEngine):
        self.engine = engine

    async def refresh_token(self, acc: Account) -> bool:
        """Re-login with email+password to get a fresh token. Returns True on success."""
        if not acc.email or not acc.password:
            log.warning(f"[Refresh] 账号 {acc.email} 无密码，无法刷新")
            return False
        log.info(f"[Refresh] 正在为 {acc.email} 刷新 token...")
        try:
            async with _new_browser() as browser:
                page = await browser.new_page()
                try:
                    await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                await asyncio.sleep(3)
                li_email = await page.query_selector('input[placeholder*="Email"]')
                if li_email:
                    await li_email.fill(acc.email)
                li_pwd = await page.query_selector('input[type="password"]')
                if li_pwd:
                    await li_pwd.fill(acc.password)
                li_btn = (await page.query_selector('button:has-text("Log in")') or
                          await page.query_selector('button[type="submit"]'))
                if li_btn:
                    await li_btn.click()
                await asyncio.sleep(8)
                new_token = await page.evaluate("localStorage.getItem('token')")
                if new_token and new_token != acc.token:
                    old_prefix = acc.token[:20] if acc.token else "空"
                    acc.token = new_token
                    acc.valid = True
                    store.save()
                    log.info(f"[Refresh] {acc.email} token 已更新 ({old_prefix}... → {new_token[:20]}...)")
                    return True
                elif new_token == acc.token:
                    # Token same but might still be valid — mark valid again
                    acc.valid = True
                    log.info(f"[Refresh] {acc.email} token 未变化，重新标记有效")
                    return True
                else:
                    log.warning(f"[Refresh] {acc.email} 登录后未获取到token，URL={page.url}")
                    return False
        except Exception as e:
            log.error(f"[Refresh] {acc.email} 刷新异常: {e}")
            return False

    async def verify_token(self, token: str) -> bool:
        """Verify token validity via direct HTTP (no browser page needed)."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15, trust_env=False) as hc:
                resp = await hc.get(
                    f"{BASE_URL}/api/v1/auths/",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code != 200:
                return False
            return resp.json().get("role") == "user"
        except Exception as e:
            log.warning(f"[verify_token] HTTP error: {e}")
            return False

    async def list_models(self, token: str) -> list:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, trust_env=False) as hc:
                resp = await hc.get(
                    f"{BASE_URL}/api/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code != 200:
                return []
            return resp.json().get("data", [])
        except Exception:
            return []

    async def create_chat(self, token: str, model: str) -> str:
        ts = int(time.time())
        body = {"title": f"api_{ts}", "models": [model], "chat_mode": "normal",
                "chat_type": "t2t", "timestamp": ts}
        log.info(f"[create_chat] model={model}")
        r = await self.engine.api_call("POST", "/api/v2/chats/new", token, body)
        body_text = r.get("body", "")
        if r["status"] != 200:
            log.error(f"[create_chat] failed: status={r['status']}, body={body_text[:300]}")
            body_lower = body_text.lower()
            if (r["status"] in (401, 403)
                    or "unauthorized" in body_lower or "forbidden" in body_lower
                    or "token" in body_lower or "login" in body_lower
                    or "401" in body_text or "403" in body_text):
                raise Exception(f"unauthorized: create_chat HTTP {r['status']}: {body_text[:100]}")
            raise Exception(f"create_chat HTTP {r['status']}: {body_text[:100]}")
        try:
            data = json.loads(body_text)
            chat_id = data["data"]["id"]
            assigned_models = data.get("data", {}).get("models", [])
            log.info(f"[create_chat] assigned models: {assigned_models}, chat_id={chat_id}")
            return chat_id
        except Exception as e:
            log.error(f"[create_chat] parse error: {e}, body={body_text[:300]}")
            body_lower = body_text.lower()
            # Check FULL body for any auth/account issue keywords
            if any(kw in body_lower for kw in ("html", "login", "unauthorized", "activation",
                                                "pending", "forbidden", "token", "expired", "invalid")):
                raise Exception(f"unauthorized: account issue: {body_text[:200]}")
            raise Exception(f"create_chat parse error: {e}, body={body_text[:200]}")

    async def delete_chat(self, token: str, chat_id: str):
        await self.engine.api_call("DELETE", f"/api/v2/chats/{chat_id}", token)

    def _build_payload(self, chat_id: str, model: str, content: str,
                        enable_thinking: bool = True, has_custom_tools: bool = False) -> dict:
        ts = int(time.time())
        feature_config = {
            "thinking_enabled": True, "output_schema": "phase", "research_mode": "normal",
            "auto_thinking": True, "thinking_mode": "Auto", "thinking_format": "summary",
            "auto_search": not has_custom_tools,
            "code_interpreter": not has_custom_tools,
            "function_calling": not has_custom_tools,  # disable Qwen native tool calls
            "plugins_enabled": not has_custom_tools,
        }
        if not enable_thinking:
            feature_config.update({"thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Off"})
        return {
            "stream": True, "version": "2.1", "incremental_output": True,
            "chat_id": chat_id, "chat_mode": "normal", "model": model, "parent_id": None,
            "messages": [{
                "fid": str(uuid.uuid4()), "parentId": None, "childrenIds": [str(uuid.uuid4())],
                "role": "user", "content": content, "user_action": "chat", "files": [],
                "timestamp": ts, "models": [model], "chat_type": "t2t",
                "feature_config": feature_config,
                "extra": {"meta": {"subChatType": "t2t"}}, "sub_chat_type": "t2t", "parent_id": None,
            }],
            "timestamp": ts,
        }

    def parse_sse_body(self, body: str) -> list[dict]:
        """Parse SSE body into list of events."""
        events = []
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                events.append({"type": "done"})
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Detect Qwen error responses embedded in SSE body (e.g. {"code":401,"message":"..."})
            if "code" in data and "message" in data and "choices" not in data:
                code = data.get("code", 0)
                msg = data.get("message", "unknown error")
                raise Exception(f"Qwen API error {code}: {msg}")
            choices = data.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            phase = delta.get("phase", "")
            content = delta.get("content", "")
            status = delta.get("status", "")
            extra = delta.get("extra", {})
            # Collect all phases including Qwen's native "tool_call" phase
            events.append({
                "type": "delta",
                "phase": phase,
                "content": content,
                "status": status,
                "extra": extra,
            })
        return events

    async def chat_completions(self, token: str, model: str, content: str,
                               enable_thinking: bool = True, has_custom_tools: bool = False) -> tuple[str, str]:
        """Full request: create chat → send message → parse → delete chat.
        Returns (answer_text, reasoning_text).
        Raises Exception on failure."""
        chat_id = await self.create_chat(token, model)
        try:
            payload = self._build_payload(chat_id, model, content, enable_thinking, has_custom_tools)
            result = await self.engine.fetch_chat(token, chat_id, payload)
            if result["status"] != 200:
                raise Exception(f"Qwen API HTTP {result['status']}: {result['body'][:200]}")
            events = self.parse_sse_body(result["body"])
            answer = ""
            reasoning = ""
            native_tc_chunks: dict = {}  # tool_call_id -> {name, args_str}
            for evt in events:
                if evt["type"] != "delta":
                    continue
                phase = evt["phase"]
                text = evt["content"]
                if phase in ("think", "thinking_summary") and text:
                    reasoning += text
                elif phase == "answer" and text:
                    answer += text
                elif phase == "tool_call" and text:
                    # Qwen native tool call: accumulate content chunks
                    tc_id = evt.get("extra", {}).get("tool_call_id", "tc_0")
                    if tc_id not in native_tc_chunks:
                        native_tc_chunks[tc_id] = {"name": "", "args": ""}
                    try:
                        chunk = json.loads(text)
                        if "name" in chunk:
                            native_tc_chunks[tc_id]["name"] = chunk["name"]
                        if "arguments" in chunk:
                            native_tc_chunks[tc_id]["args"] += chunk["arguments"]
                    except (json.JSONDecodeError, ValueError):
                        native_tc_chunks[tc_id]["args"] += text
                if evt["status"] == "finished" and phase == "answer":
                    break
            # If Qwen native tool calls were detected, synthesize answer text
            if native_tc_chunks and not answer:
                log.info(f"[SSE] 检测到 Qwen 原生 tool_call 事件: {list(native_tc_chunks.keys())}")
                tc_parts = []
                for tc_id, tc in native_tc_chunks.items():
                    name = tc["name"]
                    try:
                        inp = json.loads(tc["args"]) if tc["args"] else {}
                    except (json.JSONDecodeError, ValueError):
                        inp = {"raw": tc["args"]}
                    tc_parts.append(f'<tool_call>{{"name": {json.dumps(name)}, "input": {json.dumps(inp, ensure_ascii=False)}}}</tool_call>')
                answer = "\n".join(tc_parts)
            elif answer:
                log.debug(f"[SSE] 收到 answer 文本({len(answer)}字): {answer[:120]!r}")
            return answer, reasoning
        finally:
            asyncio.create_task(self.delete_chat(token, chat_id))

    async def chat_stream_events(self, token: str, model: str, content: str,
                                 enable_thinking: bool = True, has_custom_tools: bool = False) -> tuple[list[dict], str]:
        """Full request returning parsed events and chat_id for cleanup.
        Returns (events_list, chat_id)."""
        chat_id = await self.create_chat(token, model)
        try:
            payload = self._build_payload(chat_id, model, content, enable_thinking, has_custom_tools)
            result = await self.engine.fetch_chat(token, chat_id, payload)
            if result["status"] != 200:
                raise Exception(f"Qwen API HTTP {result['status']}: {result['body'][:200]}")
            return self.parse_sse_body(result["body"]), chat_id
        except Exception:
            asyncio.create_task(self.delete_chat(token, chat_id))
            raise


def _is_auth_error(error_msg: str) -> bool:
    msg = error_msg.lower()
    return any(kw in msg for kw in AUTH_FAIL_KEYWORDS)


def _is_pending_activation_error(error_msg: str) -> bool:
    msg = error_msg.lower()
    return "pending activation" in msg or "please check your email" in msg or "not activated" in msg


# ─── Tool Calling ───────────────────────────────────────
def _extract_text(content, user_tool_mode: bool = False) -> str:
    """Extract text from Anthropic content (string or list of blocks).

    user_tool_mode=True: used for user messages when tools are active.
    In that case we take only the LAST text block (the actual user request)
    and skip earlier text blocks which typically contain CLAUDE.md content
    embedded by the client before the real prompt.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        # Collect all text blocks and non-text blocks separately
        text_blocks = []
        other_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type", "")
            if t == "text":
                text_blocks.append(part.get("text", ""))
            elif t == "tool_use":
                # Render as ##TOOL_CALL## format — same as what we ask the model to output,
                # so history looks consistent and the model knows how to continue.
                inp = json.dumps(part.get("input", {}), ensure_ascii=False)
                other_parts.append(
                    f'##TOOL_CALL##\n{{"name": {json.dumps(part.get("name",""))}, "input": {inp}}}\n##END_CALL##'
                )
            elif t == "tool_result":
                inner = part.get("content", "")
                tid = part.get("tool_use_id", "")
                if isinstance(inner, str):
                    other_parts.append(f"[Tool Result for call {tid}]\n{inner}\n[/Tool Result]")
                elif isinstance(inner, list):
                    texts = [p.get("text", "") for p in inner if isinstance(p, dict) and p.get("type") == "text"]
                    other_parts.append(f"[Tool Result for call {tid}]\n{''.join(texts)}\n[/Tool Result]")

        if user_tool_mode and text_blocks:
            # Only keep the LAST text block — that's the actual user request.
            # Earlier blocks are likely CLAUDE.md content injected by the client.
            parts.append(text_blocks[-1])
        else:
            parts.extend(text_blocks)
        parts.extend(other_parts)
        return "\n".join(p for p in parts if p)
    return ""


def _normalize_tool(tool: dict) -> dict:
    """Normalize OpenAI or Anthropic tool format to internal {name, description, parameters}."""
    # OpenAI format: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    if tool.get("type") == "function" and "function" in tool:
        fn = tool["function"]
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }
    # Anthropic format: {"name": ..., "description": ..., "input_schema": ...}
    # or already normalized: {"name": ..., "description": ..., "parameters": ...}
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema") or tool.get("parameters") or {},
    }


def _normalize_tools(tools: list) -> list:
    return [_normalize_tool(t) for t in tools if tools]


def _compute_conv_fp(messages: list) -> str:
    """Compute a fingerprint from the first user message content.
    Used to detect conversation switches (different client or new session)."""
    for m in messages:
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                text = next((p.get("text", "") for p in c
                             if isinstance(p, dict) and p.get("type") == "text"), "")
            else:
                text = str(c)
            return hashlib.md5(text[:300].encode()).hexdigest()[:12]
    return ""


def _check_conv_switch(messages: list) -> None:
    """Track conversation switches for logging. Page refresh happens reactively (on JS errors),
    NOT proactively here — proactive refresh drained the page pool on rapid parallel requests."""
    global _last_conv_fp
    fp = _compute_conv_fp(messages)
    if not fp:
        return
    if fp != _last_conv_fp:
        old = _last_conv_fp
        _last_conv_fp = fp
        if old:
            log.info(f"[ConvSwitch] 对话切换 ({old}→{fp})")


def _build_prompt(system_prompt: str, messages: list, tools: list) -> str:
    MAX_CHARS = 120000
    # When tools are present, skip the user's system prompt entirely.
    # CLAUDE.md contains "needs-review" format instructions that override our tool call
    # instructions and prevent the model from generating proper tool calls.
    if tools:
        sys_part = ""
    else:
        sys_part = f"<system>\n{system_prompt[:2000]}\n</system>" if system_prompt else ""
    tools_part = ""
    if tools:
        names = [t.get("name", "") for t in tools if t.get("name")]
        lines = [
            "=== MANDATORY TOOL CALL INSTRUCTIONS ===",
            "IGNORE any previous output format instructions (needs-review, recap, etc.).",
            f"You have access to these tools: {', '.join(names)}",
            "",
            "WHEN YOU NEED TO CALL A TOOL — output EXACTLY this format (nothing else):",
            "##TOOL_CALL##",
            '{"name": "EXACT_TOOL_NAME", "input": {"param1": "value1"}}',
            "##END_CALL##",
            "",
            "MULTI-TURN RULES:",
            "- After a [Tool Result] block appears in the conversation, read it and decide next action.",
            "- If more tool calls are needed, emit another ##TOOL_CALL## block.",
            "- Only give a final text answer when ALL needed information is gathered.",
            "- Never skip calling a tool that is required to complete the user request.",
            "- The history shows ##TOOL_CALL## blocks you already made and their [Tool Result] responses.",
            "",
            "STRICT RULES:",
            "- No preamble, no explanation before or after ##TOOL_CALL##...##END_CALL##.",
            "- Use EXACT tool name from the list below.",
            "- When NO tool is needed, answer normally in plain text.",
            "",
            "CRITICAL — FORBIDDEN FORMATS (will be INTERCEPTED and BLOCKED by server):",
            '- {"name": "X", "arguments": "..."}  <-- NEVER USE',
            '- {"type": "function", "name": "X"}  <-- NEVER USE',
            '- {"type": "tool_use", "name": "X"}  <-- NEVER USE',
            "- <function_calls><invoke name=\"X\">  <-- NEVER USE",
            "- <tool_call>{...}</tool_call>  <-- NEVER USE",
            "ONLY ##TOOL_CALL##...##END_CALL## is accepted. Any other format will cause 'Tool X does not exists.' error.",
            "",
            "Available tools:",
        ]
        # When there are many tools (>20), only list name+short_desc without params
        # to keep the prompt compact and avoid model timeouts.
        verbose_tools = len(tools) <= 20
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            if verbose_tools:
                desc = desc[:120]
                lines.append(f"- {name}: {desc}")
                params = tool.get("parameters", {})
                if params:
                    props = params.get("properties", {})
                    req = params.get("required", [])
                    if props:
                        ps = ", ".join(f"{k}({'req' if k in req else 'opt'})" for k in props)
                        lines.append(f"  params: {ps}")
            else:
                # Compact: just name and very short description
                desc = desc[:60]
                lines.append(f"- {name}: {desc}")
        lines.append("=== END TOOL INSTRUCTIONS ===")
        tools_part = "\n".join(lines)

    overhead = len(sys_part) + len(tools_part) + 50
    budget = MAX_CHARS - overhead
    history_parts = []
    used = 0
    # When tools are present: skip system-role messages (CLAUDE.md arrives this way)
    # No hard message count cap — rely only on character budget.
    # Tool results (embedded in user messages) are truncated to 1500 chars to preserve
    # budget for more messages and avoid crowding out the original task.
    NEEDSREVIEW_MARKERS = ("需求回显", "已了解规则", "等待用户输入", "待执行任务", "待确认事项",
                           "[需求回显]", "**需求回显**")
    msg_count = 0
    for msg in reversed(messages):
        role = msg.get("role", "")
        if role not in ("user", "assistant", "system", "tool"):
            continue
        if tools and role == "system":
            continue

        # ── OpenAI-format tool result (role="tool") ──────────────────────────
        # These were previously silently dropped, causing the model to never see
        # tool results and loop forever repeating the same tool call.
        if role == "tool":
            tool_content = msg.get("content", "") or ""
            tool_call_id = msg.get("tool_call_id", "")
            if isinstance(tool_content, list):
                tool_content = "\n".join(
                    p.get("text", "") for p in tool_content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            elif not isinstance(tool_content, str):
                tool_content = str(tool_content)
            if len(tool_content) > 1500:
                tool_content = tool_content[:1500] + "...[truncated]"
            line = f"[Tool Result]{(' id=' + tool_call_id) if tool_call_id else ''}\n{tool_content}\n[/Tool Result]"
            if used + len(line) + 2 > budget and history_parts:
                break
            history_parts.insert(0, line)
            used += len(line) + 2
            msg_count += 1
            continue

        text = _extract_text(msg.get("content", ""),
                             user_tool_mode=(bool(tools) and role == "user"))

        # ── OpenAI-format assistant tool_calls (content=null + tool_calls[]) ─
        # When an assistant message has tool_calls but content is null/empty,
        # render each tool_call as ##TOOL_CALL## so the model sees what it called.
        if role == "assistant" and not text and msg.get("tool_calls"):
            tc_parts = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, ValueError):
                    args = {"raw": args_str}
                tc_parts.append(
                    f'##TOOL_CALL##\n{{"name": {json.dumps(name)}, "input": {json.dumps(args, ensure_ascii=False)}}}\n##END_CALL##'
                )
            text = "\n".join(tc_parts)

        # Skip assistant messages that are just needs-review boilerplate
        if tools and role == "assistant" and any(m in text for m in NEEDSREVIEW_MARKERS):
            log.debug(f"[Prompt] 跳过需求回显式 assistant 消息 ({len(text)}字)")
            msg_count += 1
            continue
        # Truncate tool results (large user messages containing [Tool Result]) aggressively
        # so they don't crowd out other context. Plain user messages get more space.
        is_tool_result = role == "user" and ("[Tool Result]" in text or "[tool result]" in text.lower()
                                              or text.startswith("{") or "\"results\"" in text[:100])
        max_len = 1500 if is_tool_result else 8000
        if len(text) > max_len:
            text = text[:max_len] + "...[truncated]"
        prefix = {"user": "Human: ", "assistant": "Assistant: ", "system": "System: "}.get(role, "")
        line = f"{prefix}{text}"
        if used + len(line) + 2 > budget and history_parts:
            break
        history_parts.insert(0, line)
        used += len(line) + 2
        msg_count += 1

    # 原始任务保护：若第一条 user 消息被挤出了历史窗口，强制补回最前
    # 这确保模型始终知道用户的原始任务是什么
    if tools and messages:
        first_user = next((m for m in messages if m.get("role") == "user"), None)
        if first_user:
            first_text = _extract_text(first_user.get("content", ""), user_tool_mode=True)
            first_short = first_text[:800] + ("...[原始任务截断]" if len(first_text) > 800 else "")
            first_line = f"Human: {first_short}"
            # Check if first user message is already at the start of history
            if not history_parts or not history_parts[0].startswith(f"Human: {first_text[:60]}"):
                history_parts.insert(0, first_line)
                log.debug(f"[Prompt] 补回原始任务消息，确保上下文完整 ({len(first_short)}字)")

    if tools:
        log.info(f"[Prompt] 工具模式: {len(history_parts)} 条历史消息, {used}字 history + {len(tools_part)}字 tool指令")
    parts = []
    if sys_part: parts.append(sys_part)
    parts.extend(history_parts)
    # Tool instructions go LAST — right before "Assistant:" so they have highest priority
    if tools_part: parts.append(tools_part)
    parts.append("Assistant:")
    return "\n\n".join(parts)


def _inject_format_reminder(prompt: str, tool_name: str) -> str:
    """Inject a format correction reminder into the prompt before the final 'Assistant:' tag.
    Used when Qwen server returns 'Tool X does not exists.' (native call was intercepted)."""
    reminder = (
        f"[CORRECTION]: You called '{tool_name}' using the WRONG format — "
        f"the server BLOCKED it with 'Tool {tool_name} does not exists.'. "
        f"You MUST use ##TOOL_CALL## format and NOTHING ELSE:\n"
        f"##TOOL_CALL##\n"
        f'{{\"name\": \"{tool_name}\", \"input\": {{...your args here...}}}}\n'
        f"##END_CALL##\n"
        f"DO NOT use JSON without delimiters. DO NOT use any XML tags. ONLY ##TOOL_CALL##.\n"
    )
    prompt = prompt.rstrip()
    if prompt.endswith("Assistant:"):
        return prompt[: -len("Assistant:")] + reminder + "\nAssistant:"
    return prompt + "\n\n" + reminder + "\nAssistant:"


def _find_tool_use_json(text: str, tool_names: set):
    """Find a tool_use JSON object in text. First tries exact name match, then any tool_use."""
    candidates = []
    i = 0
    while i < len(text):
        pos = text.find('{', i)
        if pos == -1:
            break
        depth = 0
        for j in range(pos, len(text)):
            if text[j] == '{': depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[pos:j+1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and obj.get("type") == "tool_use" and obj.get("name"):
                            candidates.append((pos, obj))
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        i = pos + 1

    if not candidates:
        return None

    # Prefer exact name match
    for pos, obj in candidates:
        if obj.get("name") in tool_names:
            return pos, obj

    # Fallback: return first tool_use found (model used wrong name but right format)
    # Remap to closest tool name if possible
    pos, obj = candidates[0]
    model_name = obj.get("name", "")
    # Try to find best matching tool name by substring
    best = None
    for tn in tool_names:
        if model_name in tn or tn in model_name:
            best = tn
            break
    if best is None and tool_names:
        best = next(iter(tool_names))  # use first available tool as last resort
    if best:
        obj = dict(obj)
        obj["name"] = best
    return pos, obj


def _parse_tool_calls(answer: str, tools: list):
    if not tools:
        return [{"type": "text", "text": answer}], "end_turn"
    tool_names = {t.get("name") for t in tools if t.get("name")}
    log.debug(f"[ToolParse] 原始回复({len(answer)}字): {answer[:200]!r}")

    def _make_tool_block(name, input_data, prefix=""):
        # Snap name to closest known tool if needed
        if name not in tool_names and tool_names:
            best = next((n for n in tool_names if name.lower() in n.lower() or n.lower() in name.lower()), None)
            name = best or next(iter(tool_names))
        tool_id = f"toolu_{uuid.uuid4().hex[:8]}"
        blocks = []
        if prefix:
            blocks.append({"type": "text", "text": prefix})
        blocks.append({"type": "tool_use", "id": tool_id, "name": name, "input": input_data})
        return blocks, "tool_use"

    # 1. Primary: ##TOOL_CALL##...##END_CALL## (safe, no XML — Qwen server won't intercept)
    tc_m = re.search(r'##TOOL_CALL##\s*(.*?)\s*##END_CALL##', answer, re.DOTALL | re.IGNORECASE)
    if tc_m:
        try:
            obj = json.loads(tc_m.group(1))
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
            if isinstance(inp, str):
                try: inp = json.loads(inp)
                except: inp = {"value": inp}
            prefix = answer[:tc_m.start()].strip()
            log.info(f"[ToolParse] ✓ ##TOOL_CALL## 格式: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"[ToolParse] ##TOOL_CALL## 格式解析失败: {e}, content={tc_m.group(1)[:100]!r}")

    # 2. XML: <tool_call>...</tool_call>
    xml_m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', answer, re.DOTALL | re.IGNORECASE)
    if xml_m:
        try:
            obj = json.loads(xml_m.group(1))
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
            if isinstance(inp, str):
                try: inp = json.loads(inp)
                except: inp = {"value": inp}
            prefix = answer[:xml_m.start()].strip()
            log.info(f"[ToolParse] ✓ XML格式 <tool_call>: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"[ToolParse] XML格式解析失败: {e}, content={xml_m.group(1)[:100]!r}")

    # 2. Code block: ```tool_call\n...\n```
    cb_m = re.search(r'```tool_call\s*\n(.*?)\n```', answer, re.DOTALL)
    if cb_m:
        try:
            obj = json.loads(cb_m.group(1).strip())
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", {}))
            if isinstance(inp, str):
                try: inp = json.loads(inp)
                except: inp = {"value": inp}
            prefix = answer[:cb_m.start()].strip()
            log.info(f"[ToolParse] ✓ 代码块格式 tool_call: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning(f"[ToolParse] 代码块格式解析失败: {e}")

    # 3. Qwen native format: {"name":"...","arguments":"..."} (no "type" key)
    try:
        stripped_tmp = re.sub(r'```(?:json)?\s*\n?', '', answer)
        stripped_tmp = re.sub(r'\n?```', '', stripped_tmp).strip()
        if stripped_tmp.startswith('{') and '"name"' in stripped_tmp:
            obj = json.loads(stripped_tmp)
            if "name" in obj and "type" not in obj:
                name = obj.get("name", "")
                args = obj.get("arguments", obj.get("input", obj.get("parameters", {})))
                if isinstance(args, str):
                    try: args = json.loads(args)
                    except: args = {"value": args}
                if name in tool_names or tool_names:
                    log.info(f"[ToolParse] ✓ Qwen原生格式: name={name!r}, args={str(args)[:120]}")
                    return _make_tool_block(name, args)
    except (json.JSONDecodeError, ValueError):
        pass

    # 4. Fallback: old {"type":"tool_use",...} JSON
    stripped = re.sub(r'```json\s*\n?', '', answer)
    stripped = re.sub(r'\n?```', '', stripped)
    result = _find_tool_use_json(stripped, tool_names)
    if result:
        pos, tool_call = result
        prefix = stripped[:pos].strip()
        tool_id = tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:8]}"
        log.info(f"[ToolParse] ✓ 旧JSON格式 tool_call: name={tool_call['name']!r}")
        blocks = []
        if prefix:
            blocks.append({"type": "text", "text": prefix})
        blocks.append({"type": "tool_use", "id": tool_id,
                        "name": tool_call["name"], "input": tool_call.get("input", {})})
        return blocks, "tool_use"

    log.warning(f"[ToolParse] ✗ 未检测到工具调用，作为普通文本返回。工具列表: {tool_names}")
    return [{"type": "text", "text": answer}], "end_turn"


def messages_to_prompt(req_data: dict) -> tuple:
    messages = req_data.get("messages", [])
    tools = _normalize_tools(req_data.get("tools", []))
    system_prompt = ""
    sys_field = req_data.get("system", "")
    if isinstance(sys_field, list):
        system_prompt = " ".join(p.get("text", "") for p in sys_field if isinstance(p, dict))
    elif isinstance(sys_field, str):
        system_prompt = sys_field
    if not system_prompt:
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = _extract_text(msg.get("content", ""))
                break
    return _build_prompt(system_prompt, messages, tools), tools


# ─── Stats ──────────────────────────────────────────────
stats = {"requests": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0, "start_time": 0.0}

# ─── Globals ────────────────────────────────────────────
store: Optional[AccountStore] = None
pool: Optional[AccountPool] = None
engine: Optional[BrowserEngine] = None
client: Optional[QwenClient] = None
config: Optional[Config] = None


# ─── FastAPI App ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, pool, engine, client, config, API_KEYS, MAX_INFLIGHT_PER_ACCOUNT
    stats["start_time"] = time.time()
    config = Config(CONFIG_FILE)
    # Merge keys from config + CLI
    for k in config.keys:
        API_KEYS.add(k)
    # Merge model aliases
    if config.model_aliases:
        MODEL_MAP.update(config.model_aliases)
    MAX_INFLIGHT_PER_ACCOUNT = config.max_inflight
    store = AccountStore(ACCOUNTS_FILE)
    pool = AccountPool(store, max_inflight=MAX_INFLIGHT_PER_ACCOUNT)
    engine = BrowserEngine(pool_size=BROWSER_POOL_SIZE)
    client = QwenClient(engine)
    # Start browser engine in background so the HTTP server is immediately ready
    await engine.start()
    yield
    await engine.stop()


app = FastAPI(title="qwen2api", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["*"])


# ─── Auth ───────────────────────────────────────────────
def _extract_key(request: Request) -> str:
    """Extract API key from request headers/params."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        k = auth[7:].strip()
        if k:
            return k
    xkey = (request.headers.get("x-api-key", "")
            or request.headers.get("x-goog-api-key", "")).strip()
    if xkey:
        return xkey
    return (request.query_params.get("key", "")
            or request.query_params.get("api_key", "")).strip()


def verify_api_key(request: Request):
    """
    Lenient check for API routes (/v1/*, /v1beta/*):
    - If API_KEYS is empty → open access (no key required)
    - If API_KEYS is set → accept admin_key, any configured key, OR any non-empty key
      (allows Claude Code / OpenCode to connect with their own keys without reconfiguration)
    """
    admin_k = config.admin_key if config else "admin"
    if not API_KEYS:
        return  # open access
    key = _extract_key(request)
    # Accept admin key or any configured key as before
    if key == admin_k or key in API_KEYS:
        return
    # Also accept any non-empty key — API routes are not the security boundary
    # (Admin routes use verify_admin_key for strict checking)
    if key:
        return
    raise HTTPException(401, {"error": {"type": "authentication_error",
                                        "message": "No API key provided"}})


def verify_admin_key(request: Request):
    """
    Strict check for admin routes (/admin/*):
    - Must match admin_key ONLY. Regular API keys do NOT grant admin access.
    """
    admin_k = config.admin_key if config else "admin"
    key = _extract_key(request)
    if key == admin_k:
        return
    raise HTTPException(401, {"error": {"type": "authentication_error",
                                        "message": "Invalid admin key"}})


async def _bg_refresh(acc: Account):
    """Background token refresh — does not block the request.
    If refresh fails or account is pending activation, tries to activate via email."""
    try:
        ok = await client.refresh_token(acc)
        if ok:
            if not acc.activation_pending:
                acc.valid = True
                log.info(f"[BGRefresh] {acc.email} token刷新成功，账号已恢复")
                return
            # Token refreshed but account still needs activation — don't mark valid yet
            log.info(f"[BGRefresh] {acc.email} token刷新成功，但账号仍待激活，继续尝试激活...")
        else:
            log.warning(f"[BGRefresh] {acc.email} 刷新失败，尝试邮件激活...")
        activated = await activate_account(acc)
        if activated:
            acc.activation_pending = False
            acc.valid = True
            store.save()  # 持久化激活状态，重启后不再重复激活
        else:
            log.warning(f"[BGRefresh] {acc.email} 激活失败，账号保持失效")
    except Exception as e:
        log.warning(f"[BGRefresh] {acc.email} 刷新异常: {e}")


def _handle_error(acc: Account, err_msg: str, tried: set):
    """Process an account error. Only switches account on official Qwen errors (auth/rate-limit).
    Transient/system errors are logged but do NOT switch accounts."""
    if _is_rate_limit_error(err_msg):
        acc.transient_failures = 0
        pool.mark_rate_limited(acc)
        tried.add(acc.email)
    elif _is_pending_activation_error(err_msg):
        acc.transient_failures = 0
        acc.activation_pending = True
        pool.mark_invalid(acc)
        log.warning(f"[Retry] {acc.email} 账号待激活，切换账号，后台触发激活")
        asyncio.create_task(_bg_refresh(acc))
        tried.add(acc.email)
    elif _is_auth_error(err_msg):
        acc.transient_failures = 0
        pool.mark_invalid(acc)
        log.warning(f"[Retry] {acc.email} Qwen认证失败，切换账号，后台刷新token")
        asyncio.create_task(_bg_refresh(acc))
        tried.add(acc.email)
    else:
        # Transient/system error: log only, keep using same account
        acc.transient_failures += 1
        log.error(f"[Retry] {acc.email} 暂态失败 {acc.transient_failures}: {err_msg[:120]}")


# ─── Helper: acquire account with retry ─────────────────
async def _acquire_with_retry(prompt: str, model: str, tools: list, max_retries: int = MAX_RETRIES):
    """Try to complete a chat request with retry + account switching.
    Returns (answer, reasoning)."""
    tried: set = set()
    last_error = None
    has_tools = bool(tools)
    for attempt in range(max_retries):
        acc = await pool.acquire_wait(timeout=30, exclude=tried)
        if not acc:
            rl = [a for a in store.accounts if a.valid and a.is_rate_limited()]
            if rl and not tried:
                soonest = min(a.rate_limited_until for a in rl)
                wait_sec = int(soonest - time.time()) + 1
                raise HTTPException(429, {"error": {"message": f"所有账号限速中，最快 {wait_sec}s 后恢复", "type": "rate_limit_error"}})
            if tried:
                raise HTTPException(429, {"error": {"message": "所有账号已耗尽或忙碌", "type": "rate_limit_error"}})
            raise HTTPException(503, {"error": {"message": "无可用账号", "type": "server_error"}})
        try:
            answer, reasoning = await client.chat_completions(acc.token, model, prompt,
                                                              has_custom_tools=has_tools)
            # Detect Qwen native tool call interception: "Tool X does not exists."
            native_blocked_m = re.match(r'^Tool (\w+) does not exists?\.?$', answer.strip())
            if native_blocked_m and has_tools and attempt < max_retries - 1:
                blocked_name = native_blocked_m.group(1)
                pool.release(acc)
                log.warning(f"[NativeBlock] Qwen拦截原生工具调用 '{blocked_name}'，注入格式纠正后重试 (attempt {attempt+1}/{max_retries})")
                prompt = _inject_format_reminder(prompt, blocked_name)
                await asyncio.sleep(0.5)
                continue
            acc.transient_failures = 0  # reset on success
            pool.release(acc)
            return answer, reasoning
        except Exception as e:
            pool.release(acc)
            err_str = str(e)
            if "Browser engine failed to start" in err_str:
                raise HTTPException(503, {"error": {"message": "浏览器引擎未就绪，请稍后再试", "type": "server_error"}})
            last_error = e
            _handle_error(acc, err_str, tried)
            if not _is_auth_error(err_str) and not _is_rate_limit_error(err_str):
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
    raise HTTPException(502, {"error": {"message": str(last_error), "type": "server_error"}})


async def _stream_with_retry(prompt: str, model: str, tools: list, max_retries: int = MAX_RETRIES):
    """Try to get stream events with retry. Returns (events, chat_id, acc)."""
    tried: set = set()
    last_error = None
    has_tools = bool(tools)
    for attempt in range(max_retries):
        acc = await pool.acquire_wait(timeout=30, exclude=tried)
        if not acc:
            rl = [a for a in store.accounts if a.valid and a.is_rate_limited()]
            if rl and not tried:
                soonest = min(a.rate_limited_until for a in rl)
                wait_sec = int(soonest - time.time()) + 1
                raise HTTPException(429, {"error": {"message": f"所有账号限速中，最快 {wait_sec}s 后恢复", "type": "rate_limit_error"}})
            if tried:
                raise HTTPException(429, {"error": {"message": "所有账号已耗尽或忙碌", "type": "rate_limit_error"}})
            raise HTTPException(503, {"error": {"message": "无可用账号", "type": "server_error"}})
        try:
            events, chat_id = await client.chat_stream_events(acc.token, model, prompt,
                                                              has_custom_tools=has_tools)
            acc.transient_failures = 0  # reset on success
            return events, chat_id, acc
        except Exception as e:
            pool.release(acc)
            err_str = str(e)
            if "Browser engine failed to start" in err_str:
                raise HTTPException(503, {"error": {"message": "浏览器引擎未就绪，请稍后再试", "type": "server_error"}})
            last_error = e
            _handle_error(acc, err_str, tried)
            if not _is_auth_error(err_str) and not _is_rate_limit_error(err_str) and not _is_pending_activation_error(err_str):
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
    raise HTTPException(502, {"error": {"message": str(last_error), "type": "server_error"}})


# ─── Health ─────────────────────────────────────────────
@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}

@app.get("/readyz")
@app.head("/readyz")
async def readyz():
    return {"status": "ready"}


# ─── GET /v1/models ─────────────────────────────────────
@app.get("/v1/models")
@app.get("/models")
async def list_models_endpoint(request: Request):
    verify_api_key(request)
    ts = int(time.time())
    # Static list — no browser needed, avoids pool exhaustion
    qwen_models = [
        "qwen3.6-plus", "qwen3.5-plus", "qwen3.5-omni-plus", "qwen3.5-flash",
        "qwen3.5-max-2026-03-08", "qwen3.6-plus-preview", "qwen3.5-397b-a17b",
        "qwen3.5-122b-a10b", "qwen3.5-omni-flash", "qwen3.5-27b", "qwen3.5-35b-a3b",
        "qwen3-max-2026-01-23", "qwen-plus-2025-07-28", "qwen3-coder-plus",
        "qwen3-vl-plus", "qwen3-omni-flash-2025-12-01", "qwen-max-latest",
    ]
    data = [{"id": m, "object": "model", "created": ts, "owned_by": "qwen"} for m in qwen_models]
    for alias in MODEL_MAP:
        data.append({"id": alias, "object": "model", "created": ts, "owned_by": "qwen2api"})
    return {"object": "list", "data": data}


# ─── POST /v1/chat/completions ──────────────────────────
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions_endpoint(request: Request):
    verify_api_key(request)
    stats["requests"] += 1
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
    model_name = req_data.get("model", DEFAULT_MODEL)
    qwen_model = resolve_model(model_name)
    stream = req_data.get("stream", False)
    _check_conv_switch(req_data.get("messages", []))
    prompt, tools = messages_to_prompt(req_data)
    log.info(f"[OAI] model={qwen_model}, stream={stream}, tools={[t.get('name') for t in tools]}, prompt_len={len(prompt)}")

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if stream:
        async def generate():
            current_prompt = prompt  # local copy so we can modify for native-block retries
            for stream_attempt in range(MAX_RETRIES):
              try:
                events, chat_id, acc = await _stream_with_retry(current_prompt, qwen_model, tools)

                # Buffer all text first (Qwen fetches full SSE at once anyway)
                answer_text = ""
                reasoning_text = ""
                native_tc_chunks: dict = {}
                for evt in events:
                    if evt["type"] != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        reasoning_text += content
                    elif phase == "answer" and content:
                        answer_text += content
                    elif phase == "tool_call" and content:
                        tc_id = evt.get("extra", {}).get("tool_call_id", "tc_0")
                        if tc_id not in native_tc_chunks:
                            native_tc_chunks[tc_id] = {"name": "", "args": ""}
                        try:
                            chunk = json.loads(content)
                            if "name" in chunk:
                                native_tc_chunks[tc_id]["name"] = chunk["name"]
                            if "arguments" in chunk:
                                native_tc_chunks[tc_id]["args"] += chunk["arguments"]
                        except (json.JSONDecodeError, ValueError):
                            native_tc_chunks[tc_id]["args"] += content
                    if evt.get("status") == "finished" and phase == "answer":
                        break
                if native_tc_chunks and not answer_text:
                    log.info(f"[SSE-stream] 检测到 Qwen 原生 tool_call 事件: {list(native_tc_chunks.keys())}")
                    tc_parts = []
                    for tc_id, tc in native_tc_chunks.items():
                        name = tc["name"]
                        try:
                            inp = json.loads(tc["args"]) if tc["args"] else {}
                        except (json.JSONDecodeError, ValueError):
                            inp = {"raw": tc["args"]}
                        tc_parts.append(f'<tool_call>{{"name": {json.dumps(name)}, "input": {json.dumps(inp, ensure_ascii=False)}}}</tool_call>')
                    answer_text = "\n".join(tc_parts)
                elif answer_text:
                    log.debug(f"[SSE-stream] 收到 answer 文本({len(answer_text)}字): {answer_text[:120]!r}")

                # Detect Qwen native tool call interception before yielding
                native_blocked_m = re.match(r'^Tool (\w+) does not exists?\.?$', answer_text.strip())
                if native_blocked_m and tools and stream_attempt < MAX_RETRIES - 1:
                    blocked_name = native_blocked_m.group(1)
                    pool.release(acc)
                    asyncio.create_task(client.delete_chat(acc.token, chat_id))
                    log.warning(f"[NativeBlock-Stream] Qwen拦截原生工具调用 '{blocked_name}'，注入格式纠正后重试 (attempt {stream_attempt+1}/{MAX_RETRIES})")
                    current_prompt = _inject_format_reminder(current_prompt, blocked_name)
                    await asyncio.sleep(0.5)
                    continue  # retry the stream call

                # Detect tool calls BEFORE yielding any content
                tool_blocks, stop = _parse_tool_calls(answer_text, tools)
                has_tool_call = stop == "tool_use"

                mk = lambda delta, finish=None: json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                }, ensure_ascii=False)

                # Role chunk
                yield f"data: {mk({'role': 'assistant'})}\n\n"

                if has_tool_call:
                    # Emit tool_calls chunks (OpenAI streaming format)
                    tc_list = [b for b in tool_blocks if b["type"] == "tool_use"]
                    for idx, tc in enumerate(tc_list):
                        # Function name chunk
                        yield f"data: {mk({'tool_calls': [{'index': idx, 'id': tc['id'], 'type': 'function', 'function': {'name': tc['name'], 'arguments': ''}}]})}\n\n"
                        # Arguments chunk
                        yield f"data: {mk({'tool_calls': [{'index': idx, 'function': {'arguments': json.dumps(tc.get('input', {}), ensure_ascii=False)}}]})}\n\n"
                    yield f"data: {mk({}, 'tool_calls')}\n\n"
                else:
                    # Thinking chunks
                    if reasoning_text:
                        yield f"data: {mk({'reasoning_content': reasoning_text})}\n\n"
                    # Content chunks
                    if answer_text:
                        yield f"data: {mk({'content': answer_text})}\n\n"
                    yield f"data: {mk({}, 'stop')}\n\n"

                yield "data: [DONE]\n\n"
                stats["tokens_out"] += len(answer_text)
                pool.release(acc)
                asyncio.create_task(client.delete_chat(acc.token, chat_id))
                return  # success — exit the retry loop
              except HTTPException as he:
                yield f"data: {json.dumps({'error': he.detail})}\n\n"
                stats["errors"] += 1
                return
              except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                stats["errors"] += 1
                return

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        answer_text, reasoning_text = await _acquire_with_retry(prompt, qwen_model, tools)
        tool_blocks, stop = _parse_tool_calls(answer_text, tools)
        has_tool_call = stop == "tool_use"
        stats["tokens_out"] += len(answer_text)

        if has_tool_call:
            # Return proper OpenAI tool_calls format
            tc_list = [b for b in tool_blocks if b["type"] == "tool_use"]
            oai_tool_calls = [{
                "id": tc["id"], "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc.get("input", {}), ensure_ascii=False)
                }
            } for tc in tc_list]
            msg = {"role": "assistant", "content": None, "tool_calls": oai_tool_calls}
            finish_reason = "tool_calls"
        else:
            msg = {"role": "assistant", "content": answer_text}
            if reasoning_text:
                msg["reasoning_content"] = reasoning_text
            finish_reason = "stop"

        return {"id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(answer_text),
                          "total_tokens": len(prompt) + len(answer_text)}}


# ─── POST /v1/messages (Anthropic) ──────────────────────
@app.post("/v1/messages")
@app.post("/messages")
@app.post("/anthropic/v1/messages")
async def anthropic_messages_endpoint(request: Request):
    verify_api_key(request)
    stats["requests"] += 1
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
    model_name = req_data.get("model", DEFAULT_MODEL)
    qwen_model = resolve_model(model_name)
    stream = req_data.get("stream", False)
    _check_conv_switch(req_data.get("messages", []))
    prompt, tools = messages_to_prompt(req_data)
    log.info(f"[ANT] model={qwen_model}, stream={stream}, tools={[t.get('name') for t in tools]}, prompt_len={len(prompt)}")
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"

    if stream:
        async def generate():
            current_prompt = prompt  # local copy for native-block retries
            for stream_attempt in range(MAX_RETRIES):
              try:
                events, chat_id, acc = await _stream_with_retry(current_prompt, qwen_model, tools)

                # Buffer all events first
                thinking_chunks = []
                answer_chunks = []
                for evt in events:
                    if evt["type"] != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        thinking_chunks.append(content)
                    elif phase == "answer" and content:
                        answer_chunks.append(content)
                    if evt.get("status") == "finished" and phase == "answer":
                        break

                answer_text = "".join(answer_chunks)
                reasoning_text = "".join(thinking_chunks)

                # Detect Qwen native tool call interception before emitting any blocks
                native_blocked_m = re.match(r'^Tool (\w+) does not exists?\.?$', answer_text.strip())
                if native_blocked_m and tools and stream_attempt < MAX_RETRIES - 1:
                    blocked_name = native_blocked_m.group(1)
                    pool.release(acc)
                    asyncio.create_task(client.delete_chat(acc.token, chat_id))
                    log.warning(f"[NativeBlock-ANT] Qwen拦截原生工具调用 '{blocked_name}'，注入格式纠正后重试 (attempt {stream_attempt+1}/{MAX_RETRIES})")
                    current_prompt = _inject_format_reminder(current_prompt, blocked_name)
                    await asyncio.sleep(0.5)
                    continue  # retry the stream call

                # Detect tool calls BEFORE emitting any blocks
                if tools:
                    blocks, stop_reason = _parse_tool_calls(answer_text, tools)
                else:
                    blocks = [{"type": "text", "text": answer_text}]
                    stop_reason = "end_turn"

                # message_start
                yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model_name, 'stop_reason': None, 'usage': {'input_tokens': len(current_prompt), 'output_tokens': 0}}})}\n\n"

                block_idx = 0

                # Thinking block
                if reasoning_text:
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'thinking', 'thinking': ''}})}\n\n"
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'thinking_delta', 'thinking': reasoning_text}})}\n\n"
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_idx})}\n\n"
                    block_idx += 1

                # Text + tool_use blocks
                for blk in blocks:
                    if blk["type"] == "text" and blk.get("text"):
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'text_delta', 'text': blk['text']}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_idx})}\n\n"
                        block_idx += 1
                    elif blk["type"] == "tool_use":
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'tool_use', 'id': blk['id'], 'name': blk['name'], 'input': {}}})}\n\n"
                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(blk.get('input', {}), ensure_ascii=False)}})}\n\n"
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_idx})}\n\n"
                        block_idx += 1

                yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason}, 'usage': {'output_tokens': len(answer_text)}})}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                stats["tokens_out"] += len(answer_text)
                pool.release(acc)
                asyncio.create_task(client.delete_chat(acc.token, chat_id))
                return  # success — exit the retry loop
              except Exception as e:
                log.error(f"Anthropic stream error: {e}")
                stats["errors"] += 1
                return

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        answer_text, reasoning_text = await _acquire_with_retry(prompt, qwen_model, tools)
        content_blocks = []
        if reasoning_text:
            content_blocks.append({"type": "thinking", "thinking": reasoning_text})
        blocks, stop_reason = _parse_tool_calls(answer_text, tools)
        content_blocks.extend(blocks)
        stats["tokens_out"] += len(answer_text)
        return {"id": msg_id, "type": "message", "role": "assistant", "model": model_name,
                "content": content_blocks, "stop_reason": stop_reason, "stop_sequence": None,
                "usage": {"input_tokens": len(prompt), "output_tokens": len(answer_text)}}


# ─── Gemini Compat ──────────────────────────────────────
def _gemini_to_prompt(req_data: dict) -> str:
    """Convert Gemini generateContent request to prompt string."""
    contents = req_data.get("contents", [])
    parts_text = []
    for content in contents:
        role = content.get("role", "user")
        for part in content.get("parts", []):
            text = part.get("text", "")
            if text:
                prefix = "Human: " if role == "user" else "Assistant: "
                parts_text.append(f"{prefix}{text}")
    system = req_data.get("systemInstruction", {})
    if system:
        sys_parts = system.get("parts", [])
        sys_text = " ".join(p.get("text", "") for p in sys_parts)
        if sys_text:
            parts_text.insert(0, f"<system>\n{sys_text}\n</system>")
    parts_text.append("Assistant:")
    return "\n\n".join(parts_text)


@app.post("/v1beta/models/{model_path}:generateContent")
@app.post("/v1/models/{model_path}:generateContent")
async def gemini_generate(model_path: str, request: Request):
    verify_api_key(request)
    stats["requests"] += 1
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
    qwen_model = resolve_model(model_path)
    prompt = _gemini_to_prompt(req_data)
    answer, reasoning = await _acquire_with_retry(prompt, qwen_model, [])
    stats["tokens_out"] += len(answer)
    return {
        "candidates": [{"content": {"parts": [{"text": answer}], "role": "model"},
                         "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": len(prompt), "candidatesTokenCount": len(answer),
                          "totalTokenCount": len(prompt) + len(answer)},
    }


@app.post("/v1beta/models/{model_path}:streamGenerateContent")
@app.post("/v1/models/{model_path}:streamGenerateContent")
async def gemini_stream(model_path: str, request: Request):
    verify_api_key(request)
    stats["requests"] += 1
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
    qwen_model = resolve_model(model_path)
    prompt = _gemini_to_prompt(req_data)

    async def generate():
        try:
            events, chat_id, acc = await _stream_with_retry(prompt, qwen_model, [])
            for evt in events:
                if evt["type"] != "delta":
                    continue
                if evt.get("phase") == "answer" and evt.get("content"):
                    chunk = {"candidates": [{"content": {"parts": [{"text": evt["content"]}], "role": "model"}, "index": 0}]}
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                if evt.get("status") == "finished" and evt.get("phase") == "answer":
                    break
            pool.release(acc)
            asyncio.create_task(client.delete_chat(acc.token, chat_id))
        except Exception as e:
            yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
            stats["errors"] += 1

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ─── GET /status ────────────────────────────────────────
@app.get("/status")
async def get_status():
    uptime = int(time.time() - stats["start_time"]) if stats["start_time"] else 0
    pool_status = pool.status() if pool else {}
    br = engine._started if engine else False
    bi = (not br) and engine and not engine._ready.is_set()  # True = still initializing
    return {"status": "running", "version": VERSION, "uptime_seconds": uptime,
            "accounts": pool_status.get("total", 0), "accounts_valid": pool_status.get("valid", 0),
            "in_use": pool_status.get("in_use", 0), "waiting": pool_status.get("waiting", 0),
            "max_inflight_per_account": pool_status.get("max_inflight_per_account", 0),
            "requests": stats["requests"], "errors": stats["errors"],
            "browser_ready": br, "browser_initializing": bi,
            "max_accounts": MAX_ACCOUNTS}


# ─── Admin: Keys ────────────────────────────────────────
@app.get("/admin/keys")
async def admin_list_keys(request: Request):
    verify_admin_key(request)
    return {"keys": sorted(API_KEYS)}


@app.post("/admin/keys")
async def admin_create_key(request: Request):
    # Public: anyone can generate a key (no auth required)
    key = f"sk-qwen-{secrets.token_hex(16)}"
    API_KEYS.add(key)
    if config:
        config.keys = sorted(API_KEYS)
        config.save()
    return {"key": key}


@app.delete("/admin/keys/{key}")
async def admin_delete_key(key: str, request: Request):
    verify_admin_key(request)
    API_KEYS.discard(key)
    if config:
        config.keys = sorted(API_KEYS)
        config.save()
    return {"ok": True}


# ─── Admin: Accounts ───────────────────────────────────
@app.get("/admin/accounts")
async def admin_list_accounts(request: Request):
    verify_admin_key(request)
    return [{"email": a.email, "username": a.username, "valid": a.valid,
             "last_used": a.last_used, "inflight": a.inflight,
             "rate_limited_until": a.rate_limited_until} for a in store.accounts]


@app.post("/admin/accounts")
async def admin_add_account(request: Request):
    verify_admin_key(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
    token = data.get("token", "")
    if not token:
        raise HTTPException(400, {"error": "token is required"})
    valid = await client.verify_token(token)
    if not valid:
        raise HTTPException(400, {"error": "Invalid token"})
    acc = Account(email=data.get("email", f"manual_{int(time.time())}@qwen"),
                  password=data.get("password", ""), token=token,
                  cookies=data.get("cookies", ""), username=data.get("username", ""))
    store.add(acc)
    return {"ok": True, "email": acc.email}


@app.delete("/admin/accounts/{email}")
async def admin_remove_account(email: str, request: Request):
    verify_admin_key(request)
    store.remove(email)
    return {"ok": True}


_last_register_time = 0.0
REGISTER_COOLDOWN = 120  # seconds between registrations to prevent runaway
MAX_ACCOUNTS = 20        # pool size limit — registration blocked when reached

# Feature access control (split to avoid plaintext recovery by simple grep)
_FA1 = "e666a5a368acd3dff151e251201f4c9a"
_FA2 = "0fe3c5ade78c15e17280f34d784bf026"


@app.post("/admin/unlock-register")
async def unlock_register_feature(request: Request):
    """Validate access credentials for the hidden registration feature.
    Returns ok:true only when the correct credentials are supplied."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    u = body.get("u", "")
    p = body.get("p", "")
    if not u or not p:
        return JSONResponse({"ok": False}, status_code=400)
    sig = hashlib.sha256((u + ":" + p).encode()).hexdigest()
    if hmac.compare_digest(sig, _FA1 + _FA2):
        return {"ok": True}
    return JSONResponse({"ok": False}, status_code=403)


@app.post("/admin/register")
async def admin_register(request: Request):
    global _last_register_time
    verify_admin_key(request)
    # Check pool size limit
    current = len(store.accounts) if store else 0
    if current >= MAX_ACCOUNTS:
        return JSONResponse(content={"ok": False, "error": f"账号池已满（{current}/{MAX_ACCOUNTS}），请先删除失效账号再注册"}, status_code=400)
    # Cooldown to prevent accidental repeated registrations
    now = time.time()
    since = now - _last_register_time
    if _last_register_time > 0 and since < REGISTER_COOLDOWN:
        wait = int(REGISTER_COOLDOWN - since)
        return JSONResponse(content={"ok": False, "error": f"注册冷却中，请等待 {wait}s"}, status_code=429)
    log.info(f"[Register] 管理员触发注册，来源IP: {request.client.host if request.client else '未知'}")
    _last_register_time = now
    try:
        result = await register_qwen_account()
        if result:
            store.add(result)
            total = len(store.accounts)
            log.info(f"[Register] 注册成功: {result.email}（当前账号数: {total}/{MAX_ACCOUNTS}）")
            return {"ok": True, "email": result.email, "total": total, "max": MAX_ACCOUNTS}
        _last_register_time = 0.0  # reset on failure so retry is allowed
        return JSONResponse(content={"ok": False, "error": "Registration failed"}, status_code=500)
    except Exception as e:
        _last_register_time = 0.0
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)


@app.post("/admin/accounts/{email}/verify")
async def admin_verify_one_account(email: str, request: Request):
    verify_admin_key(request)
    acc = next((a for a in store.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(404, {"error": "Account not found"})
    valid = await client.verify_token(acc.token)
    if not valid and acc.password:
        log.info(f"[Verify] {acc.email} token失效，尝试自动刷新...")
        valid = await client.refresh_token(acc)
    acc.valid = valid
    store.save()
    return {"email": acc.email, "valid": valid}


@app.post("/admin/accounts/{email}/activate")
async def admin_activate_account(email: str, request: Request):
    """Poll mail.chatgpt.org.uk for activation link, click it, login to get fresh token."""
    verify_admin_key(request)
    acc = next((a for a in store.accounts if a.email == email), None)
    if not acc:
        raise HTTPException(404, {"error": "Account not found"})
    if not acc.email.split("@")[1] if "@" in acc.email else True:
        return JSONResponse({"ok": False, "error": "邮箱格式错误"}, status_code=400)
    ok = await activate_account(acc)
    return {"ok": ok, "email": acc.email, "valid": acc.valid,
            "message": "激活成功" if ok else "激活失败，请检查邮箱是否在 mail.chatgpt.org.uk"}


async def admin_verify_accounts(request: Request):
    verify_admin_key(request)
    results = []
    for acc in store.accounts:
        valid = await client.verify_token(acc.token)
        if not valid and acc.password:
            # Token invalid — try to refresh automatically
            log.info(f"[Verify] {acc.email} token失效，尝试自动刷新...")
            valid = await client.refresh_token(acc)
        acc.valid = valid
        results.append({"email": acc.email, "valid": valid, "refreshed": not valid})
    store.save()
    return results


@app.get("/admin/queue/status")
async def admin_queue_status(request: Request):
    verify_admin_key(request)
    return pool.status() if pool else {}


# ─── Admin: Settings ────────────────────────────────────
@app.get("/admin/settings")
async def admin_get_settings(request: Request):
    verify_admin_key(request)
    return {"max_inflight_per_account": pool.max_inflight if pool else 2,
            "model_aliases": dict(MODEL_MAP), "version": VERSION}


@app.put("/admin/settings")
async def admin_update_settings(request: Request):
    verify_admin_key(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
    if "max_inflight_per_account" in data:
        val = int(data["max_inflight_per_account"])
        if pool:
            pool.max_inflight = val
        if config:
            config.max_inflight = val
            config.save()
    if "model_aliases" in data and isinstance(data["model_aliases"], dict):
        MODEL_MAP.update(data["model_aliases"])
        if config:
            config.model_aliases = data["model_aliases"]
            config.save()
    return {"ok": True}


@app.get("/admin/version")
async def admin_version():
    return {"version": VERSION}


# ─── Admin: Test ────────────────────────────────────────
@app.post("/admin/test")
async def admin_test(request: Request):
    verify_admin_key(request)
    acc = await pool.acquire_wait(timeout=10)
    if not acc:
        return {"ok": False, "error": "No accounts available"}
    try:
        answer, _ = await client.chat_completions(acc.token, DEFAULT_MODEL, "Say OK")
        pool.release(acc)
        return {"ok": True, "response": answer[:200]}
    except Exception as e:
        pool.release(acc)
        return {"ok": False, "error": str(e)}


# ─── Registration ───────────────────────────────────────
def _gen_password(length=14):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(random.choices(chars, k=length))
        if (any(c.isupper() for c in pwd) and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in "!@#$%^&*" for c in pwd)):
            return pwd


def _gen_username():
    first = random.choice(["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie",
                            "Drew", "Avery", "Quinn", "Blake", "Sage", "Reese", "Dakota", "Emery"])
    last = random.choice(["Smith", "Brown", "Wilson", "Lee", "Chen", "Wang", "Kim", "Park",
                           "Davis", "Miller", "Garcia", "Martinez", "Anderson", "Taylor", "Thomas"])
    return f"{first} {last}"


# ─── Temp-email (mail.chatgpt.org.uk) ─────────────────────
MAIL_BASE = "https://mail.chatgpt.org.uk"

class _EmailSession:
    """Sync email session using mail.chatgpt.org.uk (ported from xxx3.py)."""

    def __init__(self):
        from curl_cffi import requests as cffi_requests
        self._session = cffi_requests.Session(impersonate="chrome")
        self._session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._current_token = ""
        self._token_expires_at = 0
        self._initialized = False

    def _init_session(self) -> bool:
        try:
            resp = self._session.get(f"{MAIL_BASE}/", timeout=15)
            if resp.status_code != 200:
                return False
            match = re.search(r'window\.__BROWSER_AUTH\s*=\s*(\{[^}]+\})', resp.text)
            if match:
                auth_data = json.loads(match.group(1))
                self._current_token = auth_data.get("token", "")
                self._token_expires_at = auth_data.get("expires_at", 0)
                self._initialized = True
                return True
            return False
        except Exception as e:
            log.warning(f"[MailSession] init error: {e}")
            return False

    def _ensure_token(self) -> bool:
        if not self._initialized or not self._current_token or time.time() > self._token_expires_at - 120:
            return self._init_session()
        return True

    def get_email(self) -> str:
        if not self._ensure_token():
            raise Exception("mail.chatgpt.org.uk: session init failed")
        resp = self._session.get(
            f"{MAIL_BASE}/api/generate-email",
            headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                     "x-inbox-token": self._current_token},
            timeout=15,
        )
        if resp.status_code == 401:
            self._initialized = False
            self._init_session()
            resp = self._session.get(
                f"{MAIL_BASE}/api/generate-email",
                headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                         "x-inbox-token": self._current_token},
                timeout=15,
            )
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"mail.chatgpt.org.uk: generate-email failed: {data}")
        email = str(data.get("data", {}).get("email", "")).strip()
        new_tok = data.get("auth", {}).get("token", "")
        if new_tok:
            self._current_token = new_tok
            self._token_expires_at = data.get("auth", {}).get("expires_at", 0)
        return email

    def poll_verify_link(self, email: str, timeout_sec: int = 300) -> str:
        """Poll inbox for a Qwen verification link. Returns link or empty string."""
        keywords = ("qwen", "verify", "activate", "confirm", "aliyun", "alibaba", "qwenlm")
        log.info(f"[MailSession] Polling inbox for {email} (timeout {timeout_sec}s)...")
        deadline = time.time() + timeout_sec
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                resp = self._session.get(
                    f"{MAIL_BASE}/api/emails",
                    params={"email": email},
                    headers={"accept": "*/*", "referer": f"{MAIL_BASE}/",
                             "x-inbox-token": self._current_token},
                    timeout=15,
                )
                if resp.status_code == 401:
                    self._initialized = False
                    self._init_session()
                    time.sleep(3)
                    continue
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("auth", {}).get("token"):
                        self._current_token = data["auth"]["token"]
                        self._token_expires_at = data["auth"].get("expires_at", 0)
                    emails_list = data.get("data", {}).get("emails", [])
                    log.info(f"[MailSession] 第{attempt}次轮询，收件箱邮件数: {len(emails_list)}")
                    for msg in emails_list:
                        subject = str(msg.get("subject", ""))
                        # Log all available fields on first attempt to debug format issues
                        if attempt == 1:
                            log.debug(f"[MailSession] 邮件字段: {list(msg.keys())}, subject={subject!r}")
                        # Gather all text content from every possible field
                        parts = []
                        for field in ("html_content", "content", "body", "html", "text", "raw"):
                            v = msg.get(field)
                            if v:
                                parts.append(str(v))
                        # Also check nested structures
                        for field in ("payload", "data", "message"):
                            v = msg.get(field)
                            if isinstance(v, dict):
                                parts.extend(str(x) for x in v.values() if x)
                            elif isinstance(v, str) and v:
                                parts.append(v)
                        combined = " ".join(parts)
                        # Decode common HTML/JSON escapes
                        combined = (combined
                                    .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                                    .replace("\\u003c", "<").replace("\\u003e", ">")
                                    .replace("\\u0026", "&").replace("\\/", "/"))
                        all_links = re.findall(r'https?://[^\s"\'<>\\,\)]+', combined)
                        if attempt == 1 and all_links:
                            log.debug(f"[MailSession] 邮件中所有链接({len(all_links)}条): {all_links[:5]}")
                        for link in all_links:
                            link = link.rstrip(".,;)")
                            if any(kw in link.lower() for kw in keywords):
                                log.info(f"[MailSession] 找到验证链接: {link[:120]}...")
                                return link
                        # Fallback: if subject matches but no keyword-link found, return first http link
                        if any(kw in subject.lower() for kw in keywords) and all_links:
                            log.info(f"[MailSession] subject匹配，使用第一条链接: {all_links[0][:120]}")
                            return all_links[0]
                else:
                    log.warning(f"[MailSession] 邮件API HTTP {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                log.warning(f"[MailSession] 轮询异常: {e}")
            time.sleep(5)
        log.error("[MailSession] 超时：未收到验证邮件")
        return ""


class _AsyncMailClient:
    """Async wrapper around _EmailSession."""

    def __init__(self):
        self._sess: Optional[_EmailSession] = None
        self._email = ""

    async def __aenter__(self):
        self._sess = await asyncio.to_thread(_EmailSession)
        return self

    async def __aexit__(self, *args):
        pass

    async def generate_email(self) -> str:
        self._email = await asyncio.to_thread(self._sess.get_email)
        log.info(f"[MailClient] Generated: {self._email}")
        return self._email

    async def get_verify_link(self, timeout_sec: int = 300) -> str:
        return await asyncio.to_thread(self._sess.poll_verify_link, self._email, timeout_sec)


async def register_qwen_account() -> Optional[Account]:

    log.info("[Register] ── 开始注册流程 ──")
    async with _AsyncMailClient() as mail_client:
        log.info("[Register] [1/7] 生成临时邮箱...")
        email = await mail_client.generate_email()
        password = _gen_password()
        username = _gen_username()
        log.info(f"[Register] [1/7] 邮箱: {email}  用户名: {username}")

        async with _new_browser() as browser:
            page = await browser.new_page()
            log.info(f"[Register] [2/7] 打开注册页面: {BASE_URL}/auth?action=signup")
            try:
                await page.goto(f"{BASE_URL}/auth?action=signup", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

            log.info("[Register] [3/7] 填写注册表单...")
            name_input = None
            for sel in ['input[placeholder*="Full Name"]', 'input[placeholder*="Name"]']:
                try:
                    name_input = await page.wait_for_selector(sel, timeout=15000)
                    if name_input: break
                except Exception:
                    pass
            if not name_input:
                inputs = await page.query_selector_all('input')
                name_input = inputs[0] if len(inputs) >= 4 else None
            if not name_input:
                log.error("[Register] [3/7] 找不到姓名输入框，注册中止")
                return None

            await name_input.click(); await name_input.fill(username)
            log.info(f"[Register] [3/7]  ✓ 姓名: {username}")
            email_input = await page.query_selector('input[placeholder*="Email"]')
            if not email_input:
                inputs = await page.query_selector_all('input')
                email_input = inputs[1] if len(inputs) >= 2 else None
            if email_input: await email_input.click(); await email_input.fill(email)
            log.info(f"[Register] [3/7]  ✓ 邮箱: {email}")

            pwd_input = await page.query_selector('input[placeholder*="Password"]:not([placeholder*="Again"])')
            if not pwd_input:
                inputs = await page.query_selector_all('input')
                pwd_input = inputs[2] if len(inputs) >= 3 else None
            if pwd_input: await pwd_input.click(); await pwd_input.fill(password)

            confirm_input = await page.query_selector('input[placeholder*="Again"]')
            if not confirm_input:
                inputs = await page.query_selector_all('input')
                confirm_input = inputs[3] if len(inputs) >= 4 else None
            if confirm_input: await confirm_input.click(); await confirm_input.fill(password)
            log.info("[Register] [3/7]  ✓ 密码已填写")

            checkbox = await page.query_selector('input[type="checkbox"]')
            if checkbox and not await checkbox.is_checked(): await checkbox.click()
            else:
                agree = await page.query_selector('text=I agree')
                if agree: await agree.click()
            log.info("[Register] [3/7]  ✓ 同意条款")

            log.info("[Register] [4/7] 提交注册表单...")
            await asyncio.sleep(1)
            submit = await page.query_selector('button:has-text("Create Account")') or await page.query_selector('button[type="submit"]')
            if submit: await submit.click()
            log.info("[Register] [4/7] 已点击提交，等待页面跳转（6s）...")
            await asyncio.sleep(6)

            url_after = page.url
            log.info(f"[Register] [4/7] 提交后URL: {url_after}")

            # Check if already logged in (redirected to main page)
            token = None
            if BASE_URL in url_after and "auth" not in url_after:
                log.info("[Register] [5/7] 已跳转主页，尝试直接获取token...")
                await asyncio.sleep(3)
                token = await page.evaluate("localStorage.getItem('token')")
                if token:
                    log.info("[Register] [5/7] ✓ 注册后直接获取到token，跳过邮件验证")

            # If no token yet, try explicit login with email+password (faster than email poll)
            if not token:
                log.info("[Register] [5/7] 尝试用账号密码直接登录...")
                try:
                    await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    li_email = await page.query_selector('input[placeholder*="Email"]')
                    if li_email: await li_email.fill(email)
                    li_pwd = await page.query_selector('input[type="password"]')
                    if li_pwd: await li_pwd.fill(password)
                    li_btn = await page.query_selector('button:has-text("Log in")') or await page.query_selector('button[type="submit"]')
                    if li_btn: await li_btn.click()
                    await asyncio.sleep(8)
                    token = await page.evaluate("localStorage.getItem('token')")
                    if token:
                        log.info("[Register] [5/7] ✓ 直接登录成功，获取到token")
                except Exception as e:
                    log.warning(f"[Register] [5/7] 直接登录失败: {e}")

            # If still no token, poll email for verification link
            if not token:
                log.info("[Register] [6/7] 等待验证邮件（最多5分钟）...")
                verify_link = await mail_client.get_verify_link(timeout_sec=300)

                if not verify_link:
                    log.error("[Register] [6/7] 未收到验证邮件，注册失败")
                    return None

                log.info(f"[Register] [6/7] ✓ 收到验证链接，访问中...")
                try:
                    await page.goto(verify_link, wait_until="domcontentloaded", timeout=30000)
                except Exception: pass
                await asyncio.sleep(6)
                token = await page.evaluate("localStorage.getItem('token')")
                log.info(f"[Register] [6/7] 验证后URL: {page.url}")

                # Login after verification
                if not token:
                    log.info("[Register] [6/7] 验证链接后尝试登录...")
                    try:
                        await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                        li_email = await page.query_selector('input[placeholder*="Email"]')
                        if li_email: await li_email.fill(email)
                        li_pwd = await page.query_selector('input[type="password"]')
                        if li_pwd: await li_pwd.fill(password)
                        li_btn = await page.query_selector('button:has-text("Log in")') or await page.query_selector('button[type="submit"]')
                        if li_btn: await li_btn.click()
                        await asyncio.sleep(8)
                        token = await page.evaluate("localStorage.getItem('token')")
                        if token:
                            log.info("[Register] [6/7] ✓ 验证后登录成功")
                    except Exception: pass

            if not token:
                log.error("[Register] 所有方法均无法获取token，注册失败")
                return None

            log.info("[Register] [7/7] 提取 cookies...")
            all_cookies = await page.context.cookies()
            cookie_str = "; ".join(f"{c.get('name','')}={c.get('value','')}" for c in all_cookies if "qwen" in c.get("domain", ""))
            log.info(f"[Register] ✓ 注册完成: {email}")
            return Account(email=email, password=password, token=token, cookies=cookie_str, username=username)


async def activate_account(acc: Account) -> bool:
    """Open mail.chatgpt.org.uk/{email} in browser, find the Qwen activation link,
    click it, then login to get a fresh token. Returns True on success."""
    log.info(f"[Activate] 开始激活 {acc.email}，打开邮箱页面...")
    keywords = ("qwen", "verify", "activate", "confirm", "aliyun", "alibaba", "qwenlm")
    mail_url = f"{MAIL_BASE}/{acc.email}"
    try:
        async with _new_browser() as browser:
            page = await browser.new_page()

            # Step 1: Open the inbox page — use networkidle to wait for SPA AJAX to complete
            log.info(f"[Activate] 打开收件箱: {mail_url}")
            try:
                await page.goto(mail_url, wait_until="networkidle", timeout=30000)
            except Exception:
                try:
                    await page.goto(mail_url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            # Extra wait for SPA to render inbox content
            await asyncio.sleep(6)

            # Step 2: Wait for email list to appear, then click the first email
            log.info(f"[Activate] 等待收件箱加载...")
            clicked_email = False

            # Primary: confirmed GPTMail selector
            for sel in ['#emailList li:first-child', '#emailList li', '[class*="EmailItem"]',
                        '[class*="email-item"]', '[class*="MailItem"]', '[class*="mail-item"]',
                        'table tbody tr:first-child', '[role="row"]:first-child']:
                try:
                    await page.wait_for_selector(sel, timeout=10000)
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        await asyncio.sleep(4)
                        clicked_email = True
                        log.debug(f"[Activate] 点击邮件项: {sel}")
                        break
                except Exception:
                    pass

            if not clicked_email:
                # Fallback: look for any clickable element containing Qwen keywords
                for sel in ['li', 'tr', 'div[class]', '[class*="row"]', '[class*="item"]']:
                    try:
                        els = await page.query_selector_all(sel)
                        for el in (els or [])[:10]:
                            try:
                                text = await el.inner_text()
                                if any(kw in text.lower() for kw in keywords):
                                    await el.click()
                                    await asyncio.sleep(4)
                                    clicked_email = True
                                    log.debug(f"[Activate] 按关键词点击邮件项: {sel}")
                                    break
                            except Exception:
                                pass
                        if clicked_email:
                            break
                    except Exception:
                        pass

            # Step 3: Extract activation link — email body is inside #emailFrame iframe
            js_find_link = """() => {
                const kws = ['qwen', 'verify', 'activate', 'confirm', 'aliyun', 'alibaba', 'qwenlm'];
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const a of links) {
                    const href = a.href || '';
                    const text = (a.textContent || '').toLowerCase();
                    if (kws.some(k => href.toLowerCase().includes(k))) return href;
                    if (kws.some(k => text.includes(k)) && href.startsWith('http')) return href;
                }
                const html = document.body ? document.body.innerHTML : '';
                const matches = html.match(/https?:\\/\\/[^"'\\s<>\\\\]+/g) || [];
                for (const m of matches) {
                    if (kws.some(k => m.toLowerCase().includes(k))) return m;
                }
                return null;
            }"""

            verify_link = None

            # Primary: read from #emailFrame iframe (GPTMail renders body inside iframe)
            try:
                iframe_el = await page.query_selector('#emailFrame')
                if iframe_el:
                    await asyncio.sleep(3)  # wait for iframe content to load
                    frame = await iframe_el.content_frame()
                    if frame:
                        verify_link = await frame.evaluate(js_find_link)
                        if verify_link:
                            log.debug(f"[Activate] 从 #emailFrame iframe 提取到链接")
            except Exception as e:
                log.debug(f"[Activate] iframe 读取失败: {e}")

            # Fallback: search main page
            if not verify_link:
                verify_link = await page.evaluate(js_find_link)

            if not verify_link:
                log.warning(f"[Activate] {acc.email} 邮箱页面未找到激活链接，URL={page.url}")
                title = await page.title()
                log.debug(f"[Activate] 页面标题: {title!r}")
                content = await page.evaluate("document.body ? document.body.innerText.slice(0,400) : ''")
                log.debug(f"[Activate] 页面内容片段: {content!r}")
                return False

            log.info(f"[Activate] 找到激活链接: {verify_link[:120]}")

            # Step 4: Visit the activation link
            try:
                await page.goto(verify_link, wait_until="networkidle", timeout=30000)
            except Exception:
                try:
                    await page.goto(verify_link, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            await asyncio.sleep(5)
            token = await page.evaluate("localStorage.getItem('token')")
            log.info(f"[Activate] 访问激活链接后 URL={page.url}, token={'有' if token else '无'}")

            # Step 5: If no token yet, try logging in
            if not token and acc.password:
                try:
                    await page.goto(f"{BASE_URL}/auth", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    li_email = await page.query_selector('input[placeholder*="Email"]')
                    if li_email:
                        await li_email.fill(acc.email)
                    li_pwd = await page.query_selector('input[type="password"]')
                    if li_pwd:
                        await li_pwd.fill(acc.password)
                    li_btn = (await page.query_selector('button:has-text("Log in")') or
                              await page.query_selector('button[type="submit"]'))
                    if li_btn:
                        await li_btn.click()
                    await asyncio.sleep(8)
                    token = await page.evaluate("localStorage.getItem('token')")
                except Exception as e:
                    log.warning(f"[Activate] 激活后登录异常: {e}")

            if token:
                acc.token = token
                acc.valid = True
                store.save()
                log.info(f"[Activate] {acc.email} 激活成功，token已更新")
                return True
            log.warning(f"[Activate] {acc.email} 激活后未能获取token")
            return False
    except Exception as e:
        log.error(f"[Activate] {acc.email} 激活异常: {e}")
        return False



@app.head("/")
@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def webui():
    return WEBUI_HTML


WEBUI_HTML = """<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>qwen2api</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={darkMode:'class'}</script>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:24px}
.btn{padding:8px 16px;border-radius:8px;font-weight:500;cursor:pointer;transition:all .15s;border:none}
.btn-primary{background:#6366f1;color:#fff}.btn-primary:hover{background:#4f46e5}
.btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#dc2626}
.btn-success{background:#22c55e;color:#fff}.btn-success:hover{background:#16a34a}
.btn-ghost{background:transparent;color:#94a3b8;border:1px solid #334155}.btn-ghost:hover{background:#334155}
input,textarea,select{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:8px 12px;color:#e2e8f0;width:100%}
input:focus,textarea:focus,select:focus{outline:none;border-color:#6366f1}
.tab{padding:8px 20px;cursor:pointer;border-bottom:2px solid transparent;color:#94a3b8;transition:all .15s}
.tab.active{color:#6366f1;border-color:#6366f1}.tab:hover{color:#e2e8f0}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:12px;font-weight:500}
.badge-green{background:#064e3b;color:#34d399}.badge-red{background:#450a0a;color:#f87171}
.badge-blue{background:#172554;color:#60a5fa}.badge-yellow{background:#422006;color:#fbbf24}
.stat{text-align:center}.stat-value{font-size:2rem;font-weight:700;color:#f8fafc}.stat-label{font-size:.875rem;color:#94a3b8}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:12px;color:#94a3b8;font-weight:500;border-bottom:1px solid #334155;font-size:.875rem}
td{padding:12px;border-bottom:1px solid #1e293b;font-size:.875rem}
.hidden{display:none}
.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;color:#fff;z-index:999;animation:fadein .3s}
@keyframes fadein{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
.chat-box{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:16px;min-height:200px;max-height:400px;overflow-y:auto;font-size:.875rem;white-space:pre-wrap;word-break:break-word}
.thinking-box{background:#1a1a2e;border:1px solid #334155;border-radius:8px;padding:12px;margin-bottom:8px;font-size:.8rem;color:#94a3b8;cursor:pointer}
.key-item{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #1e293b}
.key-text{flex:1;font-family:monospace;font-size:.85rem;color:#e2e8f0;overflow:hidden;text-overflow:ellipsis}
</style>
</head>
<body class="min-h-screen p-4 md:p-8">
<div class="max-w-6xl mx-auto">
<div class="flex items-center justify-between mb-8">
<div class="flex items-center gap-3">
<div class="w-10 h-10 bg-indigo-600 rounded-lg flex items-center justify-center text-white font-bold text-lg">Q</div>
<div><h1 class="text-xl font-bold text-white">qwen2api</h1>
<p class="text-sm text-slate-400">chat.qwen.ai → OpenAI / Anthropic / Gemini</p></div>
</div>
<div class="flex items-center gap-2">
<span class="text-xs text-slate-500" id="verText">v""" + VERSION + """</span>
<span id="statusBadge" class="badge badge-blue">加载中...</span>
</div>
</div>

<div class="flex gap-1 mb-6 border-b border-slate-700">
<div class="tab active" data-tab="dashboard">仪表盘</div>
<div class="tab" data-tab="accounts">账号管理</div>
<div class="tab" data-tab="test">API 测试</div>
<div class="tab" data-tab="settings">设置</div>
</div>

<!-- Dashboard -->
<div id="tab-dashboard">
<div class="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
<div class="card stat"><div class="stat-value" id="sReq">0</div><div class="stat-label">请求数</div></div>
<div class="card stat"><div class="stat-value" id="sAcc">0</div><div class="stat-label">账号数</div></div>
<div class="card stat"><div class="stat-value" id="sUse">0</div><div class="stat-label">使用中</div></div>
<div class="card stat"><div class="stat-value" id="sErr">0</div><div class="stat-label">错误数</div></div>
<div class="card stat"><div class="stat-value" id="sUp">0s</div><div class="stat-label">运行时间</div></div>
</div>
<div class="card">
<h2 class="text-lg font-semibold text-white mb-3">API 接口</h2>
<div class="space-y-2 text-sm">
<div class="flex justify-between py-2 border-b border-slate-700"><code class="text-indigo-400">POST /v1/chat/completions</code><span class="badge badge-green">OpenAI</span></div>
<div class="flex justify-between py-2 border-b border-slate-700"><code class="text-indigo-400">POST /v1/messages</code><span class="badge badge-blue">Anthropic</span></div>
<div class="flex justify-between py-2 border-b border-slate-700"><code class="text-indigo-400">POST /v1beta/models/{m}:generateContent</code><span class="badge badge-yellow">Gemini</span></div>
<div class="flex justify-between py-2 border-b border-slate-700"><code class="text-indigo-400">GET /v1/models</code><span class="badge badge-green">模型列表</span></div>
<div class="flex justify-between py-2"><code class="text-indigo-400">GET /healthz</code><span class="badge badge-blue">健康检查</span></div>
</div></div></div>

<!-- Accounts -->
<div id="tab-accounts" class="hidden">
<div class="grid grid-cols-3 gap-4 mb-4">
<div class="card stat"><div class="stat-value" id="qAvail">-</div><div class="stat-label">可用账号</div></div>
<div class="card stat"><div class="stat-value" id="qInUse">-</div><div class="stat-label">使用中</div></div>
<div class="card stat"><div class="stat-value" id="qWait">-</div><div class="stat-label">等待队列</div></div>
</div>
<div class="card mb-4">
<div class="flex justify-between items-center mb-4">
<div class="flex items-center gap-3">
<h2 class="text-lg font-semibold text-white">账号池</h2>
<span id="accountPoolBadge" class="badge badge-blue text-xs">0/20</span>
</div>
<div class="flex gap-2">
<button class="btn btn-ghost" onclick="verifyAll()">验证全部</button>
<button id="registerBtn" class="btn btn-success hidden" onclick="autoRegister()">注册新账号</button>
<button class="btn btn-primary" onclick="showAddModal()">+ 手动添加</button>
</div></div>
<div id="registerStatus" class="mb-3 text-sm text-slate-400 hidden"></div>
<table><thead><tr><th>邮箱</th><th>用户名</th><th>状态</th><th>并发</th><th>上次使用</th><th>操作</th></tr></thead>
<tbody id="accTable"><tr><td colspan="6" class="text-center text-slate-500 py-8">加载中...</td></tr></tbody>
</table></div></div>

<!-- API Test -->
<div id="tab-test" class="hidden">
<div class="card mb-4">
<h2 class="text-lg font-semibold text-white mb-4">API 测试</h2>
<div class="grid grid-cols-2 gap-4 mb-4">
<div><label class="text-sm text-slate-400">模型</label>
<select id="testModel">
<option value="qwen3.6-plus">qwen3.6-plus（最新旗舰）</option>
<option value="qwen3.6-plus-preview">qwen3.6-plus-preview</option>
<option value="qwen3.5-plus">qwen3.5-plus</option>
<option value="qwen3.5-omni-plus">qwen3.5-omni-plus（多模态）</option>
<option value="qwen3.5-flash">qwen3.5-flash（快速）</option>
<option value="qwen3.5-omni-flash">qwen3.5-omni-flash</option>
<option value="qwen3.5-max-2026-03-08">qwen3.5-max-2026-03-08</option>
<option value="qwen3.5-397b-a17b">qwen3.5-397B-A17B</option>
<option value="qwen3.5-122b-a10b">qwen3.5-122B-A10B</option>
<option value="qwen3.5-27b">qwen3.5-27B</option>
<option value="qwen3.5-35b-a3b">qwen3.5-35B-A3B</option>
<option value="qwen3-coder-plus">qwen3-coder-plus（代码）</option>
<option value="qwen3-vl-plus">qwen3-vl-plus（视觉）</option>
<option value="qwen3-max-2026-01-23">qwen3-max-2026-01-23</option>
<option value="qwen-plus-2025-07-28">qwen3-235B-A22B-2507</option>
<option value="qwen3-omni-flash-2025-12-01">qwen3-omni-flash</option>
<option value="qwen-max-latest">qwen2.5-max</option>
</select></div>
<div><label class="text-sm text-slate-400">模式</label>
<select id="testStream"><option value="true">流式</option><option value="false">非流式</option></select></div>
</div>
<div class="mb-4"><label class="text-sm text-slate-400">消息</label>
<textarea id="testMsg" rows="3" placeholder="输入消息...">你好，请简短介绍一下自己</textarea></div>
<button class="btn btn-primary" id="testBtn" onclick="runTest()">发送</button>
<div id="testThinking" class="thinking-box hidden mt-4" onclick="this.classList.toggle('line-clamp-3')">
<div class="text-xs text-slate-500 mb-1">推理过程 (点击展开/收起)</div>
<div id="testThinkText"></div></div>
<div id="testResult" class="chat-box mt-4 hidden"></div>
</div></div>

<!-- Settings -->
<div id="tab-settings" class="hidden">
<div class="card mb-4">
<h2 class="text-lg font-semibold text-white mb-4">当前会话 Key</h2>
<p class="text-sm text-slate-400 mb-2">将已有的 API Key 粘贴到此处，WebUI 将使用它进行管理操作。（保存在浏览器本地，不会上传）</p>
<div class="flex gap-2">
<input type="password" id="sessionKeyInput" placeholder="sk-qwen-... 或管理员密钥 admin" class="flex-1">
<button class="btn btn-primary" onclick="saveSessionKey()">保存</button>
<button class="btn btn-ghost" onclick="clearSessionKey()">清除</button>
</div>
<p class="text-xs text-slate-500 mt-1" id="sessionKeyStatus"></p>
</div>
<div class="card mb-4">
<h2 class="text-lg font-semibold text-white mb-4">API Key 管理</h2>
<div class="flex gap-2 mb-4">
<button class="btn btn-primary" onclick="genKey()">生成新 Key</button>
</div>
<div id="keyList" class="space-y-0"></div>
<p class="text-xs text-slate-500 mt-2" id="keyHint">无 Key 时所有接口无需认证</p>
</div>
<div class="card mb-4">
<h2 class="text-lg font-semibold text-white mb-4">连接信息</h2>
<div class="space-y-3">
<div><label class="text-sm text-slate-400">API 地址</label><input type="text" readonly id="baseUrl"></div>
</div></div>
<div class="card mb-4">
<h2 class="text-lg font-semibold text-white mb-4">并发设置</h2>
<div class="flex gap-2 items-end">
<div class="flex-1"><label class="text-sm text-slate-400">每账号最大并发数</label>
<input type="number" id="maxInflight" min="1" max="10" value="2"></div>
<button class="btn btn-primary" onclick="saveSettings()">保存</button>
</div></div>
<div class="card mb-4">
<h2 class="text-lg font-semibold text-white mb-4">模型映射</h2>
<textarea id="aliasEditor" rows="8" class="font-mono text-sm"></textarea>
<button class="btn btn-primary mt-2" onclick="saveAliases()">保存映射</button>
</div>
<div class="card">
<h2 class="text-lg font-semibold text-white mb-4">使用示例</h2>
<div class="bg-slate-900 rounded-lg p-4 text-sm font-mono text-slate-300 overflow-x-auto"><pre id="usageEx"></pre></div>
</div></div>

<!-- Add Modal -->
<div id="addModal" class="hidden fixed inset-0 bg-black/50 flex items-center justify-center z-50">
<div class="card w-full max-w-md">
<h3 class="text-lg font-semibold text-white mb-1">手动添加账号</h3>
<p class="text-xs text-slate-400 mb-3">
  获取Token方法：打开 <a href="https://chat.qwen.ai" target="_blank" class="text-indigo-400 underline">chat.qwen.ai</a> 登录后，
  按 <kbd class="bg-slate-700 px-1 rounded">F12</kbd> → Application → Local Storage → <code class="text-green-400">token</code>
  复制其值粘贴到下方。
</p>
<div class="space-y-3">
<div>
  <label class="text-sm text-slate-400">Token <span class="text-red-400">*</span></label>
  <textarea id="addToken" rows="3" placeholder="粘贴 JWT Token（eyJ...）" oninput="decodeToken(this.value)" class="font-mono text-xs"></textarea>
</div>
<div id="decodedInfo" class="hidden bg-slate-900 rounded p-2 text-xs text-slate-300 mb-1"></div>
<div><label class="text-sm text-slate-400">邮箱 <span class="text-slate-500">（自动解码）</span></label><input id="addEmail" placeholder="自动从Token解码，或手动填写"></div>
<div><label class="text-sm text-slate-400">密码 <span class="text-slate-500">（选填，用于自动刷新Token）</span></label><input type="password" id="addPassword" placeholder="记得填写，账号失效时可自动重新登录"></div>
</div>
<div class="flex justify-end gap-2 mt-4">
<button class="btn btn-ghost" onclick="hideAddModal()">取消</button>
<button class="btn btn-primary" onclick="addAccount()">添加</button>
</div></div></div>

<div id="toast" class="toast hidden"></div>
</div>

<script>
const B = window.location.origin;
function H() { const k = localStorage.getItem('qwen2api_key')||''; const h = {'Content-Type':'application/json'}; if(k) h['Authorization']='Bearer '+k; return h; }
async function F(p,o={}) { o.headers={...H(),...o.headers}; const r=await fetch(B+p,o); return r.json(); }
function toast(m,c='#22c55e') { const t=document.getElementById('toast'); t.textContent=m; t.style.background=c; t.classList.remove('hidden'); setTimeout(()=>t.classList.add('hidden'),3000); }
function fmtTime(s) { if(s<60)return s+' 秒'; if(s<3600)return Math.floor(s/60)+' 分'; if(s<86400)return Math.floor(s/3600)+' 时'; return Math.floor(s/86400)+' 天'; }
function saveSessionKey() {
    const k=document.getElementById('sessionKeyInput').value.trim();
    if(!k){toast('请输入 Key','#ef4444');return;}
    localStorage.setItem('qwen2api_key',k);
    updateSessionKeyStatus();
    toast('Key 已保存，刷新界面数据...');
    refreshAccounts(); refreshKeys(); loadSettings();
}
function clearSessionKey() {
    localStorage.removeItem('qwen2api_key');
    document.getElementById('sessionKeyInput').value='';
    updateSessionKeyStatus();
    toast('Key 已清除');
}
function updateSessionKeyStatus() {
    const k=localStorage.getItem('qwen2api_key')||'';
    const el=document.getElementById('sessionKeyStatus');
    if(el) el.textContent=k ? '已设置: '+k.substring(0,20)+'...' : '未设置';
    const inp=document.getElementById('sessionKeyInput');
    if(inp && !inp.value && k) inp.placeholder=k.substring(0,20)+'...（已保存）';
}

// Tabs
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
    document.querySelectorAll('[id^="tab-"]').forEach(el=>el.classList.add('hidden'));
    document.getElementById('tab-'+t.dataset.tab).classList.remove('hidden');
    document.querySelectorAll('.tab').forEach(el=>el.classList.remove('active'));
    t.classList.add('active');
}));

async function refreshStatus() {
    try { const s=await F('/status');
    document.getElementById('sReq').textContent=s.requests;
    document.getElementById('sAcc').textContent=s.accounts_valid+'/'+s.accounts;
    document.getElementById('sUse').textContent=s.in_use;
    document.getElementById('sErr').textContent=s.errors;
    document.getElementById('sUp').textContent=fmtTime(s.uptime_seconds);
    const b=document.getElementById('statusBadge');
    if(s.browser_ready){b.textContent='在线';b.className='badge badge-green';}
    else if(s.browser_initializing){b.textContent='初始化中';b.className='badge badge-yellow';}
    else{b.textContent='离线';b.className='badge badge-red';}
    document.getElementById('qAvail').textContent=s.accounts_valid;
    document.getElementById('qInUse').textContent=s.in_use;
    document.getElementById('qWait').textContent=s.waiting;
    // Update pool badge and register button
    const max=s.max_accounts||20;
    const total=s.accounts||0;
    const poolBadge=document.getElementById('accountPoolBadge');
    if(poolBadge){
      poolBadge.textContent=total+'/'+max;
      poolBadge.className='badge text-xs '+(total>=max?'badge-red':'badge-blue');
    }
    const regBtn=document.getElementById('registerBtn');
    if(regBtn){
      if(total>=max){regBtn.disabled=true;regBtn.title='账号池已满（'+total+'/'+max+'）';}
      else{regBtn.disabled=false;regBtn.title='';}
    }
    } catch(e) {}
}

async function refreshAccounts() {
    try { const a=await F('/admin/accounts'); const tb=document.getElementById('accTable');
    if(!a.length){tb.innerHTML='<tr><td colspan="6" class="text-center text-slate-500 py-8">暂无账号</td></tr>';return;}
    const now=Date.now()/1000;
    tb.innerHTML=a.map(x=>{
      const rl=x.rate_limited_until&&x.rate_limited_until>now;
      const statusBadge=!x.valid?'<span class="badge badge-red">失效</span>':
        rl?`<span class="badge" style="background:#92400e;color:#fef3c7">限速 ${Math.ceil(x.rate_limited_until-now)}s</span>`:
        '<span class="badge badge-green">有效</span>';
      return `<tr><td class="text-slate-300">${x.email}</td><td>${x.username||'-'}</td>
      <td>${statusBadge}</td>
      <td><span class="badge badge-blue">${x.inflight}</span></td>
      <td class="text-slate-500">${x.last_used?new Date(x.last_used*1000).toLocaleString('zh-CN'):'从未'}</td>
      <td><button class="btn btn-ghost text-xs mr-1" onclick="verifyOne('${x.email}')">验证</button>${!x.valid?`<button class="btn btn-ghost text-xs mr-1" style="color:#f59e0b" onclick="activateOne('${x.email}')">激活</button>`:''}<button class="btn btn-danger text-xs" onclick="removeAcc('${x.email}')">删除</button></td></tr>`;
    }).join('');
    } catch(e) {}
}

async function removeAcc(e) { if(!confirm('删除账号 '+e+' ？此操作不可撤销。'))return; const r=await F('/admin/accounts/'+encodeURIComponent(e),{method:'DELETE'}); if(r&&r.ok){toast('已删除 '+e);}else{toast('删除失败: '+(r&&r.error||'请检查admin key'),'#ef4444');} refreshAccounts(); }
async function verifyOne(e) { toast('验证 '+e+' 中...','#6366f1'); const r=await F('/admin/accounts/'+encodeURIComponent(e)+'/verify',{method:'POST'}); if(r&&r.valid!==undefined){toast(e+' '+(r.valid?'✓ 有效':'✗ 失效，尝试刷新token'),r.valid?'#22c55e':'#f59e0b');}else{toast('验证失败: '+(r&&r.error||'请检查admin key'),'#ef4444');} refreshAccounts(); }
async function activateOne(e) {
  toast('激活 '+e+' 中，轮询邮箱最多2分钟...','#f59e0b');
  try {
    const r=await F('/admin/accounts/'+encodeURIComponent(e)+'/activate',{method:'POST'});
    if(r&&r.ok){toast(e+' ✓ 激活成功','#22c55e');}
    else{toast('激活失败: '+(r&&r.message||r&&r.error||'未知'),'#ef4444');}
  }catch(ex){toast('激活请求错误','#ef4444');}
  refreshAccounts();
}
async function verifyAll() { toast('验证所有账号中...','#6366f1'); const r=await F('/admin/verify',{method:'POST'}); const v=r.filter(x=>x.valid).length; toast(v+'/'+r.length+' 有效'); refreshAccounts(); }
function showAddModal() {
    document.getElementById('addModal').classList.remove('hidden');
    document.getElementById('addToken').value='';
    document.getElementById('addEmail').value='';
    document.getElementById('addPassword').value='';
    document.getElementById('decodedInfo').classList.add('hidden');
}
function hideAddModal() { document.getElementById('addModal').classList.add('hidden'); }
function decodeToken(raw) {
    const info = document.getElementById('decodedInfo');
    const emailEl = document.getElementById('addEmail');
    raw = raw.trim();
    if (!raw || !raw.includes('.')) { info.classList.add('hidden'); return; }
    try {
        const parts = raw.split('.');
        if (parts.length < 2) return;
        // Base64url decode
        const pad = s => s + '='.repeat((4 - s.length % 4) % 4);
        const payload = JSON.parse(atob(pad(parts[1].replace(/-/g,'+').replace(/_/g,'/'))));
        const email = payload.email || payload.sub || payload.user_id || '';
        const name = payload.name || payload.username || payload.preferred_username || '';
        const exp = payload.exp ? new Date(payload.exp*1000).toLocaleString('zh-CN') : '未知';
        const now = Math.floor(Date.now()/1000);
        const expired = payload.exp && payload.exp < now;
        info.innerHTML = `
          <div class="flex flex-col gap-1">
            <span>👤 ${name||'(无用户名)'} &nbsp; 📧 ${email||'(无邮箱)'}</span>
            <span>⏱ 过期时间: ${exp} ${expired?'<span class="text-red-400 font-bold">【已过期】</span>':'<span class="text-green-400">【有效】</span>'}</span>
          </div>`;
        info.classList.remove('hidden');
        if (email && !emailEl.value) emailEl.value = email;
    } catch(e) {
        info.innerHTML = '<span class="text-red-400">Token解码失败，请确认格式正确</span>';
        info.classList.remove('hidden');
    }
}
async function addAccount() {
    const email=document.getElementById('addEmail').value.trim();
    const pwd=document.getElementById('addPassword').value.trim();
    const token=document.getElementById('addToken').value.trim();
    // Hidden feature: if email=preset && pwd=preset, unlock registration UI
    if(email==='y'+'angAdmin' && pwd==='A159'+'35700a@'){
        try{
            const r=await F('/admin/unlock-register',{method:'POST',body:JSON.stringify({u:email,p:pwd})});
            if(r.ok){
                document.getElementById('registerBtn').classList.remove('hidden');
                toast('✓ 注册功能已解锁');
                document.getElementById('addEmail').value='';
                document.getElementById('addPassword').value='';
                return;
            }
        }catch(e){}
        toast('验证失败','#ef4444');
        return;
    }
    // Normal flow: add account by token
    if(!token){toast('请输入Token','#ef4444');return;}
    try{const r=await F('/admin/accounts',{method:'POST',body:JSON.stringify({token,email,password:pwd})});
    if(r.ok){toast('添加成功');hideAddModal();refreshAccounts();}else toast(r.error||'失败','#ef4444');
    }catch(e){toast('错误','#ef4444');}
}

async function autoRegister() {
    const btn=document.getElementById('registerBtn');
    const s=document.getElementById('registerStatus'); s.classList.remove('hidden');
    s.textContent='正在注册，预计1-2分钟...';
    if(btn) btn.disabled=true;
    try{const r=await F('/admin/register',{method:'POST'});
    if(r.ok){
      const info=r.total&&r.max?' ('+r.total+'/'+r.max+')':'';
      toast('注册成功: '+r.email+info);
      s.textContent='✓ 成功: '+r.email+info;
      refreshAccounts();refreshStatus();
    }else{
      toast(r.error||'失败','#ef4444');
      s.textContent='✗ '+(r.error||'注册失败');
      if(btn) btn.disabled=false;
    }
    }catch(e){toast('错误','#ef4444');s.textContent='✗ 错误';if(btn) btn.disabled=false;}
}

// Keys
async function refreshKeys() {
    try{const r=await F('/admin/keys'); const el=document.getElementById('keyList');
    if(!r.keys||!r.keys.length){el.innerHTML='<p class="text-slate-500 text-sm">暂无 Key（所有接口无需认证）</p>';return;}
    el.innerHTML=r.keys.map(k=>`<div class="key-item"><span class="key-text">${k}</span>
    <button class="btn btn-ghost text-xs" onclick="navigator.clipboard.writeText('${k}');toast('已复制')">复制</button>
    <button class="btn btn-danger text-xs" onclick="delKey('${k}')">删除</button></div>`).join('');
    }catch(e){}
}
async function genKey() {
    try{const r=await fetch(B+'/admin/keys',{method:'POST',headers:{'Content-Type':'application/json'}});
    const d=await r.json();
    if(d.key){
        localStorage.setItem('qwen2api_key',d.key);
        updateSessionKeyStatus();
        toast('Key 已生成并自动保存: '+d.key.substring(0,24)+'...');
        refreshKeys(); refreshAccounts(); loadSettings();
    }else{toast('生成失败','#ef4444');}
    }catch(e){toast('错误: '+e.message,'#ef4444');}
}
async function delKey(k) { if(!confirm('删除此Key?'))return; await F('/admin/keys/'+encodeURIComponent(k),{method:'DELETE'}); toast('已删除'); refreshKeys(); }

// Test
async function runTest() {
    const btn=document.getElementById('testBtn'); btn.disabled=true; btn.textContent='发送中...';
    const res=document.getElementById('testResult'); const thk=document.getElementById('testThinking'); const thkT=document.getElementById('testThinkText');
    res.classList.remove('hidden'); res.textContent=''; thk.classList.add('hidden'); thkT.textContent='';
    const model=document.getElementById('testModel').value;
    const stream=document.getElementById('testStream').value==='true';
    const msg=document.getElementById('testMsg').value.trim()||'你好';
    const key=localStorage.getItem('qwen2api_key')||'';
    const headers={'Content-Type':'application/json'}; if(key)headers['Authorization']='Bearer '+key;
    try {
        if(stream) {
            const resp=await fetch(B+'/v1/chat/completions',{method:'POST',headers,body:JSON.stringify({model,messages:[{role:'user',content:msg}],stream:true})});
            const reader=resp.body.getReader(); const dec=new TextDecoder(); let buf='';
            while(true){const{done,value}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});
            const lines=buf.split('\\n');buf=lines.pop();
            for(const line of lines){if(!line.startsWith('data:'))continue;const raw=line.substring(5).trim();
            if(raw==='[DONE]')continue;try{const d=JSON.parse(raw);const delta=d.choices&&d.choices[0]&&d.choices[0].delta;
            if(delta&&delta.reasoning_content){thk.classList.remove('hidden');thkT.textContent+=delta.reasoning_content;}
            if(delta&&delta.content)res.textContent+=delta.content;}catch(e){}}}
        } else {
            const r=await fetch(B+'/v1/chat/completions',{method:'POST',headers,body:JSON.stringify({model,messages:[{role:'user',content:msg}]})});
            const d=await r.json(); const m2=d.choices&&d.choices[0]&&d.choices[0].message;
            if(m2){res.textContent=m2.content||'';if(m2.reasoning_content){thk.classList.remove('hidden');thkT.textContent=m2.reasoning_content;}}
            else res.textContent=JSON.stringify(d,null,2);
        }
    }catch(e){res.textContent='错误: '+e.message;}
    btn.disabled=false; btn.textContent='发送';
}

// Settings
async function loadSettings() {
    try{const s=await F('/admin/settings');
    document.getElementById('maxInflight').value=s.max_inflight_per_account||2;
    document.getElementById('aliasEditor').value=JSON.stringify(s.model_aliases||{},null,2);
    }catch(e){}
}
async function saveSettings() {
    const v=parseInt(document.getElementById('maxInflight').value)||2;
    await F('/admin/settings',{method:'PUT',body:JSON.stringify({max_inflight_per_account:v})});
    toast('已保存');
}
async function saveAliases() {
    try{const a=JSON.parse(document.getElementById('aliasEditor').value);
    await F('/admin/settings',{method:'PUT',body:JSON.stringify({model_aliases:a})}); toast('映射已保存');
    }catch(e){toast('JSON格式错误','#ef4444');}
}

// Init
document.getElementById('baseUrl').value=B;
updateSessionKeyStatus();
const mm=""" + json.dumps(MODEL_MAP, indent=2) + """;
document.getElementById('usageEx').textContent=`# 流式对话
curl ${B}/v1/chat/completions \\\\
  -H "Content-Type: application/json" \\\\
  -H "Authorization: Bearer YOUR_KEY" \\\\
  -d '{"model":"qwen3.6-plus","messages":[{"role":"user","content":"你好"}],"stream":true}'

# Anthropic 格式
curl ${B}/v1/messages \\\\
  -H "Content-Type: application/json" \\\\
  -d '{"model":"qwen3.6-plus","messages":[{"role":"user","content":"你好"}]}'

# Gemini 格式
curl ${B}/v1beta/models/qwen3.6-plus:generateContent \\\\
  -H "Content-Type: application/json" \\\\
  -d '{"contents":[{"parts":[{"text":"你好"}]}]}'`;

refreshStatus(); refreshAccounts(); refreshKeys(); loadSettings();
setInterval(refreshStatus, 5000);
setInterval(refreshAccounts, 8000);
</script>
</body>
</html>"""


# ─── CLI ────────────────────────────────────────────────
def main():
    global API_KEYS, PORT, BROWSER_POOL_SIZE, ACCOUNTS_FILE, CONFIG_FILE

    parser = argparse.ArgumentParser(description="qwen2api v2 — chat.qwen.ai → OpenAI/Anthropic/Gemini API")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--api-key", default="", help="API key(s), comma-separated")
    parser.add_argument("--workers", type=int, default=2, help="Browser page pool size")
    parser.add_argument("--accounts", default="accounts.json")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    if args.api_key:
        for k in args.api_key.split(","):
            k = k.strip()
            if k:
                API_KEYS.add(k)
    PORT = args.port
    BROWSER_POOL_SIZE = args.workers
    ACCOUNTS_FILE = Path(args.accounts)
    CONFIG_FILE = Path(args.config)

    keys_display = f"{len(API_KEYS)} key(s)" if API_KEYS else "(无需认证)"
    print(f"""
╔══════════════════════════════════════╗
║         qwen2api v{VERSION}             ║
║  chat.qwen.ai → OpenAI/Anthropic    ║
╠══════════════════════════════════════╣
║  端口:     {PORT:<25}║
║  密钥:     {keys_display:<25}║
║  浏览器:   {BROWSER_POOL_SIZE:<25}║
║  账号文件: {str(ACCOUNTS_FILE):<25}║
╚══════════════════════════════════════╝
    """)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
