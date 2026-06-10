# Types backlog — remaining field types & structural gaps

A plan for the field types and field-config cases that still fall back to
pure-Python, ordered by return-on-effort. Successor scope to
[ROADMAP.md](ROADMAP.md) / [FAIRBACKLOG.md](FAIRBACKLOG.md).

**Guiding rules (unchanged):**

- *Measure before and after.* Add a `performance/schemas.py` case for anything
  claimed as a win; a native path that doesn't move the needle isn't worth the
  parity surface.
- *Both sides, tags in sync.* A native field is built in `_compiler.py`
  (`_build_load_element` / `_build_dump_element`, the tuple) **and** parsed +
  applied in `src/lib.rs` (`LoadElement` / `Element`), tags aligned. Bump
  `PROTOCOL_VERSION` (lib.rs) and `_EXPECTED_PROTOCOL` (`_compiler.py`) together
  whenever a payload shape/tag changes.
- *Equivalence across valid **and** error inputs.* Every native path needs an
  accel-on == accel-off case in `tests/test_equivalence.py`. Load defers via
  `AccelFallback` on any edge; **dump now has `AccelFallback` too** (Phase 5), so
  new dump elements may defer on shapes they can't reproduce rather than having
  to be provably identical.
- *When in doubt, leave it a callback.* Correctness beats coverage.

Status legend: `[ ]` planned · `[~]` partial / blocked · `[x]` already done ·
`[no]` won't-do (by-design fallback or no speedup available).

---

## Tier 1 — clean, low-risk, reuse an existing pattern

### `[ ]` IP family — `IP` / `IPv4` / `IPv6` / `IPInterface` / `IPv4Interface` / `IPv6Interface`

The cleanest addable type. Each `_deserialize` is `ipaddress.ip_address(value)`
(or the v4/v6/interface constructor); each `_serialize` is `str(value)`. This is
the **exact `UUID` pattern** already in the core (`LoadElement::Uuid`,
`Element::Uuid`):

- **Load** (`lib.rs`): new `LoadElement::IpAddr { ctor: Py<PyAny>, exact_type:
  Py<PyAny> }`. If the value is already an instance of `exact_type`, pass through;
  else call `ctor(value)`; a `ValidationError`/`ValueError` → `AccelFallback`.
  `_compiler.py` holds the field's own constructor (the `ipaddress.*` callable it
  already references) so parity is exact — we call the same function marshmallow
  would.
- **Dump**: `str(value)` — model as the existing `Uuid`-style "stringify"
  element, deferring (dump `AccelFallback`) for a non-instance.
- **Parity hazards:** low — we defer to Python's `ipaddress` constructor, so the
  error and value are identical by construction. Watch the `IPInterface`
  exact-vs-strict modes (`exploded` flag on the field) — read them off the field
  and pass through.
- **ROI:** medium. Niche but common in infra/network schemas, and nearly free
  given the UUID precedent. **Do this first.**
- **Tests:** valid v4/v6/interface, already-an-instance passthrough, malformed →
  error-parity, `None`/missing.

### `[~]` Custom (non-ISO) strptime temporal formats on load

Today `DateTime`/`Date`/`Time` are native only for formats with a built-in
`DESERIALIZATION_FUNCS` entry (ISO); a custom `format=` makes
`DESERIALIZATION_FUNCS.get(fmt)` `None` → callback.

- **Plan:** reuse the **held-method pattern** (`Decimal`/`TimeDelta`/
  `DatetimeAwareness` already do this): a `LoadElement` variant that holds the
  field's own `_deserialize` and turns any `ValidationError` into `AccelFallback`.
  `_compiler.py` stops returning `None` for a custom format and emits that
  element instead.
- **Caveat:** the win is small — the actual `datetime.strptime(value, fmt)` is a
  Python call regardless; we only move the *dispatch* into Rust (skip the field
  callback machinery), like `TimeDelta`. Worth it only if a benchmark shows it.
