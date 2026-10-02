"""Deterministic classification of verification failures."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.agents.build_verification import (
    BuildVerificationResult,
)
from migrationswarm.core.verification.decision import VerificationEvidence


class FailureCategory(StrEnum):
    """Bounded categories used to decide whether repair is appropriate."""

    COMPILATION = "compilation"
    TEST_FAILURE = "test_failure"
    GENERATED_CONFIGURATION = "generated_configuration"
    TOOLCHAIN = "toolchain"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PERMISSION = "permission"
    UNKNOWN = "unknown"


class DebugEligibilityDecision(BaseModel):
    """A deterministic, auditable repair eligibility decision."""

    model_config = ConfigDict(extra="forbid")

    category: FailureCategory
    eligible: bool
    reason: str = Field(min_length=1)


class FailureClassifier:
    """Classify bounded build evidence without using a model."""

    _ELIGIBLE = {
        FailureCategory.COMPILATION,
        FailureCategory.TEST_FAILURE,
        FailureCategory.GENERATED_CONFIGURATION,
    }

    def classify(
        self,
        result: BuildVerificationResult | None,
        evidence: VerificationEvidence,
    ) -> DebugEligibilityDecision:
        """Return one stable category and whether a repair may be attempted."""
        text_parts = list(evidence.warnings)
        if result is not None:
            text_parts.extend([result.stdout_summary, result.stderr_summary, *result.warnings])
            if any(command.timed_out for command in result.commands_run):
                return self._decision(FailureCategory.TIMEOUT, "Verification command timed out.")
        text = " ".join(text_parts).lower()
        if any(token in text for token in ("timed out", "timeout", "time out")):
            return self._decision(FailureCategory.TIMEOUT, "Verification reported a timeout.")
        if any(
            token in text
            for token in ("permission denied", "access denied", "operation not permitted")
        ):
            return self._decision(
                FailureCategory.PERMISSION, "Verification was blocked by permissions."
            )
        if any(
            token in text
            for token in (
                "connection refused",
                "connection reset",
                "network",
                "dns",
                "could not transfer",
                "401 unauthorized",
                "403 forbidden",
            )
        ):
            return self._decision(
                FailureCategory.NETWORK, "Verification reported an external network failure."
            )
        if any(
            token in text
            for token in (
                "command not found",
                "not recognized",
                "could not execute",
                "no supported",
                "mvn not found",
                "gradle not found",
            )
        ):
            return self._decision(
                FailureCategory.TOOLCHAIN, "The verification toolchain was unavailable."
            )
        summary = result.test_summary if result is not None else None
        if (
            evidence.test_failures
            and evidence.test_failures > 0
            or evidence.test_errors
            and evidence.test_errors > 0
            or summary is not None
            and (summary.failures or 0) > 0
            or summary is not None
            and (summary.errors or 0) > 0
            or "test failure" in text
            or "tests run:" in text
            and ("failures: 1" in text or "errors: 1" in text)
        ):
            return self._decision(
                FailureCategory.TEST_FAILURE, "Verification reported a test failure."
            )
        if any(
            token in text
            for token in (
                "application.yml",
                "application.yaml",
                "pom.xml",
                "configuration",
                "generated configuration",
            )
        ):
            return self._decision(
                FailureCategory.GENERATED_CONFIGURATION,
                "Failure points to generated service configuration or wiring.",
            )
        if any(
            token in text
            for token in (
                "compilation",
                "compile error",
                "compilation error",
                "cannot find symbol",
                "does not exist",
                "syntax error",
            )
        ):
            return self._decision(
                FailureCategory.COMPILATION, "Verification reported a compilation failure."
            )
        return self._decision(
            FailureCategory.UNKNOWN, "Failure did not match a bounded repair category."
        )

    def _decision(self, category: FailureCategory, reason: str) -> DebugEligibilityDecision:
        return DebugEligibilityDecision(
            category=category,
            eligible=category in self._ELIGIBLE,
            reason=reason,
        )


__all__ = ["DebugEligibilityDecision", "FailureCategory", "FailureClassifier"]
