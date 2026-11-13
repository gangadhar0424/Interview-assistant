"""GET /api/v1/ai/options: allowed providers/models and preference values (no credentials)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.ai.registry import ProviderRegistry
from app.ai.stt_router import SttRouter
from app.auth.deps import CurrentUser
from app.preferences.enums import AnswerFormat, AnswerLength, AnswerTone, Language, QuestionType

router = APIRouter(prefix="/ai", tags=["ai"])


class ModelOption(BaseModel):
    id: str
    label: str
    capabilities: list[str]
    languages: list[str]


class ProviderOption(BaseModel):
    id: str
    models: list[ModelOption]


class VoiceModelOption(BaseModel):
    id: str
    label: str


class VoiceProviderOption(BaseModel):
    id: str
    models: list[VoiceModelOption]


class AiOptions(BaseModel):
    providers: list[ProviderOption]  # reasoning providers (the answer model)
    voice_providers: list[VoiceProviderOption]  # voice-listening providers (D-57)
    languages: list[str]
    question_types: list[str]
    formats: list[str]
    lengths: list[str]
    tones: list[str]


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.llm_registry
    return registry


def get_stt_router(request: Request) -> SttRouter:
    stt_router: SttRouter = request.app.state.stt_router
    return stt_router


@router.get("/options")
async def ai_options(
    _: CurrentUser,
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    voice: Annotated[SttRouter, Depends(get_stt_router)],
) -> AiOptions:
    return AiOptions(
        providers=[
            ProviderOption(
                id=e.spec.id,
                models=[
                    ModelOption(
                        id=m.id,
                        label=m.label,
                        capabilities=sorted(m.capabilities),
                        languages=list(m.languages),
                    )
                    for m in e.spec.models
                ],
            )
            for e in registry.entries()
        ],
        voice_providers=[
            VoiceProviderOption(
                id=e.spec.provider_id,
                models=[VoiceModelOption(id=e.spec.model_id, label=e.spec.label)],
            )
            for e in voice.entries()
        ],
        # Preference values come from the enums (single source of truth), not from providers.
        languages=[v.value for v in Language],
        question_types=[v.value for v in QuestionType],
        formats=[v.value for v in AnswerFormat],
        lengths=[v.value for v in AnswerLength],
        tones=[v.value for v in AnswerTone],
    )
