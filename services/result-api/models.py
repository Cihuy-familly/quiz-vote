"""
Pydantic models for the Result API.

Defines the data structures used for quiz result responses.
"""

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class OptionResult(BaseModel):
    """Result breakdown for a single option within a quiz."""
    label: str
    text: str
    votes: int
    percentage: float


class QuizResults(BaseModel):
    """Full results for a quiz including per-option vote counts."""
    quiz_id: int
    title: str
    question: str
    options: list[OptionResult]
    total_votes: int
    created_at: Optional[datetime] = None