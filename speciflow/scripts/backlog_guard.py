#!/usr/bin/env python3
"""Recognize direct mutation targets, then remind the caller of their native owner.

This is an accident guard, not a shell/Python interpreter or a security boundary.
Unknown operations pass; a reference to managed data is never itself a mutation.
"""
import ast
import json
import os
from pathlib import Path
import re
import shlex
import sys


REMINDERS = {
    'backlog': 'Direct Backlog edit blocked. Use SpeciFlow and the native Backlog CLI to change product requirements.',
    'openspec': 'Direct active OpenSpec edit blocked. Use SpeciFlow to prepare an OpenSpec change (proposal and delta specs), then apply it through the native OpenSpec CLI.',
}
PATCH_PATH = re.compile(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$', re.MULTILINE)


def lexical(value, cwd):
    return Path(os.path.abspath(cwd / Path(value).expanduser()))


def overlaps(path, root):
    return path.is_relative_to(root) or root.is_relative_to(path)


def backlog_config(config):
    return config.is_file() and re.search(r'^project_name:', config.read_text(encoding='utf-8'), re.MULTILINE)


def owner_of(value, cwd):
    if not isinstance(value, str) or not value or '\x00' in value:
        return None
    raw = lexical(value, cwd)
    path = raw.resolve()
    parents = dict.fromkeys((raw, *raw.parents, path, *path.parents, cwd, *cwd.parents))
    for parent in parents:
        for data in (parent, parent / 'backlog', parent / '.backlog'):
            if (data / 'tasks').is_dir() and backlog_config(data / 'config.yml'):
                if overlaps(raw, data) or overlaps(path, data.resolve()):
                    return 'backlog'
        config = parent / 'backlog.config.yml'
        if backlog_config(config):
            match = re.search(r'^backlog_directory:\s*(.+)$', config.read_text(encoding='utf-8'), re.MULTILINE)
            # Match Backlog's simple scalar reader, including literal inline '#'.
            value = re.sub(r"^['\"]|['\"]$", '', match[1].strip()) if match else 'backlog'
            data = lexical(value.replace('\\', '/'), parent)
            if (data / 'tasks').is_dir() and (overlaps(raw, data) or overlaps(path, data.resolve())
                                                or overlaps(raw, config)):
                return 'backlog'
        for root in (parent, parent / 'openspec'):
            specs = root / 'specs'
            # Native initialization supplies specs and changes; name alone is insufficient.
            if (specs.is_dir() and (root / 'changes').is_dir()
                    and (root.name == 'openspec' or (root / 'config.yaml').is_file())):
                if overlaps(raw, specs) or overlaps(path, specs.resolve()):
                    return 'openspec'
    return None


def copy_destination(source, destination, cwd, directory=False):
    if source is not None and destination is not None:
        if directory or lexical(destination, cwd).is_dir():
            return str(Path(destination) / Path(source).name)
    return destination


class PythonTargets(ast.NodeVisitor):
    """Only direct calls and ordinary local literal Path bindings; no effect inference."""
    def __init__(self, cwd):
        self.cwd = cwd
        self.paths = {}
        self.targets = []
        self.imports = {}

    def symbol(self, node):
        if isinstance(node, ast.Name):
            return self.imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return self.symbol(node.value) + '.' + node.attr
        return ''

    def path(self, node):
        if isinstance(node, ast.Name):
            return self.paths.get(node.id)
        if isinstance(node, ast.Call) and self.symbol(node.func) in {'Path', 'pathlib.Path'} and node.args:
            parts = [self.literal(arg) for arg in node.args]
            if all(isinstance(part, str) for part in parts):
                return str(Path(*parts))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parent, child = self.path(node.left), self.literal(node.right)
            if parent is not None and isinstance(child, str):
                return str(Path(parent) / child)
        return None

    @staticmethod
    def literal(node):
        return node.value if isinstance(node, ast.Constant) else None

    def filename(self, node):
        value = self.literal(node)
        return value if isinstance(value, str) else self.path(node)

    def visit_Import(self, node):
        for item in node.names:
            self.imports[item.asname or item.name] = item.name

    def visit_ImportFrom(self, node):
        for item in node.names:
            self.imports[item.asname or item.name] = f'{node.module}.{item.name}'

    def visit_Assign(self, node):
        self.visit(node.value)
        value = self.path(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.paths.pop(target.id, None)
                self.imports.pop(target.id, None)
                if value is not None:
                    self.paths[target.id] = value

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self.visit_Assign(ast.Assign(targets=[node.target], value=node.value))

    def visit_FunctionDef(self, node):
        # Function/class bodies are not executed merely by defining them.
        self.paths.pop(node.name, None)
        self.imports[node.name] = ''

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def visit_Call(self, node):
        name = self.symbol(node.func)
        args = node.args
        keywords = {item.arg: item.value for item in node.keywords}
        if name in {'open', 'io.open'}:
            mode = self.literal(args[1] if len(args) > 1 else keywords.get('mode'))
            if isinstance(mode, str) and any(flag in mode for flag in 'wax+'):
                self.targets.append(self.filename(args[0] if args else keywords.get('file')))
        if isinstance(node.func, ast.Attribute):
            target = self.path(node.func.value)
            method = node.func.attr
            if target is not None:
                if method in {'write_text', 'write_bytes', 'unlink', 'rmdir', 'mkdir', 'touch', 'chmod'}:
                    self.targets.append(target)
                elif method in {'rename', 'replace'}:
                    self.targets.extend([target, self.filename(args[0] if args else keywords.get('target'))])
                elif method == 'open':
                    mode = self.literal(args[0] if args else keywords.get('mode'))
                    if isinstance(mode, str) and any(flag in mode for flag in 'wax+'):
                        self.targets.append(target)
        positions = {
            'os.remove': (0,), 'os.unlink': (0,), 'os.rmdir': (0,), 'os.mkdir': (0,),
            'os.makedirs': (0,), 'os.chmod': (0,), 'os.utime': (0,),
            'os.rename': (0, 1), 'os.replace': (0, 1),
            'shutil.rmtree': (0,), 'shutil.move': (0,),
            'shutil.copyfile': (1,), 'shutil.copytree': (1,),
        }
        for i in positions.get(name, ()):
            self.targets.append(self.filename(args[i]) if len(args) > i else None)
        if name in {'shutil.copy', 'shutil.copy2', 'shutil.move'} and len(args) >= 2:
            self.targets.append(copy_destination(self.filename(args[0]), self.filename(args[1]), self.cwd))
        self.generic_visit(node)


def python_targets(source, cwd):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    visitor = PythonTargets(cwd)
    visitor.visit(tree)
    return [path for path in visitor.targets if path is not None]


# Keep word/operator provenance: quoted '>' and '; rm ...' are just words.
TOKEN = re.compile(r'''(?P<space>[ \t\r]+)|(?P<comment>\#[^\n]*)|(?P<op>&>>|<<-|<<<|>>|<<|&&|\|\||\|&|&>|>&|<>|>\||[;&|<>()\n])|(?P<io>\d+(?=[<>]))|(?P<word>(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s;&|<>()'"\\])+ )''', re.VERBOSE)



def shell_tokens(command):
    # Heredoc data begins after the header newline, and only a real unquoted
    # shell operator can introduce it. Quoted words/comments never do so.
    tokens, docs, pending = [], {}, []
    pos, heredoc = 0, None
    while pos < len(command):
        match = TOKEN.match(command, pos)
        if not match:
            return [], docs  # Unsupported/malformed syntax is not a proven mutation.
        pos = match.end()
        if match.lastgroup == 'op':
            operator = match[0]
            tokens.append((operator, True))
            if operator in {'<<', '<<-'}:
                heredoc = operator
            elif operator == '\n':
                for key, marker, quoted, strip_tabs in pending:
                    ending = re.compile(r'^' + (r'\t*' if strip_tabs else '')
                                        + re.escape(marker) + r'\r?(?:\n|$)', re.MULTILINE).search(command, pos)
                    end = ending.start() if ending else len(command)
                    body = command[pos:end]
                    if strip_tabs:
                        body = ''.join(line.lstrip('\t') for line in body.splitlines(keepends=True))
                    docs[key] = body if quoted else None
                    pos = ending.end() if ending else len(command)
                pending = []
        elif match.lastgroup == 'word':
            try:
                word = ''.join(shlex.split(match[0]))
            except ValueError:
                return [], docs
            if heredoc:
                key = f'__speciflow_heredoc_{len(pending)}_{pos}__'
                pending.append((key, word, any(c in match[0] for c in "'\"\\"), heredoc == '<<-'))
                word, heredoc = key, None
            tokens.append((word, False))
    return tokens, docs


def operands(args, valued=()):
    """Basic POSIX options; return operands and an explicit target directory."""
    result, target, i, options = [], None, 0, True
    while i < len(args):
        arg = args[i]
        i += 1
        if options and arg == '--':
            options = False
        elif options and arg in valued:
            i += 1
        elif options and arg in {'-t', '--target-directory'}:
            if i < len(args):
                target = args[i]
            i += 1
        elif options and arg.startswith('--target-directory='):
            target = arg.split('=', 1)[1]
        elif options and arg.startswith('-') and not arg.startswith('--') and 't' in arg[1:]:
            suffix = arg.split('t', 1)[1]
            target = suffix or (args[i] if i < len(args) else None)
            i += not bool(suffix)
        elif not options or not arg.startswith('-'):
            result.append(arg)
    return result, target


def sed_targets(args):
    scripts, files, inplace = [], [], False
    explicit = any(a in {'-e', '-f', '--expression', '--file'} or a.startswith(('--expression=', '--file=')) for a in args)
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg in {'-e', '--expression', '-f', '--file'}:
            if i < len(args) and arg in {'-e', '--expression'}:
                scripts.append(args[i])
            i += 1
        elif arg.startswith('--expression='):
            scripts.append(arg.split('=', 1)[1])
        elif re.match(r'^-[nEralu]*i', arg) or arg.startswith('--in-place'):
            inplace = True
            if i < len(args) and args[i] == '':
                i += 1
        elif arg == '--':
            files.extend(args[i:])
            break
        elif not arg.startswith('-'):
            if not explicit and not scripts:
                scripts.append(arg)
            else:
                files.append(arg)
    targets = files if inplace else []
    # Literal sed `w file` is a separate output target, unlike text replacement.
    for script in scripts:
        match = re.fullmatch(r'\s*w\s+(.+)', script)
        if match:
            targets.append(match[1])
    return targets


def command_targets(words, cwd, doc=None):
    while words and (words[0] in {'env', 'command'} or re.match(r'^[A-Za-z_]\w*=', words[0])):
        words = words[1:]
    if not words:
        return []
    name, args = Path(words[0]).name, words[1:]
    if name in {'backlog', 'openspec'}:
        return []
    if name in {'rm', 'rmdir', 'tee', 'touch', 'mkdir'}:
        return operands(args)[0]
    if name in {'mv', 'cp', 'install'}:
        files, target = operands(args, {'-m', '-o', '-g', '--mode', '--owner', '--group', '-S', '--suffix'})
        if target is not None:
            destinations = [copy_destination(source, target, cwd, directory=True) for source in files]
            return files + destinations if name == 'mv' else destinations
        if len(files) >= 2:
            sources = files[:-1]
            destinations = [copy_destination(source, files[-1], cwd) for source in sources]
            return sources + destinations if name == 'mv' else destinations
        return []
    if name == 'sed':
        return sed_targets(args)
    if name == 'find':
        roots = []
        for arg in args:
            if arg.startswith('-') or arg in {'!', '('}:
                break
            roots.append(arg)
        # Only elementary expressions; filters can select drafts exclusively.
        expression = args[len(roots):]
        while expression[:2] in (['-type', 'f'], ['-type', 'd']):
            expression = expression[2:]
        if expression == ['-delete']:
            return roots
        for flag in ('-exec', '-execdir'):
            if expression and expression[0] == flag:
                nested = expression[1:]
                if nested and Path(nested[0]).name in {'rm', 'rmdir'} and '{}' in nested:
                    return roots
        return []
    if name == 'git' and args and args[0] in {'diff', 'show', 'log'}:
        return [arg.split('=', 1)[1] for arg in args if arg.startswith('--output=')] + [
            args[i+1] for i, arg in enumerate(args[:-1]) if arg == '--output']
    if re.fullmatch(r'python(?:3(?:\.\d+)?)?', name):
        if '-c' in args and args.index('-c') + 1 < len(args):
            return python_targets(args[args.index('-c') + 1], cwd)
        if '-' in args and doc is not None:
            return python_targets(doc, cwd)
    return []


def shell_targets(command, cwd):
    tokens, docs = shell_tokens(command)
    words, scopes, doc = [], [], None
    i = 0
    while i <= len(tokens):
        token, operator = tokens[i] if i < len(tokens) else (';', True)
        i += 1
        if operator and token in {'>', '>>', '>|', '&>', '&>>', '>&', '<>', '<', '<<', '<<-', '<<<'}:
            if i < len(tokens):
                target, is_op = tokens[i]
                i += 1
                if not is_op:
                    if token in {'<<', '<<-'}:
                        doc = docs.get(target)
                    elif token not in {'<', '<<<'} and not target.isdigit():
                        yield target, cwd
            continue
        if operator and token in {';', '\n', '&&', '||', '|', '|&', '&', '(', ')'}:
            for target in command_targets(words, cwd, doc):
                yield target, cwd
            if len(words) == 2 and words[0] == 'cd' and not any(c in words[1] for c in '$`'):
                cwd = lexical(words[1], cwd)
            words, doc = [], None
            if token == '(':
                scopes.append(cwd)
            elif token == ')' and scopes:
                cwd = scopes.pop()
        else:
            words.append(token)


def changed_targets(event):
    if event.get('hook_event_name') != 'PreToolUse':
        return
    name, args = event.get('tool_name'), event.get('tool_input', {})
    if not isinstance(args, dict):
        return
    cwd = lexical(event.get('cwd') or os.getcwd(), Path.cwd())
    if args.get('workdir'):
        cwd = lexical(args['workdir'], cwd)
    if name in {'Write', 'Edit', 'MultiEdit', 'NotebookEdit'}:
        for key in ('file_path', 'notebook_path'):
            if args.get(key):
                yield args[key], cwd
    elif name == 'apply_patch':
        for target in PATCH_PATH.findall(args.get('command', args.get('patch', ''))):
            yield target, cwd
    elif name in {'Bash', 'exec_command'}:
        yield from shell_targets(args.get('command', args.get('cmd', '')), cwd)


def main():
    try:
        event = json.load(sys.stdin)
        for target, cwd in changed_targets(event):
            owner = owner_of(target, cwd)
            if owner:
                print(REMINDERS[owner], file=sys.stderr)
                return 2
        return 0
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f'SpeciFlow planning guard could not inspect this call: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
