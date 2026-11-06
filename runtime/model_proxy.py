"""Private HTTP adapter for an OpenAI-compatible Chat Completions endpoint."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .http_api import Fault, serve


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def dispatch(path, body, headers):
    prefix = "LLM" if any(os.getenv("LLM_" + name) for name in ("MODEL", "BASE_URL", "API_KEY")) else "DEEPSEEK"
    model = os.getenv(prefix + "_MODEL")
    base = os.getenv(prefix + "_BASE_URL", "").rstrip("/")
    key = os.getenv(prefix + "_API_KEY")
    if not (model and base and key):
        raise Fault("configuration", "set LLM_MODEL, LLM_BASE_URL and LLM_API_KEY in .env", 503)
    url = urlsplit(base)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise Fault("configuration", "invalid LLM_BASE_URL", 503)
    if path == "/v0/model":
        return {"model": model}
    if path != "/v0/completion":
        raise Fault("unknown", "unknown endpoint", 404)
    payload = body["request"]
    if payload.get("model") != model:
        raise Fault("configuration", "model ID changed during run", 409)
    req = Request(base + "/chat/completions", json.dumps(payload).encode(),
                  {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with build_opener(NoRedirect()).open(req, timeout=min(180, body["timeout_s"])) as response:
            return json.load(response)
    except HTTPError as exc:
        raise Fault("provider_http", f"provider returned HTTP {exc.code}", 502) from exc
    except (TimeoutError, URLError, OSError, ValueError) as exc:
        raise Fault("provider_transport", "provider timeout, connection failure or invalid JSON", 502) from exc


if __name__ == "__main__":
    serve(dispatch, max_body=4 * 1024 * 1024)
