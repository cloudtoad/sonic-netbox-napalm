"""TPCM (Third-Party Container Management) install and status.

Install via SCP + SSH (image tarball transfer, docker load, tpcm install).
The RESTCONF tpcm-install RPC tries docker pull which fails on the VS
platform because the TPCM Docker daemon is not VRF-aware (no internet).
RESTCONF tpcm-list/tpcm-show RPCs are also broken on 4.4.x VS.
"""

import os
import subprocess
import time

from . import log
from .ssh import ssh_cmd

TPCM_POLL_INTERVAL = 10
TPCM_POLL_TIMEOUT = 180

# Local cache of saved image tarballs (image_name -> local_path)
_image_cache: dict[str, str] = {}


def _ensure_image_tarball(image: str) -> str:
    """Ensure we have a local tarball for the given Docker image.

    Pulls the image on the local machine (if not cached), saves it as
    a gzipped tarball in /tmp, and returns the local path.
    """
    if image in _image_cache and os.path.exists(_image_cache[image]):
        return _image_cache[image]

    safe_name = image.replace("/", "_").replace(":", "_")
    tarball = f"/tmp/tpcm_{safe_name}.tar.gz"

    if os.path.exists(tarball):
        _image_cache[image] = tarball
        return tarball

    log(f"  Pulling image {image} locally...")
    rc = subprocess.run(["docker", "pull", image],
                        capture_output=True, text=True, timeout=300)
    if rc.returncode != 0:
        raise RuntimeError(f"docker pull failed: {rc.stderr}")

    log(f"  Saving image to {tarball}...")
    with open(tarball, "wb") as f:
        proc = subprocess.Popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        gzip = subprocess.Popen(
            ["gzip"],
            stdin=proc.stdout, stdout=f, stderr=subprocess.PIPE,
        )
        proc.stdout.close()
        gzip.communicate(timeout=300)
        proc.wait()

    _image_cache[image] = tarball
    log(f"  Saved {tarball} ({os.path.getsize(tarball) // 1024 // 1024}MB)")
    return tarball


def _scp_to_switch(local_path: str, ip: str, remote_path: str,
                   user: str, password: str) -> bool:
    """SCP a file to the switch."""
    result = subprocess.run(
        ["sshpass", "-p", password, "scp",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR",
         "-o", "Compression=no",
         local_path, f"{user}@{ip}:{remote_path}"],
        capture_output=True, text=True, timeout=600,
    )
    return result.returncode == 0


def install_tpcm(ip: str, docker_name: str, image: str, args: str,
                 auth: tuple) -> bool:
    """Install a TPCM container via SCP + ``tpcm install ... file``.

    Flow:
    1. Pull image locally, save as tarball
    2. SCP tarball to /tmp on the switch
    3. ``tpcm install name <name> file <path>`` (loads + installs in one step)
    """
    try:
        tarball = _ensure_image_tarball(image)
    except Exception as e:
        log(f"  {ip}: failed to prepare image tarball: {e}")
        return False

    remote_tarball = f"/tmp/{os.path.basename(tarball)}"

    # SCP to switch
    log(f"  {ip}: uploading image ({os.path.getsize(tarball) // 1024 // 1024}MB)...")
    if not _scp_to_switch(tarball, ip, remote_tarball, auth[0], auth[1]):
        log(f"  {ip}: SCP upload failed")
        return False

    # Install via tpcm CLI — 'file' method loads and installs in one step
    tpcm_cmd = (
        f"sudo tpcm install name {docker_name} file {remote_tarball}"
        f" --args '{args}'"
        f" --start-after-system-ready True -y"
    )
    rc, out = ssh_cmd(ip, tpcm_cmd, auth[0], auth[1], timeout=120)
    if rc != 0:
        log(f"  {ip}: tpcm install failed: {out}")
        return False
    log(f"  {ip}: TPCM '{docker_name}' installed")

    # Clean up remote tarball
    ssh_cmd(ip, f"rm -f {remote_tarball}", auth[0], auth[1], timeout=10)
    return True


def get_tpcm_status_ssh(ip: str, auth: tuple) -> dict[str, str]:
    """Get TPCM container statuses via SSH 'sudo tpcm list'.

    Returns {docker_name: full_status_line}.
    """
    rc, out = ssh_cmd(ip, "sudo tpcm list", auth[0], auth[1], timeout=15)
    if rc != 0:
        return {}
    result = {}
    for line in out.splitlines():
        if line.startswith("CONTAINER") or not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0]] = line
    return result


def poll_tpcm_running(ip: str, docker_name: str, auth: tuple,
                      timeout: int = TPCM_POLL_TIMEOUT) -> bool:
    """Poll 'sudo tpcm list' via SSH until container shows 'Up'."""
    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        status_map = get_tpcm_status_ssh(ip, auth)
        if docker_name in status_map:
            line = status_map[docker_name]
            if "Up" in line:
                log(f"  {ip}: TPCM '{docker_name}' is running")
                return True
            if attempt % 6 == 1:
                log(f"  {ip}: TPCM '{docker_name}' status: {line.strip()}")
        else:
            if attempt % 6 == 1:
                log(f"  {ip}: TPCM '{docker_name}' not found yet")

        time.sleep(TPCM_POLL_INTERVAL)

    log(f"  {ip}: TPCM '{docker_name}' did not reach running within {timeout}s")
    return False


def check_tpcm_reboot_needed(ip: str, docker_name: str, auth: tuple) -> bool:
    """Check TPCM container logs for 'reboot is required' message.

    The qinq-agent patches the BCM config on first start and broadcasts
    a reboot notice.  This checks the container logs for that message.
    """
    rc, out = ssh_cmd(
        ip,
        f"sudo docker -H unix:///run/docker-default.socket logs {docker_name} 2>&1"
        " | grep -i 'reboot is required'",
        user=auth[0], password=auth[1],
        timeout=15,
    )
    if rc == 0 and out.strip():
        log(f"  {ip}: TPCM '{docker_name}' says reboot is required")
        return True
    return False


def reboot_sonic(ip: str, auth: tuple) -> None:
    """Reboot SONiC node via SSH."""
    log(f"  {ip}: rebooting...")
    ssh_cmd(
        ip,
        "sudo nohup bash -c 'sleep 2 && reboot' </dev/null &>/dev/null &",
        user=auth[0], password=auth[1],
        timeout=10,
    )
