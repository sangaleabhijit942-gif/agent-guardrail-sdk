import requests
import uuid
import threading


class GuardrailKillSignal(Exception):
    """Raised when Agent Guardrail signals that a workflow has exceeded its configured
    threshold, or when the guardrail service becomes unreachable and can no longer verify safety."""
    pass


class BudgetExceededError(Exception):
    """Raised by patched clients when a call is blocked BEFORE it reaches the LLM provider,
    because the local cost cache shows the budget is already exceeded."""
    pass


class GuardrailClient:
    def __init__(
        self,
        api_key: str,
        workflow_name: str,
        base_url: str = "http://localhost:8000",
        timeout: int = 5,
        max_consecutive_failures: int = 3
    ):
        self.api_key = api_key
        self.workflow_name = workflow_name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.trace_id = str(uuid.uuid4())
        self.max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures = 0

        # Local budget cache — used by patch_anthropic() for pre-call blocking
        self._local_cost = 0.0
        self._local_tokens = 0
        self._cached_threshold = None
        self._cached_threshold_type = "cost"
        self._lock = threading.Lock()

    def track(self, node_name: str, step: int, message: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
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
        except requests.exceptions.RequestException as e:
            self._consecutive_failures += 1
            print(f"[agentguardrail] WARNING: failed to report event ({self._consecutive_failures}/{self.max_consecutive_failures} consecutive failures): {e}")
            if self._consecutive_failures >= self.max_consecutive_failures:
                raise GuardrailKillSignal(
                    f"Guardrail service unreachable for {self._consecutive_failures} consecutive calls — "
                    f"stopping workflow, as usage can no longer be verified."
                )
            return

        self._consecutive_failures = 0

        if result.get("status") == "kill":
            raise GuardrailKillSignal(result.get("reason", "Threshold exceeded"))

    def _check_local_budget(self) -> None:
        """
        Called by patch_anthropic() BEFORE each outbound LLM call.
        Blocks locally, with no network round-trip, if the cached budget is exceeded.
        """
        with self._lock:
            if self._cached_threshold is None:
                return  # No threshold synced yet — allow the call, first sync will populate this

            if self._cached_threshold_type == "cost":
                if self._local_cost >= self._cached_threshold:
                    raise BudgetExceededError(
                        f"[agentguardrail] Call blocked BEFORE reaching the LLM provider — "
                        f"local cost cache (${self._local_cost:.6f}) has reached the threshold "
                        f"(${self._cached_threshold:.6f}). No API call was made."
                    )

    def _record_local_usage(self, tokens_in: int, tokens_out: int) -> None:
        with self._lock:
            self._local_cost += (tokens_in * 1e-6) + (tokens_out * 5e-6)
            self._local_tokens += tokens_in + tokens_out

    def patch_anthropic(self, client) -> None:
        """
        Wraps an existing anthropic.Anthropic client instance so that every
        messages.create() call is checked against the local budget cache
        BEFORE the request is sent to Anthropic's servers.

        Usage:
            client = anthropic.Anthropic(api_key=...)
            guardrail.patch_anthropic(client)
            # client.messages.create(...) now blocks locally if over budget
        """
        original_create = client.messages.create
        guardrail_ref = self

        def patched_create(*args, **kwargs):
            guardrail_ref._check_local_budget()
            response = original_create(*args, **kwargs)
            if hasattr(response, "usage"):
                guardrail_ref._record_local_usage(
                    response.usage.input_tokens,
                    response.usage.output_tokens
                )
            return response

        client.messages.create = patched_create
        print(f"[agentguardrail] Patched anthropic client — calls will be checked against local budget before sending.")