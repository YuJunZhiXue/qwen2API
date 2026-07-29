import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.services.qwen_client import QwenClient

log = logging.getLogger("qwen2api.videos")
router = APIRouter()

DEFAULT_VIDEO_MODEL = "qwen3.6-plus"
VIDEO_BUSY_EMAILS: set[str] = set()
VIDEO_BUSY_LOCK = asyncio.Lock()

# Async task store for NewAPI/ArcReel polling protocol.
# ArcReel's newapi-video backend expects:
#   POST /v1/video/generations -> {"task_id": "..."}
#   GET  /v1/video/generations/{task_id} -> {"status":"completed","url":"..."}
# The old sync path waited ~90s and exceeded ArcReel's 60s create timeout
# (create_ambiguous). Background jobs fix that.
VIDEO_TASKS: dict[str, dict[str, Any]] = {}
VIDEO_TASKS_LOCK = asyncio.Lock()
VIDEO_TASK_TTL_SECONDS = 3600

VIDEO_MODEL_MAP = {
    "sora": "qwen3.6-plus",
    "sora-1.0-turbo": "qwen3.6-plus",
    "qwen-video": "qwen3.6-plus",
    "qwen-video-plus": "qwen3.6-plus",
    "qwen3.6-plus": "qwen3.6-plus",
}

HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>)]*", re.IGNORECASE)
VIDEO_EXT_RE = re.compile(r"\.(?:mp4|webm|mov|m3u8|m4v)(?:[?#]|$)", re.IGNORECASE)
IMAGE_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp|gif|svg)(?:[?#]|$)", re.IGNORECASE)


def _resolve_video_model(requested: str | None) -> str:
    if not requested:
        return DEFAULT_VIDEO_MODEL
    return VIDEO_MODEL_MAP.get(requested, DEFAULT_VIDEO_MODEL)


def _get_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _looks_like_video_url(url: str, key_hint: str = "") -> bool:
    lowered = url.lower().rstrip(".,;)\"'>")
    if IMAGE_EXT_RE.search(lowered):
        return False
    if VIDEO_EXT_RE.search(lowered):
        return True
    hint = key_hint.lower()
    if any(token in hint for token in ("video", "media", "download", "play")):
        return True
    return any(token in lowered for token in ("/video", "video_", "video-", "/videos", "m3u8"))


def _extract_video_urls(value: Any, key_hint: str = "") -> list[str]:
    urls: list[str] = []

    if value is None:
        return urls

    if isinstance(value, str):
        for raw_url in HTTP_URL_RE.findall(value):
            url = raw_url.rstrip(".,;)\"'>")
            if _looks_like_video_url(url, key_hint):
                urls.append(url)
        return urls

    if isinstance(value, list):
        for item in value:
            urls.extend(_extract_video_urls(item, key_hint))
        return urls

    if isinstance(value, dict):
        for key, item in value.items():
            next_hint = str(key)
            urls.extend(_extract_video_urls(item, next_hint))
        return urls

    return urls


def _extract_task_ids(value: Any) -> list[str]:
    task_ids: list[str] = []

    if value is None:
        return task_ids

    if isinstance(value, list):
        for item in value:
            task_ids.extend(_extract_task_ids(item))
        return task_ids

    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"task_id", "taskId"} and isinstance(item, str) and item.strip():
                task_ids.append(item.strip())
            else:
                task_ids.extend(_extract_task_ids(item))

    return task_ids


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _build_video_prompt(prompt: str, size: str | None) -> str:
    return prompt


def _decode_image_input(image_value: str) -> tuple[bytes, str, str]:
    """Return (raw_bytes, content_type, filename) from data-url or http(s) url."""
    import base64
    import mimetypes
    import urllib.request

    value = (image_value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="empty image")

    if value.startswith("data:"):
        header, b64 = value.split(",", 1)
        content_type = "image/png"
        if ";" in header:
            content_type = header[5:].split(";")[0] or content_type
        raw = base64.b64decode(b64)
        ext = mimetypes.guess_extension(content_type) or ".png"
        return raw, content_type, f"ref{ext}"

    if value.startswith("http://") or value.startswith("https://"):
        req = urllib.request.Request(value, headers={"User-Agent": "qwen2api-video/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type") or "image/png"
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".png"
        return raw, content_type.split(";")[0].strip(), f"ref{ext}"

    # local path
    if os.path.exists(value):
        raw = open(value, "rb").read()
        content_type = mimetypes.guess_type(value)[0] or "image/png"
        return raw, content_type, os.path.basename(value) or "ref.png"

    raise HTTPException(status_code=400, detail="image must be data-url, http(s) url, or local path")


async def _upload_reference_images(client: QwenClient, acc: Any, images: list[str]) -> list[dict]:
    """Upload one or more reference images and return remote file refs for complete_once(files=...)."""
    if not images:
        return []

    from backend.core.config import settings
    from backend.services.upstream_file_uploader import UpstreamFileUploader

    uploader = UpstreamFileUploader(client, settings)
    files: list[dict] = []
    tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs", "video_refs")
    os.makedirs(tmp_dir, exist_ok=True)

    for idx, image_value in enumerate(images[:4]):
        raw, content_type, filename = _decode_image_input(image_value)
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
            # fallback construct from uploader fields if present
            put_url = str(remote.get("url") or "")

        # Frontend real i2v file object (from chat.qwen.ai frontend_main.js):
        # {type:"image", name, file_type, showType:"image", status:"uploaded",
        #  file_class:"vision", url}
        # When VideoGeneration + image file is present, frontend auto-switches to i2v.
        vision_ref = {
            "type": "image",
            "name": filename,
            "file_type": content_type if content_type.startswith("image/") else "image/png",
            "showType": "image",
            "status": "uploaded",
            "file_class": "vision",
            "url": put_url,
        }
        # Keep upstream file id if available (some servers use it).
        if isinstance(ref, dict):
            if ref.get("id"):
                vision_ref["id"] = ref.get("id")
            if isinstance(ref.get("file"), dict) and ref["file"].get("id"):
                vision_ref["file"] = ref["file"]
            # merge non-conflicting extras
            for k in ("itemId", "uploadTaskId", "size", "greenNet", "progress", "error", "collection_name"):
                if k in ref and k not in vision_ref:
                    vision_ref[k] = ref[k]

        if not vision_ref.get("url"):
            log.warning("[T2V] upload missing url, remote=%s", remote)
        files.append(vision_ref)
        log.info(
            "[T2V] uploaded reference image file_id=%s name=%s url=%s type=%s class=%s",
            remote.get("remote_file_id"),
            filename,
            (vision_ref.get("url") or "")[:120],
            vision_ref.get("type"),
            vision_ref.get("file_class"),
        )
    return files


def _collect_images_from_body(body: dict) -> list[str]:
    images: list[str] = []
    for key in ("image", "image_url", "reference_image", "ref_image"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            images.append(val.strip())
    for key in ("images", "image_urls", "reference_images"):
        val = body.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    images.append(item.strip())
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("image_url") or item.get("data")
                    if isinstance(u, str) and u.strip():
                        images.append(u.strip())
    # de-dupe keep order
    seen: set[str] = set()
    out: list[str] = []
    for u in images:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _video_account_wait_seconds() -> int:
    try:
        return max(30, int(os.getenv("QWEN_VIDEO_ACCOUNT_WAIT_SECONDS", "900")))
    except Exception:
        return 900


async def _acquire_video_account(client: QwenClient):
    if client.account_pool is None:
        raise HTTPException(status_code=500, detail="No upstream account pool configured")

    deadline = time.monotonic() + _video_account_wait_seconds()

    while time.monotonic() < deadline:
        async with VIDEO_BUSY_LOCK:
            exclude = set(VIDEO_BUSY_EMAILS)

        remaining = max(1, deadline - time.monotonic())
        acc = await client.account_pool.acquire_wait(timeout=min(10, remaining), exclude=exclude)
        if acc is None:
            await asyncio.sleep(2)
            continue

        email = getattr(acc, "email", "") or ""
        async with VIDEO_BUSY_LOCK:
            if email not in VIDEO_BUSY_EMAILS:
                VIDEO_BUSY_EMAILS.add(email)
                return acc

        client.account_pool.release(acc)
        await asyncio.sleep(1)

    raise HTTPException(status_code=503, detail="No available upstream video accounts")


async def _release_video_account(client: QwenClient, acc: Any):
    email = getattr(acc, "email", "") or ""
    async with VIDEO_BUSY_LOCK:
        VIDEO_BUSY_EMAILS.discard(email)
    client.account_pool.release(acc)


async def _poll_video_task(client: QwenClient, token: str, task_id: str, *, timeout_seconds: int = 420) -> dict:
    started_at = time.monotonic()
    last_status: dict = {}

    while time.monotonic() - started_at < timeout_seconds:
        task_status = await client.get_task_status(token, task_id)
        last_status = task_status
        data = task_status.get("data") if isinstance(task_status, dict) else None
        if not isinstance(data, dict) and isinstance(task_status, dict):
            data = task_status
        if not isinstance(data, dict):
            await asyncio.sleep(5)
            continue

        state = str(data.get("task_status") or data.get("status") or "").lower()
        log.info("[T2V] task_id=%s status=%s", task_id, state)
        if state == "success":
            return task_status
        if state not in {"running", "pending", "queued", "created", "processing", ""}:
            raise HTTPException(status_code=500, detail={"message": "Video task failed", "task_status": data})

        await asyncio.sleep(10)

    raise HTTPException(status_code=504, detail={"message": "Video task timed out", "task_id": task_id, "last_status": last_status})


def _normalize_size(body: dict) -> str | None:
    """Accept size=1280*720 or ArcReel/newapi width+height."""
    size = str(body.get("size", "")).strip() or None
    if size:
        return size
    width = body.get("width")
    height = body.get("height")
    try:
        if width and height:
            return f"{int(width)}*{int(height)}"
    except Exception:
        pass
    return None


async def _purge_old_video_tasks() -> None:
    now = time.time()
    async with VIDEO_TASKS_LOCK:
        dead = [tid for tid, t in VIDEO_TASKS.items() if now - float(t.get("created_at", now)) > VIDEO_TASK_TTL_SECONDS]
        for tid in dead:
            VIDEO_TASKS.pop(tid, None)


async def _set_video_task(task_id: str, **fields: Any) -> None:
    async with VIDEO_TASKS_LOCK:
        cur = VIDEO_TASKS.get(task_id) or {"task_id": task_id, "created_at": time.time()}
        cur.update(fields)
        cur["updated_at"] = time.time()
        VIDEO_TASKS[task_id] = cur


async def _get_video_task(task_id: str) -> dict[str, Any] | None:
    async with VIDEO_TASKS_LOCK:
        return dict(VIDEO_TASKS.get(task_id) or {}) or None


async def _run_video_job(app: Any, task_id: str, body: dict) -> None:
    """Background worker: full Qwen t2v/i2v pipeline, then update VIDEO_TASKS."""
    from backend.core.config import settings  # noqa: F401

    client: QwenClient = app.state.qwen_client
    prompt = str(body.get("prompt", "")).strip()
    n = min(max(int(body.get("n", 1)), 1), 4)
    model = _resolve_video_model(body.get("model"))
    size = _normalize_size(body)
    ref_images = _collect_images_from_body(body)
    duration = body.get("duration") or body.get("duration_seconds")

    await _set_video_task(task_id, status="processing", model=model, prompt=prompt[:200])
    log.info("[T2V-ASYNC] start task_id=%s model=%s refs=%s size=%s", task_id, model, len(ref_images), size)

    acc = None
    chat_id = None
    try:
        prompt_text = _build_video_prompt(prompt, size)
        if ref_images:
            prompt_text = (
                "Use the attached reference image(s) as strict character/appearance identity lock. "
                "Keep face, hair, clothes and body proportions consistent with the reference. "
                "Do not invent a different character.\n\n" + prompt_text
            )

        max_account_tries = 7
        tried_emails: set[str] = set()
        last_completion: Any = None
        last_chat_detail: Any = None
        video_urls: list[str] = []

        for attempt in range(1, max_account_tries + 1):
            if acc is None:
                acc = await _acquire_video_account(client)
            email = getattr(acc, "email", "") or ""
            if email:
                tried_emails.add(email)

            files = []
            img_url = None
            if ref_images:
                try:
                    files = await _upload_reference_images(client, acc, ref_images)
                    for f in files:
                        if isinstance(f, dict) and isinstance(f.get("url"), str) and f["url"].startswith("http"):
                            img_url = f["url"]
                            break
                except Exception as upload_error:
                    log.warning("[T2V-ASYNC] reference upload failed on %s: %s", email, upload_error)
                    files = []
                    img_url = None

            message_type = "i2v" if (files and img_url) else "t2v"
            try:
                chat_id = await client.create_chat(acc.token, model, chat_type="t2v")
                chat_type = message_type
            except Exception as chat_error:
                log.warning("[T2V-ASYNC] create_chat failed on %s: %s", email, chat_error)
                await _release_video_account(client, acc)
                acc = None
                chat_id = None
                continue

            log.info(
                "[T2V-ASYNC] task=%s try=%s chat_id=%s account=%s chat_type=%s files=%s",
                task_id,
                attempt,
                chat_id,
                email,
                chat_type,
                len(files),
            )

            completion = await client.complete_once(
                acc.token,
                chat_id,
                model,
                prompt_text,
                has_custom_tools=False,
                files=files or None,
                chat_type=chat_type,
                size=size,
                img_url=img_url,
            )
            last_completion = completion

            if isinstance(completion, dict):
                data_obj = completion.get("data") if isinstance(completion.get("data"), dict) else {}
                code = str((data_obj or {}).get("code") or completion.get("code") or "")
                details = str((data_obj or {}).get("details") or completion.get("details") or "")
                success_flag = completion.get("success")
                if success_flag is False or code in {"RateLimited", "rate_limited", "QuotaExceeded"} or "limit" in details.lower():
                    log.warning("[T2V-ASYNC] limited email=%s code=%s details=%s", email, code, details)
                    if chat_id:
                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                        chat_id = None
                    await _release_video_account(client, acc)
                    acc = None
                    if attempt >= max_account_tries:
                        await _set_video_task(
                            task_id,
                            status="failed",
                            error={"message": details or "RateLimited on all accounts", "code": code or "RateLimited"},
                            tried_accounts=list(tried_emails),
                        )
                        return
                    continue

            chat_detail = None
            try:
                chat_detail = await client.get_chat(acc.token, chat_id)
                last_chat_detail = chat_detail
            except Exception as chat_error:
                log.warning("[T2V-ASYNC] failed to fetch chat detail: %s", chat_error)

            search_payload: list[Any] = [completion]
            if chat_detail:
                search_payload.append(chat_detail)

            task_ids = _dedupe(_extract_task_ids(search_payload))
            if task_ids:
                log.info("[T2V-ASYNC] extracted upstream task IDs: %s", task_ids)
                try:
                    task_result = await _poll_video_task(client, acc.token, task_ids[0])
                    search_payload.append(task_result)
                except HTTPException as poll_err:
                    log.warning("[T2V-ASYNC] poll failed on %s: %s", email, poll_err.detail)
                    if chat_id:
                        asyncio.create_task(client.delete_chat(acc.token, chat_id))
                        chat_id = None
                    await _release_video_account(client, acc)
                    acc = None
                    continue

            video_urls = _dedupe(_extract_video_urls(search_payload))
            log.info("[T2V-ASYNC] extracted %s video URLs", len(video_urls))
            if video_urls:
                break

            if chat_id:
                asyncio.create_task(client.delete_chat(acc.token, chat_id))
                chat_id = None
            await _release_video_account(client, acc)
            acc = None

        if not video_urls:
            debug_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "logs",
                f"t2v-async-{task_id}.json",
            )
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {"completion": last_completion, "chat": last_chat_detail, "tried": list(tried_emails)},
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
            except Exception:
                pass
            await _set_video_task(
                task_id,
                status="failed",
                error={"message": "Video generation produced no URL"},
                tried_accounts=list(tried_emails),
            )
            return

        url = video_urls[0]
        meta: dict[str, Any] = {}
        if duration is not None:
            try:
                meta["duration"] = int(float(duration))
            except Exception:
                pass
        await _set_video_task(
            task_id,
            status="completed",
            url=url,
            urls=video_urls[:n],
            metadata=meta,
            data=[{"url": u, "revised_prompt": prompt} for u in video_urls[:n]],
        )
        log.info("[T2V-ASYNC] completed task_id=%s url=%s", task_id, url[:120])
    except Exception as e:
        log.exception("[T2V-ASYNC] task_id=%s failed: %s", task_id, e)
        await _set_video_task(task_id, status="failed", error={"message": str(e)})
    finally:
        if acc is not None:
            await _release_video_account(client, acc)
            if chat_id:
                asyncio.create_task(client.delete_chat(acc.token, chat_id))


@router.get("/v1/videos/generations/{task_id}")
@router.get("/v1/video/generations/{task_id}")
@router.get("/videos/generations/{task_id}")
@router.get("/video/generations/{task_id}")
async def get_video_task(task_id: str, request: Request):
    """NewAPI/ArcReel poll endpoint."""
    from backend.core.config import API_KEYS, settings

    token = _get_token(request)
    if API_KEYS:
        if token != settings.ADMIN_KEY and token not in API_KEYS:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    task = await _get_video_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")

    status = task.get("status") or "queued"
    body: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "model": task.get("model"),
    }
    if status == "completed":
        body["url"] = task.get("url")
        body["metadata"] = task.get("metadata") or {}
        if task.get("data"):
            body["data"] = task["data"]
    elif status == "failed":
        body["error"] = task.get("error") or {"message": "unknown"}
    return JSONResponse(body)


@router.post("/v1/videos/generations")
@router.post("/v1/video/generations")
@router.post("/videos/generations")
@router.post("/video/generations")
async def create_video(request: Request):
    """Create video.

    Default = **async NewAPI protocol** (immediate task_id) for ArcReel.
    Pass ``{"wait": true}`` to block until finished and return the old
    ``{"data":[{"url":...}]}`` shape (for scripts / manual tests).
    """
    from backend.core.config import API_KEYS, settings

    token = _get_token(request)
    if API_KEYS:
        if token != settings.ADMIN_KEY and token not in API_KEYS:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    await _purge_old_video_tasks()
    wait = bool(body.get("wait") is True or str(body.get("wait", "")).lower() in {"1", "true", "yes"})

    task_id = f"qv_{uuid.uuid4().hex[:16]}"
    await _set_video_task(task_id, status="queued", model=_resolve_video_model(body.get("model")), prompt=prompt[:200])
    asyncio.create_task(_run_video_job(request.app, task_id, body))
    log.info("[T2V] queued task_id=%s wait=%s prompt=%r", task_id, wait, prompt[:80])

    if not wait:
        # ArcReel / NewAPI path — return immediately so create never times out.
        return JSONResponse({"task_id": task_id, "status": "queued", "created": int(time.time())})

    # Sync wait path for scripts.
    deadline = time.monotonic() + int(body.get("timeout", 600) or 600)
    while time.monotonic() < deadline:
        task = await _get_video_task(task_id)
        if not task:
            break
        st = task.get("status")
        if st == "completed":
            data = task.get("data") or [{"url": task.get("url"), "revised_prompt": prompt}]
            return JSONResponse({"created": int(time.time()), "data": data, "task_id": task_id})
        if st == "failed":
            err = task.get("error") or {}
            raise HTTPException(status_code=500, detail=err.get("message") or "video failed")
        await asyncio.sleep(2)

    raise HTTPException(status_code=504, detail={"message": "wait timeout", "task_id": task_id})
