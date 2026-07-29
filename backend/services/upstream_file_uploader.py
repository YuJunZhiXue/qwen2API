from __future__ import annotations

import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import oss2


def _file_class_from_content_type(content_type: str) -> str:
    lowered = (content_type or "").lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("audio/"):
        return "audio"
    if lowered.startswith("video/"):
        return "video"
    return "document"


def _normalize_sign_region(region: str) -> str:
    region = (region or "").strip()
    if region.startswith("oss-"):
        return region[len("oss-"):]
    return region


class UpstreamFileUploader:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    async def upload_local_file(self, acc, local_file_meta: dict[str, Any]) -> dict[str, Any]:
        filename = local_file_meta["filename"]
        file_path = local_file_meta["path"]
        content_type = local_file_meta.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        raw = Path(file_path).read_bytes()

        # 与 chat.qwen.ai 前端一致：
        # getFileType() → image/video/audio/file；filesize 传字符串
        filetype = _file_class_from_content_type(content_type)
        sts_resp = await self.client._request_json(
            "POST",
            "/api/v2/files/getstsToken",
            acc.token,
            {
                "filename": filename,
                "filesize": str(len(raw)),
                "filetype": filetype,
            },
            timeout=20.0,
        )
        if sts_resp.get("status") != 200:
            raise RuntimeError(f"getstsToken failed: {sts_resp.get('status')} {sts_resp.get('body', '')[:200]}")
        sts_data = json.loads(sts_resp.get("body", "{}"))
        sts = (sts_data.get("data") or {}) if isinstance(sts_data, dict) else {}
        file_id = sts.get("file_id")
        file_path_remote = sts.get("file_path", "")
        bucketname = sts.get("bucketname", "")
        endpoint = sts.get("endpoint", "")
        region = _normalize_sign_region(sts.get("region", ""))
        access_key_id = sts.get("access_key_id", "")
        access_key_secret = sts.get("access_key_secret", "")
        security_token = sts.get("security_token", "")
        if not file_id or not file_path_remote or not bucketname or not endpoint:
            raise RuntimeError(f"getstsToken missing file data: {sts_data}")

        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token, auth_version='v4')
        bucket = oss2.Bucket(
            auth,
            f"https://{endpoint}",
            bucketname,
            region=region,
        )
        put_result = bucket.put_object(
            file_path_remote,
            raw,
            headers={"Content-Type": content_type},
        )
        if getattr(put_result, 'status', None) not in (200, 201):
            raise RuntimeError(f"OSS put_object failed: status={getattr(put_result, 'status', None)}")

        # 图片/音视频作为 vision 参考时，前端通常只拿 file_id + CDN url，
        # 不强制走 document parse。parse 对部分图片会误报「上传的文档内容为空」(1040)。
        parse_status = "skipped"
        if filetype not in {"image", "video", "audio"}:
            parse_resp = await self.client._request_json(
                "POST",
                "/api/v2/files/parse",
                acc.token,
                {"file_id": file_id},
                timeout=20.0,
            )
            if parse_resp.get("status") != 200:
                raise RuntimeError(f"files/parse failed: {parse_resp.get('status')} {parse_resp.get('body', '')[:200]}")

            deadline = time.time() + self.settings.CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS
            parse_status = "pending"
            while time.time() < deadline:
                status_resp = await self.client._request_json(
                    "POST",
                    "/api/v2/files/parse/status",
                    acc.token,
                    {"file_id_list": [file_id]},
                    timeout=20.0,
                )
                if status_resp.get("status") != 200:
                    raise RuntimeError(
                        f"files/parse/status failed: {status_resp.get('status')} {status_resp.get('body', '')[:200]}"
                    )
                status_data = json.loads(status_resp.get("body", "{}"))
                rows = status_data.get("data") or []
                row = rows[0] if isinstance(rows, list) and rows else {}
                parse_status = row.get("status", "pending")
                if parse_status == "success":
                    break
                if parse_status in ("failed", "error"):
                    raise RuntimeError(f"file parse failed: {row}")
                await __import__('asyncio').sleep(1.0)

            if parse_status != "success":
                raise RuntimeError(f"file parse timeout: {file_id}")

        user_id = file_path_remote.split('/', 1)[0] if '/' in file_path_remote else ""
        now_ms = int(time.time() * 1000)
        put_url = f"https://{bucketname}.{endpoint}/{file_path_remote.lstrip('/')}"
        remote_ref = {
            "type": "file",
            "file": {
                "created_at": now_ms,
                "data": {},
                "filename": filename,
                "hash": None,
                "id": file_id,
                "user_id": user_id,
                "meta": {
                    "name": filename,
                    "size": len(raw),
                    "content_type": content_type,
                    "parse_meta": {"parse_status": parse_status},
                },
                "update_at": now_ms,
            },
            "id": file_id,
            "url": put_url,
            "name": filename,
            "collection_name": "",
            "progress": 0,
            "status": "uploaded",
            "greenNet": "success",
            "size": len(raw),
            "error": "",
            "itemId": str(uuid.uuid4()),
            "file_type": content_type,
            "showType": "file",
            "file_class": _file_class_from_content_type(content_type),
            "uploadTaskId": str(uuid.uuid4()),
        }
        return {
            "remote_file_id": file_id,
            "remote_object_key": file_path_remote,
            "filename": filename,
            "content_type": content_type,
            "parse_status": parse_status,
            "remote_ref": remote_ref,
        }

    async def delete_remote_file(self, acc, remote_meta: dict[str, Any]) -> bool:
        # Qwen web upload delete API has not been fully confirmed yet.
        return False
