"""
Human Checkpoint Module for The Operator.
Halts high-impact write actions (refunds, external emails, password resets, cancellations),
displays the exact payload and impact to the operator, and awaits explicit authorization.
"""

from typing import Any, Callable, Dict, Optional, Tuple


class HumanCheckpoint:
    def __init__(self, mode: str = "interactive", auto_approve: bool = False):
        """
        mode: 'interactive' (prompts human via terminal input) or 'headless' (eval/automated mode).
        auto_approve: If True in headless mode, automatically approves valid actions.
        """
        if mode not in {"interactive", "headless"}:
            raise ValueError("Checkpoint mode must be interactive or headless")
        if not isinstance(auto_approve, bool):
            raise ValueError("auto_approve must be a boolean")
        self.mode = mode
        self.auto_approve = auto_approve
        self.decision_history: list = []

    def request_approval(
        self,
        ticket_id: str,
        action_type: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> Tuple[bool, str]:
        """
        Displays action details and gathers decision.
        Returns (approved: bool, reason: str).
        """
        banner = (
            "\n"
            + "=" * 65
            + "\n"
            + "                HUMAN CHECKPOINT GATEKEEPER\n"
            + "=" * 65
            + f"\nTicket ID:       {ticket_id}\n"
            + f"Action Type:     {action_type}\n"
            + f"Idempotency Key: {idempotency_key}\n"
            + f"Action Payload:  {payload}\n"
            + "-" * 65
        )

        if self.mode == "headless":
            if self.auto_approve:
                reason = "Auto-approved by headless test harness policy."
                self._record(ticket_id, action_type, True, reason)
                return True, reason
            else:
                reason = "Blocked by headless test harness default-deny policy."
                self._record(ticket_id, action_type, False, reason)
                return False, reason

        # Interactive Mode
        print(banner)
        prompt_text = "Authorize this state-modifying action? [y/N / enter rejection reason]: "
        try:
            user_input = input(prompt_text).strip()
        except EOFError:
            user_input = "n"

        if user_input.lower() in ["y", "yes"]:
            print("[Human Checkpoint] Action APPROVED.")
            self._record(ticket_id, action_type, True, "Approved by operator.")
            return True, "Approved by human operator."
        else:
            reason = user_input if user_input and user_input.lower() not in ["n", "no"] else "Rejected by human operator."
            print(f"[Human Checkpoint] Action REJECTED: {reason}")
            self._record(ticket_id, action_type, False, reason)
            return False, reason

    def _record(self, ticket_id: str, action_type: str, approved: bool, reason: str):
        self.decision_history.append({
            "ticket_id": ticket_id,
            "action_type": action_type,
            "approved": approved,
            "reason": reason,
        })

