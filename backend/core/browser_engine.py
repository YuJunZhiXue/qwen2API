import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from backend.core.config import settings

log = logging.getLogger("qwen2api.browser")


def _request_jitter_seconds() -> float:
    low = max(0, settings.REQUEST_JITTER_MIN_MS)
    high = max(low, settings.REQUEST_JITTER_MAX_MS)
    return random.uniform(low, high) / 1000.0

JS_FETCH = (
    "async (args) => {"
    "const opts={method:args.method,headers:{'Content-Type':'application/json','Authorization':'Bearer '+args.token}};"
    "if(args.body)opts.body=JSON.stringify(args.body);"
    "const res=await fetch(args.url,opts);"
    "const text=await res.text();"
    "return{status:res.status,body:text};"
    "}"
)

# 完整流式函数，单行字符串，避免 Camoufox page.evaluate 多行 JS 报错
# 不依赖 window.__qwen_stream_fetch，自包含
JS_STREAM_CHUNKED = (
    "async (args) => {"
    "const ctrl=new AbortController();"
    "const tmr=setTimeout(()=>ctrl.abort(),1800000);"
    "try{"
    "const bin=atob(args.payload_b64);"
    "const bytes=Uint8Array.from(bin,c=>c.charCodeAt(0));"
    "const body=new TextDecoder().decode(bytes);"
    "const res=await fetch(args.url,{method:'POST',"
    "headers:{'Content-Type':'application/json','Authorization':'Bearer '+args.token},"
    "body:body,signal:ctrl.signal});"
    "if(!res.ok){"
    "const t=await res.text();clearTimeout(tmr);"
    "return{status:res.status,body:t.substring(0,2000)};}"
    "const rdr=res.body.getReader();"
    "const dec=new TextDecoder();"
    "let buf='';"
    "while(true){"
    "const{done,value}=await rdr.read();"
    "if(done){if(buf)await window.send_chunk(args.chat_id,buf);break;}"
    "buf+=dec.decode(value,{stream:true});"
    "if(buf.includes('\\n\\n')||buf.length>=200){"
    "await window.send_chunk(args.chat_id,buf);buf='';}"
    "}"
    "clearTimeout(tmr);"
    "return{status:200,body:'__DONE__'};"
    "}catch(e){"
    "clearTimeout(tmr);"
    "return{status:0,body:'JS error: '+e.message};"
    "}}"
)

JS_STREAM_FULL = (
    "async (args) => {"
    "const ctrl=new AbortController();"
    "const tmr=setTimeout(()=>ctrl.abort(),1800000);"
    "try{"
    "const res=await fetch(args.url,{method:'POST',"
    "headers:{'Content-Type':'application/json','Authorization':'Bearer '+args.token},"
    "body:JSON.stringify(args.payload),signal:ctrl.signal});"
    "if(!res.ok){"
    "const t=await res.text();clearTimeout(tmr);"
    "return{status:res.status,body:t.substring(0,2000)};}"
    "const rdr=res.body.getReader();"
    "const dec=new TextDecoder();"
    "let body='';"
    "while(true){"
    "const{done,value}=await rdr.read();"
    "if(done)break;"
    "body+=dec.decode(value,{stream:true});}"
    "clearTimeout(tmr);"
    "return{status:res.status,body:body};"
    "}catch(e){"
    "clearTimeout(tmr);"
    "return{status:0,body:'JS error: '+e.message};"
    "}}"
)

_CAMOUFOX_OPTS = {
    "headless": True,
    "humanize": True,               # 启用人类化延迟，行为更自然
    "i_know_what_im_doing": True,
    "os": "windows",                # 明确 Windows 指纹，与服务器一致
    "locale": "zh-CN",              # 中文用户语言
    "firefox_user_prefs": {
        # 用软件 WebRender 替代完全禁用，真实机器通常开启 WebRender
        "gfx.webrender.software": True,
        "media.hardware-video-decoding.enabled": False,  # 服务器无 GPU，仅关闭硬件视频解码
        # 启用缓存，更像真实用户
        "browser.cache.disk.enable": True,
        "browser.cache.memory.enable": True,
        # 关闭自动更新弹窗等干扰
        "app.update.auto": False,
        "browser.shell.checkDefaultBrowser": False,
    },
}

