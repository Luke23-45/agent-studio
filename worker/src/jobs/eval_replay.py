"""
Eval Replay Job.

Replays production traces through the eval suite to detect regressions.
Triggered by:
- New model deployment
- Guardrail config changes
- Scheduled nightly runs

Integrates with Garak and PyRIT for automated scoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
import json

logger = logging.getLogger(__name__)

@dataclass
class EvalReplayResult:
    """Result of a single eval replay."""
    trace_id: str
    original_output: str
    replay_output: str
    match: bool
    score: float
    metrics: Dict[str, float] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

@dataclass
class EvalReplayReport:
    """Aggregated report for a batch replay."""
    job_id: str
    tenant_id: str
    total_traces: int
    successful_replays: int
    failed_replays: int
    avg_score: float
    regression_count: int
    results: List[EvalReplayResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

class EvalReplayJob:
    """
    Replays historical traces against current configuration.
    
    Usage:
        job = EvalReplayJob(tenant_id="tenant-123", trace_sample_size=100)
        report = await job.run()
    """
    
    def __init__(
        self,
        tenant_id: str,
        trace_sample_size: int = 50,
        include_failed_traces: bool = False,
        garak_enabled: bool = True,
        pyrith_enabled: bool = True,
    ):
        self.tenant_id = tenant_id
        self.trace_sample_size = trace_sample_size
        self.include_failed_traces = include_failed_traces
        self.garak_enabled = garak_enabled
        self.pyrith_enabled = pyrith_enabled
        self.job_id = f"eval-replay-{tenant_id}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        
    async def run(self) -> EvalReplayReport:
        """Execute the eval replay job."""
        logger.info(f"Starting eval replay job {self.job_id} for tenant {self.tenant_id}")
        
        report = EvalReplayReport(
            job_id=self.job_id,
            tenant_id=self.tenant_id,
            total_traces=0,
            successful_replays=0,
            failed_replays=0,
            avg_score=0.0,
            regression_count=0,
        )
        
        try:
            # Step 1: Fetch traces from observability backend
            traces = await self._fetch_traces()
            report.total_traces = len(traces)
            logger.info(f"Fetched {len(traces)} traces for replay")
            
            # Step 2: Replay each trace
            for trace in traces:
                try:
                    result = await self._replay_trace(trace)
                    report.results.append(result)
                    
                    if result.match:
                        report.successful_replays += 1
                    else:
                        report.failed_replays += 1
                        if result.score < 0.8:  # Regression threshold
                            report.regression_count += 1
                            
                except Exception as e:
                    logger.error(f"Replay failed for trace {trace.get('id')}: {e}")
                    report.failed_replays += 1
            
            # Step 3: Calculate aggregate metrics
            if report.results:
                report.avg_score = sum(r.score for r in report.results) / len(report.results)
            
            report.completed_at = datetime.utcnow()
            logger.info(
                f"Eval replay completed: {report.successful_replays}/{report.total_traces} passed, "
                f"avg_score={report.avg_score:.2f}, regressions={report.regression_count}"
            )
            
        except Exception as e:
            logger.error(f"Eval replay job failed: {e}")
            raise
        
        return report
    
    async def _fetch_traces(self) -> List[Dict[str, Any]]:
        """
        Fetch traces from Langfuse or other observability backend.
        In production, this would query the actual API.
        """
        # Placeholder: In real implementation, call Langfuse API
        # langfuse_client.fetch_traces(projectId=..., limit=...)
        logger.warning("Trace fetching not implemented - returning mock data")
        return [
            {
                "id": f"trace-{i}",
                "input": "Sample user input",
                "expected_output": "Sample expected output",
                "metadata": {"tenant_id": self.tenant_id},
            }
            for i in range(min(5, self.trace_sample_size))  # Mock 5 traces
        ]
    
    async def _replay_trace(self, trace: Dict[str, Any]) -> EvalReplayResult:
        """
        Replay a single trace through the current system.
        Compares new output against original output using similarity metrics.
        """
        from difflib import SequenceMatcher
        
        input_text = trace.get("input", "")
        expected_output = trace.get("expected_output", "")
        
        # Simulate running through current orchestration pipeline
        # In production: call backend/app/application/orchestration/service.py
        replay_output = await self._simulate_orchestration(input_text)
        
        # Calculate similarity
        similarity = SequenceMatcher(None, expected_output, replay_output).ratio()
        
        # Run additional evals if enabled
        metrics = {}
        if self.garak_enabled:
            metrics["garak_score"] = await self._run_garak_probe(input_text, replay_output)
        if self.pyrith_enabled:
            metrics["pyrith_safety"] = await self._run_pyrith_check(input_text, replay_output)
        
        return EvalReplayResult(
            trace_id=trace.get("id", "unknown"),
            original_output=expected_output,
            replay_output=replay_output,
            match=similarity > 0.85,
            score=similarity,
            metrics=metrics,
        )
    
    async def _simulate_orchestration(self, input_text: str) -> str:
        """
        Simulate orchestration pipeline execution.
        In production, this calls the actual orchestration service.
        """
        # Placeholder - would call backend orchestration
        return f"Simulated response to: {input_text[:50]}..."
    
    async def _run_garak_probe(self, input_text: str, output: str) -> float:
        """Run Garak probe on the output."""
        # Placeholder - would invoke garak CLI or API
        return 0.95  # Mock high score
    
    async def _run_pyrith_check(self, input_text: str, output: str) -> float:
        """Run PyRIT safety check on the output."""
        # Placeholder - would invoke PyRIT scorer
        return 0.98  # Mock high score