- **ROI:** low–medium. Dump for custom `strftime` formats is already handled by
  the `Temporal` dump element (`func`/`value.strftime(fmt)`); confirm and close.

---

## Tier 2 — real work, blocked on a prerequisite

### `[~]` `Email` / `Url` on load — blocked on native Email/URL validators

`Email`/`Url` are `String` subclasses; their `_deserialize` is identical to
`String` (`ensure_text_type`). The reason they fall back is the **built-in
`validate.Email` / `validate.URL` validator**, which isn't in the native set
(`Range`/`Length`/`OneOf`/`Equal`/`NoneOf`/`ContainsOnly`). A field with a
non-native validator becomes a full `Callback`, so the cheap String deserialize
falls back with it.

- Two ways forward, both with the same blocker:
  1. **Native Email/URL validators** (regex/parse in Rust) — then `Email`/`Url`
     compile as `String` + native validator. This is the real fix but carries the
     **false-positive / byte-parity hazard** flagged in NEXTBACKLOG (a Rust regex
     that accepts/rejects a different set than marshmallow's is a silent
     correctness bug). Must be provably equivalent or it stays a callback.
  2. **Per-field hybrid** (native deserialize + Python validator): add a
     `Validator::Python(Py<PyAny>)` arm that calls the field's validator and maps
     failure → `AccelFallback`. Lets a native String deserialize keep a non-native
     validator. But the win is marginal here (String deserialize is trivial; the
     regex call stays in Python), so this mostly helps *other* fields with a
     custom validator, not Email/Url specifically.
- **ROI:** medium, but gated on the regex-parity decision. Don't ship a regex
  that isn't byte-identical. Tie to the `Regexp` validator item in FAIRBACKLOG.

### `[ ]` `Dict` with inner key/value fields that carry validators

Typed `Dict` defers today when the key/value field has any processor **or
validator** (`_has_field_processors`). Native validators (`Range`/`Length`/...)
could run per-entry in Rust the same way they do for top-level fields.

- **Plan:** in `_compiler.py`, allow inner key/value fields whose validators are
  all native; thread their compiled `Validator`s into `LoadElement::DictTyped`
  and check them per entry in `lib.rs` (failure → `AccelFallback`).
- **ROI:** low–medium. Narrow but mechanical; reuses the existing `Validator`
  machinery.

---

## Tier 3 — fused-`loads` completeness (adjacent, high-ROI)

Not on the original list, but the biggest remaining `loads` win and the natural
home for several deferrals:

### `[ ]` Thread `Dict` / `Tuple` / `Enum` / `Pluck` through the jiter tree

Fused `loads` (`run_json` / `apply_json` in `lib.rs`) threads only `Nested` and
`List`; every other element materialises its subtree via `json_to_py` and runs
the pure `apply`. A `Dict`/`Tuple`/`Enum` under a `List` therefore loses the
per-item win.

