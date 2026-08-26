import requests
import uuid


class GuardrailKillSignal(Exception):
    """Raised when Agent Guardrail signals that a workflow has exceeded its cost threshold,
    or when the guardrail service itself becomes unreachable and can no longer verify safety."""
    pass


class GuardrailClient:
    def __init__(self, api_key: str, workflow_name: str, base_url: str = "http://localhost:8000", timeout: int = 5, max_consecutive_failures: int = 3):
        self.api_key = api_key
        self.workflow_name = workflow_name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.trace_id = str(uuid.uuid4())
        self.max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures = 0

    def track(self, node_name: str, step: int, message: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        """
        Report a step's cost to Agent Guardrail. Raises GuardrailKillSignal if this
        pushes the workflow over its configured threshold — callers should let this
        exception propagate up and stop their own execution loop.

        If the guardrail service becomes unreachable for several calls in a row,
        this also raises GuardrailKillSignal — a workflow that can no longer be
        monitored should not keep running unmonitored.
        """
        try:
            response = requests.post(
                f"{self.base_url}/events",
                json={
                    "node_name": node_name,
                    "step": step,
                    "message": message,
                    "trace_id": self.trace_id,
                    "workflow_name": self.workflow_name,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out
                },
                headers={"X-API-Key": self.api_key},
                timeout=self.timeout
            )
            result = response.json()
self._consecutive_failures = 0
except requests.exceptions.RequestException as e:
    self._consecutive_failures += 1
    print(f"[agentguardrail] WARNING: failed to report event ({self._consecutive_failures}/{self.max_consecutive_failures}...")
    if self._consecutive_failures >= self.max_consecutive_failures:
        raise GuardrailKillSignal(...)
    return

        if result.get("status") == "kill":
            raise GuardrailKillSignal(result.get("reason", "Cost threshold exceeded"))