"""Request/response models for POST /sessions/job-import (contract: docs/API.md)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JobImportRequest(BaseModel):
    # Only the URL: no user id, no storage paths, no options. Unknown fields are rejected.
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)


class JobImportResponse(BaseModel):
    status: Literal["succeeded", "partial", "failed"]
    company: str | None = None
    title: str | None = None
    description: str | None = None
    message: str
