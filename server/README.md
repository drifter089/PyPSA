# PyPSA HTTP server

A Python-only FastAPI wrapper around **this repository's local PyPSA source**.
It lives in `server/` so it can be committed and deployed with the fork. No
changes to the `pypsa/` library are required by the server.

## Native editor catalogue and editing APIs

The dedicated `app/editor/` package supplies native component definitions,
conditional form rules, registry choices, ports, validation, and native patch
editing. It includes explicit scalar/profile transitions and optional native
technology/composition recipes.

- `POST /v1/editor/catalog` — describe an empty or supplied network.
- `POST /v1/editor/evaluate` — evaluate proposed edits and form states.
- `POST /v1/networks/edit` — validate edits and return a new native input artifact.

See [EDITOR_API.md](EDITOR_API.md) for requests, response fields, examples,
capability limits, and error semantics. Swagger/OpenAPI include typed editor
requests and responses. Editing does not invoke a solver.

## Code organisation

```text
server/app/
├── main.py                  # App setup, shared authentication, router registration
├── routes/
│   ├── networks.py          # Inspect, export, validate, statistics HTTP routes
│   ├── analyses.py          # Optimisation and power-flow HTTP routes
│   └── metadata.py          # Capabilities, components, and examples HTTP routes
├── operations/
│   ├── __init__.py          # Typed Operation enum and ExecutionContext
│   ├── registry.py          # Request schema → explicit handler registrations
│   ├── networks.py          # Network/data operation handlers
│   ├── analyses.py          # Direct n.optimize(), n.lpf(), and n.pf() calls
│   └── metadata.py          # Component definitions and example handlers
├── execution.py             # Start/await/timeout/clean up a child per computation
├── worker.py                # Small registry-driven child-process entry point
├── network.py               # Shared PyPSA loading, conversion, and statistics helpers
├── examples.py              # Shared offline example catalogue
├── middleware.py            # HTTP body-size limit
├── schemas.py               # Pydantic request models
├── config.py                # Environment configuration
└── __main__.py              # Uvicorn startup
```

Routes pass typed `Operation` identifiers and validated request models to
`execute()`. That function serialises the request across the process boundary.
The child worker looks up an `OperationSpec`, validates the payload with its
schema, and calls its handler with an `ExecutionContext` (temporary directory and
limits). There is no operation `if/elif` dispatcher or dynamic user-selected
Python import. Domain decisions, such as checking solver success or choosing
linear versus nonlinear power flow, belong inside their specific handlers.

The HTTP process imports only lightweight operation identifiers, not PyPSA or
the handler registry. Numerical handlers are loaded by the computation child.
Health, capability information, and example listings remain lightweight HTTP
responses; each request that actually invokes PyPSA gets a fresh subprocess.

### Adding an API

1. Define its Pydantic request model in `schemas.py`.
2. Write a normal Python function in the relevant `operations/` module using
   PyPSA/shared network helpers. Its signature is `(body, context) -> dict`.
3. Add its typed identifier to `Operation` and register its schema/handler in
   `operations/registry.py`.
4. Add a route to the relevant `routes/` module that awaits
   `execute(request, Operation.YOUR_OPERATION, body)`.
5. Add an integration test for its numerical behaviour and HTTP contract.

Handlers have no HTTP or subprocess-management responsibilities and can be
tested directly. Timeout handling and cleanup stay in one place. Preserve this
boundary rather than calling numerical handlers from a route in the API process.

## Where networks and component definitions come from

**There is no PyPSA database.** The server imports the PyPSA Python library built
from this repository. Each computational request creates a fresh in-memory
`pypsa.Network`; its tables are pandas objects inside that Python process.

The request path is:

```text
app/main.py       Registers the grouped routes and shared auth
      |
app/routes/*.py   Receives a request and selects a typed operation
      |
app/execution.py  Starts and awaits an isolated Python child process
      |
app/worker.py     Looks up the schema and handler in operations/registry.py
      |
app/operations/   Explicit handler uses PyPSA and shared app/network.py helpers
```

There are three sources of network data:

| Source | What Python does |
| --- | --- |
| JSON component model in a request | Calls `pypsa.Network()`, `n.set_snapshots()`, and `n.add(...)` |
| NetCDF artifact in a request | Decodes the bytes to a temporary file and calls `pypsa.Network(path)` |
| A supported example ID | Constructs the two-bus example or loads a bundled upstream NetCDF file |

