from pydantic import BaseModel, Field, ConfigDict
from typing import Any

class SelectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    node_ids: list[str] = Field(min_length=1)

class TestCase(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1)
    expected_result: str = Field(min_length=5)
    source_node_ids: list[str] = Field(min_length=1)
    rationale: str = Field(default="", max_length=1000)

class LLMTestOutput(BaseModel):
    test_cases: list[TestCase] = Field(min_length=3, max_length=5)

class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    logical_id: str
    version_id: int
    parent_id: str | None
    heading: str
    section_number: str | None
    level: int
    node_type: str
    body_text: str
    content_hash: str
    path: str
    page_start: int | None
    children: list[Any] = Field(default_factory=list)
