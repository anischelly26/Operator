"""
Pydantic schemas and data structures for The Operator agent.
Ensures strict validation, type safety, trajectory logging, and error representation.
"""

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Tool Argument Models (Strict Input Schemas)
# ---------------------------------------------------------------------------

class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class GetTicketArgs(ToolArgs):
    ticket_id: str = Field(..., description="The unique ID of the ticket to inspect (e.g., TKT-101).")


class SearchKBArgs(ToolArgs):
    query: str = Field(..., min_length=2, max_length=200, description="Keywords or topic to search in the knowledge base.")


class CheckCustomerArgs(ToolArgs):
    customer_id: str = Field(..., description="Customer ID or email address to query profile and purchase history.")


class UpdateStatusArgs(ToolArgs):
    ticket_id: str = Field(..., description="The unique ID of the ticket.")
    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "ESCALATED", "ARCHIVED"] = Field(
        ..., description="The new status to assign to the ticket."
    )
    tags: List[str] = Field(default_factory=list, description="Categorization tags to apply.")


class DraftReplyArgs(ToolArgs):
    ticket_id: str = Field(..., description="The unique ID of the ticket.")
    recipient: str = Field(..., description="Email of recipient.")
    subject: str = Field(..., min_length=3, description="Subject line for the reply.")
    body: str = Field(..., min_length=5, description="Draft response body.")


class RefundPayload(ToolArgs):
    amount: float = Field(..., gt=0, allow_inf_nan=False)
    reason: Optional[str] = None


class ResetPasswordPayload(ToolArgs):
    target_email: str = Field(..., min_length=3)


class CancelOrderPayload(ToolArgs):
    order_id: str = Field(..., min_length=1)


class SendEmailPayload(ToolArgs):
    recipient: str = Field(..., min_length=3)
    subject: str = Field(..., min_length=3)
    body: str = Field(..., min_length=5)


ACTION_PAYLOAD_MODELS = {
    "ISSUE_REFUND": RefundPayload,
    "RESET_PASSWORD": ResetPasswordPayload,
    "CANCEL_ORDER": CancelOrderPayload,
    "SEND_EMAIL": SendEmailPayload,
}


class ExecuteActionArgs(ToolArgs):
    ticket_id: str = Field(..., min_length=1, description="The unique ID of the ticket.")
    action_type: Literal["SEND_EMAIL", "ISSUE_REFUND", "RESET_PASSWORD", "CANCEL_ORDER"] = Field(
        ..., description="High-impact action to execute. Must pass human checkpoint."
    )
    payload: Dict[str, Any] = Field(..., description="Specific parameters for the action.")
    idempotency_key: str = Field(
        ..., min_length=1, max_length=200, description="Unique key to prevent duplicate writes if repeated (e.g., TKT-101-REFUND-001)."
    )


# ---------------------------------------------------------------------------
# Loop & Trajectory Models
# ---------------------------------------------------------------------------

class ToolCall(BaseModel):
    name: str = Field(..., description="Name of the tool to invoke.")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Validated tool arguments.")
    call_id: Optional[str] = None


class ToolResult(BaseModel):
    tool_name: str
    output: str
    is_error: bool = False
    error_type: Optional[str] = None
    duration_ms: float = 0.0
    timestamp: str = ""


class AgentStep(BaseModel):
    step_index: int
    thought: str
    tool_call: Optional[ToolCall] = None
    observation: Optional[str] = None
    tool_result: Optional[ToolResult] = None
    is_final: bool = False
    final_response: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    timestamp: str = ""


class Trajectory(BaseModel):
    task_id: str
    scenario_name: str
    steps: List[AgentStep] = Field(default_factory=list)
    total_steps: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    status: Literal[
        "SUCCESS",
        "FAILED",
        "STOPPED_MAX_STEPS",
        "STOPPED_BUDGET",
        "STOPPED_CYCLE",
        "HUMAN_REJECTED",
        "INJECTION_BLOCKED"
    ] = "SUCCESS"
    duration_seconds: float = 0.0
    final_answer: Optional[str] = None
    error_summary: Optional[str] = None

