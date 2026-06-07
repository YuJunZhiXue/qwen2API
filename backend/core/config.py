import os
import json
import re
from pathlib import Path
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

class Settings(BaseSettings):
    # 服务配置
    PORT: int = int(os.getenv("PORT", 8080))
    WORKERS: int = int(os.getenv("WORKERS", 3))
    ADMIN_KEY: str = os.getenv("ADMIN_KEY", "admin")

    # 并发配置（浏览器仅用于账号注册，不用于对话请求）
    BROWSER_POOL_SIZE: int = int(os.getenv("BROWSER_POOL_SIZE", 1))
    MAX_INFLIGHT_PER_ACCOUNT: int = Field(
        default=2,
        validation_alias=AliasChoices("MAX_INFLIGHT_PER_ACCOUNT", "MAX_INFLIGHT"),
    )
    BROWSER_STREAM_TIMEOUT_SECONDS: int = int(os.getenv("BROWSER_STREAM_TIMEOUT_SECONDS", 1800))

    # 容灾与限流
    MAX_RETRIES: int = 3
    RATE_LIMIT_COOLDOWN: int = 600
    ACCOUNT_MIN_INTERVAL_MS: int = int(os.getenv("ACCOUNT_MIN_INTERVAL_MS", 0))
    REQUEST_JITTER_MIN_MS: int = int(os.getenv("REQUEST_JITTER_MIN_MS", 0))
    REQUEST_JITTER_MAX_MS: int = int(os.getenv("REQUEST_JITTER_MAX_MS", 0))
    RATE_LIMIT_BASE_COOLDOWN: int = int(os.getenv("RATE_LIMIT_BASE_COOLDOWN", 600))
    RATE_LIMIT_MAX_COOLDOWN: int = int(os.getenv("RATE_LIMIT_MAX_COOLDOWN", 3600))
    ACCOUNT_READY_SET_THRESHOLD: int = int(os.getenv("ACCOUNT_READY_SET_THRESHOLD", 128))

    # 上游 chat 生命周期：默认每次请求结束后删除 Qwen 会话，删除失败有限重试。
    CHAT_DELETE_RETRY_ATTEMPTS: int = int(os.getenv("CHAT_DELETE_RETRY_ATTEMPTS", 3))
    CHAT_DELETE_RETRY_DELAY_SECONDS: float = float(os.getenv("CHAT_DELETE_RETRY_DELAY_SECONDS", 0.5))
    CHAT_ID_PREWARM_TARGET_PER_ACCOUNT: int = int(os.getenv("CHAT_ID_PREWARM_TARGET_PER_ACCOUNT", 5))
    CHAT_ID_PREWARM_TTL_SECONDS: int = int(os.getenv("CHAT_ID_PREWARM_TTL_SECONDS", 120))
    CHAT_ID_PREWARM_MAX_CONCURRENCY: int = int(os.getenv("CHAT_ID_PREWARM_MAX_CONCURRENCY", 16))
    TRACE_RESPONSE_FINGERPRINTS: bool = os.getenv("TRACE_RESPONSE_FINGERPRINTS", "").strip().lower() in {"1", "true", "yes", "on"}
    TRACE_RESPONSE_TAIL_CHARS: int = int(os.getenv("TRACE_RESPONSE_TAIL_CHARS", 160))

    # 日志
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # 数据文件路径
    ACCOUNTS_FILE: str = os.getenv("ACCOUNTS_FILE", str(DATA_DIR / "accounts.json"))
    USERS_FILE: str = os.getenv("USERS_FILE", str(DATA_DIR / "users.json"))
    CAPTURES_FILE: str = os.getenv("CAPTURES_FILE", str(DATA_DIR / "captures.json"))
    CONFIG_FILE: str = os.getenv("CONFIG_FILE", str(DATA_DIR / "config.json"))

    # ????? / ????
    CONTEXT_INLINE_MAX_CHARS: int = int(os.getenv("CONTEXT_INLINE_MAX_CHARS", 4000))
    CONTEXT_FORCE_FILE_MAX_CHARS: int = int(os.getenv("CONTEXT_FORCE_FILE_MAX_CHARS", 10000))
    CONTEXT_ATTACHMENT_TTL_SECONDS: int = int(os.getenv("CONTEXT_ATTACHMENT_TTL_SECONDS", 1800))
    CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS: int = int(os.getenv("CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS", 60))
    CONTEXT_GENERATED_DIR: str = os.getenv("CONTEXT_GENERATED_DIR", str(DATA_DIR / "context_files"))
    CONTEXT_CACHE_FILE: str = os.getenv("CONTEXT_CACHE_FILE", str(DATA_DIR / "context_cache.json"))
    UPLOADED_FILES_FILE: str = os.getenv("UPLOADED_FILES_FILE", str(DATA_DIR / "uploaded_files.json"))
    CONTEXT_AFFINITY_FILE: str = os.getenv("CONTEXT_AFFINITY_FILE", str(DATA_DIR / "session_affinity.json"))
    CONTEXT_ALLOWED_GENERATED_EXTS: str = os.getenv("CONTEXT_ALLOWED_GENERATED_EXTS", "txt,md,json,log")
    CONTEXT_ALLOWED_USER_EXTS: str = os.getenv("CONTEXT_ALLOWED_USER_EXTS", "txt,md,json,log,xml,yaml,yml,csv,html,css,py,js,ts,java,c,cpp,cs,php,go,rb,sh,zsh,ps1,bat,cmd,pdf,doc,docx,ppt,pptx,xls,xlsx,png,jpg,jpeg,webp,gif,tiff,bmp,svg")

    class Config:
        env_file = ".env"
        extra = "ignore"

