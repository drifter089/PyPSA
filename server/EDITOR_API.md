# Editor APIs

Implemented in `app/editor/`, using the existing authenticated, request-scoped
subprocess executor. All examples use JSON and the existing `/v1` prefix. Swagger
at `/docs` and `/openapi.json` expose typed requests and responses.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/editor/catalog` | Native field definitions, form rules, choices, ports, capabilities, and bounded instance state |
| `POST /v1/editor/evaluate` | Apply a proposed batch to temporary state and return field states/diagnostics without export or solving |
| `POST /v1/networks/edit` | Apply and validate a batch, invalidate old results, and return a new NetCDF input artifact |

The wire contract uses **snake_case**, consistent with the existing Python API.
The earlier plan's camelCase JSON was illustrative. `schema_version` is 1.

The routes import only schemas; PyPSA imports, network loading, policy evaluation,
and native edits happen in a fresh child process. Authentication, request limits,
timeouts, concurrency admission, and cleanup are shared with existing operations.

## 1. Describe an empty or existing network

```json
{
  "analysis": "optimize",
  "components": ["Generator", "Link", "Process"],
  "limit": 50,
  "include_profiles": false
}
```

Omitting `source` means a fresh empty network. Otherwise supply the existing
`{"kind":"model","model":...}` or base64 NetCDF source contract. No filesystem
paths or URLs are accepted. `analysis` is `optimize`, `lpf`, or `pf`.

Optional `components` selects native types. Optional `names` filters native names
within those types. `offset` and `limit` paginate instance state; the maximum
limit is 200 and the default is 50. `limit: 0` requests definitions without
instances. `total_instances` describes the filtered collection. Definitions and
reference choice sets are returned independently of instance pagination.

Important response sections:

- `software`: installed PyPSA version, source digest, adapter digest/version,
  policy version. Digests identify the actual source rather than relying on Git
  being available inside the runtime container.
- `context`: source identity, analysis, snapshot identity/labels/weightings,
  limits, capability flags, solution-state marker.
- `component_types`: native categories, all field definitions, port families,
  create support, and form groups.
- `choice_sets`: buses, carriers, standard types, reviewed enums, and
  type-dependent GlobalConstraint carrier choices.
- `network_forms`: time-axis/weighting operations, analyses, registries, constraints.
- `instances`: native static values, field states, profile descriptors, and ports.
- `connection_rules`: structural actions plus named authoritative server checks.
- `presets`: optional native recipes and their assumptions/required parameters.

`defaults` defines fields; `static` supplies asset values; `dynamic` supplies
attribute-specific profiles. They are not different component types. For example,
Generator `p_nom` is scalar while `p_max_pu` permits scalar or profile values.

Fields include native key/type/unit/default/description/status, input/output role,
requiredness, static/varying flags, native/editor modes, support status, control
hint, group, choices, numeric bounds where reviewed, and declarative rules.
`name` is `create_only`; native renaming is not implemented.

Instance fields include `applicable`, `editable`, `required`, `overridden`, reason
IDs, value mode, and `matches_default`. A value equal to a default does not prove
it was omitted: imported/edit provenance is reported as `unknown`. Rule IDs map
to explanations in the corresponding field definitions.

Profiles return snapshot identity, length, and missing count. Set
`include_profiles: true` to include values for the selected instances. Piecewise
bindings are identified as read-only; their native data stays in the artifact.

### Conditions

Rules use `eq`, `ne`, `in`, `isSet`, `all`, `any`, and `not`, with `asset.*` or
`context.*` references. Logical conditions contain `args`; comparisons contain
`ref` and `value`. Rules reference raw native values, never other evaluated field
states, so field-state dependency cycles cannot arise. Python validates references.
The condition definitions are response data, not user-executable expressions.

Expansion overrides the fixed optimization capacity, not the entire asset.
Profiles, costs, efficiencies, and topology continue to matter. Cyclic storage
overrides initial energy. Standard electrical types override their exact native
manual parameters. Inactive values are retained, not erased.

### Special values

Finite numbers, strings, and booleans use normal JSON values. Supported special
values have explicit tags:

```json
{"kind":"unset"}
```

```json
{"kind":"positiveInfinity"}
```

```json
{"kind":"negativeInfinity"}
```

The server checks whether a field's native default permits that special value.
Profiles reject infinities; unset cells are permitted only where native defaults
support missing setpoints. Ordinary JSON `null` is not an edit value. An omitted
operation does nothing; `reset_to_default` is an explicit action.

## 2. Create a model

Send this to `/v1/networks/edit`:

```json
{
  "analysis": "optimize",
  "components": ["Bus", "Generator", "Load"],
  "operations": [
    {"op":"create","component":"Carrier","name":"AC"},
    {"op":"create","component":"Carrier","name":"wind"},
    {"op":"create","component":"Bus","name":"electricity","attributes":{"carrier":"AC"}},
    {"op":"create","component":"Generator","name":"wind-north","attributes":{"bus":"electricity","carrier":"wind","p_nom":100,"marginal_cost":10}},
    {"op":"create","component":"Load","name":"demand","attributes":{"bus":"electricity","p_set":20}}
  ]
}
```

Edits are ordered. Create referenced buses before connecting or applying recipes.
For `create`, dependencies are evaluated against the complete proposed attributes
plus native defaults. For later `set` operations, enable prerequisites first.

Response includes `valid`, `applied`, `base_source_id`, optional `artifact`,
`catalog` (the bounded editor projection), diagnostics, and recipe extensions.
The returned artifact uses the existing NetCDF contract and can be passed to
inspect/export/validate/optimize/power-flow APIs. Use the existing inspection API
or catalogue pagination for a broader model projection.

Successful editing does not solve. `valid` means the implemented input checks
passed, not that the optimization is feasible or the power flow converges.

## 3. Evaluate or edit a configuration

Add `source` from the previous artifact to this request:

```json
{
  "analysis": "optimize",
  "components": ["Generator"],
  "names": ["wind-north"],
  "operations": [
    {"op":"set","component":"Generator","name":"wind-north","field":"p_nom_extendable","value":true},
    {"op":"set","component":"Generator","name":"wind-north","field":"p_nom_max","value":200},
    {"op":"set","component":"Generator","name":"wind-north","field":"capital_cost","value":50}
  ]
}
```

`/v1/editor/evaluate` returns updated states without an artifact (`applied: false`).
`/v1/networks/edit` exports on success (`applied: true`). Both use the same rules.

Use `expected_source_id` from a previous `context.source_id` for source mismatch
detection. NetCDF identity is its byte checksum; model-source identity is a
canonical request hash, not a semantic equivalence fingerprint. The caller must
still handle database revision concurrency. A changed evaluation is a draft of
its base source, not an independently persisted revision.

### Available operations

| `op` | Additional fields | Behavior |
| --- | --- | --- |
| `create` | `component`, `name`, `attributes` | Native asset creation with scalar inputs |
| `set` | `component`, `name`, `field`, `value` | Set scalar and remove that asset's old profile binding |
| `set_profile` | `component`, `name`, `field`, `snapshot_id`, `values` | Replace a complete aligned profile |
| `reset_to_default` | `component`, `name`, `field` | Restore default and clear superseded profile/piecewise binding |
| `connect` | `component`, `name`, `port`, `bus` | Set/reconnect a native bus reference |
| `disconnect` | `component`, `name`, `port` | Clear a bus reference; required missing ports make a draft invalid |
| `delete` | `component`, `name` | Delete this asset only; remaining references are checked |
| `add_port` | `component`, `name`, `index`, `bus`, optional `coefficient`, `delay`, `cyclic_delay` | Link/Process port 2–999 attached to an existing bus |
| `remove_port` | `component`, `name`, `index` | Clear an extra port and its coefficients/delays/profile bindings |
| `set_snapshots` | `snapshots`, optional `snapshot_kind`, `weightings` | Explicit time-axis and weighting change |
| `apply_preset` | `preset`, `name`, `buses`, `parameters` | Expand a documented native recipe |

Default new-port coefficient is 1, delay is 0, and cyclic delay is true. Delay
configuration applies to optimization. Additional-port creation requires a bus
because an unattached optional-port enablement is not preserved in native NetCDF.
Removing a port does not renumber others; an unused all-default port column can
disappear on native export. The edit response describes the reloaded artifact.

Profile changes must supply the current `context.snapshot_id` and one value per
snapshot. A profile's presence overrides its static value; missing cells do not
automatically mean “use the static value.”

Axis changes with existing input profiles are rejected to prevent silent
reindexing. Explicitly replace profiles with scalars first, change the axis,
then supply new aligned profiles. Weighting-only edits preserve profiles.

## 4. Presets

Implemented IDs: `electrolyser`, `heat_pump`, `chp`, `transport`, and
`store_with_links`. Exact bus roles and required parameters are returned in
`presets`. Efficiencies, capacities, and COPs are user-supplied; no cost/weather
dataset is invented. These are native recipes, not new component subclasses.

Bus-role assignment is explicit. Arbitrary carrier names do not enforce a
technology ontology. `transport` is an energy-transfer abstraction, not pipe
hydraulics. `store_with_links` creates a Store, internal bus, and two Links with
independent power ratings; it does not add a simultaneous-charge/discharge
exclusivity constraint. `extensions` records applied recipe versions/assets;
the caller can persist this provenance separately from native model inputs.

## 5. Errors and preservation

- Malformed requests, invalid source data, stale source IDs, or resource-limit
  failures return the existing HTTP error contract, usually 422.
- Well-formed edit/evaluation batches with semantic errors return HTTP 200 with
  `valid: false`, `applied: false`, diagnostics, and no artifact.
- An operation rejection returns the original source projection, not a partially
  applied batch. A structurally applied but incomplete/invalid draft returns its
  evaluated projection for correction, without an exported artifact.
- Diagnostics identify severity, code, component/name, field/port, and operation
  where known. Native diagnostics remain network-level if no reliable location
  is available.
- Successful input edits clear static and dynamic solved outputs and mark the
  solution invalidated. Evaluation also invalidates stale results in its edited
  draft. Unrelated native inputs, profiles, standard types, and supported imported
  fields are preserved.
- Deleting a bus does not silently cascade. Reconnect/delete referencing assets
  explicitly in the same batch, or retain an invalid draft.

## Current capability boundaries

- Deterministic, single-period network support follows the existing server.
- Piecewise data is preserved/read-only. Explicit reset can replace a binding;
  piecewise authoring is unavailable.
- Native renaming, custom standard-type authoring, geometry editing, and manual
  derived SubNetwork editing are unavailable.
- Transformer phase-shift optimization bound fields are read-only pending their
  dedicated form policy; existing native inputs are retained.
- Compatibility checks supplement native consistency and do not certify
  engineering design or solver feasibility.

## Verification

Run from `server/`:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app/editor app/operations/editor.py tests/test_editor.py
```

`scripts/test_image.py` also exercises the editor endpoints in a built production
image alongside existing numerical, authentication, resource-limit, and worker
lifecycle checks. The implementation lives in [`app/editor/`](app/editor/), with
thin request handlers in [`app/operations/editor.py`](app/operations/editor.py).
