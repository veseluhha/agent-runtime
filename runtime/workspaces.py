import re
import threading
import time
from dataclasses import dataclass

from .completions import Conversation
from .harness import MANAGER, Sandbox
from .http_api import Fault, post
from .manager import SESSION_TTL
from .model_proxy import PROXY


@dataclass
class Message:
    prompt: str
    result: dict = None
    error: dict = None

    def status(self):
        if self.error is not None:
            return {"state": "failed", **self.error}
        if self.result is not None:
            return {"state": "completed", "result": self.result}
        return {"state": "running"}


class Workspace:
    def __init__(self, session, model):
        self.messages = {}
        self.running = False
        self.failed = False
        self.expiry = threading.Timer(SESSION_TTL, forget_session, args=(session,))
        self.expiry.daemon = True
        self.chat = Conversation(model, self.complete)

    @staticmethod
    def complete(request, timeout):
        return post(PROXY, "/v0/completion", {"request": request, "timeout_s": timeout}, timeout=timeout)


workspaces = {}
workspaces_lock = threading.Lock()


def forget_session(session):
    with workspaces_lock:
        workspace = workspaces.pop(session, None)
    if workspace:
        workspace.expiry.cancel()


def workspace_request(path, body):
    if path == "/v0/workspaces/open":
        if set(body) != {"mount_id", "access"}:
            raise Fault("invalid", "expected mount_id and access")
        model = post(PROXY, "/v0/model", {})["model"]
        session = post(MANAGER, "/v0/provision", body)["session"]
        workspace = Workspace(session, model)
        # The manager bounds container lifetime even if the client or gateway disappears
        with workspaces_lock:
            workspaces[session] = workspace
        workspace.expiry.start()
        return {"session": session, "model": model}

    session = body.get("session")
    if not isinstance(session, str) or not re.fullmatch("[0-9a-f]{32}", session):
        raise Fault("invalid", "invalid session")
    if path == "/v0/workspaces/close" and set(body) == {"session"}:
        forget_session(session)
        post(MANAGER, "/v0/release", {"session": session})
        return {"released": True}
    if path not in ("/v0/workspaces/message", "/v0/workspaces/status"):
        raise Fault("unknown", "unknown endpoint", 404)

    message_id = body.get("message_id")
    if "message_id" in body and (not isinstance(message_id, str) or not re.fullmatch("[0-9a-f]{32}", message_id)):
        raise Fault("invalid", "invalid message_id")
    with workspaces_lock:
        workspace = workspaces.get(session)
        if workspace is None:
            raise Fault("session", "session closed or expired", 410)
        message = workspace.messages.get(message_id)
        if path == "/v0/workspaces/status":
            if set(body) != {"session", "message_id"}:
                raise Fault("invalid", "expected session and message_id")
            return message.status() if message else {"state": "not_received"}
        prompt = body.get("message")
        if set(body) - {"session", "message", "message_id"} or not isinstance(prompt, str) or not prompt.strip():
            raise Fault("invalid", "expected a message")
        if message is not None:
            if message.prompt != prompt:
                raise Fault("message_id", "message_id already used with a different prompt", 409)
            if message.error is not None:
                raise Fault(**message.error)
            if message.result is None:
                raise Fault("in_progress", "message is running", 409)
            return message.result
        if workspace.failed:
            raise Fault("session", "session ended after a failed message", 410)
        if workspace.running:
            raise Fault("busy", "a message is already running", 409)
        message = Message(prompt)
        if message_id is not None:
            workspace.messages[message_id] = message
        workspace.running = True

    try:
        post(MANAGER, "/v0/workdir", {"session": session})
        sandbox = Sandbox(session, time.monotonic() + 75)
        answer = sandbox.run([prompt], workspace.chat.next_action)
        post(MANAGER, "/v0/workdir", {"session": session})
    except Exception as exc:
        fault = exc if isinstance(exc, Fault) else Fault("internal", "server error", 500)
        with workspaces_lock:
            message.error = {"kind": fault.kind, "message": str(fault), "status": fault.status}
            workspace.failed = True
            workspace.running = False
        if message_id is None:
            forget_session(session)
        post(MANAGER, "/v0/release", {"session": session})
        raise
    with workspaces_lock:
        message.result = {"session": session, "answer": answer, "events": sandbox.events}
        workspace.running = False
        return message.result
