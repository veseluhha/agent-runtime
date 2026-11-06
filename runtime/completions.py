"""Translate the harness tool history to Chat Completions and consume a response."""
import json

from .http_api import Fault

SYSTEM = ("Complete the user's task in /work using run_command. Inspect the files you need and write "
          "the requested result to disk. Commands take argv, use sh -c for shell syntax. "
          "After the task is complete, reply with a short final message and no tool calls.")
TOOL = {"type": "function", "function": {"name": "run_command", "description": "Run a command in /work",
        "parameters": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                       "required": ["argv"], "additionalProperties": False}}}


class Conversation:
    def __init__(self, model, complete):
        self.model = model
        self.complete = complete
        self.messages = []
        self.pending = []
        self.usage = []
        self.models = []

    def next_action(self, task, history, timeout):
        if not self.messages:
            self.messages = [{"role": "system", "content": SYSTEM},
                             *({"role": "user", "content": prompt} for prompt in task.messages)]
        if self.pending:
            for call, observation in zip(self.pending, history[-len(self.pending):]):
                content = json.dumps({"exit_code": observation["exit_code"], "output": observation["output"]},
                                     ensure_ascii=False)
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        request = {"model": self.model, "messages": self.messages, "tools": [TOOL], "stream": False}
        try:
            response = self.complete(request, timeout)
        except Fault:
            self.usage.append({})
            raise
        try:
            usage = response.get("usage")
            self.usage.append(usage if isinstance(usage, dict) else {})
            if response.get("model") and response["model"] not in self.models:
                self.models.append(response["model"])
            choice = response["choices"][0]
            msg = choice["message"]
            calls = msg.get("tool_calls") or []
            if msg["role"] != "assistant":
                raise ValueError("unexpected role")
            if not calls and choice["finish_reason"] == "stop":
                return {"done": True}
            if choice["finish_reason"] != "tool_calls" or not 1 <= len(calls) <= 8:
                raise ValueError("expected completed tool calls or stop")
            commands = []
            ids = set()
            for call in calls:
                if not call["id"] or call["id"] in ids or call["type"] != "function" or call["function"]["name"] != "run_command":
                    raise ValueError("invalid tool call")
                ids.add(call["id"])
                argv = json.loads(call["function"]["arguments"])["argv"]
                if not isinstance(argv, list) or not argv or not all(isinstance(s, str) and "\0" not in s for s in argv):
                    raise ValueError("expected argv")
                commands.append(argv)
            # Preserve provider reasoning fields between tool turns
            self.messages.append({k: msg[k] for k in
                                  ("role", "content", "reasoning_content", "reasoning", "reasoning_details", "tool_calls")
                                  if k in msg})
            self.pending = calls
            return {"commands": commands}
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise Fault("provider_protocol", "invalid or incomplete chat completion", 502) from exc

    def tokens(self, key):
        values = [usage.get(key) for usage in self.usage]
        return sum(values) if all(type(value) is int for value in values) else None
