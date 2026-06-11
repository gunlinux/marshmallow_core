# F_REAL_REVIEW — deep architecture & performance review

A full review of the codebase at 0.1.11 (protocol 18): complete read of
`_patch.py` (509 lines), `_compiler.py` (804), `src/lib.rs` (2903), the test
suite's structure, and a fresh benchmark run. Successor to the (now removed)
ARCH.md / F_ARCHREVIEW.md / F_SPEEDUP.md reviews and the BACKLOG/ROADMAP chain;
everything still open from those is carried forward here, so this file plus
`performance/RESULTS.md` is the complete planning surface.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12, marshmallow
  4.3.0, `maturin develop --release`, `python -m performance.benchmark`
  (number=5000, repeat=5, best-of). Dated 2026-06-11.
- Claims marked **[measured]** were reproduced this session; the repro for the
  one new critical finding is inlined.

Severity: **critical** = silent wrong results in plausible use · **high** =
correctness divergence or real maintenance hazard · **medium** = should fix,
no urgency · **low** = note.

**ID legend** — code comments and tests cite item IDs from the removed review
files; all are recoverable via `git show 'HEAD~1:<file>'` (or `git log --all
-- <file>`): **A1–A8/B1–B5** = ARCH.md · **R1–R9** = F_ARCHREVIEW.md ·
**F1–F4** = F_SPEEDUP.md · phase backlogs = BACKLOG/NEXTBACKLOG/FAIRBACKLOG/
typesbacklog/ROADMAP.md. New findings here are numbered **N1–N6** to avoid
colliding with any of those spaces.

## Verdict

The architecture is sound and the hard invariants hold up under a line-by-line
read: the dump/load split, the happy-path-only load core with `AccelFallback`,
the limited-and-documented dump fallback, the per-tag protocol guard
(`test_protocol.py` makes a desynced tag a test failure instead of a silent
misparse), the GC traverse/clear protocol, and the structurally-guarded
`_accelerated_load` transcription are all coherent and tested. The benchmark
shows 2–27× across every case with no regression case. One **critical** gap
survived all prior reviews: the R1 one-shot-iterable fix only covers the *top
level* of a dump, and the dump fallback can still silently destroy data fed
through a generator inside a nested-many or List field (N1, measured repro
below). Fix N1; the rest is polish.

## Benchmark (this session)

| case       | dump  | load   | dumps | loads  |
|------------|------:|-------:|------:|-------:|
| flat       | 3.96x | 10.68x | 3.67x |  8.85x |
| nested     | 9.42x | 20.88x | 6.68x | 14.59x |
| list       | 14.70x| 27.12x | 9.06x | 17.83x |
| validator  | 5.90x |  9.66x | 4.65x |  9.12x |
| hooks      | 5.00x |  2.09x | 4.24x |  1.73x |
| api        | 7.82x | 25.61x | 6.72x | 15.87x |
| non_ascii  | 3.75x |  6.62x | 2.16x |  2.11x |
| obj_source | 2.87x | 10.16x | 2.83x |  8.84x |

Consistent with `performance/RESULTS.md` (recorded post-B1/B2); no regression.
Reading of the floors:

- **hooks load/loads (2.09x/1.73x)** — the floor is marshmallow's Python hook
  dispatch around the core's per-field step, plus `loads` falling back to stock
  `json.loads` for hook-bearing schemas (the fused path can't run hooks
  mid-tree). Structural; not movable without reimplementing the hook system.
- **non_ascii dumps/loads (~2.1x)** — the `ensure_ascii` `\uXXXX` escaping is
  already bulk-copy + nibble-table optimized (F1); what remains is inherent
  per-char work both sides must do. Done.
- **obj_source dump (2.87x)** — attribute-source dumps pay a real `getattr`
  per field that dict sources don't; the single-slot `hasattr` cache (F3)
  already removed the probe overhead. Remaining gap is CPython attribute
  lookup itself. Done.
- Load beats dump everywhere because stock load carries far more per-field
  machinery (error store, validator dispatch, partial checks) for the core to
  bypass; stock dump was already lean.

## What is right (keep these properties)

- **The fallback asymmetry is correct and correctly documented**: load defers
  on *every* edge case (error parity is absolute); dump defers only where a
  shape can't be reproduced, and the doc comment in `lib.rs` is explicit that
  this is not a safety net — every native dump element must be provably
  identical. The code matches the doc at every element I checked.
