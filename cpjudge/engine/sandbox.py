"""Sandboxing and isolation module for CPJudge.

Enforces:
- CPU time limit (RLIMIT_CPU)
- Memory limit (RLIMIT_AS)
- File output limit (RLIMIT_FSIZE)
- Max processes / thread limit (RLIMIT_NPROC)
- Prevention of core dumps (RLIMIT_CORE = 0)
- Execution in fresh isolated directory as low-privilege user
- Process group isolation (os.setsid) for clean teardown of child processes/fork bombs
- Optional bubblewrap (bwrap) isolation with network namespace isolation (--unshare-net)
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, List, Optional, Tuple

try:
    import pwd
    import resource
    HAS_POSIX = True
except ImportError:
    HAS_POSIX = False
    resource = None
    pwd = None


def get_sandbox_user_ids(username: str = "judge_sandbox") -> Tuple[Optional[int], Optional[int]]:
    """Gets uid and gid for the low-privilege sandbox user."""
    if not HAS_POSIX or not pwd:
        return None, None
    try:
        user_entry = pwd.getpwnam(username)
        return user_entry.pw_uid, user_entry.pw_gid
    except KeyError:
        # Fallback to 'nobody' or current user
        try:
            nobody_entry = pwd.getpwnam("nobody")
            return nobody_entry.pw_uid, nobody_entry.pw_gid
        except KeyError:
            return None, None


def is_bwrap_available() -> bool:
    """Checks if bubblewrap (bwrap) is installed and functional."""
    bwrap_path = shutil.which("bwrap")
    if not bwrap_path:
        return False
    try:
        res = subprocess.run(
            [bwrap_path, "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--unshare-all", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def build_bwrap_args(
    command: List[str],
    work_dir: str | Path,
    read_only_root: bool = True,
) -> List[str]:
    """Wraps an execution command inside bubblewrap sandbox with network disabled."""
    work_dir = str(Path(work_dir).resolve())
    bwrap_cmd = [
        "bwrap",
        "--unshare-net",      # Disable all network access
        "--unshare-pid",      # New PID namespace
        "--unshare-ipc",      # New IPC namespace
        "--unshare-uts",      # New UTS namespace
        "--die-with-parent",  # Kill child if parent exits
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    if read_only_root:
        bwrap_cmd.extend(["--ro-bind", "/", "/"])
    bwrap_cmd.extend([
        "--bind", work_dir, work_dir,
        "--chdir", work_dir,
        "--",
    ])
    bwrap_cmd.extend(command)
    return bwrap_cmd


def create_preexec_fn(
    cpu_limit_seconds: float,
    memory_limit_bytes: int,
    output_limit_bytes: int,
    nproc_limit: int = 32,
    sandbox_user: Optional[str] = "judge_sandbox",
) -> Optional[Callable[[], None]]:
    """Creates a POSIX preexec_fn that sets resource limits and drops privileges."""
    if not HAS_POSIX or resource is None:
        return None

    target_uid, target_gid = get_sandbox_user_ids(sandbox_user or "judge_sandbox")
    current_uid = os.geteuid() if hasattr(os, "geteuid") else -1

    def preexec() -> None:
        # Create a new session and process group so process tree can be killed cleanly
        os.setsid()

        # 1. CPU Time Limit
        # Soft limit causes SIGXCPU, hard limit causes SIGKILL 1s later
        cpu_soft = max(1, int(cpu_limit_seconds))
        cpu_hard = cpu_soft + 1
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft, cpu_hard))
        except (ValueError, resource.error):
            pass

        # 2. Address Space (Memory) Limit
        if memory_limit_bytes > 0:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
            except (ValueError, resource.error):
                pass

        # 3. File Output Size Limit
        if output_limit_bytes > 0:
            try:
                # Add small margin for file operations before SIGXFSZ
                resource.setrlimit(resource.RLIMIT_FSIZE, (output_limit_bytes, output_limit_bytes))
            except (ValueError, resource.error):
                pass

        # 4. Process Count Limit (Protects against fork bombs)
        if nproc_limit > 0:
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))
            except (ValueError, resource.error):
                pass

        # 5. Disable Core Dumps (Prevent disk filling on segfault)
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, resource.error):
            pass

        # 6. Drop privileges if currently running as root
        if current_uid == 0 and target_uid is not None and target_gid is not None:
            try:
                os.setgid(target_gid)
                os.setuid(target_uid)
            except OSError:
                pass

    return preexec
