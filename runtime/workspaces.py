import re
import threading
import time

from .completions import Conversation
from .harness import MANAGER, Sandbox
from .http_api import Fault, post
from .manager import SESSION_TTL
from .model_proxy import PROXY


class Workspace:
    def __init__(self, session, model):
        self.lock = threading.Lock()
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
    with workspaces_lock:
        workspace = workspaces.get(session)
    if path == "/v0/workspaces/close" and set(body) == {"session"}:
        post(MANAGER, "/v0/release", {"session": session})
        forget_session(session)
        return {"released": True}
    if path != "/v0/workspaces/message":
        raise Fault("unknown", "unknown endpoint", 404)
    prompt = body.get("message")
    if set(body) != {"session", "message"} or not isinstance(prompt, str) or not prompt.strip():
        raise Fault("invalid", "expected a message")
    if workspace is None:
        raise Fault("session", "session closed or expired", 410)
    if not workspace.lock.acquire(blocking=False):
        raise Fault("busy", "a message is already running", 409)
    try:
        post(MANAGER, "/v0/workdir", {"session": session})
        sandbox = Sandbox(session, time.monotonic() + 75)
        answer = sandbox.run([prompt], workspace.chat.next_action)
        post(MANAGER, "/v0/workdir", {"session": session})
        return {"session": session, "answer": answer, "events": sandbox.events}
    except Exception:
        forget_session(session)
        post(MANAGER, "/v0/release", {"session": session})
        raise
    finally:
        workspace.lock.release()