The component metadata endpoint is different from loading a user's model:

```python
# GET /v1/components/Generator, implemented in app/operations/metadata.py
n = pypsa.Network()
attributes = n.components["Generator"].defaults
```

These definitions come from the installed PyPSA package, including files such as
`pypsa/data/component_attrs/generators.csv`. They describe fields like `p_nom`,
their units, defaults, and input/output roles. They are library metadata, not
generator records fetched from a database or remote service.

Computation workers disable PyPSA's automatic network downloads. Supported native
examples are copied into the Docker image during its build. Neither that endpoint nor the
model endpoints fetch a saved project from PostgreSQL. The caller must send the
network inputs and save any results it wants to keep.

## Execution model

```text
HTTP request
  -> validate/authenticate and reserve the instance's one computation slot
  -> start a fresh Python subprocess with a temporary working directory
  -> import local PyPSA, build/load a Network, execute the operation
  -> return JSON (including a NetCDF artifact when applicable)
  -> reap the child and delete temporary files
HTTP response completes; the host may scale the container to zero
```

The server **awaits** computation. It never returns `202`, queues background work,
calls back another application, or connects to a database. Uvicorn stays alive
until the hosting platform stops it; a child exiting does not stop the server.
Each request is independent, and a warm container can serve subsequent requests.

CPU-heavy PyPSA code runs outside the ASGI event loop, so health checks stay
responsive. The parent kills and reaps the computation on its execution timeout,
request cancellation, or an observable client disconnect. A hosting proxy may
not immediately propagate disconnects, so the service deadline remains necessary.

Set **container request concurrency to 1** and **minimum instances to 0** on the
host. Extra concurrent computational requests to one instance receive `429`.
Use one Uvicorn worker. Set the platform/client timeout longer than
`REQUEST_TIMEOUT_SECONDS` plus transfer overhead (for example a 300-second host
timeout with a 240-second service limit). The deadline includes Python imports,
model building, solving, and result extraction. HiGHS also has its own bounded
solver time limit; its timeout is not the entire request deadline.

The host's maximum HTTP duration is a real upper bound on this design. If a model
cannot finish within it, reduce the problem or later adopt a different execution
mode. A stopped container loses an in-flight request; callers must save successful
responses and decide explicitly whether to retry. No result retention is promised
by this stateless service.

## Local quickstart: build and run Docker

### 1. Start Docker and open the repository root

You need Docker Desktop (or Docker Engine) running. Host Python, uv, and a separate
PyPSA installation are **not required** for the Docker workflow. The first build
needs internet access for base images and third-party dependencies.

Run the commands below from the directory containing `pypsa/`, `pyproject.toml`,
and `server/`. On this machine:

```bash
cd /Users/akshatmittal/my-model/code/PyPSA
docker info
```

### 2. Build the production-style runtime image

```bash
docker build --target runtime -f server/Dockerfile -t pypsa-api:local .
```

The final `.` is important: Docker needs the **whole PyPSA repository as its build
context**, not just `server/`. `server/Dockerfile.dockerignore` filters that context
to exclude virtual environments, secrets, and unrelated files.

This builds your current local PyPSA source, including applicable uncommitted
library changes. The runtime image is a snapshot of the code at build time;
later edits on your Mac do not change an already-built image.

### 3. Run the server

```bash
docker run --init --rm --name pypsa-api \
  -p 127.0.0.1:8000:8000 \
  --cpus 2 --memory 4g \
  -e REQUEST_TIMEOUT_SECONDS=240 \
  pypsa-api:local
```

This runs in the foreground. Leave this terminal open and use another terminal
for requests. The initial local commands use the default unauthenticated mode.

- Interactive API / Swagger UI: http://localhost:8000/docs
- OpenAPI JSON: http://localhost:8000/openapi.json
- Health check: http://localhost:8000/healthz

```bash
curl --fail-with-body http://localhost:8000/healthz
curl --fail-with-body http://localhost:8000/v1/capabilities
curl --fail-with-body http://localhost:8000/v1/components/Generator
```

In Swagger UI, expand an endpoint, choose **Try it out**, supply its input, and
choose **Execute**. The response is returned when the PyPSA operation finishes.

### 4. Try an example → optimisation → statistics round trip

These commands use `curl` and `jq`. They save responses under `/tmp` so you do not
have to copy large base64 network artifacts by hand. Swagger UI is an alternative
if you do not have `jq`.

