from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from backend.adapter.standard_request import StandardRequest, detect_openai_client_profile
from backend.core.config import API_KEYS, settings
from backend.core.request_logging import new_request_id, request_context, update_request_context
from backend.runtime.execution import build_tool_directive, build_usage_delta_factory, request_max_attempts
from backend.services.attachment_preprocessor import preprocess_attachments
from backend.services.auth_quota import resolve_auth_context
from backend.services.completion_bridge import run_retryable_completion_bridge
from backend.services.context_attachment_manager import derive_session_key, prepare_context_attachments
from backend.services.qwen_client import QwenClient
from backend.services.responses_compat import (
    PreparedResponsesRequest,
    ResponsesStreamTranslator,
    prepare_responses_request,
    sse_chunk_to_payload,
    sse_event,
)
from backend.services.response_formatters import build_openai_response_payload
from backend.services.standard_request_builder import build_chat_standard_request
from backend.services.task_session import (
    build_openai_assistant_history_message,
    clear_invalidated_session_chat,
    log_session_plan_reuse_cancelled,
    persist_session_turn,
    plan_persistent_session_turn,
)

router = APIRouter()
log = logging.getLogger("qwen2api.responses")
ResponseDeltaHandler = Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]]


@dataclass(slots=True)
class ResponseRequestContext:
    raw_req_data: dict[str, Any]
    transformed_req: dict[str, Any]
    standard_request: StandardRequest
    session_key: str
    previous_response_id: str | None
    history_messages: list[dict[str, Any]]
    model_name: str
    prompt: str


@dataclass(slots=True)
class ResponseExecutionArtifacts:
    execution: Any
    response_payload: dict[str, Any]
    assistant_message: dict[str, Any]


def _detect_openai_client_profile(request: Request | WebSocket, req_data: dict) -> str:
    return detect_openai_client_profile(request.headers, req_data)


def _build_standard_request(req_data: dict, *, client_profile: str) -> StandardRequest:
    return build_chat_standard_request(
        req_data,
        default_model="gpt-4.1",
        surface="responses",
        client_profile=client_profile,
    )


async def _prepare_response_request_context(
    *,
    app,
    auth_token: str,
    raw_req_data: dict[str, Any],
    request_like: Request | WebSocket,
) -> ResponseRequestContext:
    prepared: PreparedResponsesRequest = await prepare_responses_request(
        response_store=app.state.response_store,
        req_data=raw_req_data,
    )
    transformed_req = prepared.transformed_payload
    client_profile = _detect_openai_client_profile(request_like, raw_req_data)
    session_key = derive_session_key("responses", auth_token, transformed_req)

    file_store = getattr(app.state, "file_store", None)
    preprocessed = None
    if file_store is not None:
        preprocessed = await preprocess_attachments(transformed_req, file_store, owner_token=auth_token)
        transformed_req = preprocessed.payload

    context_prepared = await prepare_context_attachments(
        app=app,
        payload=transformed_req,
        surface="responses",
        auth_token=auth_token,
        client_profile=client_profile,
        existing_attachments=(preprocessed.attachments if preprocessed is not None else None),
    )
    transformed_req = context_prepared["payload"]

    standard_request = _build_standard_request(transformed_req, client_profile=client_profile)
    if preprocessed is not None:
        standard_request.attachments = preprocessed.attachments
        standard_request.uploaded_file_ids = preprocessed.uploaded_file_ids
    standard_request.upstream_files = context_prepared["upstream_files"]
    standard_request.session_key = context_prepared["session_key"]
    standard_request.context_mode = context_prepared["context_mode"]
    standard_request.bound_account_email = context_prepared["bound_account_email"]
    standard_request.bound_account = context_prepared["bound_account"]

    session_plan = await plan_persistent_session_turn(
        app=app,
        request=standard_request,
        payload=transformed_req,
        surface="responses",
    )
    if session_plan.enabled:
        standard_request.persistent_session = True
        standard_request.full_prompt = session_plan.full_prompt
        standard_request.prompt = session_plan.prompt
        standard_request.session_message_hashes = session_plan.current_hashes
        standard_request.upstream_chat_id = session_plan.existing_chat_id if session_plan.reuse_chat else None
        if standard_request.bound_account is None and session_plan.account_email:
            standard_request.bound_account = await app.state.account_pool.acquire_wait_preferred(
                session_plan.account_email,
                timeout=60,
            )
            if standard_request.bound_account is not None:
                standard_request.bound_account_email = standard_request.bound_account.email
        elif standard_request.bound_account is not None and not standard_request.bound_account_email:
            standard_request.bound_account_email = standard_request.bound_account.email
        if standard_request.upstream_chat_id and standard_request.bound_account is None:
            log_session_plan_reuse_cancelled(
                request=standard_request,
                planned_chat_id=session_plan.existing_chat_id,
                reason="missing_bound_account",
            )
            standard_request.upstream_chat_id = None
            standard_request.prompt = standard_request.full_prompt or standard_request.prompt

    return ResponseRequestContext(
        raw_req_data=raw_req_data,
        transformed_req=transformed_req,
        standard_request=standard_request,
        session_key=session_key,
        previous_response_id=prepared.previous_response_id,
        history_messages=prepared.combined_messages,
        model_name=standard_request.response_model,
        prompt=standard_request.prompt,
    )


