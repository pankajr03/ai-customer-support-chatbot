# Production deployment runbook

How to prepare a Linux server, deploy the AI customer support chatbot with Docker
Compose, keep it running, and respond to incidents. For operators with root (or `sudo`)
on the server. The design behind these steps is in
[docs/deployment-architecture.md](deployment-architecture.md).

## How to read the commands

- Commands are for **bash on the server** unless a block says *on your workstation*.
- **✏️ SERVER-SPECIFIC**: replace the value before running (hostnames, paths, users,
  addresses, backup file names). Placeholders look like `support.example.com` or
  `<BACKUP_FILE>`.
- **⚠️ DESTRUCTIVE**: deletes or replaces data, or interrupts customers. Read the whole
  step first, and take the backup it names.
- Unmarked commands only read, or are safe to repeat.
- Run nothing on a production server without the approval your organisation requires.
  Commands that contact outside services (Let's Encrypt, OpenAI, Hugging Face, package
  mirrors) say so.

Set these once per shell session, then copy the commands as they are:

```bash
# ✏️ SERVER-SPECIFIC
export APP_DIR=/srv/support-chatbot
export PUBLIC_HOSTNAME=support.example.com
export ADMIN_HOSTNAME=admin.support.example.com
cd "$APP_DIR/deploy"       # every docker compose command below runs from here
```

## Contents

