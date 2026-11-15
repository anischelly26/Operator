"""Faults are actually injected, rather than merely mentioned in prompts."""

from agent.models import ToolResult
from agent.tools import ToolRegistry


class FaultInjectingTools(ToolRegistry):
    def __init__(self, *args, fail_tool=None, error_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_tool = fail_tool
        self.error_type = error_type
        self.injected = False

    def execute(self, tool_call):
        if tool_call.name == self.fail_tool and not self.injected:
            self.injected = True
            result = ToolResult(
                tool_name=tool_call.name, is_error=True, error_type=self.error_type,
                output=f"Injected {self.error_type}: retry the intended read with valid arguments.",
            )
            self.call_logs.append(result)
            return result
        return super().execute(tool_call)