async def _execute_response_turn(
    *,
    app,
    client: QwenClient,
    users_db,
    auth_token: str,
    context: ResponseRequestContext,
    response_id: str,
    created: int,
    request_surface: str,
    capture_events: bool = True,
    on_delta: ResponseDeltaHandler | None = None,
) -> ResponseExecutionArtifacts:
    with request_context(
        req_id=new_request_id(),
        surface=request_surface,
        requested_model=context.model_name,
        resolved_model=context.standard_request.resolved_model,
    ):
        async with app.state.session_locks.hold(context.session_key):
            update_request_context(stream_attempt=1)
            result = await run_retryable_completion_bridge(
                client=client,
                standard_request=context.standard_request,
                prompt=context.prompt,
                users_db=users_db,
                token=auth_token,
                history_messages=context.history_messages,
                max_attempts=request_max_attempts(context.standard_request),
                usage_delta_factory=build_usage_delta_factory(context.prompt),
                allow_after_visible_output=True,
                capture_events=capture_events,
                on_delta=on_delta,
            )
            execution = result.execution
            response_payload = build_openai_response_payload(
                response_id=response_id,
                created=created,
                model_name=context.model_name,
                prompt=result.prompt,
                execution=execution,
                standard_request=context.standard_request,
                previous_response_id=context.previous_response_id,
                store=bool(context.raw_req_data.get("store", True)),
            )
            directive = build_tool_directive(context.standard_request, execution.state)
            assistant_message = build_openai_assistant_history_message(
                execution=execution,
                request=context.standard_request,
                directive=directive,
            )
            updated_history = list(context.transformed_req.get("messages", [])) + [assistant_message]
            await app.state.response_store.save(response_id, response_payload, updated_history)
            await persist_session_turn(
                app=app,
                request=context.standard_request,
                surface="responses",
                execution=execution,
                assistant_message=assistant_message,
            )
            return ResponseExecutionArtifacts(
                execution=execution,
                response_payload=response_payload,
                assistant_message=assistant_message,
            )


async def _resolve_websocket_auth_token(websocket: WebSocket, users_db) -> tuple[str, dict[str, Any] | None]:
    auth_header = websocket.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if not token:
        token = websocket.headers.get("x-api-key", "").strip()
    if not token:
        token = websocket.query_params.get("key", "").strip() or websocket.query_params.get("api_key", "").strip()
    if not token:
        await websocket.close(code=4401, reason="Invalid API Key")
        raise WebSocketDisconnect

    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if API_KEYS and token != settings.ADMIN_KEY and token not in API_KEYS:
        saved_snapshots = getattr(users_db, "saved_snapshots", None)
        if user is None and not isinstance(saved_snapshots, list):
            await websocket.close(code=4401, reason="Invalid API Key")
            raise WebSocketDisconnect
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        await websocket.close(code=4402, reason="Quota Exceeded")
        raise WebSocketDisconnect
    return token, user


@router.get("/responses/{response_id}")
@router.get("/v1/responses/{response_id}")
async def get_response(response_id: str, request: Request):
    users_db = request.app.state.users_db
    await resolve_auth_context(request, users_db)
    stored = await request.app.state.response_store.get(response_id)
    if stored is None:
        raise HTTPException(status_code=404, detail={"error": {"message": f"Response '{response_id}' not found", "type": "invalid_request_error"}})
    return JSONResponse(stored.payload)


