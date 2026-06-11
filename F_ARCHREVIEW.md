# F_ARCHREVIEW — deep architecture review

A full architecture pass over the codebase at 0.1.11 (protocol 18): `_patch.py`,
`_compiler.py`, `src/lib.rs`, `__init__.py`, packaging (`pyproject.toml`,
`Cargo.toml`), CI, and the test suite's structure. Distinct from **ARCH.md**
(the earlier review; its A-items are fixed, its open B-items are not repeated
here) and from **F_SPEEDUP.md** (performance; cross-referenced, not duplicated).

- **Environment for measured claims:** Apple Silicon (arm64, macOS), CPython
  3.12.0, marshmallow 4.3.0, wheel 0.1.11 release build, fresh venv. Dated
  2026-06-11. Every claim marked **[measured]** was reproduced with a short
  script; the repro shapes are described inline.

Severity: **critical** = silent wrong results, data loss, or resource
exhaustion in plausible use · **high** = correctness divergence or crash on a
less-likely-but-real input · **medium** = behavioural divergence on edge
inputs, or an operational hazard · **low** = hygiene.

## Verdict in one paragraph

The three-layer design (patch shim → Python compiler → Rust interpreter) is
sound, well-bounded, and unusually well documented; the differential test
strategy and the protocol/tripwire guards are genuinely good engineering. But
the system's one central invariant — *"the accelerated path either produces a
result identical to stock, or falls back and stock re-runs unchanged"* — rests
on five unstated assumptions, and this review found a concrete, reproduced
violation of each: inputs are re-iterable (R1: they aren't — silent data
loss), Rust-held references are GC-visible (R2: they aren't — permanent
leaks), Rust cannot crash where Python raises (R3: stack overflow → SIGSEGV),
cached plan state matches the live schema (R4: `partial` mutation →
validation silently skipped), and the root schema's load internals are stock
(R5: a root `_deserialize` override is silently bypassed). None of these is
hard to fix; all of them are invisible to the current test suite because it
tests field-type parity exhaustively but the *contract's preconditions* not at
all.

## Summary of findings

| # | Finding | Severity | Where | Status |
|---|---------|----------|-------|--------|
| R1 | One-shot iterables + dump fallback: silent data loss, swallowed errors | **critical** | `_patch.py:190,383` / `lib.rs:324` | **DONE** |
| R2 | Rust-held cycles invisible to GC: every dumping schema leaks forever | **critical** | `lib.rs` (`DumpSerializer`/`LoadDeserializer`) | **DONE** |
| R3 | Unbounded recursion in the fused JSON writer: SIGSEGV vs stock `RecursionError` | **high** | `lib.rs:244` | **DONE** |
| R4 | Stale cached `partial`: validation silently skipped after mutation | **high** | `_patch.py:225-230,267-271` | **DONE** |
| R5 | Root-schema `_deserialize` override silently bypassed | **high** | `_compiler.py:727-731` | **DONE** |
| R6 | Lone-surrogate `loads` input: `UnicodeEncodeError` escapes instead of stock result | medium | `lib.rs:1252-1254` | **DONE** |
| R7 | `uninstall()` clobbers patches stacked on top of ours | medium | `_patch.py:474-486` | **DONE** |
| R8 | `__version__` drift: `__init__.py` says 0.1.8, the wheel is 0.1.11 | low | `__init__.py:28` | **DONE** |
| R9 | Internal duplication/hygiene (see list) | low | various | CANCELED — cosmetic; no correctness impact |

---

## Post-implementation results (2026-06-11)

All R1–R8 items implemented and tested. R9 canceled (hygiene items, no correctness risk).

### R1 — one-shot iterables
`_patched_serialize` and `_patched_dumps` now call `obj = list(obj)` when `many=True`
and `obj` is not already a `list`/`tuple`. Test: `test_r1_*` in `test_contract.py`.

### R2 — GC traverse/clear
`DumpSerializer` and `LoadDeserializer` pyclasses restructured to hold
`Option<Box<DsInner>>`/`Option<Box<LdInner>>`. Implemented `__traverse__` and `__clear__`
with recursive traverse helpers for all `Py<>` refs in `Element`, `FieldSpec`,
`Serializer`, `LoadElement`, `LoadFieldSpec`, `LoadSerializer`, `Validator`.
GC collection verified via `weakref` tests in `test_contract.py`.

### R3 — depth budget
`write_json_value` now takes a `depth: usize` parameter. Raises `AccelFallback` when
`depth > JSON_DEPTH_LIMIT (512)`. Two call sites from `Element::write_json` pass `depth=0`.

