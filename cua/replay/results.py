"""What a replay returns. Exactly one of Success, BusinessOutcome or Failure."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecoveryRecord(_Model):
    step_id: str
    kind: Literal["slow_load_wait", "dismiss", "reauthenticate", "human_handoff"]
    signature: str | None = None
    detail: str


class Success(_Model):
    status: Literal["success"] = "success"
    outputs: dict[str, str]
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    run_dir: str = ""


class BusinessOutcome(_Model):
    """The app answered, and the answer is not success (e.g. MEMBER_NOT_FOUND). Not an error to retry."""

    status: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    step_id: str
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    run_dir: str = ""


class FailureKind(str, Enum):
    NOT_APPROVED = "not_approved"
    INVALID_PARAMS = "invalid_params"
    PRECONDITION_FAILED = "precondition_failed"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_AMBIGUOUS = "target_ambiguous"
    ACTION_FAILED = "action_failed"
    CHECKPOINT_TIMEOUT = "checkpoint_timeout"
    POLICY_BLOCKED = "policy_blocked"       # outside the allowlist
    APPROVAL_DENIED = "approval_denied"     # irreversible step held for approval and not approved
    HARD_FAILURE = "hard_failure"            # a hard_failure error signature matched
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    UNSAFE_TO_RECOVER = "unsafe_to_recover"  # recovering would risk repeating an irreversible step
    MISSING_OUTPUT = "missing_output"        # finished, but an output step never ran (e.g. skipped in a handoff)


class Failure(_Model):
    status: Literal["failure"] = "failure"
    kind: FailureKind
    step_id: str | None
    code: str | None = None  # outcome_code of the matched error signature, if any
    message: str
    expected: str
    observed: str
    evidence_paths: list[str] = Field(default_factory=list)
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    run_dir: str = ""


ReplayResult = Annotated[Union[Success, BusinessOutcome, Failure], Field(discriminator="status")]
