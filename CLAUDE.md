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
marshmallow 4.x.

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
- **What stays pure-Python:** collection/dotted `partial` (boolean `partial=True`
  *is* accelerated), `unknown=INCLUDE`, custom `dict_class`/`get_attribute`,
  self-referential schemas, custom strptime temporal formats,
  `NaiveDateTime`/`AwareDateTime` on load, callable defaults, field validators
  /pre/post-load, and any field type without a native element.

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
