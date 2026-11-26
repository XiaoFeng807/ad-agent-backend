"""Application budget for one homepage SSE turn, shared by its model calls."""

import json
from threading import Lock

from backend.observability import add_trace_step


def tool_call_fields(call):
    if isinstance(call, dict):
        function = call["function"]
        return call["id"], function["name"], function["arguments"]
    return call.id, call.function.name, call.function.arguments


def _json_object(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_unset=True, exclude=getattr(value, "__api_exclude__", None))
    # SDK-compatible fixed models can expose tool calls as plain objects.
    call_id, name, arguments = tool_call_fields(value)
    return {"id": call_id, "type": getattr(value, "type", "function"),
            "function": {"name": name, "arguments": arguments}}


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          default=_json_object).encode("utf-8"))


def _older_turns(messages):
    """Yield prior conversation turns from oldest to newest, retaining system messages."""
    latest_user = next((i for i in range(len(messages) - 1, -1, -1)
                        if messages[i]["role"] == "user"), 0)
    turn = []
    for i in range(latest_user):
        role = messages[i]["role"]
        if role == "system":
            continue
        if role == "user" and turn:
            yield turn
            turn = []
        turn.append(i)
    if turn:
        yield turn


class ContextBudgetExceeded(RuntimeError):
    code = "context_budget_exceeded"

    def __init__(self):
        super().__init__(self.code)


class ExecutionStopped(RuntimeError):
    @property
    def code(self):
        return self.args[0]


class ChatSessionInvalid(ExecutionStopped):
    def __init__(self):
        super().__init__("chat_session_invalid")


class RequestBudget:
    def __init__(self, trace_id, *, limit=32000, output_limit=3000, safety_margin=512):
        self.trace_id = trace_id
        self.limit = limit
        self.output_limit = output_limit
        self.safety_margin = safety_margin
        self.execution = None
        self._lock = Lock()
        self._blocked = False

    def raise_if_blocked(self):
        """Preserve a required rejection before handling a concurrent failure."""
        with self._lock:
            if self._blocked:
                raise ContextBudgetExceeded()

    def prepare(self, payload, *, role, step, without_memory=None, optional=False, preflight=False):
        with self._lock:
            if self._blocked:
                raise ContextBudgetExceeded()

            # Match the JSON body shape, including adapter defaults in extra_body.
            # Timeout is a transport option. One UTF-8 JSON byte counts as one
            # estimated input token, independent of the installed tokenizer.
            extra_body = dict(payload.get("extra_body") or {})
            body = {key: value for key, value in payload.items() if key not in {"timeout", "extra_body"}}
            body.update(extra_body)
            body["messages"] = [{key: value for key, value in message.items() if key != "priority"}
                                for message in body["messages"]]
            output_key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
            output = min(body.get(output_key) or self.output_limit, self.output_limit)
            body[output_key] = payload[output_key] = output
            if output_key in extra_body:
                extra_body[output_key] = output

            initial_input = input_tokens = _json_bytes(body)
            available = self.limit - output - self.safety_margin
            memory_removed = False
            dropped_turns = 0
            if input_tokens > available and without_memory is not None:
                messages = body["messages"]
                body["messages"] = [{"role": "system", "content": without_memory}, *messages[1:]]
                input_tokens = _json_bytes(body)
                memory_removed = True
            if input_tokens > available:
                messages = body["messages"]
                removed = set()
                for turn in _older_turns(messages):
                    removed.update(turn)
                    # Each removed array entry also removes one comma. The current user remains.
                    input_tokens -= sum(_json_bytes(messages[i]) + 1 for i in turn)
                    dropped_turns += 1
                    if input_tokens <= available:
                        break
                body["messages"] = [message for i, message in enumerate(messages) if i not in removed]

            exceeded = input_tokens > available
            if exceeded or not preflight:
                add_trace_step(
                    self.trace_id, "context_budget", step, role=role,
                    status=("skipped" if optional else "error") if exceeded else "ok",
                    error_code=ContextBudgetExceeded.code if exceeded else None,
                    budget={
                        "estimator": "utf8_json_bytes_v1", "limit": self.limit,
                        "input_before_trim": initial_input, "input_tokens": input_tokens,
                        "output_reserved": output, "safety_margin": self.safety_margin,
                        "total": input_tokens + output + self.safety_margin,
                        "dropped_turns": dropped_turns, "memory_removed": memory_removed,
                    },
                )
            if exceeded:
                self._blocked = not optional
                raise ContextBudgetExceeded()
            payload["messages"] = body["messages"]
            if "messages" in extra_body:
                extra_body["messages"] = body["messages"]
            if extra_body:
                payload["extra_body"] = extra_body
            return payload
