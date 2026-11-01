"""Judge orchestrator module for CPJudge.

Compiles, executes against test cases, evaluates checkers, and calculates scores.
Can be used as a standalone library:
    result = judge_submission(source_code, language, test_cases, config)
"""
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import time
from typing import Dict, List, Optional

from cpjudge.engine.checker import Checker, CheckerType
from cpjudge.engine.compiler import Compiler
from cpjudge.engine.runner import Runner
from cpjudge.engine.scorer import Scorer, ScoringMode, TestCaseScoreItem
from cpjudge.engine.verdicts import Verdict


@dataclass
class TestCase:
    id: str | int
    input_data: str
    expected_output: str
    marks: float = 1.0
    is_hidden: bool = False
    subtask_id: Optional[str | int] = None
    __test__: bool = False


@dataclass
class JudgeConfig:
    time_limit: float = 1.0
    memory_limit_mb: int = 256
    output_limit_bytes: int = 10 * 1024 * 1024  # 10 MB
    compile_timeout: float = 10.0
    time_multipliers: Optional[Dict[str, float]] = None
    checker_type: CheckerType = CheckerType.TOKEN
    custom_checker_path: Optional[str] = None
    float_tolerance: float = 1e-6
    scoring_mode: ScoringMode = ScoringMode.PER_TEST
    late_penalty_percent: float = 0.0
    use_bwrap: bool = True
    sandbox_user: Optional[str] = "judge_sandbox"


@dataclass
class TestResult:
    test_id: str | int
    verdict: Verdict
    cpu_time: float
    wall_time: float
    memory_kb: int
    marks_awarded: float
    is_hidden: bool
    subtask_id: Optional[str | int] = None
    student_output: Optional[str] = None
    diff: Optional[str] = None
    error_message: Optional[str] = None
    feedback: Optional[str] = None


@dataclass
class SubmissionResult:
    verdict: Verdict
    total_score: float
    raw_score: float
    max_score: float
    late_penalty_deduction: float
    compile_error: Optional[str] = None
    compile_time: float = 0.0
    test_results: List[TestResult] = field(default_factory=list)
    max_time: float = 0.0
    max_memory_kb: int = 0


def judge_submission(
    source_code: str,
    language: str,
    test_cases: List[TestCase],
    config: Optional[JudgeConfig] = None,
) -> SubmissionResult:
    """Judges a submission in memory.

    Args:
        source_code: Student solution source code.
        language: Programming language ('c', 'cpp', or 'python').
        test_cases: List of TestCase objects.
        config: JudgeConfig settings.

    Returns:
        SubmissionResult with comprehensive test details and scores.
    """
    if config is None:
        config = JudgeConfig()

    compiler = Compiler(compile_timeout=config.compile_timeout)
    runner = Runner(
        time_multipliers=config.time_multipliers,
        use_bwrap=config.use_bwrap,
        sandbox_user=config.sandbox_user,
    )
    checker = Checker(
        checker_type=config.checker_type,
        float_tolerance=config.float_tolerance,
        custom_checker_path=config.custom_checker_path,
    )

    # 1. Compilation Phase
    with tempfile.TemporaryDirectory(prefix="cpjudge_build_") as build_dir:
        try:
            import os
            os.chmod(build_dir, 0o777)
        except OSError:
            pass
        comp_res = compiler.compile(source_code, language, build_dir)

        if not comp_res.success:
            total_possible = sum(tc.marks for tc in test_cases)
            return SubmissionResult(
                verdict=Verdict.CE,
                total_score=0.0,
                raw_score=0.0,
                max_score=total_possible,
                late_penalty_deduction=0.0,
                compile_error=comp_res.error_message,
                compile_time=comp_res.compile_time,
                test_results=[],
            )

        # 2. Execution & Checking Phase for Each Test Case
        test_run_items: List[TestCaseScoreItem] = []
        detailed_results: List[TestResult] = []
        max_cpu_time = 0.0
        max_memory_kb = 0

        for tc in test_cases:
            run_res = runner.run_test(
                run_cmd=comp_res.run_cmd,
                language=language,
                test_input=tc.input_data,
                time_limit=config.time_limit,
                memory_limit_mb=config.memory_limit_mb,
                output_limit_bytes=config.output_limit_bytes,
            )

            max_cpu_time = max(max_cpu_time, run_res.cpu_time)
            max_memory_kb = max(max_memory_kb, run_res.memory_kb)

            # Check if execution failed with a limit/signal verdict
            if run_res.verdict is not None:
                final_verdict = run_res.verdict
                feedback = run_res.error_message
                diff = None
                student_out = None if tc.is_hidden else run_res.stdout
            else:
                # Normal termination, run checker
                check_res = checker.check(
                    input_data=tc.input_data,
                    expected_output=tc.expected_output,
                    student_output=run_res.stdout,
                    is_visible=not tc.is_hidden,
                )
                final_verdict = check_res.verdict
                feedback = check_res.feedback
                diff = check_res.diff if not tc.is_hidden else None
                student_out = run_res.stdout if not tc.is_hidden else None

            test_run_items.append(
                TestCaseScoreItem(
                    test_id=tc.id,
                    verdict=final_verdict,
                    marks=tc.marks,
                    subtask_id=tc.subtask_id,
                )
            )

            detailed_results.append(
                TestResult(
                    test_id=tc.id,
                    verdict=final_verdict,
                    cpu_time=run_res.cpu_time,
                    wall_time=run_res.wall_time,
                    memory_kb=run_res.memory_kb,
                    marks_awarded=0.0, # Will be set by scorer below
                    is_hidden=tc.is_hidden,
                    subtask_id=tc.subtask_id,
                    student_output=student_out,
                    diff=diff,
                    error_message=run_res.error_message if not tc.is_hidden else None,
                    feedback=feedback if not tc.is_hidden else None,
                )
            )

        # 3. Scoring Phase
        scorer = Scorer(
            mode=config.scoring_mode,
            late_penalty_percent=config.late_penalty_percent,
        )
        score_res = scorer.calculate_score(test_run_items)

        # Update marks_awarded on individual test results
        for tr in detailed_results:
            tr.marks_awarded = score_res.test_marks_awarded.get(tr.test_id, 0.0)

        # 4. Overall Submission Verdict
        # AC if all tests passed; otherwise first non-AC verdict encountered
        overall_verdict = Verdict.AC
        for tr in detailed_results:
            if tr.verdict != Verdict.AC:
                overall_verdict = tr.verdict
                break

        return SubmissionResult(
            verdict=overall_verdict,
            total_score=score_res.final_score,
            raw_score=score_res.raw_score,
            max_score=score_res.max_score,
            late_penalty_deduction=score_res.late_penalty_deduction,
            compile_time=comp_res.compile_time,
            test_results=detailed_results,
            max_time=max_cpu_time,
            max_memory_kb=max_memory_kb,
        )


def judge(submission_id: int) -> SubmissionResult:
    """Library entry point for worker integration (Phase 6).

    In Phase 6, this function will query the database for submission_id,
    retrieve source code, language, and problem test cases, invoke
    judge_submission(), and persist the results.
    """
    raise NotImplementedError("Database-backed judge(submission_id) is wired in Phase 6.")
