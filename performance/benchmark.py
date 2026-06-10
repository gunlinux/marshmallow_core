"""Stock-vs-core benchmark for marshmallow_core.

Times ``dump``/``load``/``dumps``/``loads`` for each representative schema with
the core *uninstalled* (stock pure-Python marshmallow) and *installed* (the Rust
core), then prints a comparison table with the speedup ratio.

The core is toggled through the public :func:`marshmallow_core.install` /
:func:`uninstall` API — *not* ``MARSHMALLOW_NO_ACCEL`` — so the benchmark
measures exactly what an application sees when it opts in.

Usage::

    python -m performance.benchmark            # all cases, default iterations
    python -m performance.benchmark --number 20000 --only flat,list
"""

from __future__ import annotations

import argparse
import gc
import time
import typing

import marshmallow_core

from performance.schemas import CASES


def _measure(fn: typing.Callable[[], typing.Any], number: int, repeat: int) -> float:
    """Return the best per-call time (seconds) over ``repeat`` batches."""
    fn()  # warm up (triggers the lazy per-schema compile)
    best = float("inf")
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(repeat):
            start = time.perf_counter()
            for _ in range(number):
                fn()
            elapsed = time.perf_counter() - start
            best = min(best, elapsed / number)
    finally:
        if gc_was_enabled:
            gc.enable()
    return best


def _ops(fn, number, repeat) -> float:
    return _measure(fn, number, repeat)


def _bench_case(
    schema_cls, sample, number: int, repeat: int
) -> dict[str, tuple[float, float]]:
    """Return ``{op: (stock_seconds, core_seconds)}`` for one schema."""
    # Build the four callables against a fresh schema instance. A new instance
    # per (stock/core) run avoids reusing a cached compiled serializer across
    # the install boundary.
    results: dict[str, tuple[float, float]] = {}

    def make_ops(schema):
        dumped = schema.dump(sample)
        dumped_json = schema.dumps(sample)
        return {
            "dump": lambda: schema.dump(sample),
            "load": lambda: schema.load(dumped),
            "dumps": lambda: schema.dumps(sample),
            "loads": lambda: schema.loads(dumped_json),
        }

    marshmallow_core.uninstall()
    stock = {op: _ops(fn, number, repeat) for op, fn in make_ops(schema_cls()).items()}

    marshmallow_core.install()
    core = {op: _ops(fn, number, repeat) for op, fn in make_ops(schema_cls()).items()}
    marshmallow_core.uninstall()

    for op in stock:
        results[op] = (stock[op], core[op])
    return results


def _print_table(name: str, results: dict[str, tuple[float, float]]) -> None:
    print(f"\n=== {name} ===")
    print(f"  {'op':<7} {'stock (us)':>12} {'core (us)':>12} {'speedup':>9}")
    for op in ("dump", "load", "dumps", "loads"):
        stock, core = results[op]
        ratio = stock / core if core else float("inf")
        print(f"  {op:<7} {stock * 1e6:>12.2f} {core * 1e6:>12.2f} {ratio:>8.2f}x")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", type=int, default=5000, help="calls per batch")
    parser.add_argument("--repeat", type=int, default=5, help="batches (best wins)")
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="comma-separated subset of cases (flat,nested,list,validator)",
    )
    args = parser.parse_args(argv)

    if not marshmallow_core.is_available():
        print(
            "WARNING: the compiled core is unavailable "
            "(extension not built or MARSHMALLOW_NO_ACCEL set); "
            "'core' columns will equal 'stock'."
        )

    selected = args.only.split(",") if args.only else list(CASES)
    for name in selected:
        schema_cls, sample = CASES[name]
        results = _bench_case(schema_cls, sample, args.number, args.repeat)
        _print_table(name, results)


if __name__ == "__main__":
    main()
