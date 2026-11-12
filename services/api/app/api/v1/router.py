"""Versioned API root (/api/v1). Feature routers are added in later phases."""

from fastapi import APIRouter

from app.ai.router_api import router as ai_router
from app.audio.router import router as audio_router
from app.auth.router import router as auth_router
from app.files.router import documents_router, resumes_router
from app.jobs_import.router import router as job_import_router
from app.qa.router import router as qa_router
from app.realtime.gateway import router as ws_router
from app.realtime.router import router as ws_ticket_router
from app.screenshots.router import router as screenshots_router
from app.sessions.handoff import router as handoff_router
from app.sessions.router import router as sessions_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(resumes_router)
router.include_router(documents_router)
router.include_router(ai_router)
# Must precede sessions_router: /sessions/job-import would otherwise match /sessions/{session_id}.
router.include_router(job_import_router)
router.include_router(handoff_router)
router.include_router(sessions_router)
router.include_router(qa_router)
router.include_router(screenshots_router)
router.include_router(audio_router)
router.include_router(ws_ticket_router)
router.include_router(ws_router)