### R4 — stale partial cache
Removed `default_core_partial` from `_load_plan` tuple (was index 2; `fusable` now index 2).
All call sites now use `_core_partial(partial)` live. Fixed `_patched_do_load` to also
correctly fall through to `_orig_do_load` (not `ld.run()`) when `has_hooks=True` and
`_ACCEL_LOAD_VERIFIED=False`.

### R5 — root _deserialize override
Added `_deserialize` identity check in `_build_load_payload` when `is_root=True`.
Schemas overriding `_deserialize` at root now compile to `None` (pure Python).

### R6 — lone surrogates
Changed `s.to_str()?` to `s.to_str().map_err(|_| fallback())?` in `LoadDeserializer::run_json`.

### R7 — uninstall stacking
`uninstall()` now checks identity before restoring each attribute. Emits `RuntimeWarning`
and leaves the attribute alone if a foreign patch was stacked on top.

### R8 — __version__
`__init__.py` now derives `__version__` from `importlib.metadata.version("marshmallow_core")`
with a broad `except Exception` guard.

### R9 — hygiene — CANCELED
Items: body deduplication in `build_dump_serializer`/`build_dump_json_serializer`,
`known_keys` deduplication, `Ctx` singleton, stray doc line in `parse_validator_list`,
`install()` guard when core unavailable. All cosmetic; no correctness risk. Skipped.

---

## What is right (keep these properties)

Recorded so future changes don't erode them accidentally:

- **Layering.** `_patch.py` owns *when* to accelerate (entry-point wrappers,
  caching, hook orchestration), `_compiler.py` owns *what* is accelerable
  (schema introspection → payload), `lib.rs` owns *how* (payload → execution).
  No layer reaches around another; the Rust side never imports marshmallow.
  This is the right factoring for an out-of-tree accelerator.
- **The compile-to-payload design.** Schemas compile to plain tuples
  interpreted by a small Rust "VM" of `Element`/`LoadElement` variants. Tuples
  are a fragile wire format, but the fragility is contained: a single
  `PROTOCOL_VERSION` handshake disables the core on mismatch, and
  `tests/test_protocol.py` round-trips every tag with an exhaustiveness guard
  (ARCH A3). The dual tag space (dump vs load) is documented at both ends.
- **Asymmetric fallback contract.** Load = total fallback (any edge →
  pure Python re-runs, errors byte-identical); dump = limited fallback +
  "provably identical or stay a callback" discipline. The discipline is
  written down in three places (`lib.rs` header, `_compiler.py` docstrings,
  CLAUDE.md) and the held-method pattern (`Decimal`/`TimeDelta`/IP/awareness
  fields hand their own bound `_deserialize` to the core) is a clean way to
  buy parity for intrinsically-Python transforms.
- **The hook-path tripwire.** `_accelerated_load` transcribes private
  marshmallow internals — inherently risky — but `_accel_load_supported()`
  feature-probes the exact invoker signatures at `install()` and degrades to
  pure Python on mismatch. That is the correct posture for an out-of-tree
  patcher (though see R-note in F_SPEEDUP F2 for *how* it degrades).
- **Differential testing.** `_dump_both`/`_load_both` running every case
  accelerated-then-forced-pure and asserting equality is exactly the right
  oracle for a "strictly a speedup" package, and the CI matrix (py3.10–3.13 ×
  marshmallow 3.x/4.x × core-on/core-off) covers the support claim honestly.

---

## R1 (critical) — one-shot iterables turn the dump fallback into silent data loss [measured]

The dump fallback contract says: discard the partial result and re-run pure
Python, "safe because dump has no side effects" (`lib.rs:16-19`,
`_patch.py:200-206`). That premise is false for the *input*: `Serializer::run`
iterates `obj` with `try_iter()` (`lib.rs:324-333`), and stock `Schema.dump`
does **not** materialize iterables before `_serialize` (verified against
marshmallow 4.3 source — `dump()` passes `obj` straight through). So when
`obj` is a generator and any record triggers one of the dump fallbacks (a
`Tuple` length mismatch, a non-dict `DictTyped`, an unencodable JSON value),
the core has already consumed part of the iterator; the pure-Python re-run
sees only the remainder:

```python
class G(Schema):
    t = fields.Tuple((fields.Integer(), fields.Integer()))
gen = ({"t": v} for v in [(1, 2), (1, 2, 3), (5, 6)])
G(many=True).dump(gen)
# stock: ValueError: zip() argument 2 is longer than argument 1
# core : [{'t': (5, 6)}]            <- error swallowed AND two records lost
```

