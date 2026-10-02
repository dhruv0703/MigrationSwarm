"""Exceptions for deterministic verification-worker coordination."""


class VerificationWorkerError(RuntimeError):
    """Base exception for verification worker failures."""


class VerificationEvidenceError(VerificationWorkerError):
    """Raised only for direct evidence-loader API misuse or I/O failures."""
