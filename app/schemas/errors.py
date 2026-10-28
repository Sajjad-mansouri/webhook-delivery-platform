from pydantic import BaseModel, ConfigDict


class FieldError(BaseModel):
    location: list[str | int]
    message: str
    type: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: list[FieldError] | None = None


class ErrorResponse(BaseModel):
    error: ErrorInfo
    model_config = ConfigDict(extra="forbid")
