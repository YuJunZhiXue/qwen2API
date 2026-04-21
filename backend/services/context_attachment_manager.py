from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from backend.core.upstream_file_cache import UpstreamFileCacheEntry
from backend.services.context_offload import SYSTEM_CONTEXT_FILE_PREFIX, SYSTEM_CONTEXT_PROMPT_NOTE


log = logging.getLogger("qwen2api.context_attachment_manager")


def _is_retryable_attachment_upload_error(exc: Exception) -> bool:
    lowered = str(exc or "").lower()
    markers = (
        "temporary failure in name resolution",
        "failed to resolve",
        "nameresolutionerror",
        "connecterror",
        "connectionerror",
        "max retries exceeded",
        "newconnectionerror",
        "timed out",
        "timeout",
    )
    return any(marker in lowered for marker in markers)


def derive_session_key(surface: str, auth_token: str, payload: dict[str, Any]) -> str:
    explicit = (payload.get("session_key") or payload.get("conversation_id") or payload.get("metadata", {}).get("conversation_id") if isinstance(payload.get("metadata"), dict) else None)
    if explicit:
        return str(explicit)
    first_user = next((m for m in payload.get("messages", []) if m.get("role") == "user"), {})
    content = first_user.get("content", "")
    if isinstance(content, list):
        first_text = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")
    else:
        first_text = str(content)
    basis = f"{surface}::{auth_token}::{payload.get('model', '')}::{first_text[:400]}"
    return hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()[:24]


async def prepare_context_attachments(*, app, payload: dict[str, Any], surface: str, auth_token: str, client_profile: str, existing_attachments=None) -> dict[str, Any]:
    context_offloader = app.state.context_offloader
    account_pool = app.state.account_pool
    file_store = app.state.file_store
    affinity = app.state.session_affinity
    cache = app.state.upstream_file_cache
    uploader = app.state.upstream_file_uploader

    session_key = derive_session_key(surface, auth_token, payload)
    tools = payload.get("tools", []) or []
    messages = payload.get("messages", []) or []
    manual_attachments = list(existing_attachments or [])
    plan = context_offloader.plan(messages, tools=tools, client_profile=client_profile)
    use_generated_context_files = bool(plan.generated_files) and not bool(tools)
    if not use_generated_context_files and not manual_attachments:
        return {
            "payload": payload,
            "session_key": session_key,
            "context_mode": "inline",
            "upstream_files": list(payload.get("upstream_files", []) or []),
            "bound_account": None,
            "bound_account_email": None,
            "generated_local_files": [],
            "attachment_fallback": False,
        }

    record = await affinity.get(session_key)
    preferred_email = record.account_email if record else None
    acc = await account_pool.acquire_wait_preferred(preferred_email, timeout=60)
    if not acc:
        raise RuntimeError("No available upstream account for context attachment mode")
    await affinity.bind_account(session_key, surface, acc.email, context_offloader.settings.CONTEXT_ATTACHMENT_TTL_SECONDS)

    upstream_files = list(payload.get("upstream_files", []) or [])
    local_file_records: list[dict[str, Any]] = []

    async def _switch_account_on_retry(current_acc, upload_exc: Exception):
        if not _is_retryable_attachment_upload_error(upload_exc):
            raise upload_exc

        exclude = {getattr(current_acc, "email", None)}
        account_pool.release(current_acc)
        next_acc = await account_pool.acquire_wait(timeout=10, exclude=exclude)
        if not next_acc:
            raise upload_exc
        await affinity.bind_account(session_key, surface, next_acc.email, context_offloader.settings.CONTEXT_ATTACHMENT_TTL_SECONDS)
        log.warning(
            "[ContextAttachment] retrying upload with alternate account session_key=%s surface=%s previous_account=%s new_account=%s error=%s",
            session_key,
            surface,
            getattr(current_acc, "email", None),
            getattr(next_acc, "email", None),
            upload_exc,
        )
        return next_acc

    try:
        for attachment in manual_attachments:
            if getattr(attachment, "remote_ref", None):
                upstream_files.append(attachment.remote_ref)
                continue
            if not attachment.local_path:
                continue
            local_meta = {
                "id": attachment.file_id,
                "path": attachment.local_path,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "sha256": attachment.sha256,
                "created_at": __import__("time").time(),
            }
            ext = Path(attachment.filename).suffix.lstrip(".").lower()
            cache_entry = await cache.get(session_key, acc.email, local_meta["sha256"], ext)
            if cache_entry is not None:
                remote = cache_entry.remote_file_meta
            else:
                try:
                    remote = await uploader.upload_local_file(acc, local_meta)
                except Exception as upload_exc:
                    acc = await _switch_account_on_retry(acc, upload_exc)
                    cache_entry = await cache.get(session_key, acc.email, local_meta["sha256"], ext)
                    if cache_entry is not None:
                        remote = cache_entry.remote_file_meta
                    else:
                        remote = await uploader.upload_local_file(acc, local_meta)
                await cache.set(UpstreamFileCacheEntry(
                    session_key=session_key,
                    account_email=acc.email,
                    sha256=local_meta["sha256"],
                    ext=ext,
                    filename=attachment.filename,
                    remote_file_meta=remote,
                    created_at=local_meta["created_at"],
                    expires_at=local_meta["created_at"] + context_offloader.settings.CONTEXT_ATTACHMENT_TTL_SECONDS,
                ))
            upstream_files.append(remote["remote_ref"])
            await affinity.add_uploaded_file(session_key, remote)
            await file_store.delete_path(attachment.local_path)

        if use_generated_context_files:
            for index, generated in enumerate(plan.generated_files, 1):
                fixed_base = f"{SYSTEM_CONTEXT_FILE_PREFIX}_{index}" if len(plan.generated_files) > 1 else SYSTEM_CONTEXT_FILE_PREFIX
                filename = f"{fixed_base}.{generated.ext}"
                local_meta = await file_store.save_text(filename, generated.text, generated.content_type, purpose="context")
                cache_entry = await cache.get(session_key, acc.email, local_meta["sha256"], generated.ext)
                if cache_entry is not None:
                    remote = cache_entry.remote_file_meta
                else:
                    try:
                        remote = await uploader.upload_local_file(acc, local_meta)
                    except Exception as upload_exc:
                        acc = await _switch_account_on_retry(acc, upload_exc)
                        cache_entry = await cache.get(session_key, acc.email, local_meta["sha256"], generated.ext)
                        if cache_entry is not None:
                            remote = cache_entry.remote_file_meta
                        else:
                            remote = await uploader.upload_local_file(acc, local_meta)
                    await cache.set(UpstreamFileCacheEntry(
                        session_key=session_key,
                        account_email=acc.email,
                        sha256=local_meta["sha256"],
                        ext=generated.ext,
                        filename=filename,
                        remote_file_meta=remote,
                        created_at=local_meta["created_at"],
                        expires_at=local_meta["created_at"] + context_offloader.settings.CONTEXT_ATTACHMENT_TTL_SECONDS,
                    ))
                upstream_files.append(remote["remote_ref"])
                await affinity.add_uploaded_file(session_key, remote)
                await file_store.delete_path(local_meta["path"])
                local_file_records.append(local_meta)
    except Exception as exc:
        log.exception(
            "[ContextAttachment] upload failed session_key=%s surface=%s account=%s manual_attachments=%s generated_files=%s error=%s",
            session_key,
            surface,
            getattr(acc, "email", None),
            [getattr(att, "filename", "") for att in manual_attachments],
            [getattr(item, "ext", "") for item in getattr(plan, "generated_files", [])],
            exc,
        )
        account_pool.release(acc)
        fallback_payload = dict(payload)
        summary_parts: list[str] = []
        if use_generated_context_files and plan.summary_text:
            summary_parts.append(plan.summary_text[:1200])
        if manual_attachments:
            names = ", ".join(att.filename for att in manual_attachments[:4])
            summary_parts.append(f"User attachments were provided but attachment upload failed. Attachment names: {names}")
        latest_text = summary_parts[0] if summary_parts else "Attachment upload failed. Continue with the available inline context only."
        fallback_payload["messages"] = [{
            "role": "user",
            "content": f"{latest_text}\n\n{SYSTEM_CONTEXT_PROMPT_NOTE}"
        }]
        return {
            "payload": fallback_payload,
            "session_key": session_key,
            "context_mode": "inline",
            "upstream_files": list(payload.get("upstream_files", []) or []),
            "bound_account": None,
            "bound_account_email": None,
            "generated_local_files": [],
            "attachment_fallback": True,
        }

    rewritten = dict(payload)
    rewritten["messages"] = plan.inline_messages if use_generated_context_files else messages
    return {
        "payload": rewritten,
        "session_key": session_key,
        "context_mode": plan.mode if use_generated_context_files else "inline",
        "upstream_files": upstream_files,
        "bound_account": acc,
        "bound_account_email": acc.email,
        "generated_local_files": local_file_records,
        "attachment_fallback": False,
    }