API_KEYS_FILE = DATA_DIR / "api_keys.json"

KEEPALIVE_MIN_INTERVAL = 5
KEEPALIVE_MAX_INTERVAL = 86400
KEEPALIVE_DEFAULT_INTERVAL = 60

_NUMBERED_API_KEY_RE = re.compile(r"^QWEN_API_KEY_(\d+)$")
_NUMBERED_ACCOUNT_RE = re.compile(r"^QWEN_ACCOUNT_(\d+)$")


def _dedupe_nonempty(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _split_key_values(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\s,;]+", value or "") if item.strip()]


def _numbered_env_values(pattern: re.Pattern[str]) -> list[tuple[int, str, str]]:
    values: list[tuple[int, str, str]] = []
    for name, value in os.environ.items():
        match = pattern.match(name)
        if not match:
            continue
        values.append((int(match.group(1)), name, value))
    values.sort(key=lambda item: (item[0], item[1]))
    return values


def load_env_api_keys() -> list[str]:
    values: list[str] = []
    for name in ("QWEN_API_KEY", "QWEN_API_KEYS", "API_KEYS"):
        raw = os.getenv(name, "")
        if raw:
            values.extend(_split_key_values(raw))

    for _, _, raw in _numbered_env_values(_NUMBERED_API_KEY_RE):
        values.extend(_split_key_values(raw))

    return _dedupe_nonempty(values)


def load_api_keys() -> list[str]:
    if API_KEYS_FILE.exists():
        try:
            with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                keys = data.get("keys", [])
                if isinstance(keys, str):
                    return _dedupe_nonempty(_split_key_values(keys))
                if isinstance(keys, list):
                    return _dedupe_nonempty(keys)
        except Exception:
            pass
    return []


ENV_API_KEYS = load_env_api_keys()
MANAGED_API_KEYS = load_api_keys()
API_KEYS: set[str] = set()


def _sync_api_keys() -> None:
    API_KEYS.clear()
    API_KEYS.update(ENV_API_KEYS)
    API_KEYS.update(MANAGED_API_KEYS)


def save_api_keys(keys: Iterable[str]) -> None:
    global MANAGED_API_KEYS
    env_keys = set(ENV_API_KEYS)
    MANAGED_API_KEYS = _dedupe_nonempty(key for key in keys if str(key or "").strip() not in env_keys)
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({"keys": MANAGED_API_KEYS}, f, indent=2)
    _sync_api_keys()


def add_api_key(key: str) -> bool:
    key = str(key or "").strip()
    if not key or key in API_KEYS:
        return False
    MANAGED_API_KEYS.append(key)
    save_api_keys(MANAGED_API_KEYS)
    return True


def remove_api_key(key: str) -> str:
    key = str(key or "").strip()
    if key in ENV_API_KEYS:
        return "env"
    if key in MANAGED_API_KEYS:
        save_api_keys(item for item in MANAGED_API_KEYS if item != key)
        return "removed"
    return "missing"


def list_api_key_items() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    env_keys = set(ENV_API_KEYS)
    for key in ENV_API_KEYS:
        items.append({"key": key, "source": "env", "label": "环境变量注入 Key"})
    for key in MANAGED_API_KEYS:
        if key in env_keys:
            continue
        items.append({"key": key, "source": "managed", "label": "面板创建 Key"})
    return items


def load_env_accounts() -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for index, name, raw in _numbered_env_values(_NUMBERED_ACCOUNT_RE):
        parts = str(raw or "").split(";", 2)
        token = parts[0].strip() if parts else ""
        if not token:
            continue
        email = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else f"env_{index}@qwen"
        password = parts[2].strip() if len(parts) >= 3 else ""
        accounts.append({
            "email": email,
            "password": password,
            "token": token,
            "cookies": "",
            "username": "",
            "source": "env",
            "env_name": name,
        })
    return accounts


def normalize_keepalive_interval(value: Any) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return KEEPALIVE_DEFAULT_INTERVAL
    return min(KEEPALIVE_MAX_INTERVAL, max(KEEPALIVE_MIN_INTERVAL, interval))


def keepalive_env_locked_keys() -> set[str]:
    locked: set[str] = set()
    if os.getenv("KEEPALIVE_URL") is not None:
        locked.add("keepalive_url")
    if os.getenv("KEEPALIVE_INTERVAL") is not None:
        locked.add("keepalive_interval")
    return locked


async def get_keepalive_config(config_db) -> dict[str, Any]:
    data = await config_db.get()
    if not isinstance(data, dict):
        data = {}

    env_url = os.getenv("KEEPALIVE_URL")
    env_interval = os.getenv("KEEPALIVE_INTERVAL")

    url = env_url if env_url is not None else data.get("keepalive_url", "")
    interval = env_interval if env_interval is not None else data.get("keepalive_interval", KEEPALIVE_DEFAULT_INTERVAL)

    return {
        "keepalive_url": str(url or "").strip(),
        "keepalive_interval": normalize_keepalive_interval(interval),
        "env_locked": sorted(keepalive_env_locked_keys()),
    }


async def update_keepalive_config(config_db, values: dict[str, Any]) -> dict[str, Any]:
    data = await config_db.get()
    if not isinstance(data, dict):
        data = {}

    locked = keepalive_env_locked_keys()
    if "keepalive_url" in values and "keepalive_url" not in locked:
        data["keepalive_url"] = str(values.get("keepalive_url") or "").strip()
    if "keepalive_interval" in values and "keepalive_interval" not in locked:
        data["keepalive_interval"] = normalize_keepalive_interval(values.get("keepalive_interval"))

    await config_db.save(data)
    return await get_keepalive_config(config_db)


_sync_api_keys()

VERSION = "2.0.0"

settings = Settings()

# 全局映射
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
    "claude-opus-4-7":   "qwen3.6-plus",
}

def resolve_model(name: str) -> str:
    return MODEL_MAP.get(name, name)