- **`test_protocol.py`'s completeness guard** turns the two-language tag-space
  invariant from a convention into a failing test. This is the single best
  maintenance property in the repo.
- **`_accel_load_supported()`** bounds the `_accelerated_load` transcription's
  coupling to marshmallow's private `_do_load` internals: a rename/resignature
  in a future marshmallow degrades hook-bearing schemas to pure Python instead
  of diverging (see N4 for the residual risk).
- **`KeyboardInterrupt`/`SystemExit` discipline** via `to_fallback` is applied
  at every callback/held-method site on the load path (one slip: N2).
- **R-series fixes verified in code**: R1 (top-level materialize), R2 (GC
  traverse/clear, both classes), R3 (JSON depth budget), R4 (live `partial`
  recompute), R5 (root `_deserialize` override check in
  `_build_load_payload(is_root=True)`), R6 (lone-surrogate → fallback), R7
  (identity-checked uninstall). All present, all covered in
  `test_contract.py`.

## Findings

### N1 (critical) — nested one-shot iterables + dump fallback = silent data loss [measured]

R1 materializes a one-shot iterable only at the **top level** of
`_patched_serialize`/`_patched_dumps` (`many and not isinstance(obj,
(list, tuple)) -> list(obj)`). But the dump core consumes *inner* iterables
too: `Element::Nested(many)`, `Element::Pluck(many)`, and `Element::List` all
call `try_iter()` on whatever the attribute holds. If a **later** element then
raises the dump `AccelFallback` (non-dict `DictTyped`, `Tuple` length
mismatch, unencodable JSON value), `_patched_serialize` re-runs pure Python
against the *original* object — whose generator is now exhausted — and the
re-run **succeeds** with the field silently empty:

```python
from collections import UserDict
from marshmallow import Schema, fields
import marshmallow_core

class Inner(Schema):
    x = fields.Integer()

class S(Schema):
    a = fields.Nested(Inner, many=True)
    b = fields.Dict(keys=fields.String(), values=fields.Integer())

obj = {"a": ({"x": i} for i in range(3)), "b": UserDict({"k": 1})}
# stock:  {'a': [{'x': 0}, {'x': 1}, {'x': 2}], 'b': {'k': 1}}
# accel:  {'a': [],                              'b': {'k': 1}}   <- silent
```

(`UserDict` is just one trigger: it makes `DictTyped` defer while pure Python
handles any `Mapping`. The same happens with a `Tuple` sibling of the wrong
length when pure Python *recovers*, or an unencodable value on the fused
`run_json` path falling back to `dump` + `json.dumps`.)

The load path is immune by construction — `is_list_like` (list/tuple only)
gates every `many` iteration, so generators defer before being consumed.

**Fix:** make the dump arms defer *before* consuming a non-replayable
iterable, mirroring the load gate but without giving up re-iterable
containers: in `Element::Nested(many)` / `Pluck(many)` / `List` (both `apply`
and `write_json`), if the value is not list/tuple **and is an iterator**
(`PyIter_Check`, i.e. has `__next__`), raise `AccelFallback` up front. Sets,
dict views, ranges etc. are re-iterable and stay fast; generators take the
pure path, which handles them exactly once, correctly. Add a `_dump_both`
equivalence case (generator in nested-many + each fallback trigger as the
sibling) and a `test_contract.py` precondition test (this is R1's missing
half).

### N2 (low) — `Boolean` load swallows `KeyboardInterrupt`/`SystemExit`

`LoadElement::Boolean` maps *any* error from the `truthy`/`falsy` containment
check to `AccelFallback` (`.map_err(|_| fallback())`), unlike every other
user-code call site, which routes through `to_fallback` to let
KeyboardInterrupt/SystemExit propagate. A value whose `__hash__`/`__eq__`
raises KI during `value in truthy` gets the interrupt silently eaten and the
load retried. Pathological input, two-line fix: use `to_fallback` there.

### N3 (medium) — the per-instance compile cache has no invalidation; document the boundary

`_mc_dump_serializer` / `_mc_dump_json` / `_mc_load_plan` are built once per
instance and never invalidated. R4 fixed the one *runtime argument* that was
wrongly frozen (`partial`), but schema **state** mutated after first use still
diverges silently: append to `schema.fields["x"].validators` after the first
`load()` and the native path keeps validating against the compiled list —
invalid data passes that stock would reject. Stock marshmallow reads
everything live.

