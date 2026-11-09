from pydantic import BaseModel, Field


class TraceReference(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    trace_url: str
