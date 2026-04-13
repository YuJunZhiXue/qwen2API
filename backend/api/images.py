"""
图片生成接口 — 兼容 OpenAI /v1/images/generations 规范。

底层通过千问网页当前真实的“生成图像”模式触发，而不是写死 wanx 模型名。
页面实测结果显示：UI 仍显示 `Qwen3.6-Plus`，并通过“生成图像”模式完成图片生成。
"""
import re
import time
import asyncio
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from backend.services.qwen_client import QwenClient

log = logging.getLogger("qwen2api.images")
router = APIRouter()

# 默认图片生成模型：网页实测仍显示为 Qwen3.6-Plus
DEFAULT_IMAGE_MODEL = "qwen3.6-plus"

# 受支持的图片模型别名 -> 网页真实可用的基础模型
IMAGE_MODEL_MAP = {
    "dall-e-3": "qwen3.6-plus",
    "dall-e-2": "qwen3.6-plus",
    "qwen-image": "qwen3.6-plus",
    "qwen-image-plus": "qwen3.6-plus",
    "qwen-image-turbo": "qwen3.6-plus",
    "qwen3.6-plus": "qwen3.6-plus",
}


def _extract_image_urls(text: str) -> list[str]:
    """从模型输出中提取图片 URL（支持 Markdown、JSON 字段、裸 URL 三种格式）"""
    urls: list[str] = []

    # 1. Markdown 图片语法: ![...](url)
    for u in re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', text):
        urls.append(u.rstrip(").,;"))

    # 2. JSON 字段: "url":"...", "image":"...", "src":"..."
    if not urls:
        for u in re.findall(r'"(?:url|image|src|imageUrl|image_url)"\s*:\s*"(https?://[^"]+)"', text):
            urls.append(u)

    # 3. 裸 URL（以常见图片扩展名结尾，或来自已知 CDN）
    if not urls:
        cdn_pattern = r'https?://(?:cdn\.qwenlm\.ai|wanx\.alicdn\.com|img\.alicdn\.com|[^\s"<>]+\.(?:jpg|jpeg|png|webp|gif))[^\s"<>]*'
        for u in re.findall(cdn_pattern, text, re.IGNORECASE):
            urls.append(u.rstrip(".,;)\"'>"))

    # 去重并保留顺序
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _resolve_image_model(requested: str | None) -> str:
    if not requested:
        return DEFAULT_IMAGE_MODEL
    return IMAGE_MODEL_MAP.get(requested, DEFAULT_IMAGE_MODEL)


def _get_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


@router.post("/v1/images/generations")
@router.post("/images/generations")
async def create_image(request: Request):
    """
    OpenAI 兼容的图片生成接口。

    请求体示例:
    ```json
    {
      "prompt": "一只赛博朋克风格的猫",
      "model": "dall-e-3",
      "n": 1,
      "size": "1024x1024",
      "response_format": "url"
    }
    ```
    """
    from backend.core.config import API_KEYS, settings
    client: QwenClient = request.app.state.qwen_client

    # 鉴权
    token = _get_token(request)
    if API_KEYS:
        if token != settings.ADMIN_KEY and token not in API_KEYS:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    prompt: str = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")

    n: int = min(max(int(body.get("n", 1)), 1), 4)  # 最多 4 张
    model = _resolve_image_model(body.get("model"))

    log.info(f"[T2I] model={model}, n={n}, prompt={prompt[:80]!r}")

    try:
        answer_text, acc, chat_id = await client.image_generate_with_retry(model, prompt)

        # 后台清理会话
        client.account_pool.release(acc)
        asyncio.create_task(client.delete_chat(acc.token, chat_id))

        # 提取图片 URL
        image_urls = _extract_image_urls(answer_text)
        log.info(f"[T2I] 提取到 {len(image_urls)} 张图片 URL: {image_urls}")

        if not image_urls:
            log.warning(f"[T2I] 未能提取图片 URL，原始响应: {answer_text[:300]!r}")
            raise HTTPException(
                status_code=500,
                detail=f"Image generation succeeded but no URL found. Raw response: {answer_text[:200]}"
            )

        data = [{"url": url, "revised_prompt": prompt} for url in image_urls[:n]]
        return JSONResponse({"created": int(time.time()), "data": data})

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[T2I] 生成失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
