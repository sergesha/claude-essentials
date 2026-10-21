"""Target-based detection: unrecognized semantics never imply a forbidden edit."""
import shlex
import subprocess

import pytest

from test_backlog_guard import invoke, silent


@pytest.fixture(params=['backlog/tasks/task-1.md', 'openspec/specs/export/spec.md'])
def planning(tmp_path, request):
    (tmp_path / 'backlog/tasks').mkdir(parents=True)
    (tmp_path / 'backlog/config.yml').write_text('project_name: fixture\n')
    (tmp_path / 'openspec/specs/export').mkdir(parents=True)
    (tmp_path / 'openspec/changes/demo/specs/export').mkdir(parents=True)
    (tmp_path / 'openspec/config.yaml').write_text('schema: spec-driven\n')
    target = request.param
    (tmp_path / target).write_text('Contract: 30 seconds\n')
    return tmp_path, target


def python_command(source, heredoc=False):
    return "python3 - <<'PYCODE'\n" + source + '\nPYCODE' if heredoc else 'python3 -c ' + shlex.quote(source)


@pytest.mark.parametrize('source', [
    "print(open(TARGET).read().upper().splitlines())",
    "from pathlib import Path; s=Path(TARGET).read_text(); print(s.replace('30','45'))",
    "from pathlib import Path; print(Path(TARGET).read_text().replace('30','45'))",
    "import re,json; print(json.dumps(re.findall(r'\\w+',open(TARGET).read())))",
    "import pathlib as p; print(p.Path(TARGET).read_text().upper())",
    "from pathlib import Path\ndef render(p):\n return p.read_text().upper()\nprint(render(Path(TARGET)))",
    "from pathlib import Path\ndef unused():\n Path(TARGET).unlink()\nprint('not called')",
    "from pathlib import Path; src=Path(TARGET); dst=Path('openspec/changes/demo/specs/export/spec.md'); dst.write_text(src.read_text().replace('30','45'))",
    "from pathlib import Path; print(Path(TARGET).read_text()); Path('output.txt').write_text('output')",
    "from pathlib import Path; p=Path(TARGET); p='some text'; print(p.replace('some','new'))",
])
@pytest.mark.parametrize('heredoc', [False, True])
def test_reads_and_independent_draft_writes_execute(planning, source, heredoc):
    root, target = planning
    before = (root / target).read_bytes()
    command = python_command(source.replace('TARGET', repr(target)), heredoc)
    silent(invoke(root, 'Bash', {'command': command}))
    result = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / target).read_bytes() == before
    if 'dst.write_text' in source:
        assert '45 seconds' in (root / 'openspec/changes/demo/specs/export/spec.md').read_text()


@pytest.mark.parametrize('source', [
    "from pathlib import Path; Path(TARGET).write_text('lost')",
    "from pathlib import Path; Path(TARGET).write_bytes(b'lost')",
    "from pathlib import Path; Path(TARGET).unlink()",
    "from pathlib import Path; p=Path(TARGET); p.rename('moved')",
    "from pathlib import Path; p=Path(TARGET); p.replace('moved')",
    "from pathlib import Path; p=Path('draft.md'); p.rename(TARGET)",
    "from pathlib import Path; p=Path('draft.md'); p.replace(TARGET)",
    "from pathlib import Path; p=Path(TARGET); p.open(mode='w').write('lost')",
    "from pathlib import Path; p=Path(TARGET); q=p; q.unlink()",
    "from pathlib import Path; p=Path('openspec')/'specs'/'export'/'spec.md'; p.write_text('lost')",
    "import os; os.remove(TARGET)",
    "import os; os.rename(TARGET,'moved')",
    "import shutil; shutil.copyfile('draft.md',TARGET)",
    "import shutil; shutil.move(TARGET,'moved')",
    *[f"open(TARGET, {mode!r}).write('lost')" for mode in ['w','a','x','r+','wb','w+']],
])
@pytest.mark.parametrize('heredoc', [False, True])
def test_recognized_python_mutations_are_denied(planning, source, heredoc):
    root, target = planning
    command = python_command(source.replace('TARGET', repr(target)), heredoc)
    result = invoke(root, 'Bash', {'command': command})
    assert result.returncode == 2, result.stderr
    assert 'SpeciFlow' in result.stderr


