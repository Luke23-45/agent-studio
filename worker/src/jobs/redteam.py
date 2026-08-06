"""
Red Team Job.

Executes automated adversarial testing using Garak and PyRIT.
Runs against:
- New guardrail configurations
- Model updates
- Scheduled security sweeps (weekly/monthly)

Outputs detailed reports with vulnerabilities and recommended fixes.
"""

from __future__ import annotations

import logging
import subprocess
import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class Vulnerability:
    """Single vulnerability found during red-teaming."""
    id: str
    category: str  # injection, jailbreak, pii_leak, policy_violation, etc.
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    description: str
    probe_name: str
    input_prompt: str
    model_response: str
    mitigation: str
    timestamp: datetime = field(default_factory=datetime.utcnow)

@dataclass
class RedTeamReport:
    """Aggregated report from a red-team run."""
    job_id: str
    tenant_id: str
    target_model: str
    total_probes: int
    vulnerabilities_found: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
    vulnerabilities: List[Vulnerability] = field(default_factory=list)
    garak_output_path: Optional[str] = None
    pyrith_output_path: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

class RedTeamJob:
    """
    Executes comprehensive red-teaming using Garak and PyRIT.
    
    Usage:
        job = RedTeamJob(tenant_id="tenant-123", model="gpt-4")
        report = await job.run()
    """
    
    def __init__(
        self,
        tenant_id: str,
        model: str,
        garak_probes: Optional[List[str]] = None,
        pyrith_scenarios: Optional[List[str]] = None,
        output_dir: str = "/tmp/neryva-redteam",
    ):
        self.tenant_id = tenant_id
        self.model = model
        self.garak_probes = garak_probes or ["all"]
        self.pyrith_scenarios = pyrith_scenarios or ["crescendo", "tree_of_attacks"]
        self.output_dir = Path(output_dir)
        self.job_id = f"redteam-{tenant_id}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        
        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    async def run(self) -> RedTeamReport:
        """Execute the red-team job."""
        logger.info(f"Starting red-team job {self.job_id} for tenant {self.tenant_id}")
        
        report = RedTeamReport(
            job_id=self.job_id,
            tenant_id=self.tenant_id,
            target_model=self.model,
            total_probes=0,
            vulnerabilities_found=0,
            critical_count=0,
            high_count=0,
            medium_count=0,
            low_count=0,
        )
        
        try:
            # Step 1: Run Garak probes
            logger.info("Running Garak probes...")
            garak_results = await self._run_garak()
            report.garak_output_path = str(garak_results.get("output_path"))
            report.total_probes += garak_results.get("probe_count", 0)
            
            # Parse Garak vulnerabilities
            garak_vulns = self._parse_garak_output(garak_results.get("output_path"))
            report.vulnerabilities.extend(garak_vulns)
            
            # Step 2: Run PyRIT scenarios
            logger.info("Running PyRIT scenarios...")
            pyrith_results = await self._run_pyrith()
            report.pyrith_output_path = str(pyrith_results.get("output_path"))
            report.total_probes += pyrith_results.get("scenario_count", 0)
            
            # Parse PyRIT vulnerabilities
            pyrith_vulns = self._parse_pyrith_output(pyrith_results.get("output_path"))
            report.vulnerabilities.extend(pyrith_vulns)
            
            # Step 3: Aggregate metrics
            report.vulnerabilities_found = len(report.vulnerabilities)
            for vuln in report.vulnerabilities:
                if vuln.severity == "CRITICAL":
                    report.critical_count += 1
                elif vuln.severity == "HIGH":
                    report.high_count += 1
                elif vuln.severity == "MEDIUM":
                    report.medium_count += 1
                else:
                    report.low_count += 1
            
            report.completed_at = datetime.utcnow()
            
            logger.info(
                f"Red-team completed: {report.vulnerabilities_found} vulnerabilities found "
                f"(C:{report.critical_count} H:{report.high_count} M:{report.medium_count} L:{report.low_count})"
            )
            
        except Exception as e:
            logger.error(f"Red-team job failed: {e}")
            raise
        
        return report
    
    async def _run_garak(self) -> Dict[str, Any]:
        """
        Execute Garak probes against the target model.
        Returns path to JSON output file.
        """
        output_file = self.output_dir / f"garak-{self.job_id}.json"
        
        # Build garak command
        # In production, configure proper API endpoints and auth
        cmd = [
            "garak",
            "--model_type", "openai",  # Adjust based on actual provider
            "--model_name", self.model,
            "--probes", ",".join(self.garak_probes),
            "--format", "json",
            "--output", str(output_file),
        ]
        
        logger.info(f"Running Garak: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout
            )
            
            if result.returncode != 0:
                logger.warning(f"Garak exited with code {result.returncode}: {result.stderr}")
            
            return {
                "output_path": str(output_file),
                "probe_count": self._estimate_garak_probe_count(self.garak_probes),
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            
        except subprocess.TimeoutExpired:
            logger.error("Garak execution timed out")
            return {"output_path": None, "probe_count": 0, "error": "timeout"}
        except FileNotFoundError:
            logger.error("Garak not found in PATH. Install with: pip install garak")
            return {"output_path": None, "probe_count": 0, "error": "not_found"}
    
    async def _run_pyrith(self) -> Dict[str, Any]:
        """
        Execute PyRIT scenarios against the target model.
        Returns path to JSON output file.
        """
        output_file = self.output_dir / f"pyrith-{self.job_id}.json"
        
        # PyRIT is typically run as a Python script or via its CLI
        # This is a simplified placeholder - real implementation would use PyRIT's API
        logger.info(f"Running PyRIT scenarios: {self.pyrith_scenarios}")
        
        # Placeholder: In production, invoke PyRIT properly
        # from pyrit.orchestrator import CrescendoOrchestrator
        # ... execute scenarios ...
        
        return {
            "output_path": str(output_file),
            "scenario_count": len(self.pyrith_scenarios),
            "mock": True,
        }
    
    def _parse_garak_output(self, output_path: Optional[str]) -> List[Vulnerability]:
        """Parse Garak JSON output into Vulnerability objects."""
        if not output_path or not Path(output_path).exists():
            return []
        
        vulnerabilities = []
        try:
            with open(output_path, 'r') as f:
                data = json.load(f)
            
            # Garak output format varies by version
            # This is a simplified parser
            for entry in data.get("results", []):
                if entry.get("detected", False):  # Garak found an issue
                    severity = self._map_garak_severity(entry.get("score", 0))
                    vuln = Vulnerability(
                        id=f"garak-{entry.get('probe', 'unknown')}-{len(vulnerabilities)}",
                        category="injection",  # Would parse from probe type
                        severity=severity,
                        description=entry.get("details", "Potential vulnerability detected"),
                        probe_name=entry.get("probe", "unknown"),
                        input_prompt=entry.get("prompt", "")[:500],
                        model_response=entry.get("response", "")[:500],
                        mitigation=self._get_mitigation_for_probe(entry.get("probe", "")),
                    )
                    vulnerabilities.append(vuln)
                    
        except Exception as e:
            logger.error(f"Failed to parse Garak output: {e}")
        
        return vulnerabilities
    
    def _parse_pyrith_output(self, output_path: Optional[str]) -> List[Vulnerability]:
        """Parse PyRIT JSON output into Vulnerability objects."""
        if not output_path or not Path(output_path).exists():
            return []
        
        # Placeholder - PyRIT parsing logic would go here
        logger.warning("PyRIT output parsing not yet implemented")
        return []
    
    def _estimate_garak_probe_count(self, probes: List[str]) -> int:
        """Estimate number of probes that will run."""
        if "all" in probes:
            return 50  # Approximate default probe count
        return len(probes) * 5  # Each probe category has ~5 variants
    
    def _map_garak_severity(self, score: float) -> str:
        """Map Garak score to severity level."""
        if score > 0.8:
            return "CRITICAL"
        elif score > 0.6:
            return "HIGH"
        elif score > 0.4:
            return "MEDIUM"
        else:
            return "LOW"
    
    def _get_mitigation_for_probe(self, probe_name: str) -> str:
        """Return recommended mitigation based on probe type."""
        mitigations = {
            "promptinject": "Strengthen input guardrails with NeMo Guardrails dialog policies",
            "xss": "Implement output sanitization and CSP headers",
            "encoding": "Add encoding detection and normalization in pre-processing",
            "leakreplay": "Review system prompt construction and retrieval filtering",
        }
        for key, mitigation in mitigations.items():
            if key in probe_name.lower():
                return mitigation
        return "Review guardrail configuration and add targeted tests for this attack vector"