- **Plan:** add `apply_json` arms that read these straight off `JsonValue`
  (object → typed Dict, array → Tuple, scalar → Enum's inner). Mechanical given
  the existing `apply` logic; the scalar→Python conversion stays `json_to_py` so
  parity holds by construction.
- **ROI:** high for `Dict`/`Tuple`-heavy collection payloads. Highest-value item
  in this file.

### `[ ]` `bigint` fused-load coverage

`loads` defers on integers larger than i64 (jiter built without `num-bigint` to
keep its optional pyo3 0.28 dep out of our pyo3 0.27 build). Revisit if/when we
move to a single pyo3 version — enabling `num-bigint` + a `JsonValue::BigInt`
arm (`int(decimal_string)`) closes it. Low ROI (big-int payloads are rare).

---

## Tier 4 — structural gaps (mostly already done or by-design)

### `[x]` Schema-level load hooks — already accelerated

`pre_load`/`post_load`/`validates`/`validates_schema` are **not** a "whole load
goes to Python" gap any more. `_patch.py::_accelerated_load` (Phase 1) runs
`pre_load` in Python → the core per-field step → field/schema validators and
`post_load` in Python around it. The only residue:

- **Fused `loads` skips hook schemas.** `_patched_loads` defers when
  `has_hooks`. **`[no]` — investigated, not worth it.** Fused `loads` exists to
  avoid materialising the parsed `data` dict, but the hook machinery *needs* that
  dict almost everywhere: `pre_load` runs on `data` **before** the per-field step
  (so the dict must exist first); `post_load` and `validates_schema` both receive
  `original_data=data`; and the error path does `ValidationError(errors,
  data=data, …)` + `handle_error(exc, data, …)`. Any hook that touches the
  original data forces the dict to be built, which erases the only thing fusion
  buys. The lone fusable slice — a `validates`-only schema on the happy path —
  still stores `data` on the exception (so the error path rebuilds it) and would
  need a risky transcription of `_do_load` / `_invoke_field_validators` across the
  3.x and 4.x shapes for a sub-microsecond gain. Hook schemas already get the
  accelerated per-field step via the deferred `_do_load` path; only the
  stdlib-`json.loads` parse stays, and that can't be fused away while the hooks
  need the dict. Left deferred.
- The hook **dispatch itself** is user Python and caps speedup ~2× — inherent,
  not movable.

### `[ ]` Field-level `pre_load` / `post_load` (4.x)

A field carrying field-level processors compiles to `None` (callback). Supporting
it means running those processors in Python around the core's per-field step —
the per-field analogue of `_accelerated_load`, but inside the element loop. Real
complexity for a less-common feature. Low–medium ROI; defer.

### `[no]` `Function` / `Method`

`_serialize`/`_deserialize` call an arbitrary user-supplied Python function. A
"native" element would just call that function — which is exactly what the
`Callback` path already does. **No speedup exists**; the work *is* a Python call.
Leave as callback.

### `[no]` `Number` (bare base), `Mapping` (bare base), `Inferred`

- `Number` — base of `Integer`/`Float`; almost never instantiated directly.
  Could extend the exact-type match, but no real workload uses it. Skip.
- `Mapping` — base of `Dict`; a bare `Mapping` with `mapping_type=dict` could be
  folded into the `Dict` match, but it's vanishingly rare. Skip unless one shows
  up in a real schema.
- `Inferred` — an internal marshmallow field, not user-facing. Skip.

### `[no]` Custom `dict_class` / `get_attribute`, self-referential schemas, callable defaults, `marshmallow_dataclass`-style overrides

By-design fallback — forcing these native is a **correctness hazard**, not a
speedup:

- **`dict_class` / `get_attribute`** — the core builds plain dicts and reads
  attributes directly; honouring custom ones means a Python call per field, which
  erases the win and risks divergence.
- **Self-referential schemas** — the compiler breaks compile-time recursion
  deliberately; runtime indirection is possible but a correctness minefield for
  no clear demand.
- **Callable defaults** — marshmallow calls the default per missing field;
  modelling it means calling Python anyway. Low ROI.
- **Inner schema overrides `load`/`_do_load`/`_deserialize` (load) or
  `dump`/`_serialize` (dump)** — e.g. `marshmallow_dataclass`, which overrides
  `load` to build a dataclass and leaves `_hooks` empty. Compiling natively would
  **drop the instantiation** → wrong result. Must stay fallback; the compiler
  already detects the override and bails. Keep it that way.

---

## Suggested order

1. **IP family** (Tier 1) — clean, safe, reuses the UUID pattern.
2. **Thread Dict/Tuple/Enum through fused `loads`** (Tier 3) — biggest `loads`
   win, low parity risk.
3. **Custom strptime formats** (Tier 1) — cheap, behind a benchmark.
4. **Native Email/URL validators** (Tier 2) — high value but gated on byte-parity
   of the regex; do not ship a non-identical matcher.
5. Everything else as demand appears; the `[no]` items stay fallback by design.
