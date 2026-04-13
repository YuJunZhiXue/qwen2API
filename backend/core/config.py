import os
import json
from pathlib import Path
from pydantic_settings import BaseSettings
from typing import Dict, Set

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

class Settings(BaseSettings):
    # 服务配置
    PORT: int = int(os.getenv("PORT", 7860))
    WORKERS: int = int(os.getenv("WORKERS", 3))
    ADMIN_KEY: str = os.getenv("ADMIN_KEY", "admin")
    REGISTER_SECRET: str = os.getenv("REGISTER_SECRET", "")

    # 引擎模式：httpx（快速直连）、browser（浏览器指纹，防封）或 hybrid（混合）
    ENGINE_MODE: str = os.getenv("ENGINE_MODE", "hybrid")
    NATIVE_TOOL_PASSTHROUGH: bool = os.getenv("NATIVE_TOOL_PASSTHROUGH", "true").lower() in ("1", "true", "yes", "on")
    # 浏览器引擎配置
    BROWSER_POOL_SIZE: int = int(os.getenv("BROWSER_POOL_SIZE", 2))
    MAX_INFLIGHT_PER_ACCOUNT: int = int(os.getenv("MAX_INFLIGHT", 1))
    STREAM_KEEPALIVE_INTERVAL: int = int(os.getenv("STREAM_KEEPALIVE_INTERVAL", 5))

    # 容灾与限流
    MAX_RETRIES: int = 2
    TOOL_MAX_RETRIES: int = 2
    EMPTY_RESPONSE_RETRIES: int = 1
    ACCOUNT_MIN_INTERVAL_MS: int = int(os.getenv("ACCOUNT_MIN_INTERVAL_MS", 1200))
    REQUEST_JITTER_MIN_MS: int = int(os.getenv("REQUEST_JITTER_MIN_MS", 120))
    REQUEST_JITTER_MAX_MS: int = int(os.getenv("REQUEST_JITTER_MAX_MS", 360))
    RATE_LIMIT_BASE_COOLDOWN: int = int(os.getenv("RATE_LIMIT_BASE_COOLDOWN", 600))
    RATE_LIMIT_MAX_COOLDOWN: int = int(os.getenv("RATE_LIMIT_MAX_COOLDOWN", 3600))
    RATE_LIMIT_COOLDOWN: int = RATE_LIMIT_BASE_COOLDOWN

    # 数据文件路径
    ACCOUNTS_FILE: str = os.getenv("ACCOUNTS_FILE", str(DATA_DIR / "accounts.json"))
    USERS_FILE: str = os.getenv("USERS_FILE", str(DATA_DIR / "users.json"))
    CAPTURES_FILE: str = os.getenv("CAPTURES_FILE", str(DATA_DIR / "captures.json"))
    CONFIG_FILE: str = os.getenv("CONFIG_FILE", str(DATA_DIR / "config.json"))

    class Config:
        env_file = ".env"

API_KEYS_FILE = DATA_DIR / "api_keys.json"

def load_api_keys() -> set:
    if API_KEYS_FILE.exists():
        try:
            with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("keys", []))
        except Exception:
            pass
    return set()

def save_api_keys(keys: set):
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({"keys": list(keys)}, f, indent=2)

# 在内存中存储管理的 API Keys
API_KEYS = load_api_keys()

VERSION = "2.0.0"

settings = Settings()

# 全局映射
MODEL_MAP = {
    # OpenAI
    "gpt-4o":            "qwen3.6-plus",
    "gpt-4o-mini":       "qwen3.6-plus",
    "gpt-4-turbo":       "qwen3.6-plus",
    "gpt-4":             "qwen3.6-plus",
    "gpt-4.1":           "qwen3.6-plus",
    "gpt-4.1-mini":      "qwen3.6-plus",
    "gpt-3.5-turbo":     "qwen3.6-plus",
    "gpt-5":             "qwen3.6-plus",
    "o1":                "qwen3.6-plus",
    "o1-mini":           "qwen3.6-plus",
    "o3":                "qwen3.6-plus",
    "o3-mini":           "qwen3.6-plus",
    # Anthropic
    "claude-opus-4-6":           "qwen3.6-plus",
    "claude-sonnet-4-6":         "qwen3.6-plus",
    "claude-sonnet-4-5":         "qwen3.6-plus",
    "claude-3-opus":             "qwen3.6-plus",
    "claude-3-5-sonnet":         "qwen3.6-plus",
    "claude-3-5-sonnet-latest":  "qwen3.6-plus",
    "claude-3-sonnet":           "qwen3.6-plus",
    "claude-3-haiku":            "qwen3.6-plus",
    "claude-3-5-haiku":          "qwen3.6-plus",
    "claude-3-5-haiku-latest":   "qwen3.6-plus",
    "claude-haiku-4-5":          "qwen3.6-plus",
    # Gemini
    "gemini-2.5-pro":    "qwen3.6-plus",
    "gemini-2.5-flash":  "qwen3.6-plus",
    "gemini-1.5-pro":    "qwen3.6-plus",
    "gemini-1.5-flash":  "qwen3.6-plus",
    # Qwen aliases
    "qwen":              "qwen3.6-plus",
    "qwen-max":          "qwen3.6-plus",
    "qwen-plus":         "qwen3.6-plus",
    "qwen-turbo":        "qwen3.6-plus",
    # DeepSeek
    "deepseek-chat":     "qwen3.6-plus",
    "deepseek-reasoner": "qwen3.6-plus",
}

# 图片生成沿用网页当前真实可用的基础模型，不再写死 wanx 模型名
IMAGE_MODEL_DEFAULT = "qwen3.6-plus"

def resolve_model(name: str) -> str:
    return MODEL_MAP.get(name, name)
