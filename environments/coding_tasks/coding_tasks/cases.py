"""Trusted implementations and deterministic bug catalog, excluded from prompts."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Case:
    function: str
    spec: str
    correct: tuple[Callable, ...]
    mutants: dict[str, Callable]
    gold: str
    check_args: Callable


def integer(x):
    return type(x) is int and abs(x) <= 10000


def int_list(x):
    return type(x) is list and len(x) <= 100 and all(integer(v) for v in x)


def unique_loop(xs):
    result = []
    for x in xs:
        if x not in result:
            result.append(x)
    return result


def chunk_loop(xs, size):
    result = []
    for start in range(0, len(xs), size):
        result.append(xs[start:start + size])
    return result


CASES = {
    "clamp": Case(
        "clamp",
        "clamp(x, lo, hi): for integers with lo <= hi, return x if lo <= x <= hi, "
        "lo if x < lo, and hi if x > hi. Equal bounds are allowed.",
        (lambda x, lo, hi: max(lo, min(x, hi)),
         lambda x, lo, hi: lo if x < lo else hi if x > hi else x),
        {"no_lower_bound": lambda x, lo, hi: min(x, hi),
         "no_upper_bound": lambda x, lo, hi: max(x, lo),
         "always_lower": lambda x, lo, hi: lo},
        "assert clamp(-2, 0, 5) == 0\nassert clamp(9, 0, 5) == 5\nassert clamp(3, 0, 5) == 3",
        lambda a: len(a) == 3 and all(integer(v) for v in a) and a[1] <= a[2],
    ),
    "unique": Case(
        "unique",
        "unique(xs): return a list containing each integer from xs once, in order "
        "of first occurrence. xs is a list of integers; empty input returns [].",
        (unique_loop, lambda xs: list(dict.fromkeys(xs))),
        {"sorts_output": lambda xs: sorted(set(xs)),
         "keeps_duplicates": lambda xs: list(xs),
         "drops_zero": lambda xs: unique_loop([v for v in xs if v != 0])},
        "assert unique([2, 0, 2, 1, 0]) == [2, 0, 1]\nassert unique([]) == []",
        lambda a: len(a) == 1 and int_list(a[0]),
    ),
    "chunks": Case(
        "chunks",
        "chunks(xs, size): split a list of integers into consecutive lists of "
        "at most size items. size is a positive integer. Keep a shorter final "
        "chunk. Empty input returns []. Preserve item order.",
        (lambda xs, size: [xs[i:i + size] for i in range(0, len(xs), size)], chunk_loop),
        {"drops_remainder": lambda xs, size: [xs[i:i + size] for i in range(0, len(xs) - size + 1, size)],
         "extra_empty": lambda xs, size: [xs[i:i + size] for i in range(0, len(xs) + 1, size)],
         "wrong_stride": lambda xs, size: [xs[i:i + size] for i in range(0, len(xs), size + 1)]},
        "assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n"
        "assert chunks([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]\nassert chunks([], 2) == []",
        lambda a: len(a) == 2 and int_list(a[0]) and integer(a[1]) and a[1] > 0,
    ),
    "insertion_index": Case(
        "insertion_index",
        "insertion_index(xs, target): xs is an ascending sorted list of integers. "
        "Return the first index where target could be inserted while keeping "
        "xs sorted (before existing equal values). Return len(xs) if target "
        "is greater than all elements; return 0 for empty input.",
        (bisect_left, lambda xs, target: sum(v < target for v in xs)),
        {"inserts_after_equal": bisect_right,
         "never_at_end": lambda xs, target: min(bisect_left(xs, target), max(0, len(xs) - 1)),
         "one_based": lambda xs, target: bisect_left(xs, target) + 1},
        "assert insertion_index([1, 2, 2, 4], 2) == 1\n"
        "assert insertion_index([1, 2], 3) == 2\nassert insertion_index([], 3) == 0",
        lambda a: len(a) == 2 and int_list(a[0]) and a[0] == sorted(a[0]) and integer(a[1]),
    ),
}