@router.post("/responses")
@router.post("/v1/responses")
async def create_response(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    try:
        raw_req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})

    context = await _prepare_response_request_context(
        app=app,
        auth_token=token,
        raw_req_data=raw_req_data,
        request_like=request,
    )
    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if context.standard_request.stream:
        async def generate():
            translator = ResponsesStreamTranslator(
                response_id=response_id,
                created=created,
                model_name=context.model_name,
            )
            translator.start()
            for chunk in translator.pending_chunks:
                yield chunk
            translator.pending_chunks.clear()

            try:
                async def on_delta(evt: dict[str, Any], text_chunk: str | None, tool_calls: list[dict[str, Any]] | None) -> None:
                    del evt, tool_calls
                    if text_chunk:
                        translator.on_text_delta(text_chunk)

                artifacts = await _execute_response_turn(
                    app=app,
                    client=client,
                    users_db=users_db,
                    auth_token=token,
                    context=context,
                    response_id=response_id,
                    created=created,
                    request_surface="responses",
                    capture_events=False,
                    on_delta=on_delta,
                )
                for chunk in translator.finalize(
                    response_payload=artifacts.response_payload,
                    standard_request=context.standard_request,
                    execution=artifacts.execution,
                ):
                    yield chunk
                return
            except HTTPException as he:
                await clear_invalidated_session_chat(app=app, request=context.standard_request)
                yield sse_event({"type": "response.failed", "sequence_number": translator._next_sequence(), "response": {"id": response_id, "status": "failed"}, "error": he.detail})
                return
            except Exception as e:
                await clear_invalidated_session_chat(app=app, request=context.standard_request)
                yield sse_event({"type": "response.failed", "sequence_number": translator._next_sequence(), "response": {"id": response_id, "status": "failed"}, "error": {"message": str(e)}})
                return

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        artifacts = await _execute_response_turn(
            app=app,
            client=client,
            users_db=users_db,
            auth_token=token,
            context=context,
            response_id=response_id,
            created=created,
            request_surface="responses",
        )
        return JSONResponse(artifacts.response_payload)
    except Exception as e:
        await clear_invalidated_session_chat(app=app, request=context.standard_request)
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/responses")
@router.websocket("/v1/responses")
async def create_response_websocket(websocket: WebSocket):
    await websocket.accept()
    app = websocket.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    try:
        token, user = await _resolve_websocket_auth_token(websocket, users_db)
    except WebSocketDisconnect:
        return

    try:
        first_message = await websocket.receive()
    except WebSocketDisconnect:
        return

    raw_payload = first_message.get("text")
    if raw_payload is None and first_message.get("bytes") is not None:
        raw_payload = first_message["bytes"].decode("utf-8", errors="replace")
    if not raw_payload:
        await websocket.close(code=4400, reason="Empty request payload")
        return

    log.info("[WS] /v1/responses first frame preview=%r", raw_payload[:500])

    try:
        raw_req_data = json.loads(raw_payload)
    except Exception:
        await websocket.close(code=4400, reason="Invalid JSON body")
        return

    try:
        context = await _prepare_response_request_context(
            app=app,
            auth_token=token,
            raw_req_data=raw_req_data,
            request_like=websocket,
        )
        response_id = f"resp_{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        translator = ResponsesStreamTranslator(
            response_id=response_id,
            created=created,
            model_name=context.model_name,
        )
        translator.start()
        for chunk in translator.pending_chunks:
            await websocket.send_json(sse_chunk_to_payload(chunk))
        translator.pending_chunks.clear()

        async def on_delta(evt: dict[str, Any], text_chunk: str | None, tool_calls: list[dict[str, Any]] | None) -> None:
            del evt, tool_calls
            if text_chunk:
                translator.on_text_delta(text_chunk)
                while translator.pending_chunks:
                    await websocket.send_json(sse_chunk_to_payload(translator.pending_chunks.pop(0)))

        artifacts = await _execute_response_turn(
            app=app,
            client=client,
            users_db=users_db,
            auth_token=token,
            context=context,
            response_id=response_id,
            created=created,
            request_surface="responses_ws",
            capture_events=False,
            on_delta=on_delta,
        )
        for chunk in translator.finalize(
            response_payload=artifacts.response_payload,
            standard_request=context.standard_request,
            execution=artifacts.execution,
        ):
            await websocket.send_json(sse_chunk_to_payload(chunk))
    except WebSocketDisconnect:
        return
    except Exception as e:
        log.exception("[WS] /v1/responses failed: %s", e)
        try:
            await websocket.send_json({"type": "response.failed", "error": {"message": str(e)}})
            await websocket.close(code=1011, reason="Internal Server Error")
        except Exception:
            return