The same applies to fused `dumps` (`_patched_dumps` hands the raw `obj` to
`run_json`), where the repro with a non-str-keyed dict mid-stream returned
`'[{"d": {"z": 3}}]'` against stock's full three-record output. This is the
worst failure class the package can have: not an exception, not a slowdown —
*plausible-looking wrong output*, triggered by data, on an input shape
(`many=True` over a generator/ORM cursor) that is idiomatic marshmallow.

**Fix:** at each dump entry (`_patched_serialize`, `_patched_dumps`), when
`many` is true and `obj` is not a `list`/`tuple`, materialize once —
`obj = list(obj)` — *before* calling the core, and pass the same list to the
fallback. Stock consumes the iterator exactly once too, so materializing
preserves stock semantics exactly while making the retry replayable. (The
load path is already safe: `is_list_like` rejects non-list/tuple before any
iteration, `lib.rs:1308`.) Add a differential test with a generator input and
a mid-stream fallback trigger for both `dump` and `dumps`.

## R2 (critical) — Rust-held references form GC-invisible cycles; schemas leak permanently [measured]

`DumpSerializer`/`LoadDeserializer` hold `Py<PyAny>` references to schema-owned
objects: the accessor bound method (`schema.get_attribute` —
`_compiler.py:376` — which holds the schema), callback `field` objects (whose
`.parent` is the schema), and the held bound `_serialize`/`_deserialize`
methods of the `Decimal`/`TimeDelta`/IP/awareness pattern. The serializer is
cached on the schema instance (`vars(schema)["_mc_dump_serializer"]`), closing
a cycle: *schema → instance dict → Rust object → bound method/field → schema*.
Neither pyclass implements `__traverse__`/`__clear__`, so the cycle is
invisible to CPython's collector and is **never** freed:

| schema, after one op, `del` + `gc.collect()` | collected? |
|----------------------------------------------|-----------|
| dump (native-only fields)                    | **no** (accessor cycle) |
| dumps (fused)                                | **no** |
| load, native-only fields                     | yes |
| load with a `Decimal` field (held method)    | **no** |
| load with a callback field                   | **no** |
| stock marshmallow control                    | yes |

Every schema instance that ever dumps — and most that load — leaks itself,
its fields, and the Rust object, for the life of the process. Combined with
the per-instance cache (ARCH A7) and the common fresh-instance-per-request
pattern, this is an unbounded leak in exactly the deployment shape
(long-running web service) the package targets.

**Fix:** implement `__traverse__`(PyVisit)/`__clear__` on both pyclasses,
recursively visiting every `Py<...>` in `Serializer`/`FieldSpec`/`Element` and
`LoadSerializer`/`LoadFieldSpec`/`LoadElement`/`Validator` (a `fn traverse`
per type, mirroring the existing `element_is_fusable` recursion shape). Add a
regression test: build each schema flavour, run one op, drop it, `gc.collect()`,
assert a `weakref` went dead. Until that lands, the README's A7 note ("reuse
schema instances") is also load-bearing for *memory*, not just speed — say so.

## R3 (high) — fused `dumps` recursion is unbounded: SIGSEGV where stock raises `RecursionError` [measured]

`write_json_value` (`lib.rs:244-314`) recurses over **runtime values** — a
`Raw`/`Dict`/callback field can hand it arbitrarily deep nested lists/dicts.
Stock `json.dumps` guards with the interpreter recursion limit and raises a
catchable `RecursionError`; the Rust recursion has no depth budget and
overflows the native stack. Reproduced: a 100k-deep nested list in a `Raw`
field — stock `dumps` raises `RecursionError`; core `dumps` kills the process
with **SIGSEGV (exit 139)**. A hard crash is strictly outside the fallback
contract (there is nothing left to fall back *to*), and the input can be
attacker-supplied wherever dumped objects embed client data.

The other recursions are safe by construction: `Element::apply`/`write_json`
recurse over schema structure (compile-time bounded); `json_to_py` recurses
over a jiter tree, and jiter's parser enforces its own depth limit, so the
load side falls back before any deep tree exists.

**Fix:** thread a depth counter through `write_json_value` (and the
`Element::write_json` array/object arms), raising `AccelFallback` past a few
hundred levels — the stock path then raises the exact `RecursionError`. One
parameter, no parity risk.

## R4 (high) — stale cached `partial`: mutating `schema.partial` silently disables validation [measured]

`_load_plan` caches `default_core_partial = _core_partial(self.partial)` at
first load (`_patch.py:228`); `_patched_do_load` then uses the **cached**
value whenever the caller didn't pass `partial` (`_patch.py:267-271`) — while
the guard logic reads the **live** attribute. `partial` is a plain constructor
attribute and mutating it between loads is ordinary Python:

