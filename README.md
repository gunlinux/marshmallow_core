# marshmallow_core

[![CI](https://github.com/gunlinux/marshmallow_core/actions/workflows/ci.yml/badge.svg)](https://github.com/gunlinux/marshmallow_core/actions/workflows/ci.yml)

A Rust acceleration core for [marshmallow](https://github.com/marshmallow-code/marshmallow),
shipped as a **separate, opt-in package**. Install it next to stock marshmallow
and activate it explicitly — it replaces marshmallow's per-object
`_serialize` / `_deserialize` loops with a PyO3 extension while producing
**identical** results.

```bash
pip install marshmallow marshmallow_core
```

```python
import marshmallow as ma
import marshmallow_core

marshmallow_core.install()      # patch marshmallow.Schema in this process

class Person(ma.Schema):
    name = ma.fields.String()
    age = ma.fields.Integer()

Person().load({"name": "ann", "age": "30"})   # accelerated
Person().dump({"name": "ann", "age": 30})      # accelerated

marshmallow_core.uninstall()    # restore the stock pure-Python methods
```

## How it works

- `install()` monkey-patches `Schema._serialize` and `Schema._do_load`. Each
  bound schema is compiled once (cached on the instance) into a recursive
  payload describing every field as either **native** (run entirely in Rust) or
  a **callback** (defers to the Python `Field` method). Anything not modelled
  natively stays a callback, so output is behaviour-identical.
- The **load** core handles only the happy path: the instant it hits any
  error/edge case it raises an internal `AccelFallback` and marshmallow re-runs
  the unchanged pure-Python load, so every error message and value matches
  exactly. The **dump** core has no fallback, so each native dump element is
  provably identical to `Field._serialize`.
- `dumps` is **fused**: it writes JSON bytes directly in Rust, skipping the
  intermediate Python dict and the `json.dumps` pass, byte-for-byte identical to
  `json.dumps(schema.dump(obj))`. It activates for hook-free schemas using the
  stdlib `json` render module with no extra `json` kwargs, and falls back to
  `dump` + `json.dumps` for anything it can't reproduce exactly. (`loads` is
  already accelerated through the patched load path; a Rust JSON *parser* was
  prototyped but did not beat CPython's C `json.loads`, so it was not shipped.)
- Acceleration is strictly a speedup. Set `MARSHMALLOW_NO_ACCEL=1` (or hit a
  protocol-version mismatch between the Python and Rust halves) and the core
  becomes a no-op even after `install()`.

## Scope / limitations

`install()` accelerates dump for all compilable schemas, and load for most
schemas — including those with `pre_load` / `post_load` / `validates` /
`validates_schema` hooks: the core runs the per-field deserialize step while
those hooks run in Python around it (mirroring marshmallow's own `_do_load`
split). Recognized field validators (`Range` / `Length` / `OneOf`) run natively;
any other validator, or field-level `pre_load` / `post_load`, keeps that field
on the callback path. `unknown=INCLUDE`, collection/dotted `partial`, and dotted
attribute writes are all accelerated. Natively modelled fields include the
scalars plus `Decimal`, `Dict`, `Constant`, `TimeDelta`, `Boolean` and
`Integer(strict=True)`; on **load**, also typed `Dict` (keys/values), `Tuple`,
and `Pluck` (these three stay on the callback path for *dump* — the dump core
has no fallback). Custom `dict_class` / `get_attribute`, self-referential
schemas, custom strptime temporal formats, and callable defaults always fall
back to pure Python.

### Where the speedup is limited

Some shapes are inherently bounded — the work the core can't move into Rust
dominates the call. These are *correct*, just not where the gains are:

- **Hook-bearing loads are the weakest case (~2x, vs ~7x without hooks).** When a
  schema has `pre_load` / `post_load` / `validates` / `validates_schema`, the core
  runs the per-field deserialize but marshmallow's Python hook-dispatch
  (`_invoke_load_processors`, `_invoke_field_validators`) runs around it. On a
  small schema the core step is ~8% of the load; the remaining ~90% is that
  Python machinery, which wraps user callbacks and cannot be moved into Rust.
- **Small / flat schemas are capped by fixed per-call overhead (~20–30%).** The
  `dump` / `load` entry prologue (argument normalization, the per-instance
  serializer-cache lookup, the partial/unknown checks) is constant per call, so
  it dominates exactly when the payload is tiny. It amortizes to near-zero as the
  payload grows — speedup on a list of records is flat regardless of length.
- **`loads` gains less than `load`.** JSON parsing still goes through CPython's C
  `json.loads`; a fused Rust parser was prototyped but did not beat it, so only
  the subsequent per-field step is accelerated.

For collections of records (the common hot path) the fixed overhead vanishes and
the speedup is steady (~7–8x). Run `performance/analyze_paths.py` to see whether a
given schema even reaches the core, and `performance/benchmark.py` to measure it.

## Development

Requires `cargo` (rustup) and [`maturin`](https://www.maturin.rs/).

```bash
# build + install the extension into the current venv
uvx maturin develop --release

# run the tests (needs marshmallow + pytest installed)
pytest

# force the pure-Python path
MARSHMALLOW_NO_ACCEL=1 pytest
```

`tests/test_equivalence.py` asserts that `dump`/`load` produce identical output
and errors with the core active vs. forced onto pure Python, across scalars,
nested/list/enum/temporal/UUID fields, `partial=True`, and error inputs.

## Benchmarking

The `performance/` directory (not shipped in wheels) measures the core against
stock marshmallow through the public `install()` / `uninstall()` API. Run it from
the repo root with the compiled extension importable (`uvx maturin develop
--release` first, or point `PYTHONPATH` at the repo while the wheel is installed):

```bash
# stock-vs-core table for dump / load / dumps / loads on four schema shapes
python -m performance.benchmark                       # all cases
python -m performance.benchmark --number 20000 --only flat,list

# coverage probe: per-field native vs callback for each schema shape
python -m performance.analyze_paths
```

`benchmark.py` reports per-call microseconds for stock and core plus the speedup
ratio, across flat-scalar, nested, list-heavy, and validator-heavy schemas.
`analyze_paths.py` inspects the compiled payload and shows which fields run
native in Rust vs. fall back to a Python callback — it tells you exactly where a
real schema still defers to pure Python.

## Releasing

CI (`.github/workflows/ci.yml`) builds the wheel and runs the suite against
stock marshmallow on Python 3.10–3.13, both with the core active and with
`MARSHMALLOW_NO_ACCEL=1`. Publishing (`.github/workflows/release.yml`) builds
abi3 wheels + sdist for Linux/macOS/Windows on a `v*` tag and uploads them to
PyPI via [trusted publishing](https://docs.pypi.org/trusted-publishers/). Before
the first release, configure the PyPI trusted publisher for this repo and create
a `pypi` GitHub Environment, then push a tag (e.g. `git tag v0.1.0 && git push
--tags`).

## License

MIT