1. [Infrastructure preparation](#1-infrastructure-preparation)
2. [Application deployment](#2-application-deployment)
3. [Maintenance](#3-maintenance)
4. [Incident response](#4-incident-response)
5. [Reference](#5-reference)

---

## 1. Infrastructure preparation

### 1.1 Linux server requirements

| Item | Requirement | Why |
|---|---|---|
| CPU architecture | **x86_64 (amd64)** | The backend's locked dependencies (PyTorch CPU wheels) are resolved for `x86_64-manylinux_2_28`. ARM servers are not supported without a new lock file |
| Operating system | A current 64-bit Linux with Docker Engine support. These steps use **Ubuntu 24.04 LTS**; Debian 12 works with the same steps from Docker's Debian instructions | |
| Docker | Docker Engine with the **Compose v2 plugin** (`docker compose`) and BuildKit | Tested with Engine 28.1.1, Compose v2.35.1 |
| Kernel features | cgroups (memory and CPU limits apply) | The compose file sets limits per container |
| Outbound internet during setup | PyPI and `download.pytorch.org` (image build), `huggingface.co` (model, once), Let's Encrypt (certificates), Docker Hub (base images) | |
| Outbound internet while running | Only `api.openai.com` if `LLM_ENABLED=true`; Let's Encrypt for renewals | The containers have no other route out |
| Time | NTP synchronised (`timedatectl`) | Certificates, token timestamps and backup names |
| One stack per Docker host | The compose file uses fixed subnets 172.30.1.0/24 and 172.30.2.0/24 | Change them in `compose.yaml` together with Nginx's addresses if they clash with your network |

### 1.2 CPU and RAM

Container limits from [deploy/compose.yaml](../deploy/compose.yaml), and what was
measured (docs/performance-profile.md, docs/concurrency-and-limits.md):

| Container | Limit | Measured |
|---|---|---|
| backend | 2 GB, 2 CPUs | 76–94 MB idle, **541–614 MB** once the embedding model is loaded, flat under load |
| customer page | 512 MB, 1 CPU | 51–68 MB idle, plus each browser session |
| admin dashboard | 512 MB, 1 CPU | 51–69 MB idle |
| nginx | 128 MB, 0.5 CPU | about 8 MB |
| backup | 256 MB, 0.5 CPU | short daily run |

- **Minimum: 2 vCPUs and 4 GB RAM.** The limits add up to about 3.4 GB; the system and
  Docker need the rest.
- **Recommended: 4 vCPUs and 8 GB RAM**, especially if images are built on the server
  (building the backend image installs PyTorch).
- A chat turn costs about 0.08 CPU-seconds without the LLM. On a 4-CPU machine, more than
  about 4 requests in flight added latency, not throughput. **No maximum number of users
  has been established**: it depends on message rate, LLM latency and hardware.

### 1.3 Disk

| Item | Size |
|---|---|
| Images: backend, customer/admin (shared layers), Nginx, restic | backend 1.40 GiB, pages 546 MiB, Nginx about 60 MB, restic about 45 MB |
| Build cache (images built on the server) | Several GB. Not measured exactly; prune it after builds (section 3.4) |
| `models` volume | 92 MB (the 12 files the backend reads) |
| `db` volume | Small: 112 MB held 20,000 conversations and 20,000 tickets in testing |
| `backups` volume | The 14 newest backups by default, each about the database's size |
| Container logs | At most 5 × 10 MB per container (Docker's rotated `json-file`) |

- **Recommended: 40 GB free on the partition holding `/var/lib/docker`**, with room for
  a second set of images during an update.
- The backend reports **not ready** when the database's disk has less than 32 MiB free
  (every chat message is a write). Alert well before that: at 80 % used.
- Use local disk for Docker's data. The backend refuses a database on a network
  filesystem in production.

### 1.4 Install Docker

Contacts Docker's package repository. From Docker's documentation for Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git jq openssl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker

docker --version && docker compose version
```

Membership of the `docker` group is equivalent to root on the host. Either run Docker
commands with `sudo`, or add only the operators who already have root:

```bash
sudo usermod -aG docker deploy      # ✏️ SERVER-SPECIFIC: the operator's account; log in again afterwards
```

### 1.5 Firewall

Only SSH, HTTP and HTTPS are reachable from outside. The staff dashboard listens on
`127.0.0.1:8443` and is reached through an SSH tunnel (or a VPN), never through the
firewall.

```bash
# ✏️ SERVER-SPECIFIC: restrict SSH to your administration addresses if you can
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 203.0.113.0/24 to any port 22 proto tcp
sudo ufw allow 80/tcp       # certificate validation, renewals, redirects to HTTPS
sudo ufw allow 443/tcp      # the customer site
sudo ufw enable             # ⚠️ check the SSH rule first: a wrong one locks you out
sudo ufw status verbose
```

**Docker bypasses ufw for published ports.** A port published by a container is reachable
even if ufw denies it. That is why the compose file publishes the dashboard on
`127.0.0.1` only (`ADMIN_BIND`). Never change `ADMIN_BIND` to `0.0.0.0`. Check what is
published:

```bash
sudo ss -tlnp | grep -E ':(80|443|8443)\b'   # 80 and 443 on 0.0.0.0, 8443 on 127.0.0.1 only
```

Also apply the same rules in any cloud provider firewall or security group in front of
the server.

### 1.6 DNS

At your DNS provider, before requesting a certificate (details:
[docs/https-setup.md](https-setup.md#2-dns-prerequisites)):

| Record | Value |
|---|---|
| `A` for `PUBLIC_HOSTNAME` | The server's public IPv4 address |
| `AAAA` for `PUBLIC_HOSTNAME` | The server's public IPv6 address, **or none at all** (a stale AAAA breaks certificate validation) |
| `A` for `ADMIN_HOSTNAME` | The same server (only certificate validation reaches it publicly; the dashboard itself stays on loopback) |
| `CAA` on the domain (recommended) | `0 issue "letsencrypt.org"` |

Lower the TTL to 300 s a day before moving names. Check:

```bash
dig +short A "$PUBLIC_HOSTNAME"
dig +short AAAA "$PUBLIC_HOSTNAME"     # the server's IPv6 address, or nothing
dig +short A "$ADMIN_HOSTNAME"
```

---

## 2. Application deployment

### 2.1 Obtain the repository

Contacts your Git host. Deploy a specific commit or tag, never whatever the branch happens
to point at.

```bash
# ✏️ SERVER-SPECIFIC: repository URL, release tag, owner
sudo mkdir -p "$APP_DIR" && sudo chown deploy:deploy "$APP_DIR"
git clone https://git.example.com/your-org/ai-customer-support-chatbot.git "$APP_DIR"
cd "$APP_DIR"
git checkout v1.0.0                   # the release you are deploying
git log -1 --format='%h %ci %s'
cd "$APP_DIR/deploy"
```

### 2.2 Configure environment variables

Configuration lives only on the server, never in Git (`.gitignore` excludes these
files). Copy the examples:

```bash
cp .env.example .env
for f in backend customer admin; do
  cp environments/production/$f.env.example environments/production/$f.env
done
chmod 600 .env environments/production/*.env
```

**`deploy/.env`** (✏️ edit with your values):

| Variable | Set to |
|---|---|
| `DEPLOY_ENV` | `production` |
| `IMAGE_TAG` | The commit you deploy, for example the output of `git rev-parse --short HEAD`. A new tag per release keeps the previous images for rollback |
| `PUBLIC_HOSTNAME` | `support.example.com` |
| `ADMIN_HOSTNAME` | `admin.support.example.com` |
| `ADMIN_BIND` | `127.0.0.1` (keep it) |
| `EMBEDDING_MODEL_REVISION` | Keep the default: the revision every evaluation used |
| `BACKUP_AT`, `BACKUP_KEEP` | Daily backup time (UTC) and how many to keep |

```bash
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$(git -C "$APP_DIR" rev-parse --short HEAD)/" .env
grep -E '^(DEPLOY_ENV|IMAGE_TAG|PUBLIC_HOSTNAME|ADMIN_HOSTNAME|ADMIN_BIND)=' .env
```

**`environments/production/backend.env`**: start from the example; it is safe as it is.
Then decide:

| Variable | Notes |
|---|---|
| `STAFF_TOKEN_HASHES` | Filled in step 2.4. Empty (`[]`) means every staff operation is refused |
| `LLM_ENABLED`, `OPENAI_API_KEY` | `false` and empty: customers get approved FAQ answers only, no paid calls. To enable: `LLM_ENABLED=true` and the key. **Billed by OpenAI per request.** The backend refuses to start if the key is missing or malformed, and never prints it |
| `SUPPORT_RESPONSE_TIME_TEXT` | What the confirmation tells customers about response times |
| `SUPPORT_TICKET_RETENTION_DAYS`, `CONVERSATION_TTL_MINUTES` | Retention, per your privacy policy |

The production settings refuse unsafe values at startup (for example `DEBUG=true`,
rate limits off, an in-memory database, plain-HTTP CORS origins) with a message naming
the variable. `customer.env` and `admin.env` need no changes.

**TLS certificate.** Nginx does not start without one. Start with a short-lived
development placeholder for the real names, then replace it with a trusted certificate
(step 2.6):

```bash
sh dev/make-dev-certificate.sh "$PUBLIC_HOSTNAME" "$ADMIN_HOSTNAME"
sudo chown root:101 nginx/certs/privkey.pem && sudo chmod 640 nginx/certs/privkey.pem
```

**Dashboard password** (a second credential in front of the staff token). You are
prompted for the password; it is stored as a SHA-512 crypt hash:

```bash
printf 'staff:%s\n' "$(openssl passwd -6)" > nginx/admin.htpasswd
sudo chown root:101 nginx/admin.htpasswd && sudo chmod 640 nginx/admin.htpasswd
```

Back up the whole `deploy/` configuration (`.env`, `environments/`, `nginx/certs`,
`nginx/admin.htpasswd`) to your secrets store. It is not in the database backups.

### 2.3 Build containers

Contacts Docker Hub, PyPI and `download.pytorch.org`. Takes 5–15 minutes on a cold
cache (689 s measured).

```bash
docker compose build
docker image ls --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep support-chatbot
```

Then fetch the embedding model once into the `models` volume. Contacts `huggingface.co`
(92 MB, no account). Every file is checked against the Hub's checksums:

```bash
docker compose --profile setup run --rm model-init
```

### 2.4 Create staff tokens

Each person gets their own token. It is printed **once**; give it to that person only.
Only the hash goes into the configuration.

```bash
# ✏️ SERVER-SPECIFIC: staff IDs and roles (admin: FAQs, tickets, analytics; support: tickets)
docker compose run --rm --no-deps backend python -m scripts.create_staff_token alice --role admin
docker compose run --rm --no-deps backend python -m scripts.create_staff_token maria --role support
```

Put the printed entries into `STAFF_TOKEN_HASHES` in `environments/production/backend.env`
as a JSON list, for example
`STAFF_TOKEN_HASHES=["alice:admin:3f5a…","maria:support:9b1c…"]`.

### 2.5 Start services

```bash
docker compose up -d
docker compose ps
```

Start order follows the health checks: backend first, then the two pages, then Nginx.
All four should be **healthy** within about a minute (51–67 s measured). The `backup`
service shows *starting* for its first minute. The embedding model finishes loading
8–24 s after the backend is healthy; chat works meanwhile (keyword answers).

### 2.6 Replace the placeholder certificate

Contacts Let's Encrypt, and the names appear in public certificate transparency logs.
**Staging first** (untrusted, generous limits), then production. Full procedure:
[docs/https-setup.md](https-setup.md#4-issuing-the-certificate).

```bash
sudo snap install --classic certbot
chmod +x "$APP_DIR/deploy/certbot/deploy-hook.sh"

sudo certbot certonly --staging --webroot -w "$APP_DIR/deploy/nginx/acme" \
  -d "$PUBLIC_HOSTNAME" -d "$ADMIN_HOSTNAME" --cert-name support-chatbot \
  --deploy-hook "$APP_DIR/deploy/certbot/deploy-hook.sh"

sudo certbot delete --cert-name support-chatbot          # removes the staging certificate only
sudo certbot certonly --webroot -w "$APP_DIR/deploy/nginx/acme" \
  -d "$PUBLIC_HOSTNAME" -d "$ADMIN_HOSTNAME" --cert-name support-chatbot \
  --deploy-hook "$APP_DIR/deploy/certbot/deploy-hook.sh"
```

The deploy hook installs the certificate, checks Nginx's configuration and reloads it
without downtime.

### 2.7 Verify health

```bash
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}'

# Public status: the verdict only
curl -fsS "https://$PUBLIC_HOSTNAME/healthz"            # {"status":"ready"}

# Detailed readiness, from inside (not published)
docker compose exec backend python -c "import urllib.request as u; \
  print(u.urlopen('http://127.0.0.1:8000/api/v1/ready').read().decode())"
```

Ready means `knowledge_base` and `conversation_storage` are `ok`. `semantic_search`
should become `ready`; `llm` is `disabled`, `enabled` or `paused`. Requests by IP address
are refused by design: always use the host name.

Check the certificate the site serves:

```bash
python3 -m scripts.check_certificate "$PUBLIC_HOSTNAME"   # from $APP_DIR; exit 0 = valid for 21+ days
```

### 2.8 Verify the customer interface

```bash
curl -fsSI "http://$PUBLIC_HOSTNAME/" | head -3          # 308 to https://
curl -fsS "https://$PUBLIC_HOSTNAME/" | grep -c noscript  # 1: the chat page

# A real conversation through the public API (no LLM cost if LLM_ENABLED=false)
created=$(curl -fsS -X POST "https://$PUBLIC_HOSTNAME/api/v1/conversations")
sid=$(echo "$created" | jq -r .session_id); token=$(echo "$created" | jq -r .access_token)
curl -fsS -X POST "https://$PUBLIC_HOSTNAME/api/v1/conversations/$sid/chat" \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"message": "How can I track my order?"}' | jq '{decision, answer, sources}'
```

Expect `"decision": "answered"`. Then open `https://support.example.com` in a browser
and ask a question.

### 2.9 Verify protected admin access

From **your workstation**:

```bash
# ✏️ SERVER-SPECIFIC: your SSH user and server; keep this terminal open
ssh -N -L 8443:127.0.0.1:8443 deploy@support.example.com
```

In another terminal on the workstation (add `127.0.0.1 admin.support.example.com` to
your hosts file first):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://admin.support.example.com:8443/            # 401
curl -s -o /dev/null -w '%{http_code}\n' -u staff https://admin.support.example.com:8443/   # 200 after the password
```

Then open `https://admin.support.example.com:8443`, enter the dashboard password, then
a staff token.

Check that nothing staff-related is public, from **any machine outside**:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://support.example.com/api/v1/admin/faqs"   # 404
nc -zv -w 5 support.example.com 8443                                                       # must fail
```

---

## 3. Maintenance

### 3.1 View logs

```bash
docker compose logs --tail 100 backend
docker compose logs -f --since 10m backend nginx            # follow
docker compose logs --no-log-prefix backend | jq -c 'select(.level == "ERROR" or .level == "CRITICAL")'
docker compose logs --no-log-prefix backend | jq -c 'select(.event == "api_error") | {timestamp, status, error_category, retryable, route, request_id}'
# One customer-reported problem, by the reference (request ID) the page showed them:
docker compose logs --no-log-prefix backend | grep 6f1c0e3a9b2d4c7e8a5f1b3d2c4e6a8b   # ✏️ the request ID
```

Logs never contain message text, tokens or contact details (see
[docs/logging.md](logging.md)). Docker keeps 5 × 10 MB per container; ship them to a
collector if you need more. Operational figures (errors, latency, failures) are in the
dashboard's **Service monitoring** section ([docs/monitoring.md](monitoring.md)).

### 3.2 Restart services

```bash
docker compose restart backend            # same container and configuration
docker compose restart nginx
```

`restart` does **not** re-read environment files. After changing `.env` or
`environments/production/*.env`, recreate the containers:

```bash
docker compose up -d                      # recreates only what changed
```

Effects of a backend restart: requests in progress up to 20 s are completed; in-memory
metrics and rate-limit counters reset; semantic search returns 8–24 s after the
container is healthy. A message cut off by the restart is answered once when the
customer's page retries ([docs/shutdown-and-restart.md](shutdown-and-restart.md)).

### 3.3 Back up data

The `backup` service backs up the database daily at `BACKUP_AT` (UTC) and keeps the
newest `BACKUP_KEEP`. Its health turns **unhealthy** if backups stop succeeding. Watch
`docker compose ps`. Take a manual backup before every update, restore or risky change:

```bash
docker compose exec backend python -m scripts.backup_database
docker compose exec backend ls -l /app/data/backups
```

The `backups` volume is on the same disk as the database: it protects against mistakes,
not against losing the server. Configure off-server copies (encrypted, restic):
[docs/backup-and-restore.md](backup-and-restore.md#5-off-server-copies). Also keep the
`deploy/` configuration (2.2) in your secrets store.

### 3.4 Update application images

```bash
# 1. Back up first: this backup is the rollback for any schema change
docker compose exec backend python -m scripts.backup_database

# 2. Note the running release, for rollback
grep '^IMAGE_TAG=' .env

# 3. Get the new release (✏️ the release tag) and build it under a new tag
git -C "$APP_DIR" fetch --tags
git -C "$APP_DIR" checkout v1.1.0
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$(git -C "$APP_DIR" rev-parse --short HEAD)/" .env
docker compose build

# 4. Replace the containers (a brief interruption while the backend restarts)
docker compose up -d
docker compose ps
curl -fsS "https://$PUBLIC_HOSTNAME/healthz"
```

Read the release notes for changes to `.env.example` files: new settings must be added
to your `*.env` files. If `EMBEDDING_MODEL_REVISION` changed, run `model-init` again
before `up -d`.

Free disk after a successful update, keeping the previous release's images for rollback:

```bash
docker builder prune -f                              # build cache only
docker image ls support-chatbot-backend              # list releases

# ⚠️ Remove a release older than the previous one (never the running or the previous tag)
OLD_TAG=1a2b3c4                                      # ✏️ SERVER-SPECIFIC
for image in backend customer admin; do docker image rm "support-chatbot-$image:$OLD_TAG"; done
```

### 3.5 Apply database migrations

There is **no separate migration command** and no migration tool. The backend upgrades
the schema itself at every start, before serving
([docs/sqlite-review.md](sqlite-review.md#6-migrations)):

- missing tables, indexes and nullable columns are created; rows are kept;
- a table whose definition changed is rebuilt in one transaction (all rows copied); any
  failure rolls back and the backend refuses to start, changing nothing;
- a missing required column stops the backend with a message naming it.

So "applying migrations" is step 3.4: back up, then start the new release. Watch for it:

```bash
docker compose logs --no-log-prefix backend | jq -r 'select(.message | test("Added column|Upgraded table|Database upgrade failed|schema is out of date")) | .message'
```

**There is no downgrade.** Older code against a newer database is not supported: roll
back a schema change by restoring the backup taken before the update (section 4.7).

### 3.6 Restore data

First a **test restore**, which never touches the active database:

```bash
docker compose exec backend ls -l /app/data/backups
BACKUP=/app/data/backups/conversations-20261019T105622Z.db   # ✏️ SERVER-SPECIFIC: from the list
docker compose exec backend python -m scripts.restore_database "$BACKUP" --verify-only
docker compose run --rm --no-deps backend python -m scripts.restore_database \
  "$BACKUP" --to /tmp/restore-test/conversations.db
```

**⚠️ DESTRUCTIVE: replace the active database.** Everything written after the backup is
lost (conversations, tickets, FAQ edits). The script backs up the current database first
(a safety backup), so the restore itself can be undone. Customers cannot chat while the
backend is stopped.

```bash
docker compose stop backend
docker compose run --rm --no-deps backend python -m scripts.restore_database \
  "$BACKUP" --replace-active --confirm conversations.db       # $BACKUP from the test restore above
docker compose start backend
docker compose ps
# Data deleted since the backup comes back with it: apply retention again
docker compose exec backend python -m scripts.cleanup_conversations
docker compose exec backend python -m scripts.anonymize_support_data --retention
```

To undo, restore the safety backup the script printed, the same way. Details:
[docs/backup-and-restore.md](backup-and-restore.md#3-restoring).

### 3.7 Rotate secrets

| Secret | When | How |
|---|---|---|
| Staff token | A person leaves, a token may be exposed, or on your schedule | Below. Tokens never expire on their own |
| Dashboard password | Same | Below |
| OpenAI API key | Exposure, or your schedule | Below |
| TLS private key | Suspected exposure | Below |
| restic repository password | See [docs/backup-and-restore.md](backup-and-restore.md) | `restic key add` / `restic key remove` against the repository |
| Customer conversation tokens | Cannot be rotated | Each is valid for one conversation until it expires (`CONVERSATION_TTL_MINUTES`, default 60) and is deleted |

**Staff token.** Create the replacement (2.4), give it to the person, then remove the old
entry from `STAFF_TOKEN_HASHES` and recreate the backend:

```bash
docker compose run --rm --no-deps backend python -m scripts.create_staff_token alice --role admin   # ✏️
# edit environments/production/backend.env: replace alice's entry with the new one
docker compose up -d backend
```

The old token is refused from the restart on. Revoking without a replacement: remove the
entry and run `docker compose up -d backend`.

**Dashboard password.**

```bash
line="staff:$(openssl passwd -6)"                       # prompts for the new password
echo "$line" | sudo tee nginx/admin.htpasswd > /dev/null  # overwrite in place, never mv
unset line
docker compose exec nginx nginx -t -q -c /tmp/nginx.conf && docker compose exec nginx nginx -c /tmp/nginx.conf -s reload
```

The file is bind-mounted into Nginx as a single file, which Docker binds by inode:
replacing it (`mv`, or an editor that writes a new file) leaves Nginx reading the **old**
password. Overwrite it in place as above, or run `docker compose up -d --force-recreate
nginx` afterwards.

**OpenAI API key.** Create the new key at OpenAI, set `OPENAI_API_KEY` in
`environments/production/backend.env`, run `docker compose up -d backend`, check
`/api/v1/ready` (`llm: enabled`), then revoke the old key at OpenAI.

**TLS private key.** Issue a new certificate with a new key (contacts Let's Encrypt), and
revoke the old one if it was exposed:

```bash
sudo certbot renew --cert-name support-chatbot --force-renewal       # new key; the hook installs it
sudo certbot revoke --cert-path /etc/letsencrypt/archive/support-chatbot/cert1.pem   # ✏️ ⚠️ the exposed certificate
```

### 3.8 Renew certificates

Renewal is automatic: certbot's timer runs twice a day and renews close to expiry; the
deploy hook installs the new certificate and reloads Nginx without downtime.

```bash
systemctl list-timers | grep certbot            # the timer exists
sudo certbot renew --dry-run                     # rehearsal against staging; installs nothing
sudo certbot certificates                        # names and expiry dates
```

Let's Encrypt no longer emails expiry warnings. Monitor the served certificate **from
outside the server**, daily, and alert on a non-zero exit:

```bash
python3 -m scripts.check_certificate support.example.com      # ✏️ from a checkout on another machine
```

If renewal fails, the current certificate keeps working until it expires. Common causes:
port 80 blocked, DNS changed, a new AAAA record. See
[docs/https-setup.md](https-setup.md#5-renewal).

### 3.9 Routine data retention

Expired conversations are deleted at each backend start. Run these weekly as well (from
cron, for example), or after a restore:

```bash
docker compose exec backend python -m scripts.cleanup_conversations
docker compose exec backend python -m scripts.anonymize_support_data --retention
```

---

## 4. Incident response

For every incident: note the time, keep the request IDs customers report, and take a
backup before changing data (`docker compose exec backend python -m scripts.backup_database`).

### 4.1 Backend outage

**Symptoms:** `/healthz` returns `502` or `503`; the chat page shows "Unavailable"; the
backend is not *healthy* in `docker compose ps`.

```bash
docker compose ps
docker compose logs --tail 200 backend
docker compose logs --no-log-prefix backend | jq -c 'select(.level == "CRITICAL" or .level == "ERROR")' | tail -20
docker inspect --format '{{.RestartCount}} {{.State.Status}} OOMKilled={{.State.OOMKilled}}' $(docker compose ps -q backend)
```

| Finding | Action |
|---|---|
| `Cannot start: … invalid setting(s)` | A configuration error, named in the message. Fix the `*.env` file, `docker compose up -d backend` |
| `Cannot start: the FAQ storage could not be opened` | Section 4.2 |
| `OOMKilled=true` | Memory limit reached. Check `docker stats`; raise `mem_limit` in `compose.yaml` only if the host has room |
| Exited, restart count rising | The restart policy restarts a crashed process. Read the logs from just before each exit |
| Running but *unhealthy* | Docker does **not** restart unhealthy containers. Read `/api/v1/ready` (2.7): `conversation_storage` (4.2, 4.4) or `knowledge_base` (no active FAQ, or the FAQ table unreadable) |

Then `docker compose restart backend`, or `docker compose up -d backend` after a
configuration change. The pages and Nginx keep running and recover by themselves when
the backend is healthy.

### 4.2 Database failure

**Symptoms:** `/api/v1/ready` shows `conversation_storage: unavailable`; chat answers
`503 SERVICE_UNAVAILABLE`; logs show `Conversation storage unavailable` or
`Database initialisation failed`.

```bash
df -h /var/lib/docker                    # a full disk looks like a database failure (4.4)
docker compose exec backend python -c "import sqlite3; \
  c = sqlite3.connect('file:/var/lib/chatbot/conversations.db?mode=ro', uri=True); \
  print(c.execute('PRAGMA integrity_check').fetchone()[0], len(c.execute('PRAGMA foreign_key_check').fetchall()))"
```

| Finding | Action |
|---|---|
| Disk full | Section 4.4 |
| `database is locked` repeatedly (`*StoreBusyError`) | Something else is writing the file. Only the backend and the backup service should open it. A one-off `docker compose run` with a restore or import running? Let it finish |
| `integrity_check` not `ok` | Corruption. **⚠️ Restore** the newest backup that verifies (3.6). Keep the corrupt file for analysis: copy it out first (`docker run --rm -v support-chatbot_db:/db -v "$PWD":/out alpine cp /db/conversations.db /out/corrupt.db`; pulls the `alpine` image) |
| `Database upgrade failed` after an update | The new release's schema upgrade failed and changed nothing. Roll back the release (4.7) and report the message |

### 4.3 LLM outage

**Symptoms:** `/api/v1/ready` shows `llm: paused`; log lines `LLM … giving up` or `LLM
circuit open`; replies are word-for-word FAQ answers.

This is **degraded, not down**: while the LLM fails, customers get the approved FAQ
answers, and calls pause for `LLM_FAILURE_COOLDOWN_SECONDS` before retrying. No action
is needed for a short OpenAI outage.

```bash
docker compose logs --no-log-prefix backend | jq -c 'select(.logger == "app.services.llm_service") | {timestamp, level, message}' | tail -20
```

| Log category | Meaning | Action |
|---|---|---|
| `authentication`, `permission` | Key invalid or revoked | Rotate the key (3.7) |
| `quota_exhausted` | Billing limit reached | Fix billing at OpenAI |
| `invalid_configuration` | Model name or option refused | Check `OPENAI_MODEL` and the release notes |
| `timeout`, `server_error`, `network` | OpenAI or the route to it | Wait; check status.openai.com and the server's outbound access |

To switch generation off until it is resolved (approved answers only, no cost): set
`LLM_ENABLED=false` in `environments/production/backend.env`, then
`docker compose up -d backend`.

### 4.4 Disk space exhaustion

**Symptoms:** `/ready` reports `conversation_storage: unavailable` with under 32 MiB free;
chat answers `503`; builds or backups fail; `No space left on device` in logs.

```bash
df -h / /var/lib/docker
docker system df
sudo du -sh /var/lib/docker/volumes/* 2>/dev/null | sort -h | tail
docker compose exec backend du -sh /app/data/backups /var/lib/chatbot
sudo journalctl --disk-usage
```

Free space, safest first:

```bash
docker builder prune -f                          # build cache: rebuilt when needed
docker image prune -f                            # dangling images only
sudo journalctl --vacuum-size=200M               # ⚠️ system journal older entries
docker compose exec backend python -m scripts.backup_database --keep 7   # ⚠️ prunes older backups after a new one; copy them off-server first
```

Do **not** delete files inside the `db` volume, or `conversations.db-wal`: the WAL file
holds committed data. The backend becomes ready again by itself once space is free;
check `/api/v1/ready`.

### 4.5 Authentication compromise

Act first, investigate second.

| What leaked | Contain | Then |
|---|---|---|
| A **staff token** | Remove its entry from `STAFF_TOKEN_HASHES`, `docker compose up -d backend` (3.7) | Review the audit log in the dashboard (**Audit log**: what that staff ID did and when; refused attempts are recorded). Issue a new token |
| The **dashboard password** | Rotate it (3.7) | It is only the outer lock: a staff token is still needed. Check Nginx's log for `401`s and unusual sources |
| The **OpenAI key** | Revoke it at OpenAI at once, then rotate (3.7) | Check usage and billing at OpenAI |
| The **TLS private key** | New key and certificate, revoke the old one (3.7) | |
| **Server access** (SSH, root, the `docker` group) | Assume everything above leaked: rotate all of it; preserve logs; rebuild the server from a known-good state if you cannot rule out persistence | Database backups contain personal data: follow your breach-notification obligations |
| A **customer's conversation token** | Limited to that conversation, until it expires (default 60 minutes) and is deleted | Staff cannot revoke one; the customer can delete the conversation from the page |

```bash
docker compose logs --no-log-prefix backend | jq -c 'select(.error_category == "STAFF_AUTH_REQUIRED" or .error_category == "STAFF_FORBIDDEN") | {timestamp, route, request_id}' | tail -50
docker compose logs nginx | grep -E '" 401 ' | tail -50
```

### 4.6 Failed deployment

**Symptoms:** after `docker compose up -d`, a service stays *starting* or *unhealthy*,
exits, or `/healthz` fails.

```bash
docker compose ps
docker compose logs --tail 200 backend customer admin nginx
docker compose config --quiet && echo "compose configuration valid"
```

| Finding | Action |
|---|---|
| Build failed | Nothing changed in production: the old containers keep running. Fix and rebuild |
| A configuration message (`Cannot start: …`, `set DEPLOY_ENV`, a missing `*.env` setting) | Fix the named setting, `docker compose up -d` |
| `Database upgrade failed` or `schema is out of date` | Roll back (4.7) |
| Nginx fails (`nginx -t` errors, missing certificate or `admin.htpasswd`) | The certificate and password files must exist before `up` (2.2) |
| Anything else not fixed within your change window | Roll back (4.7) |

### 4.7 Rollback

**Release only (no schema change in the new release):** start the previous images again.
Data is untouched.

```bash
docker image ls support-chatbot-backend                  # find the previous tag
PREVIOUS_TAG=1a2b3c4                                      # ✏️ SERVER-SPECIFIC: the tag noted in 3.4
PREVIOUS_RELEASE=v1.0.0                                   # ✏️ SERVER-SPECIFIC: its release tag or commit
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$PREVIOUS_TAG/" .env
git -C "$APP_DIR" checkout "$PREVIOUS_RELEASE"            # keeps the compose files in step
docker compose up -d
docker compose ps && curl -fsS "https://$PUBLIC_HOSTNAME/healthz"
```

**Release with a schema change** (the new release logged `Added column` or `Upgraded
table`): older code does not support the newer schema. **⚠️ DESTRUCTIVE:** stop the
backend, restore the backup taken in 3.4 step 1 (3.6), then start the previous release
as above. Everything written since that backup is lost. Note the time window and tell
the people affected.

If the previous images were deleted, check out the previous release and
`docker compose build` first (the image tag in `.env` must match).

---

## 5. Reference

| | |
|---|---|
| Services | `backend` (API, port 8000, internal), `customer` (8501, internal), `admin` (8502, internal), `nginx` (80, 443 public; 8443 loopback), `backup` (no network), `model-init` (one-off), `backup-offsite` (opt-in) |
| Volumes | `db` (the database), `backups`, `models`, all named `support-chatbot_<name>` |
| Health | Public: `https://<PUBLIC_HOSTNAME>/healthz`. Internal: `/api/v1/health` (live), `/api/v1/ready` (ready, with checks) |
| Stop and start | `docker compose stop` (requests up to 20 s finish; 30 s before `SIGKILL`), `docker compose up -d` |
| ⚠️ Remove everything including data | `docker compose down -v` deletes the database, backups and model volumes. Never on a production server without a verified off-server backup |
| Verification suite (before a first deployment, on a test machine) | `RUN_DEPLOYMENT_TESTS=1 pytest tests/deployment` ([docs/deployment-tests.md](deployment-tests.md)) |

Related documents: [deployment-architecture.md](deployment-architecture.md),
[https-setup.md](https-setup.md), [backup-and-restore.md](backup-and-restore.md),
[health-checks.md](health-checks.md), [logging.md](logging.md),
[monitoring.md](monitoring.md), [error-reporting.md](error-reporting.md),
[shutdown-and-restart.md](shutdown-and-restart.md),
[concurrency-and-limits.md](concurrency-and-limits.md),
[embedding-model.md](embedding-model.md).