```python
s = P(partial=("a",))
s.load({"b": 1})        # plan cached with partial=("a",)
s.partial = False
s.load({"b": 1})
# stock: ValidationError {'a': ['Missing data for required field.']}
# core : {'b': 1}        <- required check silently skipped
```

The divergence direction is the bad one: the core *accepts* data stock
rejects. (The mirror direction — cache says non-partial, live says partial —
only costs a spurious fallback, which is why it hides.) This is one instance
of a **mutable-schema staleness family** the architecture has no stated policy
for: the per-instance plan snapshots `fields`/`partial`/`unknown`-derived
state, `_HAS_LOAD_HOOKS_CACHE`/`_HAS_DUMP_HOOKS_CACHE` snapshot per-*class*
hook presence (`_patch.py:164-186`), and nothing detects drift. Field-dict
mutation after first use is at least documented as exotic (A7); `partial` is
not exotic.

**Fix:** drop this particular cache — `_core_partial` is two `isinstance`
checks; caching it buys nanoseconds and costs correctness. Then write the
policy down in the README: *compiled state snapshots the schema at first
use; mutate a schema after using it and you must create a new instance* — and
make every remaining snapshot either cheap-to-recheck (like `unknown ==
self.unknown` already is) or covered by that documented line.

## R5 (high) — a root schema overriding `_deserialize` is silently bypassed [measured]

`_build_load_payload` checks `_overrides_native_load` only for **nested**
schemas (`_compiler.py:727-731`, the `not is_root` branch). The dump side is
safe at the root *by construction* — the patch sits on `_serialize` itself, so
a subclass override shadows the patch. But the load patch sits one level up,
on `_do_load`, which on stock marshmallow *calls* `self._deserialize(...)`;
the accelerated path replaces that call with `ld.run(...)` and never invokes
an override:

```python
class D(Schema):
    a = fields.Integer()
    def _deserialize(self, data, **kwargs):
        out = super()._deserialize(data, **kwargs)
        out["tag"] = "seen"
        return out
D().load({"a": 1})
# stock: {'a': 1, 'tag': 'seen'}
# core : {'a': 1}                 <- override's work silently dropped
```

(`load`/`_do_load` overrides at the root are fine — they wrap or shadow the
patched method — which is presumably why the root check was relaxed; but
`_deserialize` sits *below* the replacement point on load, exactly like the
nested case the existing check guards.)

