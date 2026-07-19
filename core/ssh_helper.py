"""
core/ssh_helper.py — safe Paramiko SSH client factory

SynthTel connects to many user-provided hosts, so host-key verification
is critical. This helper loads the standard host-key stores and uses
`WarningPolicy` for unknown keys (logs a warning, does not silently add).
Callers that need strict verification can pass `strict=True`.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _load_host_keys(client) -> None:
    """Load system and user known_hosts files if they exist."""
    files = [
        "/etc/ssh/ssh_known_hosts",
        "/etc/ssh/ssh_known_hosts2",
        str(Path.home() / ".ssh" / "known_hosts"),
        str(Path.home() / ".ssh" / "known_hosts2"),
    ]
    for env_key in ("SYNTHTEL_SSH_KNOWN_HOSTS",):
        val = os.environ.get(env_key)
        if val:
            files.append(val)

    for path in files:
        try:
            if os.path.isfile(path):
                client.load_host_keys(path)
        except Exception as exc:
            log.debug("Could not load host keys from %s: %s", path, exc)


def create_ssh_client(
    *,
    strict: bool = False,
):
    """Return a Paramiko SSHClient with safe host-key handling.

    By default unknown keys are accepted with a loud warning but are not
    added to any known_hosts file (avoids silent trust-on-first-use).
    Pass ``strict=True`` to reject unknown keys outright.
    """
    import paramiko

    client = paramiko.SSHClient()
    _load_host_keys(client)
    policy = paramiko.RejectPolicy() if strict else paramiko.WarningPolicy()
    client.set_missing_host_key_policy(policy)
    return client
