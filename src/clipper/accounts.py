"""Accounts for the editor: sign up, sign in, sessions.

Everything is stored on this computer, in the clipper data folder
(``~/.clipper`` or ``$CLIPPER_HOME``). Passwords are kept only as salted
scrypt hashes and session tokens only as SHA-256 digests, so the file never
holds anything that can be used to sign in.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

SESSION_DAYS = 30
MIN_PASSWORD = 8
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_lock = threading.Lock()
_failures: dict[str, list[float]] = {}   # email -> times of recent wrong passwords


class AccountError(Exception):
    """A problem the person signing up or in can fix; the message is shown to them."""


def data_dir() -> Path:
    return Path(os.environ.get("CLIPPER_HOME") or Path.home() / ".clipper")


def _file() -> Path:
    return data_dir() / "accounts.json"


def _load() -> dict:
    try:
        db = json.loads(_file().read_text())
    except (OSError, ValueError):
        db = {}
    db.setdefault("users", [])
    db.setdefault("sessions", {})
    return db


def _save(db: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT).hex()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public(user: dict) -> dict:
    return {"id": user["id"], "name": user["name"], "email": user["email"], "plan": user.get("plan", "free"),
            "wants_plan": user.get("wants_plan")}


def user_dir(user: dict) -> Path:
    return data_dir() / "users" / user["id"]


def has_accounts() -> bool:
    return bool(_load()["users"])


def list_users() -> list[dict]:
    return [{**public(u), "created": u.get("created")} for u in _load()["users"]]


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters.")


def sign_up(name: str, email: str, password: str) -> dict:
    name, email = (name or "").strip(), (email or "").strip().lower()
    if not name:
        raise AccountError("Enter your name.")
    if not _EMAIL.match(email):
        raise AccountError("Enter a valid email address.")
    _check_password(password)
    with _lock:
        db = _load()
        if any(u["email"] == email for u in db["users"]):
            raise AccountError("There is already an account with that email. Sign in instead.")
        salt = secrets.token_bytes(16)
        user = {"id": secrets.token_hex(8), "name": name[:80], "email": email, "salt": salt.hex(),
                "password": _hash(password, salt), "created": time.strftime("%Y-%m-%d %H:%M")}
        first = not db["users"]
        db["users"].append(user)
        _save(db)
    if first:   # the list of videos from before accounts existed goes to the first account
        old, new = data_dir() / "recent.json", user_dir(user) / "recent.json"
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
    return public(user)


def sign_in(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    now = time.time()
    recent = [t for t in _failures.get(email, []) if now - t < 600]
    if len(recent) >= 8:
        raise AccountError("Too many wrong passwords. Wait ten minutes and try again.")
    user = next((u for u in _load()["users"] if u["email"] == email), None)
    # hash even when the email is unknown, so both cases take the same time and give the same answer
    salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
    ok = hmac.compare_digest(_hash(password or "", salt), user["password"] if user else "")
    if not user or not ok:
        _failures[email] = recent + [now]
        raise AccountError("That email and password do not match an account.")
    _failures.pop(email, None)
    return public(user)


def set_password(email: str, password: str) -> dict:
    """Used by `clipper account reset`; also signs that account out everywhere."""
    email = (email or "").strip().lower()
    _check_password(password)
    with _lock:
        db = _load()
        user = next((u for u in db["users"] if u["email"] == email), None)
        if not user:
            raise AccountError(f"No account with the email {email}.")
        salt = secrets.token_bytes(16)
        user.update(salt=salt.hex(), password=_hash(password, salt))
        db["sessions"] = {k: s for k, s in db["sessions"].items() if s["user"] != user["id"]}
        _save(db)
    return public(user)


def remove(email: str) -> dict:
    email = (email or "").strip().lower()
    with _lock:
        db = _load()
        user = next((u for u in db["users"] if u["email"] == email), None)
        if not user:
            raise AccountError(f"No account with the email {email}.")
        db["users"].remove(user)
        db["sessions"] = {k: s for k, s in db["sessions"].items() if s["user"] != user["id"]}
        _save(db)
    return public(user)


def new_session(user: dict) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        db = _load()
        db["sessions"] = {k: s for k, s in db["sessions"].items() if s["expires"] > now}
        db["sessions"][_digest(token)] = {"user": user["id"], "expires": now + SESSION_DAYS * 86400}
        _save(db)
    return token


def user_for(token: str | None) -> dict | None:
    if not token:
        return None
    db = _load()
    session = db["sessions"].get(_digest(token))
    if not session or session["expires"] < time.time():
        return None
    user = next((u for u in db["users"] if u["id"] == session["user"]), None)
    return public(user) if user else None


def end_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        db = _load()
        if db["sessions"].pop(_digest(token), None):
            _save(db)


def want_plan(user: dict, plan_id: str | None, promo: str | None = None) -> dict:
    """Note which paid plan this account asked to hear about, and the offer running when it asked.
    Nothing is charged or changed."""
    with _lock:
        db = _load()
        stored = next(u for u in db["users"] if u["id"] == user["id"])
        stored["wants_plan"] = plan_id
        stored["wants_promo"] = promo if plan_id else None
        stored["wants_since"] = time.strftime("%Y-%m-%d") if plan_id else None
        _save(db)
    return public(stored)


def _usage_file(user: dict) -> Path:
    return user_dir(user) / "usage.json"


def usage_seconds(user: dict) -> float:
    """Seconds of video this account has opened this calendar month."""
    try:
        return float(json.loads(_usage_file(user).read_text()).get(time.strftime("%Y-%m"), 0))
    except (OSError, ValueError):
        return 0.0


def add_usage(user: dict, seconds: float) -> None:
    with _lock:
        path = _usage_file(user)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        month = time.strftime("%Y-%m")
        data[month] = round(float(data.get(month, 0)) + max(0.0, seconds), 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1))
