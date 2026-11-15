"""Live adapter and an explicitly simulated, offline triage planner."""

import json
import os
import re
from typing import Dict, List, Optional, Tuple

PRICE_INPUT_1M = 0.15
PRICE_OUTPUT_1M = 0.60


class BaseLLMClient:
    def complete(self, messages: List[Dict[str, str]]) -> Tuple[str, int, int, float]:
        raise NotImplementedError


class OpenAILLMClient(BaseLLMClient):
    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"), timeout=30.0, max_retries=0)

    def complete(self, messages: List[Dict[str, str]]) -> Tuple[str, int, int, float]:
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=0.0, max_tokens=800,
        )
        content = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else len(str(messages)) // 4
        tokens_out = usage.completion_tokens if usage else len(content) // 4
        cost = tokens_in / 1_000_000 * PRICE_INPUT_1M + tokens_out / 1_000_000 * PRICE_OUTPUT_1M
        return content, tokens_in, tokens_out, cost


class MockDeterministicLLMClient(BaseLLMClient):
    """Rule-based simulator, not a model-quality or injection-security benchmark.

    Decisions inspect the retrieved ticket and successful typed observations.
    Policy prose, earlier assistant text and ticket IDs are not routing signals.
    """

    def complete(self, messages):
        text = self._plan(messages)
        tokens_in = max(50, sum(len(m["content"]) for m in messages) // 4)
        tokens_out = max(1, len(text) // 4)
        cost = tokens_in / 1_000_000 * PRICE_INPUT_1M + tokens_out / 1_000_000 * PRICE_OUTPUT_1M
        return text, tokens_in, tokens_out, cost

    @staticmethod
    def _action(name, **arguments):
        return "Thought: Continue the verified ticket workflow.\nAction: " + name + " | " + json.dumps(arguments)

    @staticmethod
    def _final(text):
        return "Thought: Report only the observed outcome.\nFinal Answer: " + text

    def _plan(self, messages):
        # Only the original user task selects the ticket, never customer content.
        prompt = next((m["content"] for m in messages if m["role"] == "user"), "")
        match = re.search(r"\bTKT-[A-Za-z0-9_-]+", prompt)
        if not match:
            return self._final("A ticket ID is required; no customer records were changed.")
        ticket_id = match.group()
        observations = []
        for message in messages:
            if message["role"] == "user" and message["content"].startswith("Observation: "):
                try:
                    result = json.loads(message["content"][13:])
                except (ValueError, TypeError):
                    continue
                if isinstance(result, dict) and "tool_name" in result and "is_error" in result:
                    observations.append(result)

        def success(name):
            return next((o for o in reversed(observations) if o["tool_name"] == name and not o["is_error"]), None)

        def record(name):
            result = success(name)
            return json.loads(result["output"]) if result else None

        # Retry only transient/read schema failures, with the original identity.
        if observations and observations[-1]["is_error"]:
            failed = observations[-1]
            name = failed["tool_name"]
            attempts = sum(o["tool_name"] == name and o["is_error"] for o in observations)
            if name == "get_ticket":
                if failed.get("error_type") == "EmptyResultError":
                    return self._final(f"Ticket {ticket_id} was not found. No other ticket was processed.")
                if attempts <= 2:
                    return self._action(name, ticket_id=ticket_id)
                return self._final(f"Ticket {ticket_id} could not be read after bounded retries.")
            if name == "check_customer_status" and failed.get("error_type") == "ToolDownError" and attempts <= 2:
                ticket = record("get_ticket")
                return self._action(name, customer_id=ticket["customer_id"])
            if name == "update_ticket_status" and attempts <= 2:
                # Recompute the intended transition below; do not claim it succeeded.
                pass
            elif name not in {"search_knowledge_base"}:
                if record("get_ticket") and not success("update_ticket_status"):
                    return self._action("update_ticket_status", ticket_id=ticket_id, status="ESCALATED", tags=["action_failed"])
                return self._final(f"Ticket {ticket_id} needs human review; the requested action did not succeed.")

        ticket = record("get_ticket")
        if ticket is None:
            return self._action("get_ticket", ticket_id=ticket_id)
        text = (ticket["subject"] + "\n" + ticket["body"]).lower()
        suspicious = (
            "ignore all previous instructions", "developer mode", "system override",
            "exfiltrate", "delete database", "grant admin access", "bypass human checkpoint",
        )

        def finish(status, tags, answer):
            if not success("update_ticket_status"):
                return self._action("update_ticket_status", ticket_id=ticket_id, status=status, tags=tags)
            return self._final(answer)

        if any(signature in text for signature in suspicious):
            return finish("ESCALATED", ["SECURITY_ALERT", "INJECTION_ATTEMPT"],
                          f"Ticket {ticket_id} was escalated for suspected prompt injection. No high-impact action was executed.")

        if "password" in text or "credentials" in text:
            intent, query = "RESET_PASSWORD", "password"
        elif "cancel" in text:
            intent, query = "CANCEL_ORDER", "orders"
        elif "refund" in text or "adjustment" in text:
            intent, query = "ISSUE_REFUND", "refund"
        else:
            intent = None
            query = "shipping" if any(w in text for w in ("package", "shipping")) else "warranty"
        feedback = any(w in text for w in ("thank you", "thanks", "arrived in great condition"))
        account_enquiry = intent is None and any(w in text for w in ("invoice", "receipt", "perks", "current plan", "delivery address"))
        if not feedback and not account_enquiry and not success("search_knowledge_base"):
            failures = sum(o["tool_name"] == "search_knowledge_base" and o["is_error"] for o in observations)
            if failures < 2:
                return self._action("search_knowledge_base", query=query)
            return finish("ESCALATED", ["policy_unavailable"], "No matching policy was found; ticket escalated without a high-impact action.")

        customer = record("check_customer_status")
        if not feedback and customer is None:
            return self._action("check_customer_status", customer_id=ticket["customer_id"])

        if intent is None:
            if feedback:
                return finish("RESOLVED", ["feedback"], "Customer feedback acknowledged; no financial action was needed.")
            return finish("ESCALATED", ["needs_review"], "Enquiry triaged for human review; no refund, cancellation or account change was performed.")

        if success("execute_action"):
            return finish("RESOLVED", ["completed", "verified"], f"Ticket {ticket_id}: requested {intent} completed in the sandbox.")

        if intent == "RESET_PASSWORD":
            emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
            if any(email.casefold() != customer["email"].casefold() for email in emails):
                return finish("ESCALATED", ["unverified_recipient"], "Password reset to an unverified address was blocked.")
            payload = {"target_email": customer["email"]}
        else:
            order_ids = re.findall(r"\bORD-\d+\b", ticket["subject"] + " " + ticket["body"])
            order = next((o for o in customer["orders"] if o["order_id"] in order_ids), None)
            if intent == "CANCEL_ORDER":
                if order is None or order["status"] not in {"PENDING", "PROCESSING"}:
                    return finish("ESCALATED", ["cancellation_ineligible"], "Cancellation could not be authorized for this customer and order state.")
                payload = {"order_id": order["order_id"]}
            else:
                amounts = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", ticket["body"])
                amount = float(amounts[0].replace(",", "")) if amounts else (float(order["amount"]) if order else None)
                if amount is not None and amount <= 0:
                    return finish("RESOLVED", ["invalid_amount"], "No financial transaction was made for a zero-dollar adjustment.")
                if amount is None or amount > 150 or order is None or amount > float(order["amount"]):
                    return finish("ESCALATED", ["refund_review"], "Refund requires human review; no authorized order amount could be established within the limit.")
                payload = {"amount": amount, "reason": "Customer requested refund"}
        return self._action(
            "execute_action", ticket_id=ticket_id, action_type=intent, payload=payload,
            idempotency_key=f"{ticket_id}-{intent}-001",
        )

