"""Task 2: deterministic check registry + run_checks explicit-execution
contract (decision 16). All asserts use the FLAT verdict shape —
r["verdict_status"] / r["verdict_reasons"], never nested.
"""

import json
import subprocess

from lockstep_mcp.validators import build_manifest, run_checks


def _state(checks, evidence, **ctx):
    state = {"brief": {"checks": checks}, "evidence": evidence}
    state.update(ctx)
    return state


# ---------------------------------------------------------------------------
# base set
# ---------------------------------------------------------------------------


def test_file_exists_pass(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    r = run_checks(
        _state([{"type": "file_exists", "path": "a.txt"}], {}, _project=str(tmp_path)),
        execute=True,
    )
    assert r["verdict_status"] == "pass"
    assert r["verdict_reasons"] == []


def test_file_exists_fail(tmp_path):
    r = run_checks(
        _state([{"type": "file_exists", "path": "missing.txt"}], {}, _project=str(tmp_path)),
        execute=True,
    )
    assert r["verdict_status"] == "fail"
    assert r["verdict_reasons"]


def test_md_has_sections_missing(tmp_path):
    (tmp_path / "doc.md").write_text("# Title\n\nsome text\n")
    r = run_checks(
        _state(
            [{"type": "md_has_sections", "path": "doc.md", "sections": ["Verdict"]}],
            {},
            _project=str(tmp_path),
        ),
        execute=True,
    )
    assert r["verdict_status"] == "fail"
    assert any("Verdict" in reason for reason in r["verdict_reasons"])


def test_cmd_ok_true_and_false(tmp_path):
    r = run_checks(
        _state([{"type": "cmd_ok", "command": "true"}], {}, _project=str(tmp_path)),
        execute=True,
    )
    assert r["verdict_status"] == "pass"
    r = run_checks(
        _state([{"type": "cmd_ok", "command": "false"}], {}, _project=str(tmp_path)),
        execute=True,
    )
    assert r["verdict_status"] == "fail"


def test_unknown_check_type_fails_closed(tmp_path):
    r = run_checks(_state([{"type": "nope"}], {}, _project=str(tmp_path)), execute=True)
    assert r["verdict_status"] == "fail"


def test_no_checks_fails_closed():
    r = run_checks(_state([], {}), execute=True)
    assert r["verdict_status"] == "fail"


def test_git_clean(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = run_checks(
        _state([{"type": "git_clean", "cwd": str(tmp_path)}], {}), execute=True
    )
    assert r["verdict_status"] == "pass"
    (tmp_path / "dirty").write_text("x")
    r = run_checks(
        _state([{"type": "git_clean", "cwd": str(tmp_path)}], {}), execute=True
    )
    assert r["verdict_status"] == "fail"


def test_no_command_from_support():
    r = run_checks(
        _state([{"type": "cmd_ok", "command_from": "cmd"}], {"cmd": "true"}), execute=True
    )
    assert r["verdict_status"] == "fail"  # unknown/invalid config fails closed


# ---------------------------------------------------------------------------
# junit_gate — real pytest runs
# ---------------------------------------------------------------------------


def test_junit_gate_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_ok.py").write_text("def test_a():\n    assert True\n")
    r = run_checks(
        _state(
            [{"type": "junit_gate", "command": "pytest -q test_ok.py", "min_tests": 1}],
            {},
            _project=str(proj),
        ),
        execute=True,
    )
    assert r["verdict_status"] == "pass"
    assert not list((tmp_path / "state" / "tmp").glob("*.xml"))  # cleaned up


def test_junit_gate_min_tests_violation(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_ok.py").write_text("def test_a():\n    assert True\n")
    r = run_checks(
        _state(
            [{"type": "junit_gate", "command": "pytest -q test_ok.py", "min_tests": 5}],
            {},
            _project=str(proj),
        ),
        execute=True,
    )
    assert r["verdict_status"] == "fail"


def test_junit_gate_skip_violation(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "state"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_skip.py").write_text(
        "import pytest\n"
        "def test_a():\n    assert True\n"
        "@pytest.mark.skip\n"
        "def test_b():\n    assert True\n"
    )
    r = run_checks(
        _state(
            [
                {
                    "type": "junit_gate",
                    "command": "pytest -q test_skip.py",
                    "min_tests": 1,
                    "max_skipped": 0,
                }
            ],
            {},
            _project=str(proj),
        ),
        execute=True,
    )
    assert r["verdict_status"] == "fail"


# ---------------------------------------------------------------------------
# error verdict — raising check
# ---------------------------------------------------------------------------


def test_cmd_ok_nonexistent_binary_is_error(tmp_path):
    r = run_checks(
        _state(
            [{"type": "cmd_ok", "command": "this-binary-does-not-exist-xyz"}],
            {},
            _project=str(tmp_path),
        ),
        execute=True,
    )
    assert r["verdict_status"] == "error"


# ---------------------------------------------------------------------------
# baseline checks — hand-written manifest fixtures
# ---------------------------------------------------------------------------


def test_fresh_pass_when_absent_at_start(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps({}))
    (proj / "plan.md").write_text("# Plan\n")
    r = run_checks(
        _state(
            [{"type": "fresh", "path_from": "plan_path"}],
            {"plan_path": "plan.md"},
            _project=str(proj),
            _baseline_start=str(start_manifest),
            _baseline_globs=["*.md"],
        ),
        execute=True,
    )
    assert r["verdict_status"] == "pass"


def test_fresh_fail_when_unchanged_since_start(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "plan.md").write_text("# Plan\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps(build_manifest(proj, ["*.md"])))
    r = run_checks(
        _state(
            [{"type": "fresh", "path_from": "plan_path"}],
            {"plan_path": "plan.md"},
            _project=str(proj),
            _baseline_start=str(start_manifest),
            _baseline_globs=["*.md"],
        ),
        execute=True,
    )
    assert r["verdict_status"] == "fail"


def test_fresh_uncovered_by_globs_is_error(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "plan.md").write_text("# Plan\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps({}))
    r = run_checks(
        _state(
            [{"type": "fresh", "path_from": "plan_path"}],
            {"plan_path": "plan.md"},
            _project=str(proj),
            _baseline_start=str(start_manifest),
            _baseline_globs=["src/**"],  # plan.md not covered
        ),
        execute=True,
    )
    assert r["verdict_status"] == "error"


def test_unchanged_since_start_pass_then_fail(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "conftest.py").write_text("x = 1\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps(build_manifest(proj, ["conftest.py"])))
    state = _state(
        [{"type": "unchanged", "glob": "conftest.py", "since": "start"}],
        {},
        _project=str(proj),
        _baseline_start=str(start_manifest),
        _baseline_globs=["conftest.py"],
    )
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "pass"

    (proj / "conftest.py").write_text("x = 2\n")
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "fail"


def test_unchanged_catches_a_cmd_ok_mutation_in_the_same_pass(tmp_path):
    """item 12 — TOCTOU guard: `unchanged` is deferred to the END of the
    check pass and re-hashes AFTER every `cmd_ok`/`junit_gate` in that
    same pass, regardless of list position. A `cmd_ok` command that
    mutates a file the recipe claims is "frozen" must still be caught —
    checking the file BEFORE the mutating command ran would let it slip
    through as an ordinary TOCTOU race."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "t.py").write_text("original\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps(build_manifest(proj, ["t.py"])))

    mutate = "python3 -c \"open('t.py', 'a').write('mutated\\n')\""
    state = _state(
        [
            {"type": "cmd_ok", "command": mutate},
            {"type": "unchanged", "glob": "t.py", "since": "start"},
        ],
        {},
        _project=str(proj),
        _baseline_start=str(start_manifest),
        _baseline_globs=["t.py"],
    )

    r = run_checks(state, execute=True)

    assert r["verdict_status"] == "fail"
    assert any("t.py" in reason for reason in r["verdict_reasons"])


def test_unchanged_since_previous_survives_earlier_change(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "conftest.py").write_text("x = 1\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps(build_manifest(proj, ["conftest.py"])))

    (proj / "conftest.py").write_text("x = 2\n")  # changed in an earlier step
    prev_manifest = tmp_path / "prev.json"
    prev_manifest.write_text(json.dumps(build_manifest(proj, ["conftest.py"])))

    r = run_checks(
        _state(
            [{"type": "unchanged", "glob": "conftest.py", "since": "previous"}],
            {},
            _project=str(proj),
            _baseline_start=str(start_manifest),
            _baseline_prev=str(prev_manifest),
            _baseline_globs=["conftest.py"],
        ),
        execute=True,
    )
    assert r["verdict_status"] == "pass"  # unchanged since PREVIOUS, though changed since start


def test_changed_in_requires_an_actual_change(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "src").mkdir()
    (proj / "src" / "a.py").write_text("1\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps(build_manifest(proj, ["src/**"])))
    state = _state(
        [{"type": "changed_in", "paths": ["src/"], "since": "start"}],
        {},
        _project=str(proj),
        _baseline_start=str(start_manifest),
        _baseline_globs=["src/**"],
    )
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "fail"  # nothing changed yet

    (proj / "src" / "a.py").write_text("2\n")
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "pass"


def test_diff_only_confines_change_to_declared_paths(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "src").mkdir()
    (proj / "tests").mkdir()
    (proj / "src" / "a.py").write_text("1\n")
    (proj / "tests" / "t.py").write_text("1\n")
    prev_manifest = tmp_path / "prev.json"
    prev_manifest.write_text(json.dumps(build_manifest(proj, ["src/**", "tests/**"])))

    state = _state(
        [{"type": "diff_only", "paths": ["src/"]}],
        {},
        _project=str(proj),
        _baseline_prev=str(prev_manifest),
        _baseline_globs=["src/**", "tests/**"],
    )

    (proj / "src" / "a.py").write_text("2\n")
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "pass"  # change confined to src/

    (proj / "tests" / "t.py").write_text("2\n")  # leaks outside src/
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "fail"


def test_diff_only_prefix_match_does_not_bless_sibling_dir(tmp_path):
    """item 10: a naive `rel.startswith(p)` lets a declared path `src`
    wrongly cover `src-evil/...` (it's a string prefix, not a path-segment
    prefix). `paths: ["src"]` must confine to `src/...` exactly, not
    anything merely starting with the same characters."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "src").mkdir()
    (proj / "src-evil").mkdir()
    (proj / "src" / "a.py").write_text("1\n")
    (proj / "src-evil" / "a.py").write_text("1\n")
    prev_manifest = tmp_path / "prev.json"
    prev_manifest.write_text(json.dumps(build_manifest(proj, ["src/**", "src-evil/**"])))

    state = _state(
        [{"type": "diff_only", "paths": ["src"]}],
        {},
        _project=str(proj),
        _baseline_prev=str(prev_manifest),
        _baseline_globs=["src/**", "src-evil/**"],
    )

    (proj / "src-evil" / "a.py").write_text("2\n")  # leaks outside the declared "src"
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "fail"


def test_changed_in_prefix_match_does_not_bless_sibling_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "src").mkdir()
    (proj / "src-evil").mkdir()
    (proj / "src-evil" / "a.py").write_text("1\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps(build_manifest(proj, ["src/**", "src-evil/**"])))

    state = _state(
        [{"type": "changed_in", "paths": ["src"], "since": "start"}],
        {},
        _project=str(proj),
        _baseline_start=str(start_manifest),
        _baseline_globs=["src/**", "src-evil/**"],
    )

    (proj / "src-evil" / "a.py").write_text("2\n")  # change is in src-evil/, not src/
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "fail"  # "no changes detected under ['src']"


def test_changed_in_uncovered_path_is_error(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps({}))
    r = run_checks(
        _state(
            [{"type": "changed_in", "paths": ["docs/"], "since": "start"}],
            {},
            _project=str(proj),
            _baseline_start=str(start_manifest),
            _baseline_globs=["src/**"],  # docs/ not covered
        ),
        execute=True,
    )
    assert r["verdict_status"] == "error"


# ---------------------------------------------------------------------------
# M2 — hidden files must be visible to baseline manifests
# ---------------------------------------------------------------------------


def test_build_manifest_hashes_hidden_files(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env.test").write_text("A=1\n")
    (proj / "visible.txt").write_text("x\n")

    manifest = build_manifest(proj, ["*"])

    assert ".env.test" in manifest
    assert "visible.txt" in manifest


def test_unchanged_catches_a_hidden_file_edit(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env.test").write_text("A=1\n")
    start_manifest = tmp_path / "start.json"
    start_manifest.write_text(json.dumps(build_manifest(proj, ["*"])))
    state = _state(
        [{"type": "unchanged", "glob": "*", "since": "start"}],
        {},
        _project=str(proj),
        _baseline_start=str(start_manifest),
        _baseline_globs=["*"],
    )
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "pass"

    (proj / ".env.test").write_text("A=2\n")  # edit to a dotfile, not a "visible" one
    r = run_checks(state, execute=True)
    assert r["verdict_status"] == "fail"


# ---------------------------------------------------------------------------
# in-graph republish path (execute unset/False) — anti-forgery detection
# ---------------------------------------------------------------------------


def test_republish_embedded_verdict_verbatim():
    state = {"evidence": {"_verdict_status": "pass", "_verdict_reasons": []}}
    r = run_checks(state)
    assert r == {"verdict_status": "pass", "verdict_reasons": []}


def test_republish_absent_verdict_is_error():
    state = {"evidence": {}}
    r = run_checks(state)
    assert r["verdict_status"] == "error"


# ---------------------------------------------------------------------------
# file_matches_hash (Task 6) — pin from the denied-side state, bytes from
# the contained project path
# ---------------------------------------------------------------------------

import hashlib


def _hash_state(art, digest):
    return {"brief": {"checks": [{"type": "file_matches_hash", "path_from": "p",
                                  "hash_from": "_subcall_envelope.artifact_hashes.review"}]},
            "evidence": {"p": art.name}, "_project": str(art.parent),
            "_state": {"_subcall_envelope": {"artifact_hashes": {"review": digest}}}}


def test_file_matches_hash_pass_and_fail(tmp_path):
    art = tmp_path / "review.md"; art.write_text("Verdict: PASS\n")
    digest = hashlib.sha256(art.read_bytes()).hexdigest()
    state = _hash_state(art, digest)
    assert run_checks(state, execute=True)["verdict_status"] == "pass"
    art.write_text("Verdict: FAIL\n")                      # tampered after the pin
    out = run_checks(state, execute=True)
    assert out["verdict_status"] == "fail"
    assert any("hash" in r for r in out["verdict_reasons"])


def test_file_matches_hash_errors_when_pin_absent(tmp_path):
    art = tmp_path / "review.md"; art.write_text("x")
    state = _hash_state(art, "unused"); state["_state"] = {}
    out = run_checks(state, execute=True)
    assert out["verdict_status"] == "error"
    assert any("not present" in r for r in out["verdict_reasons"])


def test_file_matches_hash_errors_when_pin_present_but_empty(tmp_path):
    # m6.4: absent and present-but-falsy are DIFFERENT failures — both
    # error (fail-closed), but the message must say which.
    art = tmp_path / "review.md"; art.write_text("x")
    out = run_checks(_hash_state(art, ""), execute=True)
    assert out["verdict_status"] == "error"
    assert any("not a" in r or "empty" in r for r in out["verdict_reasons"])
