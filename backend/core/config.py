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

    # 引擎模式：browser 或 httpx
    ENGINE_MODE: str = os.getenv("ENGINE_MODE", "browser")
    # 浏览器引擎配置
    BROWSER_POOL_SIZE: int = int(os.getenv("BROWSER_POOL_SIZE", 4))
    MAX_INFLIGHT_PER_ACCOUNT: int = int(os.getenv("MAX_INFLIGHT", 4))
    STREAM_KEEPALIVE_INTERVAL: int = int(os.getenv("STREAM_KEEPALIVE_INTERVAL", 5))

    # 容灾与限流
    MAX_RETRIES: int = 3
    RATE_LIMIT_COOLDOWN: int = 600

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

# 图片模型不走 chat，不加入 MODEL_MAP
IMAGE_MODEL_DEFAULT = "wanx2.1-t2i-plus"

def resolve_model(name: str) -> str:
    return MODEL_MAP.get(name, name)