```bash
# Get an input network from the bundled example endpoint.
curl --fail-with-body --silent --show-error \
  http://localhost:8000/v1/examples/two-bus \
  -o /tmp/pypsa-example.json

# Pass that exact artifact to the optimiser; request PyPSA's supply statistics.
# In a shell pipeline, pipefail also propagates an earlier jq/curl failure.
set -o pipefail
jq '{source: .artifact, statistics: [{metric: "supply"}]}' \
  /tmp/pypsa-example.json \
  | curl --fail-with-body --silent --show-error \
      -H 'Content-Type: application/json' \
      --data-binary @- \
      http://localhost:8000/v1/networks/optimize \
      -o /tmp/pypsa-solved.json

# Display the outcome without printing the large NetCDF artifact.
jq '{status, termination_condition, solution_available, objective, statistics}' \
  /tmp/pypsa-solved.json

# Reuse the solved network for another statistic; this does not optimise again.
jq '{source: .artifact, statistics: [{metric: "opex"}]}' \
  /tmp/pypsa-solved.json \
  | curl --fail-with-body --silent --show-error \
      -H 'Content-Type: application/json' \
      --data-binary @- \
      http://localhost:8000/v1/networks/statistics
```

For this two-bus example, expect `termination_condition: "optimal"`,
`solution_available: true`, and an objective of approximately `1400`. The network
has 50 units of cheap generation and 30 units of expensive generation. Other
models may be infeasible or time-limited even if the endpoint returns HTTP `200`;
check the outcome before passing an artifact to another operation.

### 5. Stop, configure, or rebuild

Press **Ctrl+C** in the server terminal, or run:

```bash
docker stop pypsa-api
```

`--rm` removes the stopped container; the image remains available. After source
changes, stop the old container, repeat the build command, and start a new one.

For environment-file configuration, create a local file from `.env.example`,
edit the desired values, then include `--env-file server/.env` in `docker run`:

```bash
cp server/.env.example server/.env
```

Docker injects these values at container startup; they are not baked into the
image. If `SERVER_API_KEY` is nonempty, add
`-H "Authorization: Bearer $SERVER_API_KEY"` to `/v1` curl requests after setting
the matching variable in your client terminal, or use Swagger's **Authorize**
button. An environment file passed to Docker does not export variables into the
client shell. Keep `PORT=8000` with the port mapping above, or change both sides
appropriately.

## Which Dockerfile instructions do what?

The same `server/Dockerfile` contains three named stages:

| Stage | Purpose |
| --- | --- |
| `build` | Build/install local PyPSA and locked third-party dependencies |
| `development` | Editable local-source environment for Compose |
| `runtime` | Production-style image with installed packages and the API |

### Bring the local checkout into the builder

```dockerfile
FROM python:3.12-slim-bookworm AS build
WORKDIR /src
# Install Git/build tools and uv, then:
COPY . .
```

`COPY . .` copies the filtered repository build context into `/src`. Git is used
for package version discovery; `build-essential` provides native build tools if
a dependency needs them. Neither instruction by itself builds PyPSA.

### Build and install PyPSA

```dockerfile
RUN uv sync --project server --frozen --no-dev --no-editable
```

**This is the instruction responsible for building/installing PyPSA.** It reads
`server/pyproject.toml`, where the source is declared as:

```toml
[tool.uv.sources]
pypsa = { path = "..", editable = true }
```

That `..` is relative to `server/`, so it refers to the local repository at
`/src`, not a PyPI package or a remote Git checkout.

- `--project server`: use the server's dependency configuration.
- `--frozen`: use `server/uv.lock` without updating its resolution.
- `--no-dev`: omit pytest, lint tools, and other development dependencies.
- `--no-editable`: override the editable development setting and build/install
  a normal package from the local source.

The resulting environment is `/src/server/.venv`. PyPSA is principally Python;
this is Python package building, not compilation into a standalone executable.
Libraries such as NumPy, SciPy, and HiGHS also provide compiled numerical code.

### Assemble the runtime image

```dockerfile
FROM python:3.12-slim-bookworm AS runtime
# Install runtime system libraries and create appuser, then:
WORKDIR /src/server
COPY --from=build /src/server/.venv ./.venv
COPY --chown=appuser:appuser server/app ./app
COPY --chown=appuser:appuser examples/networks/ac-dc-meshed/ac-dc-meshed.nc /src/examples/networks/ac-dc-meshed/ac-dc-meshed.nc
```

