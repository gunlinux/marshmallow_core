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
layout under `python/`, Rust under `src/`). Python 3.10+ ; the extension builds
a per-version wheel (`cpXY-cpXY-...`) for each supported interpreter rather
than a single `abi3` wheel — dropped deliberately so the core can use
CPython's datetime C accessors, which aren't in the limited API.

## Provenance

The Rust core (`src/lib.rs`) and the compiler (`python/marshmallow_core/_compiler.py`)
were lifted from marshmallow's own `rust-core` branch — `crates/marshmallow-core/src/lib.rs`
and `src/marshmallow/_accel.py` respectively. The crucial difference: in that
branch the core is wired *into* `schema.py`; here it lives outside marshmallow
and patches in from the outside via `_patch.py`. When fixing core logic, the
upstream branch is the reference implementation to diff against.

## Commands

Requires `cargo` (rustup), [`maturin`](https://www.maturin.rs/) (run via `uvx`),
and `uv`. The Makefile is the front door:

```bash
make dev        # uv sync --dev (pytest, ruff, pyright)
make develop    # uvx maturin develop --release — build the core into the venv
make check      # lint + types + test, both languages (run before committing)
make fix        # ruff --fix/format + cargo fmt/clippy --fix
make py-test    # rebuilds the core, then uv run pytest
```

```bash
# Iterating on tests directly (after make develop):
uv run pytest -q
uv run pytest tests/test_equivalence.py::test_load_equivalence -q   # one test

# Full-fidelity run: builds the wheel and tests it in a throwaway venv against
# STOCK marshmallow from PyPI (never a checkout of the fork — it has its own
# baked-in accel and would double-patch). Extra args go to pytest:
./run_tests.sh -q
MARSHMALLOW_NO_ACCEL=1 ./run_tests.sh -q -k "not core_active and not protocol"  # pure-Python path

# Benchmark stock vs core (dump/load/dumps/loads per case, speedup table):
uv run python -m performance.benchmark               # all cases
uv run python -m performance.benchmark --only flat,list --number 20000

# Coverage probe: which fields run native in Rust vs fall back to Python callback:
uv run python -m performance.analyze_paths
```

`MARSHMALLOW_NO_ACCEL=1` disables the core even after `install()` (the
pure-Python path is always correct). CI (`.github/workflows/ci.yml`) runs the
suite on py3.10–3.13 both with the core active and disabled. Past benchmark
numbers live in `performance/RESULTS.md`.

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
  `unknown` and `partial` is a bool or a name collection. Hook-free schemas call
  the core directly; schemas with load hooks
  (`pre_load`/`post_load`/`validates`/`validates_schema`) go through
  `_accelerated_load` — a verbatim transcription of `Schema._do_load` with the
  per-field `self._deserialize(...)` call replaced by the core, so Python runs
  the hooks *around* the Rust per-field step (same split as the upstream
  branch). On `AccelFallback` either path falls through to the original
  `_do_load`.

Because `_accelerated_load` transcribes `_do_load`'s body, it pins to
marshmallow's private hook invokers (both the 3.x and 4.x shapes, branching on
`_MA4`). `install()` runs `_accel_load_supported()` — a structural check that
those invokers still exist with the expected keyword parameters — and on a
mismatch (an untested future marshmallow) routes hook-bearing schemas to the
pure-Python load instead: correct, just unaccelerated. Dump is fully
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
converted to `AccelFallback`). The **dump core has only a *limited*
`AccelFallback`**: a few elements raise it for a shape they can't reproduce (a
`Tuple` length mismatch, a non-dict `DictTyped`, a value the JSON writer can't
encode) and `_patched_serialize` discards the partial result and re-runs pure
Python (safe — dump has no side effects). It is **not** a general safety net,
though: an element that silently produces the *wrong* value is never caught, so
every native dump element must still be provably identical to `Field._serialize`
for every input it accepts, and must defer on any shape it does not.

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
  custom (non-ISO) strptime temporal formats on load, ISO `DateTime`/`Date`/`Time`
  dump (`_TEMPORAL_NATIVE` — formats directly off the C-level date/time struct
  accessors instead of calling Python's `isoformat()`; non-ISO formats still go
  through the callback `_TEMPORAL` element),
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

### Per-instance cache contract (N3)

The compiled dump/load plan is built once per schema *instance* on first use
and cached on `vars(schema)`. **A schema instance is immutable after its first
accelerated call.** Mutating `schema.fields["x"].validators` (or any other
field state) after the first `dump`/`load` is not reflected in the cached plan
— the native path keeps using the compiled-at-first-use snapshot while the
pure-Python path reads live state. This is a contract boundary, not a bug;
recompiling per call would erase the speedup and detecting arbitrary field
mutation is not tractable. The fix is to build a fresh `Schema()` instance or
call `marshmallow_core.invalidate(schema)` (drops all three cache keys and
forces a recompile on the next call) before reconfiguring an existing instance.

## Testing conventions

`tests/test_equivalence.py` is the source of truth: an autouse fixture calls
`install()`, and each `_dump_both`/`_load_both` helper runs once accelerated,
then `monkeypatch`es `_compiler.build_*` to `None` to force the pure path, and
asserts the two are equal. New native fields need a case there.

The other three files cover what equivalence can't:

- `tests/test_protocol.py` — tag-sync contract: reflects every `_*`/`_L_*`/`_V_*`
  tag constant from `_compiler.py` and round-trips a minimal payload per tag
  through the extension's constructors, so a tag added or renumbered on the
  Python side without a matching Rust arm fails here instead of surfacing as a
  missed equivalence case. **A new tag needs an entry in its `_MIN_*` table**
  (a completeness guard trips otherwise).
- `tests/test_contract.py` — invariant *preconditions* (F_ARCHREVIEW R1–R7):
  one-shot iterables are replayable, errors don't leak as wrong exception
  types, cached compiled state doesn't diverge from live schema state,
  `uninstall()` doesn't clobber foreign patches.
- `tests/test_smoke.py` — quick install/uninstall + equivalence sanity check.

## Architecture review & planning surface

`F_REAL_REVIEW.md` is the primary architecture/planning document. It carries:

- Benchmark baselines (use these to confirm no regression before landing perf changes).
- Findings N1–N6 with their fix status (N1–N4 done; N5 profiled/skipped; N6 low, noted).
- The standing won't-do list — deliberate non-features not to re-implement.

### Deliberate non-features (do not re-litigate)

These were evaluated, measured, and rejected. Full rationale is in `F_REAL_REVIEW.md`.

- **Native float formatting in the JSON writer** — `ryu` differs from CPython `float.__repr__` (shortest-round-trip) on a measurable corpus. Stays `repr()`-via-Python.
- **Native `Regexp` validator** — `regex` crate vs `re` semantics cannot be guaranteed equal. Already handled via `_V_PYTHON` with fallback.
- **Per-class payload cache** — cross-instance sharing introduces R4-class staleness risk. Rejected.
- **Big-int fused `loads`** — jiter deliberately built without `num-bigint` to keep its pyo3 optional dep out of the build. Values >i64 fall back to stock `loads`.
- **hooks-case `loads` fusion** — would require running Python hooks mid-tree. The ~1.7x floor on `hooks loads` is accepted.

## Development

 - commit only after passing tests
 - for every big part create a new branch
 - for every small task (feature) make a commit
 - do not commit changes that don't add functionality
 - never commit unnecessary files to the repository — temp markdown notes,
   debug/scratch scripts, one-off benchmark dumps, etc. Delete them when done
   or keep them untracked (add to `.gitignore` if they recur)
