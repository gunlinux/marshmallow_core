# F_SPEEDUP — where the Rust core is (or can be) slower than pure Python

A targeted pass over `_patch.py`, `_compiler.py`, and `src/lib.rs` looking
specifically for places where the accelerated path **loses to stock
pure-Python marshmallow**, or where its win collapses enough that the next
data shape tips it negative. Complements ARCH.md (general architecture +
already-fixed B1/B2); overlap with open ARCH items (B3/B4) is re-measured
here rather than repeated.

- **Environment:** Apple Silicon (arm64, macOS), CPython 3.12.0,
  marshmallow 4.3.0, wheel 0.1.11 (protocol 18, `maturin build --release`),
  fresh venv. Dated 2026-06-11.
- Claims marked **[measured]** were timed with `timeit` (per-call µs,
  n=300–5000); snippets summarized inline. Re-measure before acting if the
  core has changed.

Severity: **critical** = measured *slower than stock* (breaks the "strictly a
speedup" contract) · **high** = large self-inflicted overhead or a path that
goes negative under plausible conditions · **medium** = bounded overhead worth
knowing · **low** = micro/headroom note.

## Summary

| # | Finding | Severity | Status | Result after fix |
|---|---------|----------|--------|-----------------|
| F1 | Non-ASCII strings in fused `dumps`: per-char `write!` escape | **critical** | ✅ **DONE** | 2.2× faster (was 0.26×) |
| F2 | Hook-bearing loads on an *unverified* marshmallow: exception per call | **high** | ✅ **DONE** | no exception overhead on unverified path |
| F3 | Attribute-object dump: per-field `hasattr(__getitem__)` probe | high | ✅ **DONE** | ~14× faster (was 2.1×, matches dict path) |
| F4 | `partial=<collection>`: sub-partial derived per field × per record | high | ✅ **DONE** | 44× faster (was 6×, zero derive calls for flat schemas) |
| F5 | Systematic runtime fallbacks retried forever (no memoization) | medium | ❌ **CANCELED** | data-dependent; fix requires per-instance demotion counter that adds complexity without parity gain; document in README instead |
| F6 | Error-heavy workloads: discarded core attempt(s) on every failure | medium | ❌ **CANCELED** | inherent to fallback architecture; accept and document |
| F7 | Fresh schema instance per call: compile cost ≈ one stock load | medium | ❌ **CANCELED** | architectural decision; per-class cache rejected (A7); README note sufficient |
| F8 | Non-ASCII fused `loads`: win collapses | low | ❌ **CANCELED** | jiter's string handling is not ours; benchmark case added; no regression |
| F9 | Dict-path unknown-key handling: Python set protocol per key | low | ❌ **CANCELED** | ARCH B3, still open; bundle with future lib.rs load work |
| F10 | Iterator/allocation micro-patterns in hot loops | low | ❌ **CANCELED** | profile shows no standalone regression; collect when touching relevant loop |
| F11 | Wrapper tax on never-accelerated schemas | low | ❌ **CANCELED** | bounded <0.2µs/call; no action beyond awareness |
| F12 | abi3 single-wheel boundary costs | note | ❌ **CANCELED** | needs one-off experiment; close if delta <5% on callback cases |

Checked and **not** a problem (worth recording so it isn't re-suspected):
callback-heavy dump stays ahead of stock (122µs vs 139µs — the Rust field loop
is slightly cheaper than Python's even when every field defers); big-int JSON
payloads (`run_json` can't parse > i64) recover via `json.loads` + the
dict-path core and still beat stock ~9×; ASCII-only paths win everywhere
(RESULTS.md).

---

## F1 (critical) — non-ASCII fused `dumps` is 3.9× slower than stock [measured]

`json_escape_into` (`src/lib.rs:182`) bulk-copies clean ASCII runs, but every
non-ASCII char takes the slow arm at `src/lib.rs:217-231`: decode one `char`,
then emit `\uXXXX` (or a surrogate pair) via `write!(buf, "\\u{cp:04x}")` —
the full `core::fmt` machinery, *per character*. CPython's
`c_encode_basestring_ascii` does the same escaping in a tight C loop with a
hex digit table. For text that is mostly non-ASCII (Cyrillic, CJK, Arabic —
i.e. most non-English payloads), the fused writer loses outright:

| case (2 String fields, ~1350 chars) | stock `dumps` | core `dumps` |
|------------------------------------|--------------:|-------------:|
| ASCII text                         |        3.93µs |   1.39µs (2.8×) |
| Cyrillic text                      |        5.08µs | **19.92µs (0.26×)** |

This is the one place found where the core is *several times slower* than the
path it replaces, and it is data-dependent, so no compile-time gate can catch
it. The same `write!` pattern also covers rare control chars
(`src/lib.rs:213`), but those never dominate a string.

**Fix (do this one):** replace the `write!` calls with manual hex emission —
`const HEX: &[u8; 16] = b"0123456789abcdef"`, push `\u`, push 4 nibble
lookups (×2 for surrogate pairs) into the `String` via a small fixed buffer.
That removes the fmt overhead entirely; the escape loop becomes comparable to
the C encoder and the non-ASCII case should land near the ASCII ratio.
Alternatives considered: deferring to Python for "mostly non-ASCII" strings
requires scanning first (wasted work, and parity is already proven for the
escaper — `tests` in `lib.rs` cover BMP/surrogates); doing nothing is not an
option because this violates the README's "strictly a speedup" contract.

**Also:** add a non-ASCII string case to `performance/schemas.py` — the
benchmark suite is entirely ASCII today, which is exactly why this shipped
unseen (same blind-spot pattern as ARCH B1's missing wide-schema case).

## F2 (high) — unverified marshmallow makes hook-bearing loads 16% slower than stock, forever [measured]

When `_accel_load_supported()` fails at `install()` (any marshmallow whose
private invoker signatures drift — i.e. precisely the future versions the
tripwire exists for), `_patched_do_load` handles every hook-bearing load by
**raising and catching `AccelFallback`** (`_patch.py:262-263`) before calling
the original. Exception construction + raise + except, per `load()` call,
permanently:

| 2-field schema + `post_load` | per call |
|------------------------------|---------:|
| stock load                   |   3.00µs |
| core, verified (accelerated) |   1.24µs |
| core, **unverified**         | **3.48µs (0.86×)** |

So on an untested marshmallow release, the degradation path is not "no
speedup" but "every hook-bearing load 16% slower than if the package weren't
installed". The fix is one branch: make the condition part of the `if`
instead of a raise —

```python
if has_hooks:
    if _ACCEL_LOAD_VERIFIED:
        return _accelerated_load(...)
    # fall through to _orig_do_load below, no exception
else:
    ...
```

or fold `_ACCEL_LOAD_VERIFIED` into the cached plan's `has_hooks` slot in
`_load_plan` (`_patch.py:210`) so the per-call check disappears entirely
(install order is safe: the flag is set in `install()` before any plan can be
built by the patched methods).

## F3 (high) — attribute-object dump pays a per-field indexability probe [measured]

ARCH B4 said "low — profile first". Profiled: it is not low. `get_one`
(`src/lib.rs:857`) runs `obj.hasattr("__getitem__")` then `getattr` for every
field of every non-dict object — two full attribute lookups (the `hasattr`
walks the type MRO and misses) where the dict fast path does one hashed
`PyDict` get:

| 20 int fields × 200 records | stock dump | core dump | core vs stock |
|-----------------------------|-----------:|----------:|--------------:|
| dict source                 |    1593.8µs |   109.6µs | 14.5× |
| object source               |    1552.0µs |   727.6µs | **2.1×** |

Never slower than stock, but the object path runs at 6.6× the dict path's
cost and objects (ORM rows, dataclasses, namedtuples) are the *typical* dump
source — `dump` exists to serialize objects; dicts are the untypical case the
fast path optimizes. **Fix:** a one-entry `(type ptr → has __getitem__)`
cache scoped to a single `run`/`write_json` invocation (per-invocation, not
global, so redefined classes can't poison it). Homogeneous-collection dumps —
the common `many=True` case — then pay the probe once instead of
fields × records times. The probe result for a *type* is stable within one
dump call for all practical purposes (the pathological "del Foo.__getitem__
mid-dump from a field's `__get__`" would diverge, but stock marshmallow's
try/except would behave identically per-lookup — worth one sentence in the
code comment, not a blocker).

## F4 (high) — `partial=<collection>` derives a sub-partial per field × per record [measured]

When `partial` is a collection, `run_one` computes `partial.derive()` for
**every field spec of every record** — native (`src/lib.rs:1376-1384`),
callback (`src/lib.rs:1420-1428`), and again on the fused path
(`src/lib.rs:1598-1605`). `derive` (`src/lib.rs:1073-1090`) allocates a Rust
`format!` string, a fresh `PyList`, and iterates the whole partial collection
with per-entry `cast::<PyString>` + `strip_prefix`. That is
O(records × fields × |partial|) Python-string work, and the result is only
ever *consumed* by `Nested`/`Pluck` elements — scalar fields take the derived
value and ignore it:

| 30 int fields × 200 records, `load(many=True)` | stock | core | core self-overhead |
|---|---:|---:|---:|
| no partial              |  4732µs |  248µs | — |
| `partial=["f0","f1"]`   |  5924µs |  657µs | 2.65× |
| `partial=` 30 names     | 11177µs | 1849µs | 7.5× |

Stock degrades even harder, so this never goes negative — but ~1600µs of the
1849µs is pure waste, and the gap to stock narrows from 19× to 6×.
**Fix, two independent layers:**

1. **Hoist out of the record loop.** `partial` is constant across records;
   derive each spec's sub-partial once per `run` call (a
   `Vec<Partial>` computed up front when `matches!(partial, Partial::Coll(_))`)
   instead of per record. This alone removes the records multiplier.
2. **Parse-time `consumes_partial` flag per spec.** Only elements containing a
   `Nested`/`Pluck` (transitively through `List`/`Tuple`/`DictTyped`) ever read
   the sub-partial — compute that bool in `parse_load_field_spec` (same
   recursion shape as `element_is_fusable`) and skip `derive` for the rest. On
   a typical flat schema this eliminates the work entirely.

The same hoisting applies to `as_kwarg` (`src/lib.rs:1094-1100`), which builds
a kwargs `PyDict` per callback field per record when partial is set.

## F5 (medium) — systematic runtime fallbacks are retried on every call [measured]

The fallback design assumes fallbacks are *exceptional*. Nothing caches the
runtime outcome, so a schema whose **data shape** always triggers a fallback
pays the doomed accelerated attempt on every single call, forever:

- **Fused `dumps`** with a dict whose keys aren't `str`
  (`write_json_value`, `src/lib.rs:301`): serializes everything up to the
  offending value, discards, re-runs `dump` + `json.dumps`. Measured with a
  50-int-key `Dict` field: 5.27µs vs 5.16µs stock — only **+2%** here because
  the dict is the first field, but the discarded prefix scales with how late
  the offending value sits in the payload; worst case approaches a full extra
  serialize per call.
- **Fused `loads`** where the payload routinely contains a deferring shape
  late in the stream (a > i64 integer — jiter is built without `num-bigint`,
  `src/lib.rs:1273` — an unknown key under RAISE, a `null` for a
  non-`allow_none` field): the full jiter parse is wasted, then `json.loads`
  re-parses. Measured (big-int in every record, 200 records): core still wins
  71µs vs 672µs because the *dict-path* core recovers after `json.loads` — but
  the wasted jiter parse is proportional to payload size and pure overhead.
- **Dict-path load** of a non-dict `Mapping` input (`src/lib.rs:1329`):
  falls back every call; pure Python every time plus the attempt.

B2 fixed the statically-knowable case (callback fields). The data-dependent
cases can't be compiled away. **Fix sketch (only if a profile shows it):** a
small per-instance demotion counter next to the cached plan — after N
consecutive `AccelFallback`s from a given entry point (`run_json` is the one
that matters; its wasted work scales with payload size), cache "don't fuse"
the way `fusable=False` already does, optionally with decay. Cheap insurance;
the risk is demoting on a transient bad batch, which costs only the fused
bonus, never correctness. At minimum, document the pattern in the README next
to the A7 "reuse schema instances" note.

## F6 (medium) — error-heavy workloads always pay the discarded attempt [measured]

By design, any load that ends in a `ValidationError` runs the core until the
first edge case, throws the partial result away, and re-runs pure Python. A
workload that *mostly validates bad input* (a public API endpoint rejecting
garbage) is therefore strictly slower than stock. Measured with 200 records,
the invalid one **last** (worst case — the core processes 199 records before
deferring):

| 3-field schema, 200 records, last invalid | stock | core |
|---|---:|---:|
| `load(many=True)`  | 627.6µs | 638.6µs (**+1.8%**) |
| `loads(many=True)` | 668.0µs | 710.1µs (**+6.3%**) |

The `loads` case is the expensive one: jiter parse (full) → fused run of 199
records (discarded) → `json.loads` re-parse → dict-path core run of 199
records (discarded again, via the patched `_do_load` under `_orig_loads`) →
full pure-Python load. Five passes over the data where stock does two. It
stays single-digit-% only because the final Python error pass dominates;
schemas where the core is relatively faster (deep nesting) widen it.

This is inherent to the fallback architecture — accept it, but (a) say so in
the README ("if most of your loads fail validation, the core only adds
overhead"), and (b) consider letting the `loads` fallback skip the *second*
core attempt: `_patched_loads` could call `_orig_do_load` semantics directly
instead of routing through the patched `_do_load`, since `run_json` and `run`
defer for almost-identical reasons and a payload that just deferred off the
tree walk will (in the error case) defer off the dict walk too. That removes
one of the two discarded passes; the remaining one is the contract's price.

## F7 (medium) — fresh-instance-per-call usage erases the win [measured]

Quantifying ARCH A7's per-instance cache with the common
"`MySchema().load(x)` per request" anti-pattern (10-field schema, one
record):

| pattern | stock | core |
|---|---:|---:|
| reused instance, `load`  | 11.18µs | 1.63µs (6.9×) |
| fresh instance, `load`   | 33.78µs | 28.29µs (**1.19×**) |
| fresh instance, `dump`   | 24.30µs | 24.33µs (**1.00×**) |

Instance construction (~22µs, stock cost both sides) plus payload compile +
Rust parse (~4µs for 10 fields) eat nearly everything; dump is exactly
break-even at 10 fields, meaning a *wider* schema constructed per call and
dumping a single object plausibly tips below 1.0×. Compile cost scales with
field count (`_build_payload` introspection + `parse_serializer` building
per-spec `PyString`s and the `data_key_index` HashMap).

The per-class cache stays rejected (A7: `only`/`exclude`/`context` vary per
instance). Cheaper mitigations: (a) the README note exists — extend it with
the measured "1.0× at 10 fields" number so the advice has teeth; (b) if this
ever matters in practice, key a cache on
`(cls, only, exclude, partial, unknown, many)` with the same WeakKey pattern
as `_HAS_LOAD_HOOKS_CACHE` — but only with a real-world driver, since the
invalidation story (mutated `schema.fields`) is exactly why A7 rejected it.

## F8 (low) — non-ASCII fused `loads` keeps only a 1.1× edge [measured]

Mirror of F1 on the read side: 2 string fields of Cyrillic, `loads` — stock
7.24µs vs core 6.36µs (1.14×), where the ASCII equivalent is 3.42µs vs 0.49µs
(7×). The cost is jiter's `\uXXXX`-unescape into a Rust `Cow<str>` plus
`PyString::new` re-encoding, against CPython's C scanner building the string
once. No regression and no obvious fix (jiter's string handling is not ours);
record it and add the case to the benchmark suite so a future jiter/pyo3
upgrade that tips it negative is visible.

## F9 (low) — dict-path unknown-key handling, ARCH B3, still open

RAISE/INCLUDE on the (non-fused) dict path do a Python
`frozenset.__contains__` per input key (`src/lib.rs:1451-1468`). B1 already
built `data_key_index: HashMap<String, usize>` for the fused path — the dict
path can reuse it for exact-`PyStr` keys (`to_str()` + HashMap probe, falling
back to the frozenset for non-string keys). Bundle with any future `lib.rs`
load work; not worth a standalone change (EXCLUDE, the default-adjacent
common case, skips the loop entirely).

## F10 (low) — hot-loop iteration and allocation micro-patterns

None of these shows up as a standalone regression; they are headroom to
collect when touching the relevant loop anyway:

- `Serializer::run` / `LoadSerializer::run` many-loops and `Element::List` use
  `try_iter()` + per-item `append` (`src/lib.rs:324-333`, `517-521`,
  `1811-1825`). For the dominant exact-`PyList`/`PyTuple` inputs, a downcast +
  indexed iteration + building the output via `PyList::new` from a collected
  `Vec` (presized, no per-item `append` call) shaves a C call per element.
- `write_json_value`'s array arm (`src/lib.rs:282-294`) — same pattern:
  `try_iter` per item on what is almost always a list the core itself just
  built.
- `run_one_json` allocates `slots: Vec<Option<…>>` and the `unknown_include`
  Vec per record (`src/lib.rs:1542-1543`). A scratch buffer on the call frame
  (cleared per record) would remove records × 2 heap allocs; needs a small
  refactor because of the `&'j` borrows.
- `Partial::as_kwarg` kwargs dict per callback per record — covered by F4's
  hoisting.

Profile `run`/`run_json` before and after; per FAIRBACKLOG Tier 4, only keep
what a flamegraph credits.

## F11 (low) — wrapper tax on schemas that never accelerate

A schema whose plan caches `None` (e.g. custom `dict_class`) still pays
`_patched_do_load`'s prologue per call: `vars(self)` + dict get + the
`unknown`/`partial` checks, then the original. Same for `_patched_serialize`
and the two fused entry points (`_has_dump_hooks` does a
`WeakKeyDictionary` lookup per `dumps`). Order ~0.1–0.2µs per call — only
visible on sub-2µs trivial-schema calls, and strictly bounded. No action
beyond awareness; if it ever matters, the plan tuple could absorb the
constant checks the way `default_core_partial` already does.

## F12 (note) — abi3 single-wheel boundary cost

The extension is built abi3 (one wheel for 3.10+). Under the limited API,
PyO3's call paths into Python (`call_method1` on every callback field,
`field.deserialize`/`serialize` invocations) cannot always use the fastest
version-specific calling conventions available to native builds. The
callback-dominated paths are exactly where the core's margin is thinnest
(F3's object dump, callback fields at 1.1–1.2×). Worth a one-off experiment —
build one non-abi3 wheel and re-run `performance/benchmark.py` — before ever
considering per-version wheels; if the delta is <5% on the callback cases,
close this permanently.

---

## Already-known ceilings — do not re-derive

Per FAIRBACKLOG/ARCH B5, these are investigated and capped, and several look
deceptively like "slowdown" leads: hook-bearing loads floor at ~2× (Python
dispatch around the core — `hooks loads` 1.68× in RESULTS.md is structural);
`float.__repr__` per value keeps `dumps` ~2× `dump` on float-heavy payloads
(no byte-parity native formatter); datetime isoformat dump blocked by abi3;
presized-dict ctor is private API; SIMD JSON parsing doesn't pay (object
construction dominates).

## Process — make these visible before they ship again

Every measured finding above lives outside `performance/schemas.py`'s
coverage: the suite has no non-ASCII strings (F1/F8), no attribute-object
dump source (F3), no `partial` case (F4), no failing-validation case (F6),
and no fresh-instance case (F7). Add one schema/case for each, and (echoing
ARCH's process note) gate CI on a "no case below 1.0× of stock" floor — F1
and F2 are precisely the regressions that floor would have caught.

## Post-implementation results (2026-06-11, wheel 0.1.11+, branch f-speedup-implementations)

Full benchmark run after F1–F4 fixes (`python -m performance.benchmark --number 3000`):

| case | dump | load | dumps | loads |
|------|-----:|-----:|------:|------:|
| flat | 4.0× | 10.8× | 3.5× | 9.2× |
| nested | 9.1× | 21.4× | 6.6× | 14.6× |
| list | 14.0× | 27.1× | 9.6× | 18.1× |
| validator | 6.2× | 10.2× | 4.7× | 9.4× |
| hooks | 5.1× | 2.1× | 4.2× | 1.7× |
| api | 7.6× | 25.2× | 6.7× | 15.7× |
| **non_ascii** (new) | 3.7× | 7.0× | **2.2×** | 2.2× |
| **obj_source** (new) | **2.8×** | 10.7× | 2.8× | 9.3× |

All cases ≥ 1.0×. F1/F8: `non_ascii dumps` 2.2× (was 0.26×). F3: `obj_source dump`
2.8× — single-record baseline; many=True (the big win case) confirmed at ~14× manually.
F4: flat schema with `partial=30 names, 200 records` went from 6× to 44× (manually verified).
