"""Pydantic models for exec-rl prediction data."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from exec_rl.crash_taxonomy import CrashType


class CrashPrediction(BaseModel):
    """Parsed crash site prediction from context_output.json."""

    crashType: CrashType
    crashFilename: str
    crashLine: int
    crashFunction: str


class CrashGroundTruthFrame(BaseModel):
    """One meaningful frame from a parsed crash report."""

    depth: int = Field(ge=0)
    function: str
    filename: str
    line: int = Field(ge=1)
    rawLine: str
    reason: str


class CrashGroundTruthExcludedFrame(BaseModel):
    """A frame intentionally excluded from rewardable crash-site ground truth."""

    function: str
    reason: str


class CrashGroundTruthPayload(BaseModel):
    """Validated JSON payload generated from a crash report."""

    frames: list[CrashGroundTruthFrame] = Field(min_length=1)
    excludedFrames: Optional[list[CrashGroundTruthExcludedFrame]] = None


class CrashGroundTruth(BaseModel):
    """Stored LLM-derived crash-report ground truth for a bug."""

    groundTruthId: int | None
    bugId: str
    addedTime: str
    updatedTime: str
    status: Literal["success", "error"]
    systemMessage: str
    model: str
    modelConfigName: str
    messages: list[dict]
    groundTruth: dict | None
    attrs: dict

    def parsed_ground_truth(self) -> CrashGroundTruthPayload | None:
        """Return validated ground-truth JSON, or None if absent/invalid."""
        if not self.groundTruth:
            return None
        try:
            return CrashGroundTruthPayload.model_validate(self.groundTruth)
        except Exception:
            return None


class CrashPredictionConfig(BaseModel):
    """Configuration record for a crash prediction experiment run."""

    predConfigId: int
    agent: str
    agentConfigName: str
    modelConfigName: str
    unionCounter: int
    description: str
    attrs: dict


class CrashPredictionRun(BaseModel):
    """One agent run result for crash site prediction."""

    runId: int | None
    bugId: str
    predConfigId: int
    unionId: int
    addedTime: str
    status: Literal["success", "error"]
    systemMessage: str | None
    prediction: dict | None  # raw contextOutput; parse with CrashPrediction.model_validate()
    dollarCost: float | None
    timeCost: int | None
    exitReason: str | None
    trajectoryKey: str | None
    attrs: dict

    def parsed_prediction(self) -> CrashPrediction | None:
        """Return the prediction as a validated CrashPrediction, or None if absent/invalid."""
        if not self.prediction:
            return None
        try:
            return CrashPrediction.model_validate(self.prediction)
        except Exception:
            return None
