# Local deployment verification tests (Stage 13)

[tests/deployment/](../tests/deployment/) builds the three images from your working
tree, starts the production compose stack under its own project name, and checks it
from outside, as customers, staff and operators use it. It replaces the one-off scripts
behind the Stage 13 container checks ([docs/deployment-architecture.md](deployment-architecture.md#11-verification)).

## Running them

They are **opt-in**: building and starting the stack takes minutes and needs Docker.

```powershell
# Docker Engine on this machine (Linux, Docker Desktop)
$env:RUN_DEPLOYMENT_TESTS = "1"; python -m pytest tests/deployment -v

# Docker Engine inside WSL, tests on Windows
$env:RUN_DEPLOYMENT_TESTS = "1"; $env:DEPLOYMENT_DOCKER = "wsl.exe -e docker"
python -m pytest tests/deployment -v
```

```bash
RUN_DEPLOYMENT_TESTS=1 python -m pytest tests/deployment -v
```

`DEPLOYMENT_DOCKER` is the docker command to use (default `docker`). When it starts with
`wsl`, paths are passed to Docker as WSL paths (`/mnt/c/...`).

### When they do not run

Every test in the folder is **skipped, never passed**, with the reason in pytest's
summary (`-ra` is set in `pyproject.toml`):

```
SKIPPED [28] ... deployment test: builds and runs the Docker stack (minutes); set RUN_DEPLOYMENT_TESTS=1 to run it
SKIPPED [28] ... deployment test: Docker is not available (the docker command 'docker' was not found)
28 skipped
```

Docker counts as available when `docker version` reaches the engine and `docker compose
version` answers. The ordinary test run (`python -m pytest`, `scripts/quality_checks.ps1`)
therefore reports these 28 as skipped. A green run of the ordinary suite says nothing
about the deployment.

## What a run does

1. **Exports the working tree** (tracked and new files; no `.env`, database, models or
   virtual environment) into a temporary folder. Nothing in your checkout is changed.
2. **Configures a test deployment** there: `deploy/.env` with `DEPLOY_ENV=production`,
   `PUBLIC_HOSTNAME=localhost` and ports 18080 / 18443 / 18444 on `127.0.0.1`; the
   production environment examples as `*.env`, with `LLM_ENABLED=false` (**no OpenAI
   request, nothing paid**) and two staff tokens (an admin, a support member); a
   self-signed certificate for `localhost`, made with the backend image's `openssl`; a
   basic-auth file for the dashboard.
3. **Builds** the images (`docker compose build`, tag `deploytest`), fetches the
   embedding model with `model-init`, starts everything with `docker compose up -d`, and
   waits until the backend, both pages and Nginx are healthy.
4. Runs the tests, then **removes** the containers, networks and the `db` and `backups`
   volumes of the `chatbot-deploytest` project. The `models` volume is kept, so the model
   (about 92 MB from Hugging Face) is downloaded on the first run only.

The stack uses the fixed subnets of `deploy/compose.yaml` (172.30.1.0/24, 172.30.2.0/24):
it cannot run while another stack from the same compose file is up on the same Docker
host. Stop that one first.

## What they check

| Area | Test | What must hold |
|---|---|---|
| Container build | `test_the_images_build_from_the_working_tree` | All three images build; each runs as `app` (not root), has its health check and stops on `SIGTERM` |
| | `test_the_backend_image_holds_no_model_and_no_secrets` | No `.env`, no model weights, no database in the image |
| Service startup | `test_every_service_starts_and_becomes_healthy` | Backend, customer page, dashboard and Nginx running and healthy; the backup scheduler running |
| Backend health | `test_backend_health` | `/api/v1/health` 200, inside the network and through Nginx |
| Backend readiness | `test_backend_readiness` | `/api/v1/ready` ready with knowledge base and storage `ok` and `llm: disabled`; publicly only `/healthz` with the verdict alone; `/api/v1/ready` not published |
| Customer frontend | `test_customer_frontend_is_served_over_https` | The page over HTTPS (certificate verified), `/_stcore/health`, HSTS, and plain HTTP redirected (`308`) |
| Admin frontend | `test_admin_frontend_is_served_with_credentials` | The dashboard over HTTPS with its password |
| API connectivity | `test_api_connectivity_through_nginx` | A conversation created and a question answered through Nginx, with a request ID; history readable |
| | `test_the_pages_reach_the_backend_over_the_private_networks` | Both pages reach the backend by name; neither can resolve the other |
| Unauthorized admin access | `test_the_dashboard_requires_its_password` | No password or a wrong one: `401`; the password never echoed |
| | `test_the_dashboard_listens_on_loopback_only` | The dashboard's port is published on `127.0.0.1` only |
| | `test_the_staff_api_is_not_published` | `/api/v1/admin/*` is `404` on the public site; a staff token there is refused (`403 STAFF_FORBIDDEN`); the dashboard does not proxy the API |
| | `test_the_staff_api_refuses_missing_and_insufficient_tokens` | No token or a forged one `401`; a support member on an admin endpoint `403`; an admin `200` |
| Safe error handling | `test_backend_errors_reach_the_customer_safely` | An unknown conversation: `404 SESSION_NOT_FOUND`, the error contract, a request ID matching `X-Request-ID`, no traceback, path or setting |
| | `test_nginx_errors_follow_the_same_contract` | Nginx's own errors use the same contract (`retryable` included); `Server: nginx` without a version |
| | `test_requests_for_other_host_names_are_refused` | Another host name: connection closed without a response |
| Configuration validation | `test_the_compose_configuration_is_valid` | `docker compose config` passes |
| | `test_compose_refuses_to_run_without_a_deployment_environment` | Without `DEPLOY_ENV`: refused, naming it |
| | `test_an_invalid_setting_stops_the_backend_with_a_readable_message` | `DEBUG=true` in production: the backend stops with *Cannot start … DEBUG must be false*, no traceback |
| Missing secrets | `test_llm_enabled_without_an_api_key_refuses_to_start` | `LLM_ENABLED=true` without `OPENAI_API_KEY`: refuses to start, naming the variable |
| | `test_an_invalid_api_key_is_refused_without_being_printed` | A malformed key is refused and never printed |
| | `test_without_staff_tokens_every_staff_operation_is_disabled` | No `STAFF_TOKEN_HASHES`: staff endpoints `403 STAFF_ACCESS_DISABLED`, and a warning |
| Backup and restore | `test_backup_and_restore` | A verified backup on the backups volume; `--verify-only` passes; a test restore into a new file contains the conversation; the active database untouched |
| Persistent storage | `test_data_survives_down_and_up` | A conversation survives `docker compose down` and `up -d` |
| Container restart | `test_the_backend_restarts_after_its_process_dies` | Killing the server process: restarted by the policy, healthy again, `/healthz` ready |
| Rollback ([docs/deployment-rollback.md](deployment-rollback.md)) | `test_a_rollback_without_a_schema_change_keeps_everything` | Another release and back: healthy, data intact, both revisions recorded |
| | `test_an_upgrade_with_a_schema_change_backs_up_first` | A release adding a column: a verified backup first, then the column |
| | `test_rolling_back_past_the_schema_change_is_refused_then_recovered_by_hand` | Back to the older release: it refuses, changing nothing; restoring the pre-upgrade backup with it recovers, losing what the newer release wrote |

The configuration tests start one-off backend containers (`docker compose run --rm
--no-deps`) with a throwaway database in the container's `/tmp`, so they never open the
running stack's database.

## Results

First runs on 2026-10-20, Docker 28.1.1 in WSL 2 (4 CPUs), tests on Windows: **25
passed** in 3 min 49 s; after a change to the test client's certificate handling, **25
passed** again in 3 min 09 s, without warnings. With the rollback tests (Task 24): **28
passed** in 6 min 25 s. Build 25 s (layers
cached), stack healthy 67 s after `up`, `down` + `up` with health 40 s, restart after the
kill 17 s.

**Found while writing them:** Nginx's own JSON errors (`404`, `413`, `429`, `503` on
`/api/`, and the `403` for staff tokens on the public site) lacked the `retryable` field
every backend error has carried since Task 16. They now include it, with the backend's
value for each code. A static test keeps them in step, without Docker
(`test_every_nginx_json_error_follows_the_api_error_contract`).

**Not done:** checking that these tests *fail* when a protection is removed (for example
by taking the password off the dashboard and running them again). Removing a security
control, even temporarily in a working copy, was not permitted in the session that wrote
them. Each test's assertions name the expected refusal (`401`, `403`, `404`, a closed
connection), so a removed control would surface as a failed assertion, but that was
reasoned, not observed.

## Not covered

- A real certificate, the ACME challenge, and public DNS ([docs/https-setup.md](https-setup.md)).
- Browser behaviour: the Streamlit WebSocket through Nginx, page rendering (checked by
  hand in Stage 13, [section 11](deployment-architecture.md#11-verification)).
- The `backup-offsite` profile (needs a restic repository).
- Linux and macOS hosts: these runs used Docker inside WSL 2.