Recompiling per call would erase the speedup, and detecting arbitrary field
mutation is not tractable — so this is a **contract boundary, not a bug to
fix in code**. But today it is undocumented. Add to README + CLAUDE.md: *a
schema instance is treated as immutable after its first dump/load; mutate it
(or build a new instance) before first use.* Optionally expose
`marshmallow_core.invalidate(schema)` (three `vars(schema).pop` calls) for
the rare legitimate reconfigure-in-place user.

### N4 (medium) — the `_accelerated_load` guard catches renames, not semantics

`_accel_load_supported()` verifies the three private invokers exist with the
expected kwargs. A future marshmallow that keeps the signatures but changes
`_do_load`'s *logic* (ordering of validator passes, a new error-store step)
slips through and the transcription diverges until someone runs the
equivalence suite against that version. CI currently tests whatever PyPI
serves at run time — good, but reactive. Cheap insurance: add a CI job
against marshmallow's `--pre`/git-main so the suite fails *before* a release
ships, and pin the lowest supported 3.x in the matrix so both line endpoints
are always exercised.

### N5 (medium, perf) — unknown-key handling on the dict path is O(keys) Python-API calls

(Carried from old ARCH B3, still open.) `run_one` under `RAISE` re-walks
`data.keys()` calling `frozenset.__contains__` per key; under `INCLUDE` it
walks `data.iter()` the same way. The fused JSON path already solved this with
the single-pass `data_key_index` bucket fill; the `PyDict` path could reuse
the same index (one `HashMap` probe per key on the Rust side for exact-str
keys) instead of the per-key Python set lookups. Only matters for wide
payloads under RAISE/INCLUDE; profile before building (the `api` case is the
one to watch: dump 15.7µs / load 12.7µs core-side).

### N6 (low) — `INCLUDE` key-order parity is approximate

Both `run_one` (INCLUDE arm) and `run_one_json` (pass 3) append unknown keys
*after* known fields, matching marshmallow's append order — but dict equality
is order-insensitive and the equivalence suite asserts `==`, so iteration
order of the result is unverified. A duplicate unknown key in JSON keeps the
last value at the *last* position via the fused path, where
`json.loads`-then-load would keep it at first-occurrence position. Nobody has
hit this; noting it so a future "order-sensitive consumer" bug report has a
head start.

## Standing won't-do / blocked list (carried from removed docs, do not re-litigate)

- **Native float formatting in the JSON writer** — no byte-identical parity
  with CPython `float.__repr__` (shortest-round-trip); Rust `ryu` differs on
  a measurable corpus. Stays `repr()`-via-Python.
- **Native datetime isoformat dump** — blocked by abi3: the datetime C
  accessors aren't in the limited API. Revisit only if abi3 is dropped.
- **Native `Regexp` validator** — `regex` crate vs `re` semantics can't be
  guaranteed equal; `_V_PYTHON` already runs it in-core with fallback.
- **Per-class payload cache** — investigated, rejected (R4-class staleness
  risks for cross-instance sharing outweigh the compile saving).
- **Big-int fused `loads`** — jiter is deliberately built without
  `num-bigint` (keeps its pyo3 optional dep out of the build); >i64 ints
  fall back to stock `loads`, which is correct and rare.
- **hooks-case `loads` fusion** — would require running Python hooks
  mid-tree; the 1.73x floor is accepted.

## Test architecture note

`test_equivalence.py` (2369 lines) asserts accel == pure per case;
`test_protocol.py` makes tag desync a failure; `test_contract.py` covers the
R-series preconditions. The gap N1 exposes is a *pattern* gap: every
fallback-triggering dump shape (`UserDict`, wrong-length tuple, unencodable
JSON value) should be paired in a contract test with a consumable iterable in
a *different* field, since "fallback discards partial work" is only safe when
no partial work is observable. Add that family alongside the N1 fix.

## Priority order

1. **N1** — fix + equivalence + contract tests (critical; silent data loss).
2. **N2** — two-line `to_fallback` consistency fix; fold into the N1 PR.
3. **N3** — document the immutable-after-first-use boundary (README +
   CLAUDE.md), optional `invalidate()` helper.
4. **N4** — CI: marshmallow `--pre` job + lowest-3.x pin.
5. **N5** — profile, then (only if the api case shows it) port the
   `data_key_index` bucket fill to the dict path.
