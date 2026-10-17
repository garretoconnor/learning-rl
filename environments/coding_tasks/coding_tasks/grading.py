"""Interpret bounded assertion syntax; never exec/eval model-supplied Python."""
import ast
import copy
from dataclasses import asdict, dataclass
from coding_tasks.cases import CASES


@dataclass(frozen=True)
class Grade:
    score: float = 0.0
    valid: bool = False
    correct_passed: bool = False
    mutants_killed: int = 0
    mutants_total: int = 0
    assertions: int = 0
    killed: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self):
        return asdict(self)


def literal(node):
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or type(value) is bool:
            return value
        if type(value) is int and abs(value) <= 10000:
            return value
        if type(value) is str and len(value) <= 1000:
            return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        # Only one unary minus applied directly to an integer literal.
        if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
            value = -node.operand.value
            if abs(value) <= 10000:
                return value
    if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) <= 100:
        values = [literal(v) for v in node.elts]
        return values if isinstance(node, ast.List) else tuple(values)
    raise ValueError("Only bounded literal values are allowed")


def parse(source, case):
    if len(source) > 16000:
        raise ValueError("Submission exceeds 16000 characters")
    source = source.strip()
    if source.startswith("```"):
        lines = source.splitlines()
        if lines[0] not in ("```", "```python") or lines[-1] != "```":
            raise ValueError("Use one complete Python code fence")
        source = "\n".join(lines[1:-1])
    tree = ast.parse(source)
    if sum(1 for _ in ast.walk(tree)) > 3000:
        raise ValueError("Submission exceeds syntax complexity limit")
    if not 1 <= len(tree.body) <= 32:
        raise ValueError("Submit 1–32 assertions")
    tests = []
    for statement in tree.body:
        if not isinstance(statement, ast.Assert) or statement.msg is not None:
            raise ValueError("Each statement must be a plain assertion")
        expr = statement.test
        if not (isinstance(expr, ast.Compare) and len(expr.ops) == 1
                and isinstance(expr.ops[0], ast.Eq)):
            raise ValueError("Use assert FUNCTION(...) == EXPECTED")
        call = expr.left
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == case.function and not call.keywords):
            raise ValueError(f"Call only {case.function} with positional literal arguments")
        args = [literal(arg) for arg in call.args]
        if not case.check_args(args):
            raise ValueError("Arguments are outside the function's documented domain")
        tests.append((args, literal(expr.comparators[0])))
    return tests


def passes(implementation, tests):
    for args, expected in tests:
        try:
            if implementation(*copy.deepcopy(args)) != expected:
                return False
        except Exception:
            return False
    return True


def grade(case_id: str, source: str) -> Grade:
    case = CASES[case_id]
    total = len(case.mutants)
    try:
        tests = parse(source, case)
    except (ValueError, SyntaxError, RecursionError) as exc:
        return Grade(mutants_total=total, error=str(exc))
    if not all(passes(fn, tests) for fn in case.correct):
        return Grade(valid=True, mutants_total=total, assertions=len(tests),
                     error="Tests fail a correct implementation")
    killed = tuple(name for name, fn in case.mutants.items() if not passes(fn, tests))
    return Grade(score=len(killed) / total, valid=True, correct_passed=True,
                 mutants_killed=len(killed), mutants_total=total,
                 assertions=len(tests), killed=killed)