@pytest.mark.parametrize('source', [
    "custom(TARGET)",
    "from pathlib import Path; p=Path(TARGET); p.custom_method()",
    "from pathlib import Path; callback=Path(TARGET).write_text; callback('unknown indirect effect')",
    "exec(\"open(TARGET,'w').write('unknown dynamic effect')\")",
    "from pathlib import Path; sorted(['unknown callback effect'],key=Path(TARGET).write_text)",
])
def test_unknown_code_passes_without_claiming_prevention(planning, source):
    root, target = planning
    # Do not execute these: the contract deliberately makes no safety claim.
    silent(invoke(root, 'Bash', {'command': python_command(source.replace('TARGET', repr(target)))}))


@pytest.mark.parametrize('template', [
    'unknown-tool TARGET',
    "printf '%s' 'rm TARGET'",
    "printf '%s' '>' TARGET",
    "printf '%s' '; rm TARGET'",
    "cat <<'TEXT'\nrm TARGET\nTEXT",
    'openspec future-command TARGET',
    'backlog future-command TARGET',
    'sed s/30/45/ TARGET > openspec/changes/demo/specs/export/spec.md',
    'cp TARGET output.txt',
    'python3 -c "print(\'TARGET\')" > output.txt',
    'cat TARGET; rm output.txt',
    "sed -i '' s/x/y/ output.txt; cat TARGET",
])
@pytest.mark.parametrize('tool,key', [('Bash','command'),('exec_command','cmd')])
def test_mentions_and_unrelated_targets_pass(planning, template, tool, key):
    root, target = planning
    silent(invoke(root, tool, {key: template.replace('TARGET', shlex.quote(target))}))


@pytest.mark.parametrize('template', [
    'openspec future-command; rm TARGET',
    'backlog task list > TARGET',
    "python3 - <<'CODE' > TARGET\nprint('lost')\nCODE",
    "python3 - <<'CODE'\nprint('read')\nCODE\nrm TARGET",
    'install draft.md TARGET',
    'find TARGET -delete',
])
def test_native_commands_and_heredocs_do_not_hide_adjacent_writes(planning, template):
    root, target = planning
    result = invoke(root, 'Bash', {'command': template.replace('TARGET', shlex.quote(target))})
    assert result.returncode == 2, result.stderr


