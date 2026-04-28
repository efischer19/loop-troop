"""Shared execution profile models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class WorkerTier(str, Enum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class TargetExecutionProfile(BaseModel):
    tier: WorkerTier
    model_name: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
