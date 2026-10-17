"""Test-only submission contract. This check supplements, rather than replaces, isolation."""
import ast
from pathlib import Path

ALLOWED_IMPORTS = {'click', 'unittest', 'io', 'sys', 'os', 'errno', 'tempfile', 'pathlib',
                   're', 'math', 'itertools', 'functools', 'collections', 'contextlib', 'codecs'}
FORBIDDEN_NAMES = {'eval', 'exec', 'compile', '__import__', 'getattr', 'setattr', 'delattr', 'globals', 'locals', 'vars', 'breakpoint', 'input'}
FORBIDDEN_ATTRIBUTES = {'__file__', '__code__', '__globals__', '__dict__', '__class__', '__subclasses__',
                        '__builtins__', '__getattribute__', '_outcome', 'setprofile', 'settrace',
                        'modules', 'meta_path', 'path_hooks', 'system', 'popen', 'fork', 'forkpty',
                        'execv', 'execve', 'execvp', 'execl', '_exit', 'kill', 'killpg', 'setsid', 'posix_spawn',
                        'skip', 'skipIf', 'skipUnless', 'expectedFailure'}


def read_submission(folder):
    folder = folder.resolve()
    files = {}
    for path in folder.rglob('*'):
        if '__pycache__' in path.relative_to(folder).parts:
            continue
        if path.is_symlink():
            raise ValueError('Submission symlinks are not allowed')
        if path.is_dir():
            if path.name == '__pycache__':
                continue
            raise ValueError('Submit flat test_*.py files only')
        if not path.name.startswith('test_') or path.suffix != '.py':
            raise ValueError('Only test_*.py files may be submitted')
        raw = path.read_bytes()
        if len(raw) > 32768:
            raise ValueError('A test file exceeds 32 KiB')
        text = raw.decode('utf-8')
        tree = ast.parse(text, filename=path.name)
        if sum(1 for _ in ast.walk(tree)) > 10000:
            raise ValueError('Test syntax is too large')
        for node in tree.body:
            if isinstance(node, ast.If):
                expected = ast.parse("if __name__ == '__main__':\n    unittest.main()\n").body[0]
                if ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False):
                    continue
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.decorator_list:
                    raise ValueError('No module-level decorators')
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                ast.literal_eval(node.value)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not all(isinstance(t, ast.Name) for t in targets):
                    raise ValueError('Module constants must use plain names')
                continue
            raise ValueError('Only imports, definitions, literal constants, and the standard unittest main guard at module level')
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split('.')[0] not in ALLOWED_IMPORTS for alias in node.names):
                    raise ValueError('Import outside the pilot test contract')
            if isinstance(node, ast.ImportFrom):
                if node.level or not node.module or node.module.split('.')[0] not in ALLOWED_IMPORTS:
                    raise ValueError('Import outside the pilot test contract')
                if any(alias.name == '*' for alias in node.names):
                    raise ValueError('Wildcard imports are not allowed')
                if any(alias.name in FORBIDDEN_NAMES | FORBIDDEN_ATTRIBUTES for alias in node.names):
                    raise ValueError('Forbidden runtime capability imported through an alias')
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                raise ValueError('Dynamic execution/introspection is outside the pilot contract')
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
                raise ValueError('Runtime/source introspection or process control is outside the pilot contract')
            if isinstance(node, ast.FunctionDef):
                if node.name in {'run', 'load_tests', 'id', 'countTestCases', 'skipTest', 'doCleanups'}:
                    raise ValueError('Do not override test-runner behavior')
                for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    ast.literal_eval(default)
            if isinstance(node, ast.ClassDef):
                if node.keywords:
                    raise ValueError('Metaclasses are outside the pilot contract')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {'skip', 'skipIf', 'skipUnless', 'expectedFailure'}:
                raise ValueError('Skipped/expected-failure tests are not accepted')
        files[path.name] = text
    if not 1 <= len(files) <= 16 or sum(len(s.encode()) for s in files.values()) > 131072:
        raise ValueError('Submit 1–16 test files totaling at most 128 KiB')
    return files
