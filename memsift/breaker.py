"""Machine-wide service pauses and token calibration through locked storage."""

from datetime import datetime, timezone
import math

from . import store
from .vendor.logfiles import decision, error
from .vendor.redact import redact

# Equal weight for the previous estimate and the latest observed request ratio.
RATIO_WEIGHT = 0.5


class ServiceError(Exception):
    """Redacted call diagnostics; raise from the original transport exception."""

    def __init__(self, service, reason, status=0, response="", duration=0.0):
        self.service = service
        self.reason = redact(reason)
        self.status = status
        self.response = redact(response)
        self.duration = duration
        super().__init__(self.reason)


class PausedError(Exception):
    """No call was attempted because the service requires explicit resume."""

    def __init__(self, service):
        self.service = service
        super().__init__(service + " is paused")


def _initial_state():
    return {"token_ratio": 3.0, "services": {
        service: {"failures": 0, "paused": False, "reason": ""}
        for service in ("jev", "compactor", "cognee")}}


def state():
    return store.read_json(store.home() / "state.json", _initial_state())


def paused(service):
    return state()["services"][service]["paused"]


def require_active(service):
    if paused(service):
        raise PausedError(service)


def record(service, outcome):
    def change(value):
        current = value["services"][service]
        kind = outcome["kind"]
        if kind == "success":
            current["failures"] = 0
            if not current["paused"]:
                current["reason"] = ""
        elif kind in ("failure", "auth"):
            current["failures"] += 1
            if not current["paused"]:
                current["reason"] = outcome["reason"]
                if kind == "auth" or current["failures"] >= 3:
                    current["paused"] = True
                    current["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif kind != "limited":
            raise ValueError("Invalid service outcome")
        return value

    return store.update_json(store.home() / "state.json", _initial_state(), change)


def resume(service):
    def change(value):
        value["services"][service] = {"failures": 0, "paused": False, "reason": ""}
        return value

    store.update_json(store.home() / "state.json", _initial_state(), change)


def update_ratio(characters, input_tokens):
    """Use character count of the same redacted serialized request estimated."""
    try:
        if (not math.isfinite(characters) or not math.isfinite(input_tokens) or
                characters <= 0 or input_tokens <= 0):
            raise ValueError("invalid Jev token usage")
        observed = characters / input_tokens
        if not math.isfinite(observed) or observed <= 0:
            raise ValueError("invalid Jev token usage")
    except (ValueError, TypeError, OverflowError) as exc:
        error("token-ratio", exc, "jev")
        raise ValueError("invalid Jev token usage") from exc

    def change(value):
        value["token_ratio"] = (1 - RATIO_WEIGHT) * value["token_ratio"] + RATIO_WEIGHT * observed
        return value

    updated = store.update_json(store.home() / "state.json", _initial_state(), change)["token_ratio"]
    decision("token-ratio", {"characters": characters, "input_tokens": input_tokens,
                             "token_ratio": updated})
    return updated


def ratio():
    return state()["token_ratio"]


def classify(exc):
    status = getattr(exc, "status", 0) or getattr(exc, "code", 0)
    if status in (401, 403):
        kind = "auth"
    elif status in (429, 529):
        kind = "limited"
    else:
        kind = "failure"
    reason = str(getattr(exc, "reason", exc)) or type(exc).__name__
    return {"kind": kind, "reason": redact(reason), "status": status}
