# Architecture review — issues and speed opportunities

Findings from a full read of `_patch.py`, `_compiler.py`, `src/lib.rs`, and the
performance harness, dated 2026-06-11 (v0.1.9, marshmallow 4.3.0, CPython 3.12).
Claims marked **[measured]** were reproduced with the scripts inlined under each
item; re-run them before acting if the core has changed since.

Severity: **critical** = user-visible breakage of the "strictly a speedup"
contract · **high** = real performance/maintenance liability · **medium** =
should fix, no urgency · **low** = note for later.

## Resolution status (branch `fix/arch-issues`)

All of section **A** is addressed; section **B** (speed) is untouched.

| Issue | Status | Where |
|-------|--------|-------|
| A1 truthy non-bool `many` | **fixed** | `_patch.py` coerces `bool(...)` at every boundary + regression test |
| A2 `_accelerated_load` pins to internals | **fixed** | `install()` runs `_accel_load_supported()` tripwire; degrades hook loads on mismatch |
| A3 hand-synced tag spaces | **fixed** | `tests/test_protocol.py` round-trips every tag + exhaustiveness guard |
| A4 stale dump-fallback docs | **fixed** | corrected `lib.rs` header, `CLAUDE.md`, `_compiler.py` docstring, dead refs |
| A5 no Rust unit tests | **fixed** | `#[cfg(test)]` for `json_escape_into`/`lookup_last`; `extension-module` feature-gated so `cargo test` links |
| A6 version drift | **fixed** | `Cargo.toml` version annotated as deliberately unused |
| A7 per-instance cache | **documented** | README: reuse schema instances (per-class cache stays rejected, by design) |
| A8 free-threading unaudited | **documented** | README: 3.13t declared unsupported |

Section **B** (speed), branch `perf/b1-fused-loads-single-pass`:

| Issue | Status | Result |
|-------|--------|--------|
| B1 fused `loads` quadratic | **fixed** | single-pass `run_one_json` (data_key→slot map); 100-field load 575→99µs, now flat ~0.44× of `json.loads`+load at every width |
| B2 doomed `run_json` for callbacks | open | next |
| B3 dict-path unknown alloc | open | (bundle with B2) |
| B4 `get_one` re-probe | open | profile-gated |

---

## A. Architecture issues

### A1 (critical) — truthy non-bool `many` crashes dump/dumps/loads [measured]

Stock marshmallow stores `Schema(many=1)` raw (`self.many == 1`) and treats it
truthily everywhere. Three of the four patched entry points hand it to a PyO3
`many: bool` parameter, which rejects non-`bool` with a `TypeError`:

```
stock dump:  [{'a': 1, 'b': 'x'}]            # works
core  dump:  TypeError: argument 'many': 'int' object cannot be cast as 'bool'
core  dumps: TypeError ...
core  loads: TypeError ...
core  load:  works (only _patched_do_load coerces with bool())
```

This is not a fallback-to-Python case — the `TypeError` escapes to the user, so
the core *changes behaviour* for code that works on stock marshmallow.

- `_patch.py:127` — `_patched_serialize` receives `many=self.many` raw from
  `Schema.dump` and passes it to `ds.run(obj, many)`.
- `_patch.py:322` — `_patched_dumps`: `js.run_json(obj, self.many if many is None else bool(many))`
  — the `self.many` arm is uncoerced.
- `_patch.py:366` — `_patched_loads`: same pattern.
- `_patch.py:154` — `_patched_do_load` coerces correctly; copy that.

**Fix:** `bool(...)` at every Rust boundary (cheap), and add an equivalence test
with `many=1`. Alternatively accept `Bound<PyAny>` + `is_truthy()` in Rust, but
the Python-side coercion is one line per call site.

### A2 (high) — `_accelerated_load` pins to marshmallow's private internals

`_patch.py:204-299` is a line-for-line transcription of `Schema._do_load`'s
body, calling the private `_invoke_load_processors` / `_invoke_field_validators`
/ `_invoke_schema_validators` and branching on `_MA4` (probed via
`inspect.signature` of a private method, `_patch.py:64`). A marshmallow point
release that reorders that body or renames an invoker changes accelerated-load
semantics silently — the equivalence suite only protects the versions in CI's
matrix.

**Mitigations** (no full fix exists for an out-of-tree patcher):
- Pin an upper bound tested in CI (`marshmallow<5` is already the dep; CI tests
  3.x+4.x lines — keep that).