@asynccontextmanager
async def _new_browser():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(**_CAMOUFOX_OPTS) as browser:
        yield browser

class BrowserEngine:
    def __init__(self, pool_size: int = 3, base_url: str = "https://chat.qwen.ai"):
        self.pool_size = pool_size
        self.base_url = base_url
        self._browser = None
        self._browser_cm = None
        self._pages: asyncio.Queue = asyncio.Queue()
        self._started = False
        self._ready = asyncio.Event()

    async def start(self):
        if self._started:
            return
        try:
            await self._start_camoufox()
        except Exception as e:
            log.error(f"[Browser] camoufox failed: {e}")
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
        log.info("Browser engine started")

    async def _init_pages(self):
        log.info(f"[Browser] 正在初始化 {self.pool_size} 个并发渲染引擎页面...")
        for i in range(self.pool_size):
            page = await self._browser.new_page()
            try:
                await page.set_viewport_size({"width": 1920, "height": 1080})
            except Exception:
                pass
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            self._pages.put_nowait(page)
            log.info(f"  [Browser] Page {i+1}/{self.pool_size} ready")

    @staticmethod
    async def _ensure_browser_installed():
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
                exe_name = "camoufox.exe" if os.name == "nt" else "camoufox"
                exe_path = os.path.join(cache_dir, exe_name)
                if os.path.exists(exe_path):
                    return
        except Exception:
            pass
        log.info("[Browser] 未检测到 camoufox，正在自动下载...")
        try:
            loop = asyncio.get_event_loop()
            def _do_install():
                from camoufox.pkgman import CamoufoxFetcher
                CamoufoxFetcher().install()
            await loop.run_in_executor(None, _do_install)
        except Exception as e:
            log.error(f"[Browser] 下载失败: {e}")

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
        try:
            page = await asyncio.wait_for(self._pages.get(), timeout=60)
        except asyncio.TimeoutError:
            return {"status": 429, "body": "Too Many Requests (Queue full)"}

        needs_refresh = False
        try:
            await asyncio.sleep(_request_jitter_seconds())
            result = await page.evaluate(JS_FETCH, {
                "method": method, "url": path, "token": token, "body": body or {},
            })
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

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        """Camoufox Firefox 完整收取 SSE 响应后一次性返回，绕开 expose_function 跨语言回调限制。"""
        await asyncio.wait_for(self._ready.wait(), timeout=300)
        if not self._started:
            yield {"status": 0, "body": "Browser engine failed to start"}
            return

        try:
            page = await asyncio.wait_for(self._pages.get(), timeout=60)
        except asyncio.TimeoutError:
            yield {"status": 429, "body": "Too Many Requests (Queue full)"}
            return

        needs_refresh = False
        url = f'/api/v2/chat/completions?chat_id={chat_id}'
        try:
            await asyncio.sleep(_request_jitter_seconds())
            res = await asyncio.wait_for(
                page.evaluate(JS_STREAM_FULL, {"url": url, "token": token, "payload": payload}),
                timeout=1800,
            )
            if isinstance(res, dict) and res.get("status") == 0:
                needs_refresh = True
            yield res if isinstance(res, dict) else {"status": 0, "body": str(res)}
        except asyncio.TimeoutError:
            needs_refresh = True
            yield {"status": 0, "body": "Timeout"}
        except Exception as e:
            needs_refresh = True
            yield {"status": 0, "body": str(e)}
        finally:
            if needs_refresh:
                asyncio.create_task(self._refresh_page_and_return(page))
            else:
                self._pages.put_nowait(page)

    async def _refresh_page(self, page):
        try:
            await asyncio.wait_for(
                page.goto(self.base_url, wait_until="domcontentloaded"),
                timeout=20000,
            )
        except Exception:
            pass

    async def _refresh_page_and_return(self, page):
        await self._refresh_page(page)
        self._pages.put_nowait(page)
