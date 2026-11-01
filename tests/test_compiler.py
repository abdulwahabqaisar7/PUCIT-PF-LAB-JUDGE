"""Unit tests for the CPJudge compiler module."""
import tempfile
import pytest

from cpjudge.engine.compiler import Compiler
from cpjudge.engine.verdicts import Verdict


def test_compile_c_success():
    compiler = Compiler()
    c_code = """
    #include <stdio.h>
    int main() {
        printf("Hello C\\n");
        return 0;
    }
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = compiler.compile(c_code, "c", tmp_dir)
        assert res.success is True
        assert res.verdict is None
        assert res.executable_path is not None
        assert len(res.run_cmd) >= 1


def test_compile_cpp_success():
    compiler = Compiler()
    cpp_code = """
    #include <iostream>
    #include <vector>
    int main() {
        std::vector<int> v = {1, 2, 3};
        std::cout << "Size: " << v.size() << "\\n";
        return 0;
    }
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = compiler.compile(cpp_code, "cpp", tmp_dir)
        assert res.success is True
        assert res.verdict is None
        assert res.executable_path is not None


def test_compile_python_success():
    compiler = Compiler()
    py_code = """
import sys
print("Hello Python")
"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = compiler.compile(py_code, "python", tmp_dir)
        assert res.success is True
        assert res.verdict is None
        assert res.executable_path is not None


def test_compile_cpp_error():
    compiler = Compiler()
    bad_code = """
    #include <iostream>
    int main() {
        this_is_a_syntax_error();
        return 0;
    }
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = compiler.compile(bad_code, "cpp", tmp_dir)
        assert res.success is False
        assert res.verdict == Verdict.CE
        assert res.error_message is not None
        assert "this_is_a_syntax_error" in res.error_message


def test_compile_python_syntax_error():
    compiler = Compiler()
    bad_code = "def invalid_func(:\n    pass\n"
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = compiler.compile(bad_code, "python", tmp_dir)
        assert res.success is False
        assert res.verdict == Verdict.CE
        assert "SyntaxError" in res.error_message