These instructions copy the already-built environment, our FastAPI code, and the
AC/DC example into the final image. The full Dockerfile also copies the storage/HVDC
and model.energy examples. The runtime uses installed PyPSA
from the virtual environment and needs neither Git nor the source checkout.

### Start FastAPI when a container starts

```dockerfile
ENV PATH="/src/server/.venv/bin:$PATH"
CMD ["python", "-m", "app"]
```

`CMD` runs at **container startup**, not during image building. It invokes
`server/app/__main__.py`, which starts Uvicorn with `app.main:app`, one worker,
host `0.0.0.0`, and the configured `PORT`. PyPSA calculations begin only when a
computational request arrives.

Git metadata is included only in the builder for `setuptools_scm` version
discovery. Use a regular checkout with tags. A worktree whose `.git` points
outside the context needs a self-contained checkout for this Dockerfile. Missing
tags can produce a fallback `0.0...` version. Commit and record the actual
fork/image revision separately; lock metadata is not a source pin.

## Production uses the same runtime image

There is no separate `Dockerfile.prod`. The local quickstart explicitly builds
`--target runtime`, which is also the final/default stage and the stage to deploy.

For a container host, build the runtime image for its required CPU architecture,
push it to your chosen registry, and deploy that image. The platform executes the
same `CMD ["python", "-m", "app"]`. It supplies environment variables and routes
traffic to `PORT`. There is no need to rebuild or install PyPSA on each cold start;
the image already contains it.

The host controls minimum instances zero, request concurrency one, CPU/memory,
credentials/IAM, and request deadlines. Locally, Docker does not automatically
scale to zero when a response finishes; it keeps serving until you stop it.

## Editable-source development with Docker Compose

From the PyPSA repository root:

```bash
docker compose -f server/compose.yaml up --build
```

This selects the `development` stage. It runs `uv sync --project server --frozen`
without `--no-editable`, then bind-mounts `pypsa/` and `server/app/` into the
container. New computation children import your current library source. Restart
the API after changing routes or schemas:

```bash
docker compose -f server/compose.yaml restart api
docker compose -f server/compose.yaml down
```

Rebuild after dependency or package-metadata changes. Do not edit the library
during an active solve. Development reload is deliberately not enabled because
it can interrupt work. Use the immutable `runtime` stage for deployment.

## Run directly in Python without Docker