- Add a source-hash tripwire: at `install()`, hash
  `inspect.getsource(Schema._do_load)` against the hashes of the versions the
  transcription was verified for; on mismatch, route hook-bearing schemas to the
  pure-Python path (still correct, just unaccelerated) instead of trusting the
  transcription.

### A3 (high) — element tag spaces are hand-synchronized across two languages

Dump tags (`_compiler.py:81-97` ↔ `lib.rs parse_element`), load tags
(`_compiler.py:100-119` ↔ `lib.rs parse_load_element`), and validator tags
(`_compiler.py:122-128` ↔ `lib.rs parse_validator`) are bare integers maintained
in parallel by hand. `PROTOCOL_VERSION` catches a *stale build*, not a
*mis-synced edit* committed in the same change — nothing fails at build time if
a tag is added on one side with the wrong number; you find out via an equivalence
failure (or, for an element only exercised on an untested shape, not at all).

**Fix options**, cheapest first:
1. A test that imports `_compiler`, reflects its `_L_*`/`_V_*`/dump constants,
   and round-trips every tag through a tiny payload into the extension (a
   `parse-only` debug entry point), failing on `unknown element tag`.
2. Generate both sides from one table (a small `tags.toml` + codegen in
   `build.rs` and a generated `_tags.py`). More moving parts; only worth it if
   tags keep churning.

### A4 (medium) — stale and self-contradictory documentation of the dump fallback

The code grew a dump-side `AccelFallback` (in `Tuple` length mismatch
`lib.rs:589`, `DictTyped` non-dict `lib.rs:565`, `write_json_value` unencodable
`lib.rs:306`, and `_patched_serialize` catches it at `_patch.py:135`), but three
places still assert the opposite:

- `lib.rs:16-20` header: "the dump path has **no `AccelFallback` safety net**".
- `CLAUDE.md`: "The **dump core has no `AccelFallback`**".
- `_compiler.py:1` module docstring describes the module as dump-only (it is
  half load), documents a 6-tag wire format (there are 17 dump / 20 load tags),
  and `lib.rs:20` points new equivalence tests at `tests/test_accel.py`, which
  does not exist (`tests/test_equivalence.py` is the suite).

For a codebase whose safety story is "every native element is provably identical
or it defers", the *rules for when deferring is possible* being wrong in the
docs is how a future change picks the wrong pattern. Rewrite the three blocks;
keep the invariant statement in one place (CLAUDE.md) and reference it.

### A5 (medium) — no Rust-side unit tests

`cargo test` runs zero tests; all coverage flows through the Python equivalence
suite. That is the right *primary* harness (parity is the product), but the
pure-Rust leaf functions are unit-testable cheaply and their edge cases are
expensive to reach from Python:

- `json_escape_into` (`lib.rs:175`) — surrogate pairs, control chars, long clean
  runs; a proptest against `serde_json`-style escapes would document the
  CPython-compat contract.
- `lookup_last` / duplicate-key semantics (`lib.rs:1271`).

### A6 (low) — version drift between packaging layers

`pyproject.toml` is 0.1.9; `Cargo.toml` is 0.1.0. Harmless (the wheel version
comes from pyproject) but confusing in backtraces and `cargo` output. Either
sync it in the release flow or comment in Cargo.toml that the field is unused.

### A7 (low) — per-instance compile cache

Compiled plans cache on `vars(schema)`, so an app constructing a schema per
request re-runs `_build_load_payload` every time. A per-class cache was
investigated and rejected (NEXTBACKLOG: payloads capture instance-bound state —
`field._serialize` bound methods, resolved `only`/`exclude`/`unknown`), which is
the right call; recording it here so it isn't re-litigated. The practical advice
belongs in README: reuse schema instances (stock marshmallow rewards that too).

### A8 (low) — process-wide patching vs. free-threaded CPython

`install()` swaps class attributes and the hook caches are unlocked
`WeakKeyDictionary`s (`_patch.py:101-102`). Fine under the GIL; under 3.13t the
races are benign-looking (double compile, lost cache write) but unaudited, and
`#[pyclass]` state (`Py<PyAny>` fields) is shared. Before claiming free-threaded
support: audit, or declare unsupported explicitly.

---

## B. Speed opportunities

Ordered by measured impact. B1/B2 are regressions/waste in the current design —
fix before adding features; the rest are incremental.

### B1 (high) — fused `loads` goes quadratic in schema width [measured]

