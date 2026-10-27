# Validate the installed distribution

The regular test matrix uses an editable checkout. It checks behavior but cannot
prove that a third party receives every package resource or console entry point.
`scripts/validate_distribution.py` checks an ordinary wheel installation from a
working directory outside the checkout. It does not install dependencies, build
an image, start the HTTP API, download model weights, or prove rendering readiness.

## Build, then install the artifact

Use a clean source extraction or checkout. Build a wheel with `pip wheel --no-deps
--wheel-dir <wheel-directory> <source-directory>`. Create a fresh venv without
`--system-site-packages`, install that wheel with its MCP extra, and run `pip
check`. For example, a wheel requirement is
`/absolute/artifacts/reelpilot-0.5.1-py3-none-any.whl[mcp]`.

The complete cross-platform driver verifies each source-ZIP member against the
release manifest before extraction, builds one wheel, creates a normal venv,
installs its MCP extra, runs `pip check`, then launches the validator from an
outside directory. It leaves existing environments untouched:

```bash
python scripts/package_release.py
python scripts/verify_release_install.py \
  --archive dist/reelpilot-source.zip --manifest dist/publication_manifest.json \
  --work-root /absolute/temporary/work --output dist/distribution-installed.json
```

The driver needs network access to PyPI to resolve dependencies. Build the MCP
container too by adding `--build-docker` on a machine with Docker. That option
uses the verified source extraction as its build context and records the built
image ID and configured non-root user. Both ordinary-wheel installation and
container checks run in CI; consult the actual run and uploaded JSON artifacts
for their outcome. The script itself is not evidence that a run passed.

Change to a separate empty working directory and use the new venv's interpreter:

```bash
/absolute/venv/bin/python -I /absolute/source/scripts/validate_distribution.py \
  --legacy --output /absolute/results/distribution.json
```

On Windows use the venv's `Scripts/python.exe`. Do not activate a different
environment, inject the source through `PYTHONPATH`, or use `pip install -e` for
this check. `-I` is recommended so the runner ignores user-site and environment
path injections; the launched Python MCP server always uses isolated mode.

The validator rejects an editable install, a system-package-inheriting venv, an
import originating outside the current venv's site-packages, or a working
directory inside its source checkout. It verifies installed `index.html`,
`app.js` and `style.css`, and invokes both installed console scripts with
`--help`. The JSON record includes package version and resource hashes without
publishing machine-specific absolute paths.

## Actual protocol smoke check

The official SDK launches the installed module in a new stdio process. The
validator lists tools, resources and prompts, checks the three default tools and
their read-only annotations, then calls `service_status` against a deliberately
unavailable loopback port. The call must report `backend_unavailable`; a forced
fresh tool-list request must still succeed afterward. No HTTP fixture or fake
successful backend response is supplied.

Modern negotiation is always checked. `--legacy` additionally exercises the
SDK's compatibility mode. Ping is sent only in legacy mode, because the modern
wire protocol removed that method. These are actual negotiated protocol checks,
not a claim that every historical SDK or desktop host is compatible.

## Optional prebuilt container

Build the separate **stdio MCP Dockerfile** (`Dockerfile.mcp`) before running the validator. The
repository's HTTP API Dockerfile is a different entry point. Pass the local image:

```bash
/absolute/venv/bin/python -I /absolute/source/scripts/validate_distribution.py \
  --legacy --docker-image reelpilot-mcp:smoke \
  --output /absolute/results/container-distribution.json
```

The client still runs in the verified wheel venv. It uses `docker run --rm -i --pull never`
with networking disabled, a read-only root filesystem, a small `/tmp` tmpfs,
dropped capabilities and `no-new-privileges`. It preserves the image's entry
point; it does not substitute a working command for a broken image command.
Do not add `-t`: a TTY is inappropriate for the stdio protocol. Each invocation
uses its own UUID container name, with explicit cleanup after errors as well as
normal exit. No user volume is mounted.

This checks the same three default tools and the documented absent-backend
failure. The container itself does **not** include a working ReelPilot backend,
media library or pretrained model. Container loopback refers to the container;
it cannot reach a host API. Successful discovery is not a functional media
deployment, a Glama score, a security sandbox claim, or an end-to-end render test.
The validator assumes the provided image is the intended backend-free MCP image.

## Interpreting evidence

The release packager uses a new staging directory on every run. The manifest's
`staging_directory` names the current one; a reused `dist/public` directory is
not a release source. The source ZIP is created only from the manifest entries,
and the installation driver rejects missing, unexpected, duplicated or changed
archive members before building anything.

Only exit code zero and `status: passed` confirm the requested checks passed.
A failure emits a short JSON error and exits nonzero. The report retains any
completed earlier stages. `docker_stdio` appears only when a Docker check was
requested and completed. Keep the source-package and wheel hashes alongside the
report when recording which artifacts were actually tested. Package validation
is separate from application correctness and model-quality experiments.
