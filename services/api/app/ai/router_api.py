"""GET /api/v1/ai/options: allowed providers/models and preference values (no credentials)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.ai.registry import ProviderRegistry
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


class AiOptions(BaseModel):
    providers: list[ProviderOption]
    languages: list[str]
    question_types: list[str]
    formats: list[str]
    lengths: list[str]
    tones: list[str]


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.llm_registry
    return registry


@router.get("/options")
async def ai_options(
    _: CurrentUser, registry: Annotated[ProviderRegistry, Depends(get_registry)]
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
        # Preference values come from the enums (single source of truth), not from providers.
        languages=[v.value for v in Language],
        question_types=[v.value for v in QuestionType],
        formats=[v.value for v in AnswerFormat],
        lengths=[v.value for v in AnswerLength],
        tones=[v.value for v in AnswerTone],
    )
