"""FastAPI application factory. Read-only by default; play mode enables session writes and generation."""

from __future__ import annotations

import os
import asyncio
import json
from queue import Queue, Empty, Full
from threading import Event, Thread
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from .api_models import (
    ReadingReceiptRequest, PrepareChoicesRequest, BranchPage, BranchView, ContextRequest, ContextView, ErrorResponse, HealthResponse, PackageCatalog,
    PackageList, ParseRequest, PlayContinueRequest, PlayContinueResponse, PlayCreateRequest,
    PlayCreateResponse, SessionList, SessionView,
    SourceChapterView, StateView, RenameRequest, EndRouteRequest, RouteClosureIntentRequest, EndingProposalRequest, EndingProposalView, CharacterProfileRequest, RouteMonitorView, RouteOutlineView,
    RouteClosurePreparation, JourneyView, EndRouteResponse, EndingCommitResponse, RouteErrorResponse,
)
from .api_read import ReadError, ReadService
from .content import StoryPackageError
from .module_context import ModuleContextError


def create_app(package_root: Path | None = None, database_path: Path | None = None,
               cors_origins: list[str] | None = None, play: bool | None = None) -> FastAPI:
    root = Path(__file__).resolve().parents[1]
    service = ReadService(
        package_root or root / "content" / "packages",
        database_path or Path(os.environ.get("STORY_DATABASE_PATH", str(root / "data" / "open-story-engine.sqlite"))),
    )
    if play is None:
        play = os.environ.get("STORY_API_PLAY", "").strip().lower() in ("1", "true", "yes")
    illustrations = None

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if illustrations is not None:
                illustrations.close()
            if play_service is not None:
                play_service.drafts.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Open Story Engine · 读取 API", version="0.1.0",
        description="首批提供故事包与会话浏览、局部上下文预览。未开放生成及状态写入。",
        responses={status: {"model": ErrorResponse} for status in (404, 409, 422, 503)},
    )
    route_errors = {status: {"model": RouteErrorResponse} for status in (404, 409, 422, 503)}
    if cors_origins is None:
        cors_origins = [origin.strip() for origin in os.environ.get(
            "STORY_API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware, allow_origins=cors_origins, allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"] if play else ["GET", "POST"], allow_headers=["Content-Type"],
    )

    @app.exception_handler(ReadError)
    async def read_error(_request: Request, error: ReadError):
        return JSONResponse(status_code=error.status, content={"error": {"code": error.code, "message": error.message}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "请求字段或参数格式无效，请参照 /docs 接口契约"}})

    @app.exception_handler(ResponseValidationError)
    async def response_error(_request: Request, _error: ResponseValidationError):
        return JSONResponse(status_code=503, content={"error": {"code": "invalid_response", "message": "服务数据不符合读取契约，请检查故事包或会话数据"}})

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException):
        return JSONResponse(status_code=error.status_code, headers=error.headers,
                            content={"error": {"code": "http_error", "message": str(error.detail)}})

    @app.exception_handler(StoryPackageError)
    @app.exception_handler(ModuleContextError)
    async def package_error(_request: Request, _error: ValueError):
        return JSONResponse(status_code=409, content={"error": {"code": "invalid_package", "message": "故事包或上下文模块校验失败"}})

    play_service = None
    if play:
        from .api_play import PlayService
        play_service = PlayService(service, root)

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health():
        if play_service is None:
            return HealthResponse()
        return HealthResponse(
            phase="play",
            generation_available=play_service.generation_available,
            state_updates_available=play_service.generation_available,
        )

    @app.get("/api/v1/packages", response_model=PackageList)
    def packages():
        return service.packages()

    @app.get("/api/v1/packages/{package_id}/{version}", response_model=PackageCatalog, response_model_exclude_unset=True)
    def package_catalog(package_id: str, version: str):
        path, package = service.load_package(package_id, version)
        return service.catalog(package, path)

    @app.post("/api/v1/package/parse", response_model=PackageCatalog, response_model_exclude_unset=True)
    def parse(request: ParseRequest):
        try:
            return PackageCatalog.model_validate(service.parse(request.package))
        except ValidationError as error:
            raise ReadError(422, "invalid_package", "故事包元数据不符合接口契约") from error

    @app.post("/api/v1/context/build", response_model=ContextView, response_model_exclude_unset=True)
    def context(request: ContextRequest):
        return service.context(request)

    @app.get("/api/v1/sessions", response_model=SessionList)
    def sessions():
        return service.sessions()

    @app.get("/api/v1/sessions/{session_id}", response_model=SessionView, response_model_exclude_unset=True)
    def session(session_id: str, include_branches: bool = True):
        return service.session_view(session_id, include_branches)

    @app.get("/api/v1/sessions/{session_id}/branches", response_model=BranchPage)
    def branches(session_id: str, after_sequence: int = Query(default=-1, ge=-1, le=9223372036854775807),
                 limit: int = Query(default=50, ge=1, le=200), include_actions: bool = False):
        return service.branch_page(session_id, after_sequence, limit, include_actions)

    @app.get("/api/v1/sessions/{session_id}/branches/{branch_id}", response_model=BranchView, response_model_exclude_unset=True)
    def branch(session_id: str, branch_id: str):
        return service.branch_view(session_id, branch_id)

    @app.get("/api/v1/sessions/{session_id}/branches/{branch_id}/source-chapter", response_model=SourceChapterView)
    def source_chapter(session_id: str, branch_id: str):
        return service.branch_source_chapter(session_id, branch_id)

    @app.get("/api/v1/sessions/{session_id}/state", response_model=StateView, response_model_exclude_unset=True)
    def state(session_id: str, branch_id: str | None = None):
        return service.state(session_id, branch_id)

    @app.get('/api/v1/sessions/{session_id}/journey', response_model=JourneyView, responses=route_errors)
    def player_journey(session_id: str, branch_id: str):
        return service.journey(session_id, branch_id)

    @app.get('/api/v1/sessions/{session_id}/route-monitor', response_model=RouteMonitorView)
    def route_monitor(session_id: str, branch_id: str):
        return service.route_monitor(session_id, branch_id)

    @app.get('/api/v1/sessions/{session_id}/route-outline', response_model=RouteOutlineView)
    def route_outline(session_id: str, branch_id: str):
        return service.route_outline(session_id, branch_id)

    @app.get('/api/v1/sessions/{session_id}/route-closure', response_model=RouteClosurePreparation, responses=route_errors)
    def route_closure(session_id: str, branch_id: str):
        return service.route_closure(session_id, branch_id)

    @app.get('/api/v1/sessions/{session_id}/ending-proposals', response_model=list[EndingProposalView], responses=route_errors)
    def ending_proposals(session_id: str, branch_id: str):
        return service.ending_proposals(session_id, branch_id)

    @app.get('/api/v1/sessions/{session_id}/ending-proposals/{proposal_id}', response_model=EndingProposalView, responses=route_errors)
    def ending_proposal(session_id: str, proposal_id: str, branch_id: str):
        return service.ending_proposal(session_id, branch_id, proposal_id)

    if play_service is not None:
        from .api_illustrations import IllustrationService
        illustrations = IllustrationService(service, service.database_path.parent / 'illustrations')

        @app.get('/api/v1/sessions/{session_id}/branches/{branch_id}/illustrations')
        def scene_illustrations(session_id: str, branch_id: str):
            return illustrations.view(session_id, branch_id)

        @app.post('/api/v1/sessions/{session_id}/branches/{branch_id}/illustrations')
        def generate_illustrations(session_id: str, branch_id: str, retry: bool = False,
                                  subscriber: str = Query(..., min_length=1, max_length=100), draw: bool = False):
            return illustrations.ensure(session_id, branch_id, retry, subscriber, draw)

        @app.post('/api/v1/sessions/{session_id}/branches/{branch_id}/illustrations/release')
        def release_illustrations(session_id: str, branch_id: str, subscriber: str = Query(..., min_length=1, max_length=100)):
            return illustrations.release(session_id, branch_id, subscriber)

        @app.post('/api/v1/sessions/{session_id}/branches/{branch_id}/illustrations/shown')
        def shown_illustrations(session_id: str, branch_id: str, subscriber: str = Query(..., min_length=1, max_length=100),
                               display_ms: int = Query(..., ge=0, le=3600000)):
            return illustrations.shown(session_id, branch_id, subscriber, display_ms)

        @app.get('/api/v1/scene-assets/{package_id}/{version}/{asset_id}')
        def published_scene_image(package_id: str, version: str, asset_id: str):
            try:
                path = illustrations.library.asset(package_id, version, asset_id)
            except (ValueError, OSError):
                raise ReadError(404, 'scene_asset_not_found', '插图不存在')
            return FileResponse(path, media_type='image/png', headers={'Cache-Control': 'public, max-age=86400'})

        @app.get('/api/v1/sessions/{session_id}/branches/{branch_id}/illustrations/{index}/image')
        def scene_image(session_id: str, branch_id: str, index: int):
            path, mime = illustrations.asset(session_id, branch_id, index)
            return FileResponse(path, media_type=mime, headers={'Cache-Control': 'private, max-age=86400'})

        @app.post('/api/v1/sessions/{session_id}/character-profile')
        def character_profile(session_id: str, request: CharacterProfileRequest):
            return _play().character_profile(session_id, request.branch_id, request.character_id)

        @app.delete("/api/v1/sessions/{session_id}", status_code=204)
        def play_delete(session_id: str):
            _play().delete_session(session_id)
            return Response(status_code=204)

        @app.post("/api/v1/sessions", response_model=PlayCreateResponse, response_model_exclude_unset=True, status_code=201)
        def play_create(request: PlayCreateRequest):
            return _play().create_session(
                request.package.package_id, request.package.version,
                request.entry_point_id, request.source_character_id,
                request.new_character, request.identity_opening, request.request_id,
            )

        @app.post('/api/v1/sessions/{session_id}/rename')
        def rename_session(session_id: str, request: RenameRequest):
            return _play().rename_session(session_id, request.title)

        @app.post('/api/v1/sessions/{session_id}/end', response_model=EndRouteResponse, responses=route_errors)
        def end_route(session_id: str, request: EndRouteRequest):
            return _play().end_route(session_id, request.branch_id)

        @app.post('/api/v1/sessions/{session_id}/route-closure', response_model=RouteClosurePreparation, responses=route_errors)
        def plan_route_closure(session_id: str, request: RouteClosureIntentRequest):
            return _play().plan_route_closure(session_id, request.branch_id, request.intended_type)

        @app.post('/api/v1/sessions/{session_id}/ending-proposals', response_model=EndingProposalView, responses=route_errors)
        def propose_ending(session_id: str, request: EndingProposalRequest):
            return _play().propose_ending(session_id, request.branch_id, request.request_id,
                                          request.outcome_summary, request.ending_quote)

        @app.post('/api/v1/sessions/{session_id}/ending-proposals/{proposal_id}/commit', response_model=EndingCommitResponse, responses=route_errors)
        def commit_ending(session_id: str, proposal_id: str, request: EndRouteRequest):
            return _play().commit_ending(session_id, request.branch_id, proposal_id)

        @app.post('/api/v1/sessions/{session_id}/ending-proposals/{proposal_id}/cancel', response_model=EndingProposalView, responses=route_errors)
        def cancel_ending(session_id: str, proposal_id: str, request: EndRouteRequest):
            return _play().cancel_ending(session_id, request.branch_id, proposal_id)

        @app.post('/api/v1/sessions/stream')
        async def opening_stream(request: PlayCreateRequest):
            return stream_response(lambda stream, reset: _play().create_session(
                request.package.package_id, request.package.version, request.entry_point_id,
                request.source_character_id, request.new_character, request.identity_opening,
                request.request_id, stream, reset), PlayCreateResponse)

        @app.post('/api/v1/sessions/{session_id}/choices/prepare')
        def prepare_choices(session_id: str, request: PrepareChoicesRequest):
            return _play().prepare_choices(session_id, request.parent_branch_id, request.subscriber_id, request.history_id)

        @app.post('/api/v1/sessions/{session_id}/reading-receipts')
        def reading_receipt(session_id: str, request: ReadingReceiptRequest):
            return _play().record_display(session_id, request.branch_id)

        @app.get('/api/v1/sessions/{session_id}/turn-usage')
        def turn_usage(session_id: str):
            return _play().turn_usage(session_id)

        @app.post('/api/v1/sessions/{session_id}/choices/release')
        def release_choices(session_id: str, request: PrepareChoicesRequest):
            _play().drafts.release(session_id, request.parent_branch_id, request.subscriber_id, retire=True)
            return {'released': True}

        @app.post("/api/v1/sessions/{session_id}/branches/stream")
        async def play_stream(session_id: str, request: PlayContinueRequest):
            return stream_response(lambda stream, reset: _play().continue_turn(
                session_id, request.parent_branch_id, request.direction_id, request.text,
                request.request_id, stream, reset, request.choice_id, request.subscriber_id, request.draft_id, request.history_id), PlayContinueResponse)

        def stream_response(operation, response_model):
            # Keep SQLite and the synchronous planner in one worker thread.
            # Disconnecting stops delivery; the in-flight turn can finish saving
            # and a retry with the same request ID recovers its final result.
            events: Queue = Queue(maxsize=128)
            disconnected = Event()

            def emit(event: str, data: dict):
                while not disconnected.is_set():
                    try:
                        events.put((event, data), timeout=0.1)
                        return
                    except Full:
                        continue

            def generate():
                try:
                    result = operation(lambda text: emit("delta", {"text": text}),
                                       lambda _reason: emit("reset", {}))
                    validated = response_model.model_validate(result)
                    emit("done", validated.model_dump(mode="json", exclude_unset=True))
                except ReadError as error:
                    emit("error", {"code": error.code, "status": error.status,
                                   "message": "这次续写未能完成，请重试或调整行动。"})
                except Exception:
                    emit("error", {"code": "generation_failed", "status": 503,
                                   "message": "这次续写未能完成，请稍后重试。"})

            async def event_stream():
                Thread(target=generate, daemon=True).start()
                try:
                    yield ": connected\n\n"
                    while True:
                        try:
                            event, data = await asyncio.to_thread(events.get, True, 1)
                        except Empty:
                            yield ": heartbeat\n\n"
                            continue
                        yield "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
                        await asyncio.sleep(0)
                        if event in ("done", "error"):
                            break
                finally:
                    disconnected.set()

            return StreamingResponse(event_stream(), media_type="text/event-stream", headers={
                "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
            })

        @app.post("/api/v1/sessions/{session_id}/branches", response_model=PlayContinueResponse, response_model_exclude_unset=True)
        def play_continue(session_id: str, request: PlayContinueRequest):
            return _play().continue_turn(
                session_id, request.parent_branch_id,
                direction_id=request.direction_id, text=request.text,
                request_id=request.request_id, choice_id=request.choice_id, subscriber_id=request.subscriber_id, draft_id=request.draft_id,
                history_id=request.history_id,
            )

    def _play():
        assert play_service is not None
        return play_service

    # Return explicit JSON errors for API capabilities that are not available yet.
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def unknown_api(path: str):
        raise ReadError(404, "endpoint_unavailable", "该接口尚未开放")

    return app
