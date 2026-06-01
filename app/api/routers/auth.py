from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.repositories.auth_repository import AuthRepository
from app.schemas.auth import LoginRequest, LoginResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

REQUIRED_APP_VERSION_CODE = 5
REQUIRED_APP_VERSION_NAME = "5.2.3"
REQUIRED_PLATFORM = "android"


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    if int(payload.app_version_code) != REQUIRED_APP_VERSION_CODE or str(payload.app_version_name).strip() != REQUIRED_APP_VERSION_NAME:
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail=f"Unsupported app version. Required: code={REQUIRED_APP_VERSION_CODE}, name={REQUIRED_APP_VERSION_NAME}",
        )
    if str(payload.platform).strip().lower() != REQUIRED_PLATFORM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform. Required: {REQUIRED_PLATFORM}",
        )
    service = AuthService(repository=AuthRepository(db))
    return service.login(username=payload.username, password=payload.password)
