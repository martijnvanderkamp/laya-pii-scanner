"""Local PII scanner for Dutch and English text, built on the Laya decision model."""
from .files import discover
from .scanner import CATEGORIES, SPECIAL, THRESHOLDS, ChunkResult, Finding, PIIScanner, redact
from .tree import FileFinding, FileReport, TreeReport, scan_file, scan_tree

__version__ = "0.2.0"
__all__ = ["PIIScanner", "redact", "scan_tree", "scan_file", "discover", "ChunkResult", "Finding",
           "FileReport", "FileFinding", "TreeReport", "CATEGORIES", "SPECIAL", "THRESHOLDS"]
