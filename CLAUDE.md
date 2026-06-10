# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**marshmallow_core** is a standalone, opt-in Rust acceleration core for the
published [marshmallow](https://github.com/marshmallow-code/marshmallow) library.
It installs *alongside* an unmodified `pip install marshmallow` and, when
activated, monkey-patches `Schema._serialize` / `Schema._do_load` to run a PyO3
extension instead of marshmallow's per-object Python loops. It is **strictly a
speedup**: with the core installed, `dump`/`load` produce results identical to
stock marshmallow, falling back to pure Python for anything not modelled
natively (and, on load, for *every* error/edge case).

Activation is explicit and process-wide:

```python
import marshmallow_core
marshmallow_core.install()      # patch marshmallow.Schema
...                             # use marshmallow as usual
marshmallow_core.uninstall()    # restore the stock methods
```

This is a mixed Python/Rust project built with **maturin** (`flit`-style src
layout under `python/`, Rust under `src/`). Python 3.10+ ; the extension is
`abi3` so one wheel covers all supported versions.

## Provenance

The Rust core (`src/lib.rs`) and the compiler (`python/marshmallow_core/_compiler.py`)
were lifted from marshmallow's own `rust-core` branch — `crates/marshmallow-core/src/lib.rs`
and `src/marshmallow/_accel.py` respectively. The crucial difference: in that
branch the core is wired *into* `schema.py`; here it lives outside marshmallow
and patches in from the outside via `_patch.py`. When fixing core logic, the
upstream branch is the reference implementation to diff against.

## Commands

Requires `cargo` (rustup) and [`maturin`](https://www.maturin.rs/) (run via `uvx`).

```bash
# Build the Rust core + install the package into the current venv (iterating):
uvx maturin develop --release

# Build a wheel without installing (verifies Rust compiles + maturin metadata):
uvx maturin build --release            # -> target/wheels/marshmallow_core-*.whl

# Run the suite. IMPORTANT: test against STOCK marshmallow, not a checkout of
# the fork (the fork has its own baked-in accel and would double-patch). Use a
# throwaway venv with marshmallow from PyPI:
WHEEL=$(ls -t target/wheels/*.whl | head -1)
uv venv /tmp/mc && uv pip install --python /tmp/mc/bin/python marshmallow pytest "$WHEEL"
/tmp/mc/bin/python -m pytest -q                       # whole suite
/tmp/mc/bin/python -m pytest tests/test_equivalence.py::test_load_equivalence -q   # one test
MARSHMALLOW_NO_ACCEL=1 /tmp/mc/bin/python -m pytest -q -k "not core_active and not protocol"  # pure-Python path
```

`MARSHMALLOW_NO_ACCEL=1` disables the core even after `install()` (the
pure-Python path is always correct). CI (`.github/workflows/ci.yml`) runs the
suite on py3.10–3.13 both with the core active and disabled.

## Architecture

Three Python modules plus one Rust file. Read `_patch.py`, then `_compiler.py`,
then `src/lib.rs`.

### `_patch.py` — the install() shim (replaces the fork's `schema.py` edits)

`install()` saves and overwrites `Schema._serialize` and `Schema._do_load`.
The wrappers try the compiled core and otherwise call the saved originals:

- The compiled serializer/deserializer is **cached per Schema instance** on
  `vars(schema)["_mc_dump_serializer"]` / `["_mc_load_deserializer"]` — `_UNSET`
  (not built), `None` (not compilable → always pure Python), or a core object.
- `_do_load` only attempts the core when the call uses the schema's own
  `unknown`, `partial` is `True`-or-falsy, and the schema has **no load hooks**
  (`_has_load_hooks` checks `pre_load`/`post_load`/`validates`/`validates_schema`).
  On `AccelFallback` from the core it falls through to the original `_do_load`.

**Key divergence from the upstream branch:** there, a *root* schema with load
hooks still uses the core for the per-field step with Python running the hooks
*around* it. That split lives *inside* `_do_load` and cannot be reproduced by
wrapping the method from outside, so here **hook-bearing schemas use the
pure-Python load path entirely**. Correct, just not accelerated. Dump is fully
accelerated regardless of dump hooks (they run via `dump`, not `_serialize`).

### `_compiler.py` — Schema → payload (the "compiler")

Inspects a *bound* Schema and compiles it into a recursive tuple "payload"
describing each field as either **native** (formatted/parsed entirely in Rust)
or a **callback** (defers to the Python `Field.serialize`/`deserialize`).
`build_dump_serializer` / `build_load_deserializer` return `None` to mean "use
pure Python". `is_available()` gates everything (extension importable + protocol
match + not `MARSHMALLOW_NO_ACCEL`). It reads only attributes present in stock
marshmallow >=3.23 (across both the 3.x and 4.x lines); where the two differ
(e.g. `marshmallow.constants` is 4.x-only, field-level `pre_load`/`post_load`
are 4.x-only) it imports from the common location or probes with `getattr`.

### `src/lib.rs` — the PyO3 core

Parses payloads into `DumpSerializer` / `LoadDeserializer`, built as the
extension `marshmallow_core._core` (`[lib] name = "_core"`, `module-name =
"marshmallow_core._core"`). The **load** core handles only the happy path: the
instant it hits any error/edge case it raises `AccelFallback` and Python re-runs
the unchanged pure-Python load, so every message/value matches exactly.
`KeyboardInterrupt`/`SystemExit` from a callback propagate unchanged (never
converted to `AccelFallback`). The **dump core has no `AccelFallback`** — it
can't defer mid-serialization — so every native dump element must be provably
identical to `Field._serialize`.

### Invariants when changing the core

- **Dump and load tag spaces are distinct integers** and must stay in sync
  between `_compiler.py` and `lib.rs`. When adding a native field type, add it
  in *both* (build the element tuple in `_compiler.py`, parse + apply in
  `lib.rs`), keep tags aligned, and verify accel-on output equals accel-off
  output across **valid and error** inputs (`tests/test_equivalence.py`).
- The extension exports `PROTOCOL_VERSION`; `_compiler._EXPECTED_PROTOCOL` must
  match it. Bump both together when payload shapes/tags change.
- **What stays pure-Python:** custom `dict_class`/`get_attribute`,
  self-referential schemas, callable defaults, field-level `pre_load`/`post_load`,
  `Function`/`Method` (the work *is* a Python call), a `Nested` whose inner
  schema overrides `load`/`_do_load`/`_deserialize` (load) or `dump`/`_serialize`
  (dump) directly rather than via the hook system — e.g. `marshmallow_dataclass`,
  which overrides `load` to build a dataclass and leaves `_hooks` empty; compiling
  it natively would drop the instantiation — and any field type without a native
  element. **Now accelerated** (Phase 1/3): `unknown=INCLUDE`,
  collection/dotted `partial`, dotted attribute writes (`set_value`),
  `Range`/`Length`/`OneOf`/`Equal`/`NoneOf`/`ContainsOnly` validators (and **any
  other validator** — custom callables, `Email`/`URL`/`Regexp` — via the
  `_V_PYTHON` arm, which runs it in the core and falls back on failure),
  `Decimal`/`Dict`/`Constant` fields (typed `Dict` including inner key/value
  *validators*), the `IP`/`IPv4`/`IPv6`/`IPInterface` family, `Email`/`Url` load,
  custom (non-ISO) strptime temporal formats on load,
  schema-level load hooks (`pre_load`/`post_load`/`validates`/`validates_schema`
  run in Python around the core's per-field step), `dumps` (fused to JSON in
  Rust), and `loads` (**fused**: `_patched_loads` parses JSON with the pure-Rust
  `jiter` tree and deserializes straight off it via `LoadDeserializer.run_json`,
  skipping the intermediate Python dict `json.loads` would build — 1.2–1.8× over
  the prior `json.loads` + accelerated load. `Nested`/`List`/`Dict`/typed-`Dict`/
  `Tuple` are threaded through the tree so a list-of-records never materialises an
  intermediate. It defers to stock `loads` for callback fields, load hooks, a
  non-`json` render module, extra kwargs, or big-int payloads, since jiter is
  built without `num-bigint` to keep its pyo3 optional dep out of our build).

## Testing conventions

`tests/test_equivalence.py` is the source of truth: an autouse fixture calls
`install()`, and each `_dump_both`/`_load_both` helper runs once accelerated,
then `monkeypatch`es `_compiler.build_*` to `None` to force the pure path, and
asserts the two are equal. `tests/test_smoke.py` is a quick install/uninstall +
equivalence sanity check. New native fields need a case in `test_equivalence.py`.

## Development

 - commit only after passing tests
 - for every big part create a new branch 
 - for every small task(feature) make a commit
 - do not commit thing that i'nt make functionality
