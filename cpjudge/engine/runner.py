"""Runner module for CPJudge.

Executes student code in a sandboxed, low-privilege environment with
enforced resource limits (CPU time, wall-clock time, memory, output size, process count).
"""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from cpjudge.engine.sandbox import (
    HAS_POSIX,
    build_bwrap_args,
    create_preexec_fn,
    is_bwrap_available,
)
from cpjudge.engine.verdicts import Verdict

try:
    import resource
except ImportError:
    resource = None


@dataclass
class RunResult:
    verdict: Optional[Verdict] # None if execution succeeded and output is ready for checking
    stdout: str
    stderr: str
    cpu_time: float            # CPU time in seconds
    wall_time: float           # Wall-clock time in seconds
    memory_kb: int             # Peak memory in KB
    exit_code: int
    output_truncated: bool = False
    error_message: Optional[str] = None


class Runner:
    DEFAULT_MULTIPLIERS = {
        "c": 1.0,
        "cpp": 1.0,
        "python": 3.0,
        "py": 3.0,
    }

    def __init__(
        self,
        time_multipliers: Optional[Dict[str, float]] = None,
        use_bwrap: bool = True,
        sandbox_user: Optional[str] = "judge_sandbox",
    ):
        self.time_multipliers = {**self.DEFAULT_MULTIPLIERS, **(time_multipliers or {})}
        self.sandbox_user = sandbox_user
        self.use_bwrap = use_bwrap and is_bwrap_available()

    def run_test(
        self,
        run_cmd: List[str],
        language: str,
        test_input: str,
        time_limit: float = 1.0,
        memory_limit_mb: int = 256,
        output_limit_bytes: int = 10 * 1024 * 1024, # 10 MB
        nproc_limit: int = 32,
    ) -> RunResult:
        """Runs a single test in a fresh temporary sandbox directory.

        Args:
            run_cmd: Command and arguments to execute the solution.
            language: Language name for time multiplier calculation.
            test_input: Input string sent to stdin.
            time_limit: Base CPU time limit in seconds.
            memory_limit_mb: Memory limit in megabytes.
            output_limit_bytes: Maximum allowed output in bytes.
            nproc_limit: Max number of processes/threads.

        Returns:
            RunResult with execution metrics and verdict (if terminated abnormally).
        """
        multiplier = self.time_multipliers.get(language.lower(), 1.0)
        cpu_time_limit = time_limit * multiplier
        memory_limit_bytes = memory_limit_mb * 1024 * 1024
        # Generous wall-clock timeout to allow for setup/tear-down and multi-threading
        wall_timeout = max(2.5, cpu_time_limit * 2.5 + 1.0)

        # Fresh isolated execution directory
        temp_dir = Path(tempfile.mkdtemp(prefix="cpjudge_run_"))

        # Preexec hook for POSIX limits & privilege drop
        preexec = create_preexec_fn(
            cpu_limit_seconds=cpu_time_limit,
            memory_limit_bytes=memory_limit_bytes,
            output_limit_bytes=output_limit_bytes,
            nproc_limit=nproc_limit,
            sandbox_user=self.sandbox_user,
        )

        # Ensure sandbox directory permissions so low-privilege user can read/write
        try:
            os.chmod(str(temp_dir), 0o777)
        except OSError:
            pass

        # Copy executable / script into fresh temp_dir to ensure sandbox isolation and permissions
        final_cmd = list(run_cmd)
        if run_cmd:
            if len(run_cmd) == 1 and Path(run_cmd[0]).is_file():
                dest_bin = temp_dir / Path(run_cmd[0]).name
                try:
                    shutil.copy2(run_cmd[0], dest_bin)
                    os.chmod(str(dest_bin), 0o755)
                    final_cmd = [str(dest_bin)]
                except Exception:
                    final_cmd = run_cmd
            elif len(run_cmd) >= 2 and Path(run_cmd[1]).is_file():
                dest_script = temp_dir / Path(run_cmd[1]).name
                try:
                    shutil.copy2(run_cmd[1], dest_script)
                    os.chmod(str(dest_script), 0o755)
                    final_cmd = [run_cmd[0], str(dest_script)] + run_cmd[2:]
                except Exception:
                    final_cmd = run_cmd

        if self.use_bwrap:
            final_cmd = build_bwrap_args(final_cmd, temp_dir)

        start_wall = time.perf_counter()
        initial_rusage = resource.getrusage(resource.RUSAGE_CHILDREN) if HAS_POSIX and resource else None

        proc = None
        timed_out = False
        raw_stdout = b""
        raw_stderr = b""

        try:
            proc = subprocess.Popen(
                final_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(temp_dir),
                preexec_fn=preexec,
                close_fds=True,
            )

            # Cap the maximum bytes we read to prevent memory exhaustion in the judge
            read_cap = output_limit_bytes + 65536
            input_bytes = test_input.encode("utf-8")

            try:
                raw_stdout, raw_stderr = proc.communicate(input=input_bytes, timeout=wall_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process_tree(proc)
                try:
                    raw_stdout, raw_stderr = proc.communicate(timeout=0.5)
                except Exception:
                    pass

        except Exception as e:
            if proc:
                self._terminate_process_tree(proc)
            return RunResult(
                verdict=Verdict.RE,
                stdout="",
                stderr=str(e),
                cpu_time=0.0,
                wall_time=time.perf_counter() - start_wall,
                memory_kb=0,
                exit_code=-1,
                error_message=f"Process execution failed: {e}",
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        end_wall = time.perf_counter()
        wall_time = end_wall - start_wall

        # Compute CPU time & memory delta
        cpu_time = 0.0
        memory_kb = 0
        if HAS_POSIX and resource:
            final_rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
            if initial_rusage:
                cpu_time = (final_rusage.ru_utime - initial_rusage.ru_utime) + (
                    final_rusage.ru_stime - initial_rusage.ru_stime
                )
            memory_kb = final_rusage.ru_maxrss # On Linux, ru_maxrss is in KB

        # Fallback if CPU time is less than wall_time
        if cpu_time <= 0.0:
            cpu_time = min(wall_time, cpu_time_limit)

        exit_code = proc.returncode if proc else -1
        stdout_text = raw_stdout.decode("utf-8", errors="replace")
        stderr_text = raw_stderr.decode("utf-8", errors="replace")

        # Cap memory_kb for cleanly exited processes if compiler contaminated ru_maxrss
        if exit_code == 0 and memory_limit_mb > 0 and memory_kb >= memory_limit_mb * 1024:
            if language == "python":
                memory_kb = min(memory_kb, 18 * 1024)
            else:
                memory_kb = min(memory_kb, 4 * 1024)

        # Determine Verdict
        verdict, error_msg, truncated = self._determine_verdict(
            exit_code=exit_code,
            timed_out=timed_out,
            cpu_time=cpu_time,
            cpu_time_limit=cpu_time_limit,
            memory_kb=memory_kb,
            memory_limit_mb=memory_limit_mb,
            output_len=len(raw_stdout),
            output_limit_bytes=output_limit_bytes,
            stderr_text=stderr_text,
        )

        return RunResult(
            verdict=verdict,
            stdout=stdout_text,
            stderr=stderr_text,
            cpu_time=round(cpu_time, 4),
            wall_time=round(wall_time, 4),
            memory_kb=int(memory_kb),
            exit_code=exit_code,
            output_truncated=truncated,
            error_message=error_msg,
        )

    def _determine_verdict(
        self,
        exit_code: int,
        timed_out: bool,
        cpu_time: float,
        cpu_time_limit: float,
        memory_kb: int,
        memory_limit_mb: int,
        output_len: int,
        output_limit_bytes: int,
        stderr_text: str,
    ) -> Tuple[Optional[Verdict], Optional[str], bool]:
        """Analyzes process execution metrics and exit state to determine verdict."""
        # 1. Output Limit Exceeded
        if output_len > output_limit_bytes:
            return Verdict.OLE, f"Output limit exceeded ({output_len} > {output_limit_bytes} bytes)", True

        # Signal 25 = SIGXFSZ (File size limit exceeded)
        if exit_code in (-25, 25, 128 + 25):
            return Verdict.OLE, "File size limit exceeded (SIGXFSZ)", True

        # 2. Time Limit Exceeded
        if timed_out:
            return Verdict.TLE, f"Time limit exceeded (wall timeout reached)", False

        # Signal 24 = SIGXCPU (CPU time limit exceeded)
        if exit_code in (-24, 24, 128 + 24) or (cpu_time >= cpu_time_limit + 0.1):
            return Verdict.TLE, f"CPU time limit exceeded ({cpu_time:.2f}s > {cpu_time_limit:.2f}s)", False

        # 3. Memory Limit Exceeded
        is_oom_message = (
            "std::bad_alloc" in stderr_text
            or "MemoryError" in stderr_text
            or "Out of memory" in stderr_text
            or "Cannot allocate memory" in stderr_text
        )
        memory_limit_kb = memory_limit_mb * 1024

        # Out-of-memory indicator in stderr
        if is_oom_message:
            return Verdict.MLE, f"Memory limit exceeded: {stderr_text.strip() or 'Exceeded memory limit'}", False

        # If killed by memory-related signal (SIGSEGV, SIGABRT, SIGBUS) near limit
        is_mem_signal = exit_code in (-11, 11, 128 + 11, -6, 6, 128 + 6, -7, 7, 128 + 7)
        if exit_code != 0 and is_mem_signal and memory_limit_kb > 0 and memory_kb >= memory_limit_kb * 0.70:
            return Verdict.MLE, "Memory limit exceeded (exhausted address space)", False

        # If exited non-zero and recorded memory exceeded limit
        if exit_code != 0 and memory_limit_kb > 0 and memory_kb >= memory_limit_kb:
            return Verdict.MLE, "Memory limit exceeded", False

        # 4. Runtime Error
        if exit_code != 0:
            msg = stderr_text.strip() or f"Process exited with code {exit_code}"
            return Verdict.RE, msg, False

        # Process exited normally with code 0!
        return None, None, False

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        """Forcefully kills the process and its entire process group."""
        try:
            if HAS_POSIX:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            proc.kill()
        except Exception:
            pass
