"""Shared configuration for the Cognee Claude Code plugin.

Loads settings from (in priority order):
  1. Environment variables (runtime overrides)
  2. Config file (~/.cognee-plugin/config.json)
  3. Defaults

Config file is created on first SessionStart if it doesn't exist.

Supports three modes:
  - Local: Cognee runs in-process (SQLite + LanceDB + Kuzu)
  - Cloud: Connect to Cognee Cloud via cognee.serve()
  - Server: Legacy — direct base_url (kept for backward compat)
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


_CONFIG_DIR = Path.home() / ".cognee-plugin"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

_DEFAULTS = {
    "dataset": "claude_sessions",
    "session_strategy": "per-directory",  # per-directory | git-branch | static
    "session_prefix": "cc",
    "top_k": 3,
    # Cloud / remote
    "service_url": "",
    "api_key": "",
    # Local mode
    "llm_api_key": "",
    "llm_model": "",
    # Legacy server mode
    "base_url": "",
}

# Env var overrides (env var name → config key)
_ENV_MAP = {
    "COGNEE_PLUGIN_DATASET": "dataset",
    "COGNEE_SESSION_STRATEGY": "session_strategy",
    "COGNEE_SESSION_PREFIX": "session_prefix",
    "COGNEE_SERVICE_URL": "service_url",
    "COGNEE_API_KEY": "api_key",
    "COGNEE_BASE_URL": "base_url",
    "LLM_API_KEY": "llm_api_key",
    "LLM_MODEL": "llm_model",
    # Legacy compat
    "COGNEE_SESSION_ID": "_static_session_id",
    "COGNEE_PLUGIN_DATASET": "dataset",
}


def load_config() -> dict:
    """Load merged config: defaults → file → env vars."""
    config = dict(_DEFAULTS)

    # Layer 2: config file
    if _CONFIG_FILE.exists():
        try:
            file_cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass

    # Layer 3: env vars (highest priority)
    for env_key, config_key in _ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            config[config_key] = val

    return config


def save_config(config: dict) -> None:
    """Write config to disk. Creates directory if needed."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Only save non-secret, non-default values
    to_save = {k: v for k, v in config.items()
               if not k.startswith("_") and v and v != _DEFAULTS.get(k)}
    _CONFIG_FILE.write_text(json.dumps(to_save, indent=2), encoding="utf-8")


def get_session_id(config: dict, cwd: Optional[str] = None) -> str:
    """Compute session ID based on the configured strategy.

    Strategies:
      - per-directory: prefix + hash of cwd → stable per-project
      - git-branch: prefix + hash of cwd + branch → stable per-branch
      - static: uses COGNEE_SESSION_ID env var or fallback
    """
    # Legacy: explicit static session ID
    static_id = config.get("_static_session_id", "")
    if static_id:
        return static_id

    strategy = config.get("session_strategy", "per-directory")
    prefix = config.get("session_prefix", "cc")

    if cwd is None:
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())

    if strategy == "static":
        return f"{prefix}_session"

    # Per-directory: hash the cwd for a stable, short ID
    dir_hash = hashlib.sha256(cwd.encode()).hexdigest()[:12]
    dir_name = Path(cwd).name

    if strategy == "git-branch":
        branch = _get_git_branch(cwd)
        if branch:
            return f"{prefix}_{dir_name}_{branch}_{dir_hash}"

    return f"{prefix}_{dir_name}_{dir_hash}"


def get_dataset(config: dict) -> str:
    """Get the dataset name from config."""
    return config.get("dataset", "claude_sessions")


def is_cloud_mode(config: dict) -> bool:
    """Check if cloud/remote mode is configured."""
    return bool(config.get("service_url"))


def is_local_mode(config: dict) -> bool:
    """Check if local mode (has LLM key, no cloud URL)."""
    return bool(config.get("llm_api_key")) and not is_cloud_mode(config)


_IDENTITY_EMAIL = "claude-code@cognee.local"
_IDENTITY_PASSWORD = "claude-code-plugin"


async def ensure_identity():
    """Ensure the Claude Code integration has its own user identity in Cognee.

    Creates a user with email 'claude-code@cognee.local' if it doesn't exist.
    Returns the User object for passing to SDK calls.
    """
    from cognee.modules.users.methods import create_user, get_user_by_email

    user = await get_user_by_email(_IDENTITY_EMAIL)
    if user:
        return user

    try:
        user = await create_user(
            email=_IDENTITY_EMAIL,
            password=_IDENTITY_PASSWORD,
            is_verified=True,
            is_active=True,
        )
        print(f"cognee-plugin: created identity {_IDENTITY_EMAIL} (id={user.id})", file=sys.stderr)
        return user
    except Exception:
        # UserAlreadyExists or other race — try fetching again
        user = await get_user_by_email(_IDENTITY_EMAIL)
        if user:
            return user
        # Last resort: fall back to default user
        from cognee.modules.users.methods import get_default_user
        return await get_default_user()


async def ensure_cognee_ready(config: dict) -> None:
    """Configure cognee for the active mode (cloud or local).

    Call once per session (in SessionStart). Subsequent scripts
    in the same process inherit the configuration.
    """
    import cognee

    if is_cloud_mode(config):
        url = config["service_url"]
        api_key = config.get("api_key", "")
        kwargs = {"url": url}
        if api_key:
            kwargs["api_key"] = api_key
        await cognee.serve(**kwargs)
        print(f"cognee-plugin: connected to {url}", file=sys.stderr)
    elif config.get("llm_api_key"):
        cognee.config.set_llm_api_key(config["llm_api_key"])
        if config.get("llm_model"):
            cognee.config.set_llm_model(config["llm_model"])


def _get_git_branch(cwd: str) -> str:
    """Get current git branch, or empty string if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            # Sanitize for use in session IDs
            return branch.replace("/", "-").replace(" ", "-")[:40]
    except Exception:
        pass
    return ""
