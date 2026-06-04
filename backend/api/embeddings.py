from fastapi import APIRouter, Request, HTTPException
import json
import uuid
import logging
from backend.services.auth_quota import extract_api_token, has_quota_exceeded
from backend.services.token_calc import count_tokens

log = logging.getLogger("qwen2api.embeddings")
router = APIRouter()

@router.post("/embeddings")
@router.post("/v1/embeddings")
async def create_embeddings(request: Request):
    """
    Embeddings 模拟/转发接口。
    通义千问 Web 版没有原生的 Embeddings 接口。
    为了兼容部分客户端强制验证 Embeddings 的情况（如 OpenWebUI 的某些场景），
    这里提供基于 Tiktoken 和 Hash 的模拟响应，或配置专门的小模型转发。
    目前实现为模拟返回。
    """
    app = request.app
    users_db = app.state.users_db
    
    # 鉴权 (完全复原单文件逻辑)
    token = extract_api_token(request)

    from backend.core.config import API_KEYS, settings
    admin_k = settings.ADMIN_KEY

    if API_KEYS:
        if token != admin_k and token not in API_KEYS and not token:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and has_quota_exceeded(user):
        raise HTTPException(status_code=402, detail="Quota Exceeded")
        
    body = await request.json()
    model = body.get("model", "text-embedding-ada-002")
    input_text = body.get("input", "")
    
    if isinstance(input_text, str):
        input_list = [input_text]
    else:
        input_list = input_text
        
    data = []
    total_tokens = 0
    
    for i, text in enumerate(input_list):
        tokens = count_tokens(text)
        total_tokens += tokens
        
        # 模拟生成 1536 维的特征向量
        # 基于文本 hash 的简单确定性伪随机向量，保证同一文本结果一致
        import hashlib
        h = hashlib.sha256(text.encode('utf-8')).hexdigest()
        base_val = int(h[:8], 16) / 0xffffffff
        vector = [(base_val * (j % 10) / 10.0) - 0.5 for j in range(1536)]
        
        data.append({
            "object": "embedding",
            "embedding": vector,
            "index": i
        })
        
    usage = {
        "prompt_tokens": total_tokens,
        "total_tokens": total_tokens
    }
    
    # 异步扣除 Token
    for u in users:
        if u["id"] == token:
            u["used_tokens"] += usage["total_tokens"]
            break
    await users_db.save(users)
    
    return {
        "object": "list",
        "data": data,
        "model": model,
        "usage": usage
    }
