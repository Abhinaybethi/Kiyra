"""Settings and model configuration routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.database import get_db
from db.models import ModelConfiguration, ApplicationSettings
from api.schemas import ModelConfigUpdate, ModelConfigResponse, SettingUpdate, OKResponse
from ai.provider import get_provider, OllamaProvider

router = APIRouter(prefix="/api/settings", tags=["settings"])


def get_active_config(db: Session) -> ModelConfiguration:
    config = db.query(ModelConfiguration).filter_by(is_active=True).first()
    if not config:
        config = ModelConfiguration()
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


@router.get("/models", response_model=ModelConfigResponse)
def get_model_config(db: Session = Depends(get_db)):
    return get_active_config(db)


@router.patch("/models", response_model=ModelConfigResponse)
def update_model_config(data: ModelConfigUpdate, db: Session = Depends(get_db)):
    config = get_active_config(db)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(config, field, value)
    db.commit()
    db.refresh(config)
    return config


@router.get("/models/available")
async def list_available_models(db: Session = Depends(get_db)):
    """List models available in Ollama."""
    provider = OllamaProvider()
    healthy = await provider.health_check()
    if not healthy:
        return {"ollama_available": False, "models": [], "error": "Ollama is not running. Start with: ollama serve"}
    models = await provider.list_models()
    return {"ollama_available": True, "models": models}


@router.get("/models/health")
async def model_health():
    """Check AI provider health."""
    provider = get_provider()
    healthy = await provider.health_check()
    return {
        "healthy": healthy,
        "provider": type(provider).__name__,
        "model": provider.model_name,
    }


@router.get("")
def get_all_settings(db: Session = Depends(get_db)):
    rows = db.query(ApplicationSettings).all()
    return {row.key: row.value for row in rows}


@router.get("/{key}")
def get_setting(key: str, db: Session = Depends(get_db)):
    row = db.query(ApplicationSettings).filter_by(key=key).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
    return {"key": row.key, "value": row.value}


@router.put("/{key}", response_model=OKResponse)
def set_setting(key: str, data: SettingUpdate, db: Session = Depends(get_db)):
    # Only allow safe setting keys (no injection)
    import re
    if not re.match(r"^[a-zA-Z0-9_.-]{1,100}$", key):
        raise HTTPException(status_code=422, detail="Invalid setting key")

    row = db.query(ApplicationSettings).filter_by(key=key).first()
    if row:
        row.value = data.value
    else:
        row = ApplicationSettings(key=key, value=data.value)
        db.add(row)
    db.commit()
    return OKResponse(message=f"Setting '{key}' updated")
