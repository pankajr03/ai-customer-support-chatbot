# Deployment security checklist (Stage 13)

A go-live checklist for the single-server Docker Compose deployment. Work through it
on the target server **before customers are sent to the site**, and again after major
changes. Each item says how to check it. The design behind the items is in
[docs/deployment-architecture.md](deployment-architecture.md); the steps that set them
up are in the runbook ([docs/deployment-runbook.md](deployment-runbook.md)).

**Completing this checklist does not make the deployment production-ready.** Some
security gaps cannot be closed by configuration: they are listed in
[section 12](#12-known-security-limitations), and the overall verdict is in
[docs/deployment-readiness.md](deployment-readiness.md).

## How to read it

- **Built in**: the repository enforces it (configuration, tests). Check that nothing
  on the server overrides it.
- **Operator**: you must do it on the server or with your providers. The repository
  cannot.
- **Decision**: someone with authority must decide or approve it.
- Commands are bash, run from `deploy/` on the server with the runbook's variables set
  (`APP_DIR`, `PUBLIC_HOSTNAME`, `ADMIN_HOSTNAME`), unless a line says *from outside*
  (a machine on the internet that is not the server) or *on your workstation*.

## Contents

1. [Decisions and approvals](#1-decisions-and-approvals)
2. [Server](#2-server)
3. [Network and firewall](#3-network-and-firewall)
4. [HTTPS](#4-https)
5. [Public site exposure](#5-public-site-exposure)
6. [Staff access](#6-staff-access)
7. [Secrets and configuration](#7-secrets-and-configuration)
8. [Containers and images](#8-containers-and-images)
9. [Data protection](#9-data-protection)
10. [Logging, monitoring and alerting](#10-logging-monitoring-and-alerting)
11. [After go-live](#11-after-go-live)
12. [Known security limitations](#12-known-security-limitations)

---

## 1. Decisions and approvals

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | **The staff identity gap is accepted** for this deployment: static staff tokens with no expiry or MFA, revocation by restart (blockers B1–B3, [section 12](#12-known-security-limitations)). The dashboard stays off the public internet | Decision | A recorded decision naming who accepted it and until when |
| ☐ | **The LLM stays off** (`LLM_ENABLED=false`), **or** all of these exist: a reviewed live evaluation, a spending limit at OpenAI, a data-processing agreement, and a customer notice that messages go to OpenAI (SEC-07, SEC-11) | Decision | `grep '^LLM_ENABLED' environments/production/backend.env` |
| ☐ | Retention periods match your privacy policy (`CONVERSATION_TTL_MINUTES`, `SUPPORT_TICKET_RETENTION_DAYS`), and a maximum backup age is chosen ([backup-and-restore.md §4](backup-and-restore.md#4-schedule-and-retention)) | Decision | Read `environments/production/backend.env` |
| ☐ | Approval to request certificates (the hostnames appear in public certificate transparency logs) | Decision | Runbook 2.6 |
| ☐ | Who is on call, and who may restore data (a restore loses recent data) | Decision | Written down where the operators find it |

## 2. Server

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | Security updates applied, and applied automatically from now on (`unattended-upgrades` on Ubuntu) | Operator | `sudo apt list --upgradable`; `systemctl status unattended-upgrades` |
| ☐ | SSH: key-based login only, no root login | Operator | `sudo sshd -T \| grep -E '^(passwordauthentication\|permitrootlogin) '` shows `no` |
| ☐ | Only trusted people are in the `docker` group: membership is **equivalent to root** on the server | Operator | `getent group docker` |
| ☐ | Disk encryption for the volume holding `/var/lib/docker` (the database and backups hold contact details in clear) | Operator | `lsblk -o NAME,TYPE,MOUNTPOINT` shows a `crypt` device, or the provider's volume encryption is on |
| ☐ | The clock is synchronised (certificates, token expiry, backup names) | Operator | `timedatectl` shows `System clock synchronized: yes` |
| ☐ | Docker's data is on a **local** disk (the backend refuses a database on a network filesystem in production) | Built in | `docker info --format '{{.DockerRootDir}}'`; `df -T` for that path |

## 3. Network and firewall

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | Inbound only 22 (SSH, preferably restricted to known addresses), 80 and 443 | Operator | `sudo ufw status verbose` (runbook 1.5) |
| ☐ | **From outside**, a port scan shows only those ports. The dashboard port 8443 must not be reachable | Operator | *From outside:* `nmap -Pn -p 1-65535 "$PUBLIC_HOSTNAME"`, or at least `nc -zv -w 5 "$PUBLIC_HOSTNAME" 8443` (must fail) |
| ☐ | Docker's published ports are not opened behind the firewall's back: only Nginx publishes ports, and the dashboard listener binds to `127.0.0.1` | Built in | `docker compose ps --format '{{.Service}}: {{.Ports}}'`; `grep '^ADMIN_BIND=' .env` shows `127.0.0.1` |
| ☐ | IPv6: either the same rules apply, or there is no AAAA record | Operator | `dig AAAA "$PUBLIC_HOSTNAME" +short`; `sudo ip6tables -S` |
| ☐ | The backend has no internet route it does not need: with the LLM off, drop the `egress` network from the backend in an override file if you want no outbound access at all | Operator (optional) | [deployment-architecture.md §2](deployment-architecture.md#2-networks) |

## 4. HTTPS

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | A **trusted** certificate (Let's Encrypt) for both hostnames; the development placeholder is gone | Operator | *From outside:* `python3 -m scripts.check_certificate "$PUBLIC_HOSTNAME"` → `OK`, issuer Let's Encrypt |
| ☐ | The dashboard's certificate, through the tunnel or VPN | Operator | *On your workstation:* `python3 -m scripts.check_certificate "$ADMIN_HOSTNAME" --port 8443` |
| ☐ | TLS 1.2 and 1.3 only; HTTP redirects with `308`; HSTS on every HTTPS response | Built in | `curl -sI "http://$PUBLIC_HOSTNAME/" \| head -1` → `308`; `curl -sI "https://$PUBLIC_HOSTNAME/" \| grep -i strict-transport` |
| ☐ | Renewal works without downtime | Operator | `sudo certbot renew --dry-run`; `systemctl status certbot.timer` (or `snap.certbot.renew.timer`) |
| ☐ | **Daily expiry monitoring from another machine**, alerting someone (Let's Encrypt sends no reminders) | Operator | [https-setup.md §8](https-setup.md#8-monitoring-certificate-expiry) |
| ☐ | Cookies carry `Secure` | Built in | `curl -sI "https://$PUBLIC_HOSTNAME/" \| grep -i set-cookie` (if the page sets one): `Secure` present |

Turn the real certificate on and check every name **before** customers visit: after one
visit, browsers refuse the site for a year (HSTS) if the certificate later fails.

## 5. Public site exposure

Run *from outside*:

```bash
# ✏️ SERVER-SPECIFIC: the server's public IP address
SERVER_IP=203.0.113.10
curl -s -o /dev/null -w '%{http_code}\n' "https://$PUBLIC_HOSTNAME/api/v1/admin/faqs"   # 404: no staff API
curl -s -o /dev/null -w '%{http_code}\n' "https://$PUBLIC_HOSTNAME/api/v1/ready"        # 404: detailed readiness is internal
curl -s "https://$PUBLIC_HOSTNAME/healthz"                                              # {"status":"ready"} only
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer stf_test' \
  "https://$PUBLIC_HOSTNAME/api/v1/faqs"                                                # 403: staff tokens refused
curl -sk -o /dev/null -w '%{http_code}\n' "https://$SERVER_IP/"                         # 000: by IP, closed without a reply
curl -sI "https://$PUBLIC_HOSTNAME/" | grep -i '^server:'                               # "nginx", no version
```

| ✓ | Item | Kind |
|---|---|---|
| ☐ | Every line above gives the expected answer | Built in |
| ☐ | Security headers on the pages and the API: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Content-Security-Policy` (`frame-ancestors 'none'`), `Permissions-Policy` | Built in |
| ☐ | Error responses carry no internals: a code, a message, a request ID and `retryable` ([error-reporting.md](error-reporting.md)) | Built in |
| ☐ | A dynamic scan of the deployed site (for example OWASP ZAP baseline) with the results reviewed. **Never done yet** | Operator |

## 6. Staff access

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | The dashboard is reached only through an SSH tunnel or a VPN; `ADMIN_BIND=127.0.0.1` unless a VPN interface is used | Built in, Operator | Runbook 2.9 |
| ☐ | The dashboard refuses non-private source addresses (second control) | Built in | [authentication-review.md §10](authentication-review.md#10-changes-made-in-this-review) |
| ☐ | **One basic-auth entry per person** in `nginx/admin.htpasswd`, strong passwords, SHA-512 crypt | Operator | `cut -d: -f1 nginx/admin.htpasswd`; each hash starts with `$6$` |
| ☐ | **One staff token per person**, created on the server, given to that person only; only hashes in `STAFF_TOKEN_HASHES` | Operator | Runbook 2.4 |
| ☐ | Least privilege: `admin` only for people who manage FAQs and see analytics; everyone else `support` | Operator | The role is the middle field of each `STAFF_TOKEN_HASHES` entry |
| ☐ | A written procedure for a leaked staff token or password (remove, restart, review the audit log) | Operator | Runbook 4.5 |
| ☐ | Staff know: support staff can read every ticket's contact details (SEC-06), and contact views are recorded in the audit log | Decision | |

## 7. Secrets and configuration

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | Configuration files readable by the deploying user only | Operator | `stat -c '%a %U %n' .env environments/production/*.env` shows `600` |
| ☐ | The private key and the password file readable by Nginx's group only | Operator | `stat -c '%a %U:%G %n' nginx/certs/privkey.pem nginx/admin.htpasswd` shows `640 root:101` |
| ☐ | No secret in Git | Built in | `git -C "$APP_DIR" status --ignored --short deploy/` lists them as ignored (`!!`) |
| ☐ | `APP_ENV=production`, `DEBUG=false`, rate limits on, an on-disk database: the backend refuses to start otherwise | Built in | `grep -E '^(APP_ENV\|DEBUG\|RATE_LIMIT_ENABLED)=' environments/production/backend.env` |
| ☐ | The development `.env` at the project root is not used (Compose reads `deploy/.env`; the root `.env` is excluded from images) | Built in | [deployment-architecture.md §6](deployment-architecture.md#6-environment-specific-configuration) |
| ☐ | Never share `docker compose config` output: it prints secrets. Use `docker compose config --quiet` | Operator | |
| ☐ | A copy of `deploy/` configuration and the restic password in your secrets store | Operator | [backup-and-recovery.md §1](backup-and-recovery.md#1-what-needs-protecting) |
| ☐ | A rotation schedule for the OpenAI key, staff tokens and the dashboard password | Operator | Runbook 3.7 |

## 8. Containers and images

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | Containers run as non-root, with a read-only root filesystem, no capabilities, `no-new-privileges`, PID and memory limits | Built in | `docker inspect --format '{{.Name}} user={{.Config.User}} ro={{.HostConfig.ReadonlyRootfs}} caps={{.HostConfig.CapDrop}}' $(docker compose ps -q)` |
| ☐ | Base images pinned by digest; Python dependencies hash-locked for the images (SEC-16 addressed for the deployment) | Built in | [deployment-architecture.md §13](deployment-architecture.md#13-images) |
| ☐ | Each release has its own `IMAGE_TAG` (the commit), recorded in the image and in the database's release history | Operator | `grep '^IMAGE_TAG=' .env`; `docker compose exec backend python -m scripts.release_history` |
| ☐ | The embedding model is the pinned revision, verified, loaded offline (`HF_HUB_OFFLINE=1`) | Built in | `/api/v1/ready` shows `semantic_search: ready` (runbook 2.7) |
| ☐ | One backend worker (rate limits and the FAQ catalogue are per process, SEC-09, SEC-10) | Built in | `docker inspect --format '{{.Config.Cmd}}' $(docker compose ps -q backend)` shows `--workers 1` |
| ☐ | Images rebuilt for base-image security updates on a schedule; dependency audit (`pip-audit`) run on each release | Operator | [test-and-quality-report.md](test-and-quality-report.md) |

## 9. Data protection

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | The `backup` service is healthy | Built in | `docker compose ps backup` |
| ☐ | **Off-server copies configured**, encrypted, and the first copy succeeded. Until then every backup is on the same disk as the database | Operator | `docker compose --profile offsite ps backup-offsite` healthy; [backup-and-restore.md §5](backup-and-restore.md#5-off-server-copies) |
| ☐ | A restore drill done: verify and test-restore a backup | Operator | [backup-and-recovery.md §7](backup-and-recovery.md#7-drills) |
| ☐ | Retention scripts scheduled (weekly), not only at startup (SEC-12) | Operator | `crontab -l` shows the two commands from runbook 3.9 |
| ☐ | Backups and manifests owner-only (`0600`), never in Git or images | Built in | `docker compose exec backend ls -l /app/data/backups` |

## 10. Logging, monitoring and alerting

| ✓ | Item | Kind | How to check |
|---|---|---|---|
| ☐ | Logs contain no message text, tokens or contact details | Built in (tested) | [logging.md](logging.md#what-is-never-logged) |
| ☐ | An **external uptime check** on `https://$PUBLIC_HOSTNAME/healthz`, alerting someone | Operator | [monitoring.md](monitoring.md#monitoring-the-deployment) |
| ☐ | Alerts on: an unhealthy container, a failed backup, the disk at 80 %, server errors, bursts of refused staff access, the certificate expiring | Operator | [monitoring.md](monitoring.md#alerts-to-set-up) |
| ☐ | Logs kept long enough for an investigation (Docker keeps only 5 × 10 MB per container): ship them to a collector if needed | Operator | [logging.md](logging.md#docker) |

## 11. After go-live

- Watch for the first day: `docker compose ps`, `docker stats --no-stream`, errors in the
  logs (runbook 3.1), and the dashboard's **Service monitoring**.
- Check Nginx's `429` rate: limits that are too tight refuse real customers; too loose
  ones let one client use the backend's capacity.
- Repeat sections 3–5 *from outside* after every change to the firewall, DNS or Nginx.
- Repeat the whole checklist at least every six months, and after any security incident.

## 12. Known security limitations

These remain after every item above is done. **Blocking** ones keep the readiness
assessment's Authentication or Security category Blocked; accepted ones need the
decision in section 1.

| ID | Limitation | Status | Mitigation in this deployment |
|---|---|---|---|
| SEC-08, B1 | Staff authentication is static tokens: no per-person sign-in, no MFA | **Blocking** | Dashboard on loopback behind a tunnel or VPN, plus basic auth, plus a private-network allow-list |
| B2 | Staff tokens never expire | **Blocking** | Dashboard sessions expire after 30 minutes idle, 8 hours at most (page side only) |
| B3 | Revoking a staff token needs a backend restart | **Blocking** | Runbook 3.7 and 4.5 (seconds of downtime) |
| B4 | No sign-in auditing or alerting on failed staff access | Open | Refused staff requests are in the audit log and in `api_error` log lines; alert on them yourself |
| SEC-06, B5 | Support staff can see every ticket's contact details | Accepted (small trusted team) | Contact views recorded in the audit log |
| B6 | The customer page container can reach the staff API on the customer network | Open | Staff routes still need a staff token; the page holds none |
| B7 | Basic authentication: no logout or expiry | Open | One entry per person; replaced when B1 is done |
| SEC-07 | `/answer` and search need no token | Mitigated | Own rate limits; with the LLM off nothing is paid |
| SEC-11 | Customer text goes to OpenAI as typed (LLM on only) | Open | LLM off by default |
| SEC-12 | Retention runs at startup or by hand | Open | Schedule it (section 9) |
| SEC-14, SEC-15, SEC-21 | Prompt injection and grounding checks are layered heuristics, not proofs | Accepted | The LLM may only reword an approved answer; failures fall back to the approved text |
| SEC-09, SEC-10 | Rate limits and the FAQ catalogue are per process | Addressed by the design | One backend worker; per-visitor limits at Nginx with trusted forwarded addresses |
| — | Never tested from outside on a real server: no external scan, no penetration test | **Blocking** until done | Sections 3 and 5 |
| — | No encryption at rest by the application | Open | Disk encryption (section 2), encrypted off-server copies (section 9) |
| — | Traffic between containers is plain HTTP on internal Docker networks of one host | Accepted for one server | Internal networks with no route outside |

Sources: [security-audit.md](security-audit.md#3-findings-summary),
[authentication-review.md §11](authentication-review.md#11-deployment-blockers),
[deployment-readiness.md](deployment-readiness.md).
