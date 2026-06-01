from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=50)
    app_version_code: int
    app_version_name: str = Field(min_length=1, max_length=50)
    platform: str = Field(min_length=1, max_length=20)


class UserInfo(BaseModel):
    id: int
    name: str
    designation: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo
