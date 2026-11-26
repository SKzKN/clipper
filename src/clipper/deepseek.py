"""DeepSeek client: the model that chooses clips and plans edits.

Uses the Chat Completion API (``POST {base}/chat/completions``, OpenAI format)
as documented at https://api-docs.deepseek.com: JSON Output for clip choice,
Tool Calls for editing. Only text leaves the machine: the transcript and the
state of the edit. Video and audio files are never sent.

The key comes from ``$DEEPSEEK_API_KEY`` or from the file written by
``clipper deepseek set-key`` (in the clipper data folder, readable only by you).
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from .accounts import data_dir
from .ffmpeg import ClipperError

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"      # the other current model is deepseek-v4-pro


def base_url() -> str:
    return os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE).rstrip("/")


def model() -> str:
    return os.environ.get("CLIPPER_DEEPSEEK_MODEL", DEFAULT_MODEL)


def key_file() -> Path:
    return data_dir() / "deepseek.key"


def api_key() -> str | None:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        return key_file().read_text().strip() or None
    except OSError:
        return None


def is_ready() -> bool:
    return bool(api_key())


def status() -> dict:
    source = "environment" if os.environ.get("DEEPSEEK_API_KEY", "").strip() else "file" if is_ready() else None
    return {"provider": "DeepSeek", "ready": is_ready(), "model": model(), "key_from": source}


def set_key(key: str) -> None:
    key = (key or "").strip()
    if not key:
        raise ClipperError("No key given. Nothing was changed.")
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n")
    os.chmod(path, 0o600)


def remove_key() -> bool:
    try:
        key_file().unlink()
        return True
    except OSError:
        return False


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_PROBLEMS = {
    401: "DeepSeek refused the API key. Set it again with `clipper deepseek set-key`.",
    402: "The DeepSeek account has run out of balance. Top it up at platform.deepseek.com.",
    429: "DeepSeek is rate-limiting this account. Wait a moment and try again.",
}


def chat(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False, effort: str | None = None,
         max_tokens: int = 16000, timeout: float = 240.0) -> dict:
    """One Chat Completion call. Returns the assistant message (a dict with content and maybe tool_calls)."""
    key = api_key()
    if not key:
        raise ClipperError("DeepSeek is not connected. Run `clipper deepseek set-key`, or set DEEPSEEK_API_KEY.")
    body: dict = {"model": model(), "messages": messages, "max_tokens": max_tokens, "stream": False,
                  "thinking": {"type": "enabled"},
                  "reasoning_effort": effort or os.environ.get("CLIPPER_DEEPSEEK_EFFORT", "high")}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(base_url() + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    context = _ssl_context() if request.full_url.startswith("https") else None
    last = "no answer"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                data = json.loads(response.read())
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ClipperError("DeepSeek's answer was cut off before it finished. Try a shorter video or request.")
            message = choice["message"]
            message["_model"] = data.get("model", model())
            return message
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = (json.loads(exc.read()).get("error") or {}).get("message", "")
            except (ValueError, AttributeError):
                pass
            if exc.code in (429, 500, 503) and attempt < 2:
                last = _PROBLEMS.get(exc.code) or f"DeepSeek had a server problem ({exc.code})."
                time.sleep(2 + 4 * attempt)
                continue
            raise ClipperError(_PROBLEMS.get(exc.code) or f"DeepSeek returned an error ({exc.code}). {detail}".strip()) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = f"Could not reach DeepSeek ({getattr(exc, 'reason', exc)})."
            if attempt < 2:
                time.sleep(2)
                continue
        except (KeyError, IndexError, ValueError) as exc:
            raise ClipperError(f"DeepSeek sent an answer clipper could not read ({type(exc).__name__}).") from exc
    raise ClipperError(last)


def chat_json(messages: list[dict], **kwargs) -> dict:
    """A call in JSON Output mode. The docs warn the content can come back blank; that is retried once."""
    for _ in range(2):
        text = (chat(messages, json_mode=True, **kwargs).get("content") or "").strip()
        if text:
            try:
                return json.loads(text)
            except ValueError:
                continue
    raise ClipperError("DeepSeek did not return usable JSON.")
