"""
Pydantic models for the Vote API.

Defines the data structures used for request/response serialization
and type validation.
"""

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class Option(BaseModel):
    """A single answer option within a quiz."""
    id: int
    quiz_id: int
    label: str
    text: str

    class Config:
        from_attributes = True


class Quiz(BaseModel):
    """A quiz containing a question and multiple choice options."""
    id: int
    title: str
    question: str
    options: list[Option]
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class VoteRequest(BaseModel):
    """Request body for submitting a vote on a quiz."""
    option_id: int


class VoteResponse(BaseModel):
    """Response returned after a vote is recorded."""
    status: str
    vote_id: int