**Fix:** in the `is_root` arm, still require
`cls._deserialize is _Schema._deserialize` (the `load`/`_do_load` identity
checks legitimately don't apply at root). One condition plus a differential
test mirroring the existing `_OverridesLoadSchema` nested-case test, at root.

## R6 (medium) — `loads` of text with lone surrogates: `UnicodeEncodeError` escapes [measured]

`LoadDeserializer::run_json` converts a `str` payload with `s.to_str()?`
(`lib.rs:1252-1254`). For a Python `str` containing lone surrogates that
conversion fails — and the `?` propagates the raw `UnicodeEncodeError`
instead of `AccelFallback`. Stock `json.loads` operates on the `str` directly
and *succeeds* (`'{"a": "\ud800"}'` → `{'a': '\ud800'}` through a `Raw`/
`String` field). So the core turns a working input into an unrelated
exception type — the same contract breach class as ARCH A1 (an error escaping
where stock behaves differently), reproduced: stock `ok`, core
`UnicodeEncodeError`. Surrogates in JSON text are rare but reachable (any
`str` decoded with `surrogateescape`, e.g. file/network data).

**Fix:** `s.to_str().map_err(|_| fallback())` at that one site (the `bytes`
arm can't fail). Audit the few other `to_str()?` on *data-derived* strings on
hot paths for the same conversion-error class; key/spec strings from the
compiler are not exposed.

## R7 (medium) — `uninstall()` blindly restores, clobbering stacked patches [measured]

`uninstall()` writes the saved originals back unconditionally
(`_patch.py:474-486`). If anything else patched `Schema._do_load`/`dumps`/...
*after* `install()` (APM agents and tracing libraries do exactly this),
`uninstall()` silently removes their wrapper too — reproduced: a foreign
wrapper installed after `install()` is gone after `uninstall()`. The package
is itself a monkey-patcher, so peaceful stacking is part of its architecture
brief.

**Fix:** before restoring each attribute, check it is still our wrapper
(`Schema._do_load is _patched_do_load` etc.); if not, leave that attribute
alone and emit a `RuntimeWarning` naming the conflict. Symmetric hardening in
`install()` is unnecessary (wrapping whatever is current is correct stacking
behaviour — and is what it already does).

## R8 (low) — user-visible version drift

`__init__.py:28` hardcodes `__version__ = "0.1.8"`; the wheel is 0.1.11.
ARCH A6 annotated the *Cargo* version as deliberately unused, but
`marshmallow_core.__version__` is the one users and bug reports read, and it
is three releases stale — a drift A6's fix didn't cover. **Fix:** derive it
(`importlib.metadata.version("marshmallow_core")` with a `PackageNotFoundError`
guard), or add it to the release-bump checklist next to `pyproject.toml`.

## R9 (low) — internal duplication and hygiene

None urgent; collect when touching the files anyway:

- `build_dump_serializer` and `build_dump_json_serializer`
  (`_compiler.py:379-416`) are character-identical bodies. The split is
  API-intentional (call sites read differently; a future JSON-specific gate
  has a home), but the bodies should share one private helper so they can't
  drift.
- `LoadSerializer` carries the known-key set twice: the Python `frozenset`
  (`known_keys`, dict path) and the Rust `HashMap` (`data_key_index`, fused
  path). One Rust-side set can serve both (this is also ARCH B3's fix).
- Each constructed serializer builds its own `Ctx` (re-importing `builtins` —
  `lib.rs:49-66`); a process-wide `OnceLock` (or one `Ctx` shared by the three
  per-schema objects) removes the triplication. Cosmetic at current cost.
- `parse_validator_list` carries a stray leftover doc line from
  `parse_validator` (`lib.rs:2216-2217`).
- `_NoFallbackError` (`_compiler.py:67`) makes `except AccelFallback` valid
  when the extension is absent — fine — but `_patched_*` would then be
  installed with a dead `try/except`; harmless only because `install()` is
  useless without the extension anyway. A comment, or a guard in `install()`
  (`if not is_available(): return`), would make the intent explicit —
  *currently `install()` happily patches even when the core can never run*,
  adding pure overhead (F_SPEEDUP F11) for nothing.

---

## Cross-cutting: harden the contract, not just the bugs

R1–R6 are each one violated precondition of the same invariant. Fixing them
individually is necessary; keeping them fixed needs the preconditions to be
*stated and tested*:

1. **Write the contract's preconditions into `lib.rs`'s header** alongside the
   fallback description: inputs must be replayable before any retry
   (materialize one-shot iterables at the boundary); every error leaving the
   core is either `AccelFallback`, `KeyboardInterrupt`/`SystemExit`, or a
   *genuine* user-code error — infrastructure errors (string conversion,
   depth) must map to fallback; recursion over runtime data needs a depth
   budget; Python objects held across calls need GC integration.
2. **Add a contract-violation test family** beside the field-parity suite:
   generator inputs + forced fallback; a `gc` leak check per schema flavour;
   deep-nesting inputs (assert `RecursionError`, not a crash); surrogate and
   other non-UTF-8-able text; post-first-use mutation of `partial`/`unknown`.
   These are ~5 short tests and would have caught every critical/high finding
   in this review.
3. **Property-based differential fuzzing** is the natural extension of the
   `_dump_both`/`_load_both` oracle: a `hypothesis` strategy generating
   (schema, data) pairs and asserting accelerated == pure (result *or*
   exception type+message). The oracle already exists; only the generator is
   missing. R1 and R6 are exactly the class of finding such a fuzzer surfaces
   mechanically.
4. **The mutable-schema policy** (R4): one README sentence plus a decision per
   cached datum — recheck live (cheap values) or covered by the documented
   immutability line (structural state).

Process notes that remain open from ARCH.md and still bite: benchmarks are
not regression-gated in CI, and `performance/schemas.py` lacks the shapes
where regressions hid (see F_SPEEDUP "Process"). The CI matrix itself is
solid; add the contract-violation tests to the same jobs rather than a new
lane.

## Priority order

1. **R1** (silent data loss — one-line materialize + tests)
2. **R2** (permanent leak — `__traverse__`/`__clear__` + leak test)
3. **R4** (validation silently skipped — delete a needless cache)
4. **R5** (root `_deserialize` bypass — one condition + test)
5. **R3** (SIGSEGV — depth budget)
6. **R6** (surrogate fallback — one `map_err`)
7. **R7** (uninstall stacking — identity guard + warning)
8. R8/R9 + the cross-cutting test families.

Items 1–6 are all small, isolated changes; the only structurally laborious fix
is R2's traverse implementation (a recursive visitor over the spec enums).
Nothing here argues against the architecture itself — the layering, payload
design, and fallback asymmetry are right; the work is closing the gap between
the contract as documented and the contract as enforced.