If you prefer host execution, install Python 3.12 and
[uv](https://docs.astral.sh/uv/). From the PyPSA repository root:

```bash
uv sync --project server --frozen
uv run --project server uvicorn app.main:app --app-dir server --host 0.0.0.0 --port 8000
```

Or from `server/`, run `uv run --frozen python -m app`. The latter uses `PORT`.
Configuration comes from exported environment variables; `.env.example` is a
template and Python does not automatically load it. The service uses an editable
installation of the local library. Re-sync after dependency changes, and restart
the API after changing route/schema code.

## Endpoints

All `/v1` endpoints accept `Authorization: Bearer <SERVER_API_KEY>` when the key is
configured. The default is unauthenticated for local development. Health and API
documentation remain public. A platform IAM layer can also protect the service.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Lightweight liveness check; no PyPSA import or solve |
| GET | `/v1/capabilities` | Installed version, operations, execution limits |
| GET | `/v1/components/{component_type}` | PyPSA attribute metadata, defaults, units, input/output roles |
| GET | `/v1/examples` | Explicitly supported example IDs |
| GET | `/v1/examples/{name}` | Load an example, returning its projection and native artifact |
| POST | `/v1/networks/inspect` | Create/import a network and return component tables; optionally time series |
| POST | `/v1/networks/export` | Create/import a network and return a native NetCDF artifact |
| POST | `/v1/networks/validate` | PyPSA consistency checks with structured diagnostic messages |
| POST | `/v1/networks/optimize` | `n.optimize(solver_name="highs")`, optional statistics, solved NetCDF |
| POST | `/v1/networks/power-flow` | `n.lpf()` or `n.pf()`, with nonlinear convergence reporting |
| POST | `/v1/networks/statistics` | Public `n.statistics.*` methods on supplied data; never reruns optimisation |

An HTTP `200` means the operation produced a response, **not** that an optimisation
was optimal. Check `termination_condition` and `solution_available`. Infeasible
or time-limited optimisation returns its outcome without claiming a reusable
optimal solution. Partial incumbent solutions are not exported in API v1.

Validation's `valid` means the selected consistency checks passed, not that the
model is feasible or physically appropriate. `strict: true` promotes all supported
consistency checks to errors. Diagnostics currently contain levels/messages;
component-specific diagnostic parsing is not implemented yet.

## Input contract

Every network operation accepts a `source` with one of two forms:

### Declarative input model

```json
{
  "source": {
    "kind": "model",
    "model": {
      "name": "Single-bus example",
      "snapshots": ["hour-1", "hour-2"],
      "components": [
        {"type": "Carrier", "name": "AC"},
        {"type": "Bus", "name": "electricity", "attributes": {"carrier": "AC"}},
        {"type": "Generator", "name": "supply", "attributes": {
          "bus": "electricity", "p_nom": 100, "marginal_cost": 10
        }},
        {"type": "Load", "name": "demand", "attributes": {
          "bus": "electricity", "p_set": [20, 30]
        }}
      ]
    }
  }
}
```

Attributes use PyPSA names and units. Arrays represent a series aligned exactly
with `snapshots`. Component order does not matter for bus references. Duplicate
component IDs, unknown attributes, output-only attributes, and misaligned series
are rejected. Missing attributes retain PyPSA defaults.

Snapshots default to `['now']`. Set `snapshot_kind: 'datetime'` to interpret
timezone-naive ISO strings as timestamps. Optional `snapshot_weightings` contains
aligned positive arrays for `objective`, `stores`, and `generators`.

### Native network artifact

```json
{
  "source": {
    "kind": "netcdf",
    "data": "<base64-encoded NetCDF bytes>"
  }
}
```

Artifact responses include `kind`, `data`, `sha256`, and `size_bytes`. The entire
artifact can be passed back as `source`; checksums are verified if supplied.
Paths, URLs, pickles, arbitrary Python, and solver callbacks are not accepted.
The existing network preserves native data that may not be editable through the
initial JSON model schema.

Base64 JSON deliberately keeps v1 to one self-contained request and response.
It adds about 33% to binary size. Both bodies and results are capped; tune those
limits to the caller and host. Multipart/binary streaming can be added later if
real examples need it. The checksum is byte integrity, not a semantic cache key.

### Optimisation options

Add these fields alongside `source`:

```json
{
  "time_limit": 120,
  "mip_gap": 0.01,
  "statistics": [
    {"metric": "supply", "components": ["Generator"], "groupby": "carrier"},
    {"metric": "supply", "groupby_time": false},
    {"metric": "optimal_capacity"}
  ]
}
```

Fixed versus extendable component capacities select dispatch versus capacity
optimisation through PyPSA's own model formulation. HiGHS is fixed to one thread
per request. The service clips solver time to 80% of the request deadline to
leave time for other work; this does not guarantee export finishes before the
remaining deadline. Objective constants are explicitly excluded for consistency.

Power flow accepts `mode: 'linear' | 'nonlinear'` and `x_tol`. Supply appropriate
setpoints and electrical parameters. Optimisation and power flow answer different
questions and do not automatically run one another.

## Results and chart data

Inspect projections and statistics use a table envelope:

```json
{
  "index": [["Generator", "gas"]],
  "columns": ["value"],
  "data": [[80]],
  "index_names": ["component", "carrier"],
  "column_names": [null]
}
```

The wrapper preserves axes, including MultiIndexes. Consumers can pivot these
tables for charts without reimplementing the statistics. Non-finite values become
JSON `null`; therefore an inspect projection is **not** a lossless substitute for
the native artifact. Attribute metadata exposes units; metric-specific chart
labels and units must be interpreted with grouping, time aggregation, and carrier
units in mind.

Statistics use public PyPSA methods and retain their weighting/grouping logic.
`groupby_time: false` keeps snapshot series. Omitted grouping/time options use
PyPSA's native defaults for the selected metric. Supplied options are forwarded
directly; the response's `parameters` records them. Not every metric accepts every
option: for example, `installed_capacity` does not accept `groupby_time`, and
`prices` does not accept `components`. Unsupported options return `422`; they are
never silently dropped. Do not
interpret a statistic on an unsolved network as a verified solution. Imported
network provenance is not authenticated by this service.

The initial API supports deterministic, single-investment-period networks.
Stochastic/multi-period inputs, patch-edit operations, custom constraints, broad
example discovery, and arbitrary plotting conversion are outside this initial
implementation.

## Built-in example coverage

The catalogue in `app/examples.py` includes these offline networks:

| API example ID | Source | Buses | Snapshots |
| --- | --- | --- | --- |
| `two-bus` | Small constructed dispatch example | 2 | 1 |
| `ac-dc-meshed` | `examples/networks/ac-dc-meshed/ac-dc-meshed.nc` | 9 | 10 |
| `storage-hvdc` | `examples/networks/storage-hvdc/storage-hvdc.nc` | 6 | 12 |
| `model-energy` | `examples/networks/model-energy/model-energy.nc` | 2 | 2,920 |

These need no per-example optimisation code: load the example artifact and send
it to the same `/v1/networks/optimize` and `/v1/networks/statistics` endpoints.
The native snapshots carry their existing profiles, capacities, costs, storage,
and supported global constraints.

PyPSA also exposes example-loading functions in `pypsa/examples.py`. Those
functions normally download versioned assets from PyPSA's data host. This API
instead uses the checked-in files to make requests reproducible and offline.

The other example files/functions have different requirements:

- `scigrid_de`: 585 buses and 3,499 components in this checkout; it exceeds the
  default 2,000-component limit and the intended country-scale exclusion.
- `stochastic_network`: uses scenarios. API v1 explicitly rejects stochastic
  inputs; supporting them needs contracts for scenario inputs and results.
- `carbon_management`: exposed by PyPSA's Python helper but not bundled in this
  checkout. It is a large sector-coupled example and is outside the intended scope.
- Documentation notebooks are executable workflows, not just network files. Some
  build custom constraints, run multiple analyses, or obtain external data. They
  need curated adapters when their behaviour goes beyond a saved network.

Adding a compatible saved-network example normally means adding one catalogue
entry and including its artifact in the image, not writing a new endpoint.

## Errors and limits

- `401`: missing/invalid configured service credential.
- `413`: request or response exceeds its transport limit.
- `422`: invalid schema, unsupported input, or failed input validation.
- `429`: the instance already has a computation in progress.
- `499`: client disconnect detected; computation is stopped.
- `504`: execution exceeded the service deadline; child was killed and reaped.
- `500`: unexpected worker failure; details are in container logs.

Defaults: 2,000 components, 8,760 snapshots, and a 2,000,000 component × snapshot
budget. This budget is an approximate model-size check, not a memory guarantee.
Native import itself can allocate memory before dimensions are checked, so set
container memory/CPU limits as well. See `.env.example` for all settings.

## Checks and upstream maintenance

The repository's default pytest discovery targets the upstream `test/` directory,
and its mypy discovery excludes `server/`. The inherited PyPSA CI therefore does
not collect the backend's tests or type-check its code. Backend tests are run
explicitly with the separate configuration below; no backend CI job is added.

From the repository root:

```bash
uv run --project server ruff check --config server/pyproject.toml server/app server/tests server/scripts
uv run --project server ruff format --check --config server/pyproject.toml server/app server/tests server/scripts
uv run --project server pytest -c server/pyproject.toml server/tests -q
```

Tests exercise actual PyPSA/HiGHS execution, NetCDF round trips, snapshot-weighted
statistics, both power-flow modes, infeasibility, input checks, authentication
across all grouped routes, timeout cleanup, and busy-instance behaviour. They
also observe real process IDs to verify fresh subprocesses per computation and
temporary-directory cleanup after success and timeout.

After building the runtime image, exercise it over real HTTP without source
mounts or rebuilding it:

```bash
uv run --project server python server/scripts/test_image.py pypsa-api:local
```

This uses temporary containers with 2 CPUs and 4 GiB of memory. It tests every
endpoint, all exposed statistics, constructed dispatch/capacity/storage/conversion
networks, compatible built-in examples at their full snapshot counts, explicit
rejection of out-of-scope examples, authentication, transport limits, concurrent
health checks, and timeout/disconnect cleanup. It removes its containers and
prints the temporary JSON report path. Docker must be running; run it from a
checkout containing the example assets.

Keep changes in `server/` unless intentionally modifying PyPSA. Sync the fork with
upstream as usual, regenerate `server/uv.lock` when its local library requirements
change, then run these compatibility checks. The wrapper uses public PyPSA APIs
and keeps its own packaging configuration and tests separate from upstream's.

For reproducible deployed runs, use clean committed source, record the deployed
image digest/fork revision, and keep the returned PyPSA version. A development
package version alone does not identify uncommitted edits; do not reuse production
result caches solely on that value.
