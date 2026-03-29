"""SSH helper for Debian hosts."""

import subprocess


def ssh_cmd(ip: str, cmd: str, user: str = "debian", password: str = "debian",
            timeout: int = 15) -> tuple[int, str]:
    """Run command on Debian host via SSH. Returns (returncode, stdout+stderr)."""
    result = subprocess.run(
        ["sshpass", "-p", password, "ssh",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR",
         f"{user}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr
