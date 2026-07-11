from pydantic import BaseModel, ConfigDict, Field, JsonValue


class CodexRpcError(BaseModel):
    """An app-server JSON-RPC error response."""

    message: str


class CodexRpcMessage(BaseModel):
    """A response or notification emitted by Codex app-server."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    method: str | None = None
    result: JsonValue = None
    params: JsonValue = None
    error: CodexRpcError | None = None


class CodexRpcRequest(BaseModel):
    """A request sent to Codex app-server."""

    method: str
    id: int | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)


class CodexTurn(BaseModel):
    """The minimal turn state needed by the review backend."""

    id: str
    status: str
    error: dict[str, JsonValue] | None = None


class CodexThread(BaseModel):
    """The minimal thread state needed by the review backend."""

    id: str
