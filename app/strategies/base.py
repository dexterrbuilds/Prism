from __future__ import annotations

from typing import Protocol

from app.analysis.context import AnalysisContext
from app.models import SetupCandidate


class SetupDetector(Protocol):
    def detect(self, context: AnalysisContext) -> list[SetupCandidate]: ...
