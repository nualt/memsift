"""Machine-wide service breaker and token ratio.

ServiceError and PausedError are the public exceptions. state() -> dict;
paused(service) -> bool; require_active(service) -> None; record(service,
outcome) -> dict; resume(service) -> None; update_ratio(characters,
input_tokens) -> float; ratio() -> float; classify(exc) -> Outcome.
"""
