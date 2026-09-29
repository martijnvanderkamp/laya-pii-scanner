"""Local PII scanner for Dutch and English text, built on the Laya decision model."""
from .scanner import CATEGORIES, SPECIAL, THRESHOLDS, ChunkResult, Finding, PIIScanner, redact

__version__ = "0.1.0"
__all__ = ["PIIScanner", "redact", "ChunkResult", "Finding", "CATEGORIES", "SPECIAL", "THRESHOLDS"]
