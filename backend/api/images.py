"""
图片生成接口 — 兼容 OpenAI /v1/images/generations 与 /v1/images/edits。

- generations: 文生图 t2i
- edits: 图生图 i2i（参考角色/场景 sheet 生成分镜，供 ArcReel 使用）

优先走与视频相同的 HTTP 直连 complete_once，
避免依赖 Playwright 浏览器路径（易超时）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.services.qwen_client import QwenClient

log = logging.getLogger("qwen2api.images")
router = APIRouter()

# 注意：chat.qwen.ai 网页侧没有独立的“专用生图模型 ID”，
# t2i 实际是聊天模型挂载 image_gen/t2i 能力。默认改用更高档模型。
DEFAULT_IMAGE_MODEL = "qwen3.7-max"

IMAGE_MODEL_MAP = {
    "dall-e-3": "qwen3.7-max",
    "dall-e-2": "qwen3.6-plus",
    "qwen-image": "qwen3.7-max",
    "qwen-image-plus": "qwen3.7-max",
    "qwen-image-max": "qwen3.8-max-preview",
    "qwen-image-turbo": "qwen3.6-plus",
    "qwen3.6-plus": "qwen3.6-plus",
    "qwen3.6-max-preview": "qwen3.6-max-preview",
    "qwen3.7-plus": "qwen3.7-plus",
    "qwen3.7-max": "qwen3.7-max",
    "qwen3.8-max-preview": "qwen3.8-max-preview",
    "qwen3.8": "qwen3.8-max-preview",
}

HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)
IMAGE_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp|gif)(?:[?#]|$)", re.IGNORECASE)


def _extract_image_urls(value: Any) -> list[str]:
    urls: list[str] = []

    def walk(v: Any, key_hint: str = ""):
        if v is None:
            return
        if isinstance(v, str):
            # markdown images
            for u in re.findall(r"!\[.*?\]\((https?://[^\s\)]+)\)", v):
                urls.append(u.rstrip(").,;"))
            for u in HTTP_URL_RE.findall(v):
                u2 = u.rstrip(".,;)\"'>")
                low = u2.lower()
                hint = key_hint.lower()
                if IMAGE_EXT_RE.search(low) or any(x in low for x in ("cdn.qwenlm.ai", "alicdn.com", "/image", "img")):
                    if not any(x in low for x in (".mp4", ".webm", ".mov")):
                        urls.append(u2)
                elif any(x in hint for x in ("image", "url", "src", "thumb")) and u2.startswith("http"):
                    if not any(x in low for x in (".mp4", ".webm", ".mov")):
                        urls.append(u2)
            return
        if isinstance(v, list):
            for item in v:
                walk(item, key_hint)
            return
        if isinstance(v, dict):
            for k, item in v.items():
                walk(item, str(k))

    walk(value)

    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_task_ids(value: Any) -> list[str]:
    ids: list[str] = []

    def walk(v: Any):
        if v is None:
            return
        if isinstance(v, list):
            for item in v:
                walk(item)
            return
        if isinstance(v, dict):
            for k, item in v.items():
                if k in {"task_id", "taskId"} and isinstance(item, str) and item.strip():
                    ids.append(item.strip())
                else:
                    walk(item)

    walk(value)
    # de-dupe
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _resolve_image_model(requested: str | None) -> str:
    if not requested:
        return DEFAULT_IMAGE_MODEL
    return IMAGE_MODEL_MAP.get(requested, DEFAULT_IMAGE_MODEL)


def _get_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _build_image_prompt(prompt: str) -> str:
    return (
        "Generate an image now. Return an accessible image URL if possible. "
        "Do not only describe the image in words.\n\n"
        f"User request: {prompt}"
    )


async def _poll_image_task(client: QwenClient, token: str, task_id: str, *, timeout_seconds: int = 300) -> dict:
    started = time.monotonic()
    last: dict = {}
    while time.monotonic() - started < timeout_seconds:
        task_status = await client.get_task_status(token, task_id)
        last = task_status if isinstance(task_status, dict) else {}
        data = last.get("data") if isinstance(last.get("data"), dict) else last
        state = str((data or {}).get("task_status") or (data or {}).get("status") or "").lower()
        log.info("[T2I] task_id=%s status=%s", task_id, state)
        if state == "success":
            return last
        if state not in {"running", "pending", "queued", "created", "processing", ""}:
            raise HTTPException(status_code=500, detail={"message": "Image task failed", "task_status": data})
        await asyncio.sleep(5)
    raise HTTPException(status_code=504, detail={"message": "Image task timed out", "task_id": task_id, "last_status": last})


@router.post("/v1/images/generations")
@router.post("/images/generations")
async def create_image(request: Request):
    from backend.core.config import API_KEYS, settings

    client: QwenClient = request.app.state.qwen_client

    token = _get_token(request)
    if API_KEYS:
        if token != settings.ADMIN_KEY and token not in API_KEYS:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt: str = str(body.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    n: int = min(max(int(body.get("n", 1)), 1), 4)
    model = _resolve_image_model(body.get("model"))
    size = str(body.get("size", "")).strip() or None

    log.info("[T2I] model=%s n=%s size=%s prompt=%r", model, n, size, prompt[:100])

    acc = None
    chat_id = None
    try:
        prompt_text = _build_image_prompt(prompt)

        # 1) Prefer same HTTP path as videos: create_chat(t2i) + complete_once
        if client.account_pool is None:
            raise HTTPException(status_code=500, detail="No upstream account pool configured")

        acc = await client.account_pool.acquire_wait(timeout=30)
        if acc is None:
            raise HTTPException(status_code=503, detail="No available upstream accounts")

        # Try t2i first; some deployments may only honor image mode via t2i chat_type
        chat_type = "t2i"
        try:
            chat_id = await client.create_chat(acc.token, model, chat_type=chat_type)
        except Exception as e:
            log.warning("[T2I] create_chat t2i failed (%s), fallback t2t", e)
            chat_type = "t2t"
            chat_id = await client.create_chat(acc.token, model, chat_type=chat_type)

        log.info("[T2I] chat_id=%s account=%s chat_type=%s", chat_id, getattr(acc, "email", ""), chat_type)

        completion = await client.complete_once(
            acc.token,
            chat_id,
            model,
            prompt_text,
            has_custom_tools=False,
            chat_type=chat_type,
            size=size,
        )

        search_payload: list[Any] = [completion]
        try:
            chat_detail = await client.get_chat(acc.token, chat_id)
            search_payload.append(chat_detail)
        except Exception as chat_error:
            log.warning("[T2I] get_chat failed: %s", chat_error)

        task_ids = _extract_task_ids(search_payload)
        if task_ids:
            log.info("[T2I] task IDs: %s", task_ids)
            task_result = await _poll_image_task(client, acc.token, task_ids[0])
            search_payload.append(task_result)

        image_urls = _extract_image_urls(search_payload)
        log.info("[T2I] extracted %s image URLs: %s", len(image_urls), image_urls[:5])

        if not image_urls:
            # fallback: old stream path (may need playwright)
            log.warning("[T2I] HTTP path got no image URL, trying stream fallback")
            event_payloads: list[str] = []
            async for item in client.chat_stream_events_with_retry(model, prompt_text, has_custom_tools=False):
                if item.get("type") == "event":
                    event_payloads.append(json.dumps(item.get("event", {}), ensure_ascii=False))
            image_urls = _extract_image_urls("\n".join(event_payloads))

        if not image_urls:
            debug_path = (
                __import__("os").path.join(
                    __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.dirname(__file__))),
                    "logs",
                    "t2i-last-debug.json",
                )
            )
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    json.dump({"completion": completion, "search": search_payload}, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            raise HTTPException(status_code=500, detail="Image generation succeeded but no URL found")

        # 默认返回 b64_json：ArcReel 等 Docker 容器常无法稳定下载 cdn.qwenlm.ai，
        # 由宿主机网关下载后转 base64，可避免容器二次拉 CDN 失败。
        response_format = str(body.get("response_format", "b64_json") or "b64_json").lower()
        selected = image_urls[:n]
        data: list[dict[str, Any]] = []
        if response_format == "url":
            data = [{"url": url, "revised_prompt": prompt} for url in selected]
        else:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
                for url in selected:
                    item: dict[str, Any] = {"url": url, "revised_prompt": prompt}
                    try:
                        resp = await http.get(url)
                        resp.raise_for_status()
                        item["b64_json"] = base64.b64encode(resp.content).decode("ascii")
                    except Exception as dl_err:
                        log.warning("[T2I] download for b64 failed url=%s err=%s", url[:120], dl_err)
                    data.append(item)
            if not any(x.get("b64_json") for x in data):
                # 全部下载失败时退回纯 URL，至少不让接口 500
                log.warning("[T2I] all b64 downloads failed, fallback to url-only response")
        return JSONResponse({"created": int(time.time()), "data": data})

    except HTTPException:
        raise
    except Exception as e:
        log.error("[T2I] generation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if acc is not None:
            try:
                client.account_pool.release(acc)
            except Exception:
                pass
            if chat_id:
                asyncio.create_task(client.delete_chat(acc.token, chat_id))


def _build_i2i_prompt(prompt: str) -> str:
    return (
        "Image-to-image edit / storyboard frame generation. "
        "STRICTLY keep the same character identity, face, hairstyle, clothing colors, "
        "art style and overall look from the attached reference image(s). "
        "Only change pose, camera angle, composition and scene details as requested. "
        "Generate an image now and return an accessible image URL if possible. "
        "Do not only describe the image in words.\n\n"
        f"User request: {prompt}"
    )


async def _upload_i2i_refs(client: QwenClient, acc: Any, raw_files: list[tuple[bytes, str, str]]) -> list[dict]:
    """Upload local image bytes and build vision file refs for complete_once(files=...)."""
    from backend.core.config import settings
    from backend.services.upstream_file_uploader import UpstreamFileUploader

    uploader = UpstreamFileUploader(client, settings)
    out: list[dict] = []
    tmp_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "logs",
        "image_refs",
    )
    os.makedirs(tmp_dir, exist_ok=True)

    for idx, (raw, content_type, filename) in enumerate(raw_files[:4]):
        local_path = os.path.join(tmp_dir, f"{int(time.time())}_{idx}_{filename}")
        with open(local_path, "wb") as f:
            f.write(raw)
        meta = {
            "filename": filename,
            "path": local_path,
            "content_type": content_type,
            "size": len(raw),
        }
        remote = await uploader.upload_local_file(acc, meta)
        ref = remote.get("remote_ref")
        put_url = ""
        if isinstance(ref, dict):
            put_url = str(ref.get("url") or "")
        if not put_url:
            put_url = str(remote.get("url") or "")

        vision_ref: dict[str, Any] = {
            "type": "image",
            "name": filename,
            "file_type": content_type if content_type.startswith("image/") else "image/png",
            "showType": "image",
            "status": "uploaded",
            "file_class": "vision",
            "url": put_url,
        }
        if isinstance(ref, dict):
            if ref.get("id"):
                vision_ref["id"] = ref.get("id")
            if isinstance(ref.get("file"), dict) and ref["file"].get("id"):
                vision_ref["file"] = ref["file"]
            for k in ("itemId", "uploadTaskId", "size", "greenNet", "progress", "error", "collection_name"):
                if k in ref and k not in vision_ref:
                    vision_ref[k] = ref[k]
        if not vision_ref.get("url"):
            log.warning("[I2I] upload missing url, remote=%s", remote)
        out.append(vision_ref)
        log.info(
            "[I2I] uploaded ref name=%s url=%s",
            filename,
            (vision_ref.get("url") or "")[:120],
        )
    return out


async def _pack_image_response(
    image_urls: list[str],
    *,
    prompt: str,
    n: int,
    response_format: str,
) -> JSONResponse:
    selected = image_urls[:n]
    data: list[dict[str, Any]] = []
    if response_format == "url":
        data = [{"url": url, "revised_prompt": prompt} for url in selected]
    else:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
            for url in selected:
                item: dict[str, Any] = {"url": url, "revised_prompt": prompt}
                try:
                    resp = await http.get(url)
                    resp.raise_for_status()
                    item["b64_json"] = base64.b64encode(resp.content).decode("ascii")
                except Exception as dl_err:
                    log.warning("[I2I] download for b64 failed url=%s err=%s", url[:120], dl_err)
                data.append(item)
        if not any(x.get("b64_json") for x in data):
            log.warning("[I2I] all b64 downloads failed, fallback to url-only response")
    return JSONResponse({"created": int(time.time()), "data": data})


@router.post("/v1/images/edits")
@router.post("/images/edits")
async def edit_image(request: Request):
    """OpenAI-compatible image edits (i2i).

    Accepts multipart form (OpenAI SDK style) or JSON with image/images fields.
    Uses Qwen vision + t2i tool path with attached reference image(s).
    """
    from backend.core.config import API_KEYS, settings

    client: QwenClient = request.app.state.qwen_client
    token = _get_token(request)
    if API_KEYS:
        if token != settings.ADMIN_KEY and token not in API_KEYS:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    content_type = (request.headers.get("content-type") or "").lower()
    raw_files: list[tuple[bytes, str, str]] = []
    prompt = ""
    model_name: str | None = None
    n = 1
    size = None
    response_format = "b64_json"

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        prompt = str(form.get("prompt") or "").strip()
        model_name = str(form.get("model") or "") or None
        try:
            n = min(max(int(form.get("n") or 1), 1), 4)
        except Exception:
            n = 1
        size = str(form.get("size") or "").strip() or None
        response_format = str(form.get("response_format") or "b64_json").lower()

        # OpenAI SDK may send image / image[] / image[0]
        candidates: list[Any] = []
        for key in form.keys():
            kl = key.lower()
            if kl == "image" or kl.startswith("image[") or kl.startswith("image_") or kl in {"mask"}:
                if kl == "mask":
                    continue
                val = form.getlist(key) if hasattr(form, "getlist") else [form.get(key)]
                candidates.extend(val)
        # also single get
        if not candidates and form.get("image") is not None:
            candidates = [form.get("image")]

        for item in candidates:
            if item is None:
                continue
            if hasattr(item, "read"):
                raw = await item.read()
                filename = getattr(item, "filename", None) or "ref.png"
                ctype = getattr(item, "content_type", None) or mimetypes.guess_type(filename)[0] or "image/png"
                if raw:
                    raw_files.append((raw, ctype, os.path.basename(filename) or "ref.png"))
            elif isinstance(item, (bytes, bytearray)):
                raw_files.append((bytes(item), "image/png", "ref.png"))
            elif isinstance(item, str) and item.strip():
                # data url or path unlikely in multipart text; skip
                pass
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body; use multipart or JSON")
        prompt = str(body.get("prompt") or "").strip()
        model_name = body.get("model")
        try:
            n = min(max(int(body.get("n", 1)), 1), 4)
        except Exception:
            n = 1
        size = str(body.get("size") or "").strip() or None
        response_format = str(body.get("response_format") or "b64_json").lower()

        def _add_str_image(val: str):
            v = val.strip()
            if not v:
                return
            if v.startswith("data:"):
                header, b64 = v.split(",", 1)
                ctype = "image/png"
                if ";" in header:
                    ctype = header[5:].split(";")[0] or ctype
                raw = base64.b64decode(b64)
                ext = mimetypes.guess_extension(ctype) or ".png"
                raw_files.append((raw, ctype, f"ref{ext}"))
            elif v.startswith("http://") or v.startswith("https://"):
                import urllib.request

                req = urllib.request.Request(v, headers={"User-Agent": "qwen2api-i2i/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    ctype = resp.headers.get("Content-Type") or "image/png"
                ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ".png"
                raw_files.append((raw, ctype.split(";")[0].strip(), f"ref{ext}"))
            elif os.path.exists(v):
                raw = open(v, "rb").read()
                ctype = mimetypes.guess_type(v)[0] or "image/png"
                raw_files.append((raw, ctype, os.path.basename(v) or "ref.png"))

        for key in ("image", "image_url", "reference_image", "ref_image"):
            val = body.get(key)
            if isinstance(val, str):
                _add_str_image(val)
        for key in ("images", "image_urls", "reference_images"):
            val = body.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        _add_str_image(item)
                    elif isinstance(item, dict):
                        u = item.get("url") or item.get("image_url") or item.get("b64_json") or item.get("data")
                        if isinstance(u, str):
                            if item.get("b64_json") and not u.startswith("data:"):
                                _add_str_image("data:image/png;base64," + u)
                            else:
                                _add_str_image(u)

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if not raw_files:
        raise HTTPException(status_code=400, detail="image is required for edits/i2i")

    model = _resolve_image_model(model_name)
    log.info("[I2I] model=%s n=%s refs=%s prompt=%r", model, n, len(raw_files), prompt[:100])

    acc = None
    chat_id = None
    try:
        if client.account_pool is None:
            raise HTTPException(status_code=500, detail="No upstream account pool configured")
        acc = await client.account_pool.acquire_wait(timeout=30)
        if acc is None:
            raise HTTPException(status_code=503, detail="No available upstream accounts")

        files = await _upload_i2i_refs(client, acc, raw_files)
        if not files or not any(f.get("url") for f in files):
            raise HTTPException(status_code=500, detail="Failed to upload reference image(s) to upstream")

        prompt_text = _build_i2i_prompt(prompt)
        # 网页侧图生图：带 vision 参考图的 t2i / t2t 会话
        chat_type = "t2i"
        try:
            chat_id = await client.create_chat(acc.token, model, chat_type=chat_type)
        except Exception as e:
            log.warning("[I2I] create_chat t2i failed (%s), fallback t2t", e)
            chat_type = "t2t"
            chat_id = await client.create_chat(acc.token, model, chat_type=chat_type)

        log.info("[I2I] chat_id=%s account=%s chat_type=%s files=%s", chat_id, getattr(acc, "email", ""), chat_type, len(files))

        completion = await client.complete_once(
            acc.token,
            chat_id,
            model,
            prompt_text,
            has_custom_tools=False,
            files=files,
            chat_type=chat_type,
            size=size,
        )

        search_payload: list[Any] = [completion]
        try:
            chat_detail = await client.get_chat(acc.token, chat_id)
            search_payload.append(chat_detail)
        except Exception as chat_error:
            log.warning("[I2I] get_chat failed: %s", chat_error)

        task_ids = _extract_task_ids(search_payload)
        if task_ids:
            log.info("[I2I] task IDs: %s", task_ids)
            task_result = await _poll_image_task(client, acc.token, task_ids[0])
            search_payload.append(task_result)

        image_urls = _extract_image_urls(search_payload)
        log.info("[I2I] extracted %s image URLs: %s", len(image_urls), image_urls[:5])
        if not image_urls:
            raise HTTPException(status_code=500, detail="Image edit succeeded but no URL found")

        return await _pack_image_response(
            image_urls,
            prompt=prompt,
            n=n,
            response_format=response_format,
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error("[I2I] edit failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if acc is not None:
            try:
                client.account_pool.release(acc)
            except Exception:
                pass
            if chat_id:
                asyncio.create_task(client.delete_chat(acc.token, chat_id))