def test_names_alone_do_not_initialize_native_owners(tmp_path):
    for target in ['backlog/tasks/task.md','openspec/specs/export/spec.md']:
        (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
        silent(invoke(tmp_path, 'Write', {'file_path': target, 'content': 'ordinary file'}))
    (tmp_path / 'backlog.config.yml').write_text('unrelated: configuration\n')
    silent(invoke(tmp_path, 'Write', {'file_path': 'backlog/tasks/task.md'}))


@pytest.mark.parametrize('command', [
    'cp TARGET .',
    'cp -t . TARGET',
    'install TARGET .',
    "python3 -c \"import shutil; shutil.copy('TARGET', '.')\"",
    "python3 -c \"import shutil; shutil.copy2('TARGET', '.')\"",
])
def test_copy_to_project_directory_changes_only_its_own_destination(planning, command):
    root, target = planning
    before = (root / target).read_bytes()
    command = command.replace('TARGET', target)
    silent(invoke(root, 'Bash', {'command': command}))
    # -t is a GNU extension unavailable on macOS; other controls run natively.
    if ' -t ' not in command:
        subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
        assert (root / target).read_bytes() == before
        assert (root / target.split('/')[-1]).read_bytes() == before


def test_generic_specs_and_changes_directories_are_not_openspec(tmp_path):
    (tmp_path / 'docs/specs').mkdir(parents=True)
    (tmp_path / 'docs/changes').mkdir()
    silent(invoke(tmp_path, 'Write', {'file_path': 'docs/specs/example.md'}))


def test_find_unknown_filters_do_not_mark_the_entire_root_as_mutated(planning):
    root, _ = planning
    silent(invoke(root, 'Bash', {'command': "find openspec -path 'openspec/changes/*' -type f -delete"}))


@pytest.mark.parametrize('source', [
    "from pathlib import Path; Path(TARGET).write_text('lost')",
    "from pathlib import Path; p=Path(TARGET); p.unlink()",
    "open(TARGET, 'w').write('lost')",
])
def test_python_decision_prevents_demonstrated_effect(planning, source):
    root, target = planning
    path = root / target
    before = path.read_bytes()
    command = python_command(source.replace('TARGET', repr(target)))
    subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert not path.exists() or path.read_bytes() != before
    path.write_bytes(before)
    decision = invoke(root, 'exec_command', {'cmd': command})
    if decision.returncode == 0:
        subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert decision.returncode == 2, decision.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize('operation', ['cp', 'install'])
@pytest.mark.parametrize('redirect', ['2>/dev/null', '2>&1'])
def test_descriptor_redirect_preserves_actual_copy_destination(planning, operation, redirect):
    root, target = planning
    path = root / target
    before = path.read_bytes()
    (root / 'draft.md').write_text('changed\n')
    command = f'{operation} draft.md {shlex.quote(target)} {redirect}'
    subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert path.read_text() == 'changed\n'
    path.write_bytes(before)
    decision = invoke(root, 'Bash', {'command': command})
    if decision.returncode == 0:
        subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert decision.returncode == 2, decision.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize('suffix', ['2 > /dev/null', "'2'> /dev/null"])
def test_numeric_copy_operand_is_not_a_descriptor(planning, suffix):
    root, target = planning
    before = (root / target).read_bytes()
    command = f'cp {shlex.quote(target)} {suffix}'
    silent(invoke(root, 'Bash', {'command': command}))
    subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert (root / '2').read_bytes() == before
    assert (root / target).read_bytes() == before


@pytest.mark.parametrize('source,command,destination', [
    ('scratch/unrelated.md', 'mv scratch/unrelated.md .', 'unrelated.md'),
    ('openspec/changes/demo/proposal.md', 'mv openspec/changes/demo .', 'demo/proposal.md'),
    ('scratch/unrelated.md', "python3 -c \"import shutil; shutil.move('scratch/unrelated.md', '.')\"", 'unrelated.md'),
])
def test_move_to_directory_changes_only_source_and_destination_child(planning, source, command, destination):
    root, target = planning
    before = (root / target).read_bytes()
    (root / source).parent.mkdir(parents=True, exist_ok=True)
    (root / source).write_text('ordinary draft\n')
    decision = invoke(root, 'exec_command', {'cmd': command})
    # Exact unguarded command proves the false-denial case is ordinary work.
    subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert not (root / source).exists()
    assert (root / destination).read_text() == 'ordinary draft\n'
    assert (root / target).read_bytes() == before
    silent(decision)


@pytest.mark.parametrize('command', ['mv TARGET .', "python3 -c \"import shutil; shutil.move('TARGET', '.')\""])
def test_move_to_directory_still_checks_protected_source(planning, command):
    root, target = planning
    decision = invoke(root, 'Bash', {'command': command.replace('TARGET', target)})
    assert decision.returncode == 2, decision.stderr


@pytest.mark.parametrize('header', ["printf '%s\\n' '<<END'", "printf ok # <<END", 'printf "%s\\n" "<<END"'])
def test_heredoc_text_and_comments_do_not_hide_following_mutation(planning, header):
    root, target = planning
    path = root / target
    before = path.read_bytes()
    command = header + '\nrm ' + shlex.quote(target)
    subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert not path.exists()
    path.write_bytes(before)
    decision = invoke(root, 'Bash', {'command': command})
    if decision.returncode == 0:
        subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert decision.returncode == 2, decision.stderr
    assert path.read_bytes() == before


def test_grouped_sed_inplace_flag_changes_protected_file(planning):
    root, target = planning
    path = root / target
    before = path.read_bytes()
    command = "sed -ni.bak 's/30/45/p' " + shlex.quote(target)
    subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert '45 seconds' in path.read_text()
    path.write_bytes(before)
    decision = invoke(root, 'Bash', {'command': command})
    if decision.returncode == 0:
        subprocess.run(command, cwd=root, shell=True, check=True, capture_output=True)
    assert decision.returncode == 2, decision.stderr
    assert path.read_bytes() == before
