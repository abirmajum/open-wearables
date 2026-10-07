from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, status

from app.database import DbSession
from app.schemas.model_crud.credentials import ApiKeyRead, ApiKeyUpdate, ApiKeyWithSecret
from app.services import DeveloperDep, api_key_service

router = APIRouter()


@router.get("/api-keys", response_model=list[ApiKeyRead])
def list_api_keys(db: DbSession, _developer: DeveloperDep):
    """List all API keys."""
    return api_key_service.list_api_keys(db)


@router.post("/api-keys", status_code=status.HTTP_201_CREATED, response_model=ApiKeyWithSecret)
def create_api_key(
    db: DbSession,
    _developer: DeveloperDep,
    name: Annotated[str, Body(embed=True, description="Name for the API key")] = "Default",
):
    """Generate new API key."""
    return api_key_service.create_api_key(db, _developer.id, name)


@router.delete("/api-keys/{key_id}", response_model=ApiKeyRead)
def delete_api_key(key_id: UUID, db: DbSession, _developer: DeveloperDep):
    """Delete an API key by opaque ID."""
    return api_key_service.delete(db, key_id, raise_404=True)


@router.patch("/api-keys/{key_id}", response_model=ApiKeyRead)
def update_api_key(
    key_id: UUID,
    payload: ApiKeyUpdate,
    db: DbSession,
    _developer: DeveloperDep,
):
    """Update API key (future: name, scopes)."""
    return api_key_service.update(db, key_id, payload, raise_404=True)


@router.post(
    "/api-keys/{key_id}/rotate",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyWithSecret,
)
def rotate_api_key(key_id: UUID, db: DbSession, _developer: DeveloperDep):
    """Rotate credential material while preserving the opaque key ID."""
    return api_key_service.rotate_api_key(db, key_id)
