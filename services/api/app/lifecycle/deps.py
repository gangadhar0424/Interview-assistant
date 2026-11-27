"""FastAPI dependency for the deletion recorder (SEC-13, D-85)."""

from typing import Annotated

from fastapi import Depends, Request

from app.lifecycle.recorder import DeletionRecorder


def get_deletion_recorder(request: Request) -> DeletionRecorder:
    recorder: DeletionRecorder | None = getattr(request.app.state, "deletion_recorder", None)
    # Apps built without the full lifespan (some tests) still get a working, inert recorder.
    return recorder if recorder is not None else DeletionRecorder(None, None)


DeletionRecorderDep = Annotated[DeletionRecorder, Depends(get_deletion_recorder)]