`run_one_json` calls `lookup_last` (`lib.rs:1271`) per field spec, and
`lookup_last` scans the whole jiter object — O(fields × keys) per record, vs the
dict path's hashed O(fields). The unknown-key pass (`lib.rs:1571-1587`) then
rescans all keys, allocating a fresh `PyString` per key for the frozenset probe.
Measured (20 records/list, ints, `uv run python`, release build):

| fields | fused `loads` | `json.loads`+`load` | fused/unfused |
|-------:|--------------:|--------------------:|--------------:|
|      5 |        8.4 µs |             10.6 µs |         0.79× |
|     20 |       39.6 µs |             42.6 µs |         0.93× |
|     50 |      175.9 µs |            114.6 µs |     **1.53×** |
|    100 |      575.0 µs |            234.8 µs |     **2.45×** |

Past ~25 fields the "fused" path is *slower* than the path it replaces, and it
wins by under 10% well before that. Two-part fix:

1. **Invert the loop** (proper fix): at `parse_load_serializer` time, build a
   Rust `HashMap<String, usize>` from `data_key` → spec index. Per record,
   iterate the JSON object *once*: known keys fill a `Vec<Option<&JsonValue>>`
   slot (last-wins falls out naturally), unknown keys handle RAISE/INCLUDE in
   the same pass with `&str` comparisons — no per-key `PyString`, no frozenset
   probe. Then walk specs in order applying missing/partial/default logic.
   O(fields + keys), and `known_keys`/`lookup_last` disappear.
2. **Stopgap** (one line, if 1 waits): have `_patched_loads` skip `run_json`
   when `len(specs)` exceeds ~20 and take `json.loads` + accelerated `load`.

Same single-pass structure also removes the repeated
`data_key.bind(py).to_str()?` per field per record (`lib.rs:1526`).

### B2 (medium) — doomed `run_json` attempt for callback-bearing schemas [measured]

A payload containing any `Callback` spec can never finish `run_one_json`
(`lib.rs:1522-1525` defers on the first one) — but `_patched_loads` still parses
the *entire* JSON text with jiter first, walks to the first record, falls back,
and `json.loads` re-parses everything. Measured on a 3-field schema (one
`Function` field), 100 records: 73.3 µs vs 65.0 µs going straight to the stock
path — **13% of every `loads` call burned**, scaling with payload size.

The compiler knows this statically. **Fix:** compute `fusable` (no callback spec
anywhere in the payload tree, transitively through `Nested`/`List`/`Pluck`) in
`_build_load_payload`, expose it (a tuple field or a `LoadDeserializer` getter),
and store it in the cached `_mc_load_plan` so `_patched_loads` skips `run_json`
entirely. The dump side does **not** need this — callback dump fields produce
ordinary encodable values, so fused `dumps` genuinely can finish.

### B3 (medium) — unknown-key handling allocates per key (dict path too)

Even on the `PyDict` path, RAISE/INCLUDE do a Python `frozenset.contains` per
input key (`lib.rs:1435-1452`). With the B1 inversion the JSON path gets this
for free; for the dict path, storing `known_keys` additionally as a Rust
`HashSet<String>` and comparing `to_str()?` against it avoids the Python set
protocol per key. Only worth it bundled with B1's parse-time restructuring.

### B4 (low) — `get_one` re-probes `hasattr(__getitem__)` per field per record

`lib.rs:850` probes indexability for every field of every non-dict object dumped.
The object's *type* rarely changes mid-run; a one-entry `(type ptr → bool)` cache
in `run`/`write_json` (per invocation, not global — types can be redefined)
would drop F−1 of F probes per record. Profile first: attribute-dump workloads
only.

### B5 — already investigated, do not redo (see FAIRBACKLOG.md)

Kept here so the list above isn't "completed" by reopening these: native float
formatting (no byte parity with `float.__repr__`), datetime isoformat in Rust
(abi3 lacks the datetime C accessors), presized dict ctor (private API), SIMD
JSON tokenizer (object construction dominates), per-class payload cache (A7),
Email/URL native regex (parity risk). Structural ceilings: hook-bearing loads
cap ~2× (Python dispatch around the core), `loads` lower bound is the object
construction itself.

### Process note — benchmarks are not regression-gated

`performance/RESULTS.md` is updated by hand; nothing in CI would catch B1-style
regressions (it shipped). Cheapest guard: a CI job running
`python -m performance.benchmark --number 500` and failing if any core/stock
ratio drops below 1.0 (a "never slower than stock" floor), plus a wide-schema
case (50+ fields) in `performance/schemas.py` — the current cases top out at
~10 fields, which is exactly why B1 was invisible.
