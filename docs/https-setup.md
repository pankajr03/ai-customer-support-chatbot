# HTTPS setup with a trusted certificate authority (Stage 13)

How to give the Docker deployment ([docs/deployment-architecture.md](deployment-architecture.md))
a publicly trusted certificate from Let's Encrypt, keep it renewed, and know before it
expires. Written for whoever sets up and runs the server.

> **Status.** The configuration and tools below are in the repository and were checked
> on Docker with **development-only, self-signed certificates** (section 9). No
> certificate has been requested from any certificate authority and no DNS record has
> been changed. A self-signed certificate proves the mechanics (routing, reloads,
> cookie flags, monitoring), **not** that public HTTPS works. That is proven only by
> section 4 on the real server, followed by the checks in section 10.

## Contents

1. [How TLS works in this deployment](#1-how-tls-works-in-this-deployment)
2. [DNS prerequisites](#2-dns-prerequisites)
3. [Hostnames and the staff dashboard](#3-hostnames-and-the-staff-dashboard)
4. [Issuing the certificate](#4-issuing-the-certificate)
5. [Renewal](#5-renewal)
6. [HTTP to HTTPS redirects and HSTS](#6-http-to-https-redirects-and-hsts)
7. [Cookies](#7-cookies)
8. [Monitoring certificate expiry](#8-monitoring-certificate-expiry)
9. [Local verification (development only)](#9-local-verification-development-only)
10. [Go-live checklist](#10-go-live-checklist)

---

## 1. How TLS works in this deployment

TLS ends at Nginx. Nothing behind it handles certificates.

```
browser ──HTTPS──► Nginx (TLS termination) ──HTTP──► customer / admin / backend
          :443     certificate: deploy/nginx/certs/      private Docker networks only
                   fullchain.pem + privkey.pem (read-only)
```

| Topic | How it works here |
|---|---|
| Certificate files | `deploy/nginx/certs/fullchain.pem` (certificate plus intermediates) and `privkey.pem`, bind-mounted read-only into Nginx. One certificate covers both hostnames (`PUBLIC_HOSTNAME`, `ADMIN_HOSTNAME`) |
| Choosing a site | By SNI, the name in the TLS handshake. A handshake for any other name, or without a name (by IP address), is refused (`ssl_reject_handshake`). A request whose `Host` differs from the handshake name gets `421` |
| Protocols | TLS 1.2 and 1.3; session tickets off; HTTP/2 on |
| Behind Nginx | Plain HTTP on internal Docker networks that never leave the server and have no route to the internet |
| What the backend knows | Nginx sends `X-Forwarded-Proto: https` and the client address; the backend trusts them only from Nginx's fixed address ([reverse proxy](deployment-architecture.md#14-reverse-proxy)) |
| What the pages know | Streamlit sees plain HTTP. That is why it does not mark its cookie `Secure`, and why Nginx does ([section 7](#7-cookies)) |
| HSTS | Added by Nginx on HTTPS responses only, never on port 80 |
| Changing the certificate | Replace the two files and reload Nginx (`nginx -c /tmp/nginx.conf -s reload`). Open connections finish on the old certificate; nothing restarts. The deploy hook does this ([section 5](#5-renewal)) |
| Port 80 | Kept open: it serves Let's Encrypt's challenge files and redirects everything else to HTTPS ([section 6](#6-http-to-https-redirects-and-hsts)) |

No OCSP stapling is configured: Let's Encrypt has stopped running OCSP responders, so
there is nothing to staple.

## 2. DNS prerequisites

Do these at your DNS provider **before** requesting a certificate. This project does
not change DNS. The commands below only read.

| Record | Value | Why |
|---|---|---|
| `A` for `PUBLIC_HOSTNAME` (e.g. `support.example.com`) | The server's public IPv4 address | Customers, and Let's Encrypt's validation, reach the server |
| `AAAA` for the same name | The server's public IPv6 address, **or no AAAA record at all** | Let's Encrypt prefers IPv6 when an AAAA record exists. A stale or unrouted AAAA makes validation fail even when IPv4 works. Docker publishes on IPv6 only if configured to |
| `A` (and optionally `AAAA`) for the dashboard name, if it is a real name ([section 3](#3-hostnames-and-the-staff-dashboard)) | The same server | HTTP-01 validation of that name, which is served on public port 80 |
| `CAA` on the domain (optional, recommended) | `0 issue "letsencrypt.org"` | Only Let's Encrypt may issue for the domain. If a `CAA` record already exists, it must allow `letsencrypt.org`, or issuance fails |

Also:

- **Firewall:** inbound TCP 80 and 443 open to everyone. Port 80 must stay open after
  issuance, for renewals and redirects. The dashboard port stays closed to the
  internet (`ADMIN_BIND=127.0.0.1`).
- **Lower the TTL** (for example to 300 s) a day before moving a name to a new server,
  so a mistake can be fixed quickly.
- **Check before issuing:**

  ```bash
  dig +short A support.example.com      # the server's IPv4 address
  dig +short AAAA support.example.com   # the server's IPv6 address, or nothing
  dig +short CAA example.com            # empty, or includes letsencrypt.org
  curl -sI http://support.example.com/.well-known/acme-challenge/x   # 404 from this server
  ```

## 3. Hostnames and the staff dashboard

`deploy/.env` sets the names Nginx answers to, and the certificate must cover both:

| Setting | Example | Notes |
|---|---|---|
| `PUBLIC_HOSTNAME` | `support.example.com` | The customer site |
| `ADMIN_HOSTNAME` | `admin.support.example.com` | The dashboard. The default, `localhost`, works with an SSH tunnel and a development certificate, but **no public certificate authority certifies `localhost`**. With a trusted certificate, use a real name |

A real dashboard name does not expose the dashboard. Its listener is still bound to
`127.0.0.1` (or a VPN address), and only certificate validation happens on public port
80. Staff reach it in one of two ways:

- **SSH tunnel:** `ssh -L 8443:127.0.0.1:8443 you@server`, plus a hosts-file entry on
  the staff computer, `127.0.0.1 admin.support.example.com`. Then open
  `https://admin.support.example.com:8443`. The browser sees the real name, so the
  trusted certificate matches.
- **VPN:** set `ADMIN_BIND` to the server's VPN address, and point the name at that
  address in the VPN's DNS.

If the dashboard name must not appear in public DNS at all, validate it with a **DNS-01**
challenge instead (certbot's plugin for your DNS provider). That is not covered here.

## 4. Issuing the certificate

**Ask for approval before running anything in this section:** it contacts Let's Encrypt
and creates real certificates, which appear in public certificate transparency logs.

### Tools

Use **certbot on the host** (not in a container), in **webroot** mode. Nginx keeps
running and serves the challenge files from `deploy/nginx/acme/`. That folder is
mounted read-only at `/var/www/acme`; only `/.well-known/acme-challenge/` is served,
as plain text, with no listings. certbot needs no access to the containers or to the
Docker socket.

```bash
sudo snap install --classic certbot      # or the distribution's certbot package
```

### First start: a placeholder certificate

Nginx does not start without *some* certificate, and Let's Encrypt can only validate
once Nginx is serving port 80. Start with a short-lived development certificate for
the real names. It is replaced in a few minutes and is never what customers should
see.

```bash
cd /srv/support-chatbot/deploy
sh dev/make-dev-certificate.sh support.example.com admin.support.example.com
sudo chown root:101 nginx/certs/privkey.pem && sudo chmod 640 nginx/certs/privkey.pem
docker compose up -d
```

### Staging first, then production

Let's Encrypt's **staging** environment issues untrusted certificates with generous rate
limits: use it to prove DNS, the firewall and the webroot. Production has strict rate
limits on failed validations and duplicate certificates.

```bash
# 1. Staging (untrusted certificate; proves that validation works)
sudo certbot certonly --staging \
  --webroot -w /srv/support-chatbot/deploy/nginx/acme \
  -d support.example.com -d admin.support.example.com \
  --cert-name support-chatbot \
  --deploy-hook /srv/support-chatbot/deploy/certbot/deploy-hook.sh

# 2. Production: remove the staging lineage, then issue for real
sudo certbot delete --cert-name support-chatbot
sudo certbot certonly \
  --webroot -w /srv/support-chatbot/deploy/nginx/acme \
  -d support.example.com -d admin.support.example.com \
  --cert-name support-chatbot \
  --deploy-hook /srv/support-chatbot/deploy/certbot/deploy-hook.sh
```

An account email (`--email`) is optional: Let's Encrypt no longer sends expiry
reminders ([section 8](#8-monitoring-certificate-expiry)).

### What the deploy hook does

[deploy/certbot/deploy-hook.sh](../deploy/certbot/deploy-hook.sh) runs after every
successful issuance and renewal. certbot stores it in the renewal configuration, so
renewals run it too. It:

1. copies `fullchain.pem` and `privkey.pem` out of `/etc/letsencrypt/live/<name>/`
   (resolving certbot's symlinks) into `deploy/nginx/certs/`;
2. refuses a certificate that has already expired, or a key that does not match it;
3. makes the key readable by Nginx's group only (`root:101`, mode `640`);
4. keeps the previous pair in `deploy/nginx/certs/previous/`;
5. runs `nginx -t` with the new files, restoring the previous pair if it fails;
6. reloads Nginx gracefully.

The hook must be executable (`chmod +x`), or called through `sh`.

## 5. Renewal

- **Automatic:** the certbot package installs a systemd timer (or cron job) that runs
  `certbot renew` twice a day. certbot renews when a certificate is close to expiry: by
  default about 30 days before, for today's 90-day certificates, or as the authority's
  renewal information suggests. Let's Encrypt has announced that certificate lifetimes
  will shorten (towards 45 days), so **do not assume 90 days anywhere**; let certbot
  decide when, and monitor.
- **Installed automatically:** each renewal runs the deploy hook, which installs the new
  files and reloads Nginx. Customers see no interruption.
- **Check the timer exists:** `systemctl list-timers | grep certbot`.
- **Rehearse a renewal:** `sudo certbot renew --dry-run`. This uses the staging
  environment and installs nothing, and the deploy hook does not run. Rehearse the hook
  separately, with the production files certbot already holds:
  `sudo RENEWED_LINEAGE=/etc/letsencrypt/live/support-chatbot sh /srv/support-chatbot/deploy/certbot/deploy-hook.sh`.
  It reinstalls the current certificate and reloads Nginx, which is harmless.
- **If renewal fails:** the old certificate keeps working until it expires. Monitoring
  ([section 8](#8-monitoring-certificate-expiry)) is what tells you. The usual causes:
  port 80 blocked, DNS moved, an `AAAA` record added, the webroot path changed, or the
  `CAA` record changed.

## 6. HTTP to HTTPS redirects and HSTS

| Request on port 80 | Answer |
|---|---|
| `/.well-known/acme-challenge/<token>` (either hostname) | The token file, if it exists; otherwise `404` |
| Anything else for `PUBLIC_HOSTNAME` | `308` to `https://PUBLIC_HOSTNAME:PUBLIC_HTTPS_PORT/<same path and query>` |
| Any other hostname, or by IP address | Connection closed without a response (`444`) |

- **`308`, not `301`:** the method and body are kept, so an API client that sent a `POST`
  over plain HTTP is not silently turned into a `GET`. Such a client should still be
  fixed to use HTTPS: its request already crossed the network unencrypted.
- **The redirect uses the configured name**, never the client's `Host` header, so it
  cannot be steered to another site.
- **HSTS** (`Strict-Transport-Security: max-age=31536000`) is sent on every HTTPS
  response and never over HTTP. After one visit, browsers use HTTPS for a year without
  trying HTTP first. Turn the real certificate on and check every name **before**
  customers visit: a browser that has stored HSTS will refuse to load the site at all
  (no "continue anyway") if the certificate later fails. `includeSubDomains` and
  `preload` are deliberately not set: they affect every site on the domain, which is
  the domain owner's decision.

## 7. Cookies

What the application stores in cookies, checked on Docker (section 9):

| Cookie or credential | Set by | Flags in the browser |
|---|---|---|
| `_streamlit_xsrf`, the pages' double-submit XSRF token (customer page and dashboard) | Streamlit | `Path=/; SameSite=Lax; Secure` |
| API conversation and staff tokens | Not cookies: `Authorization: Bearer` headers | n/a: no cookie, so no CSRF exposure through the API |
| Dashboard basic authentication | Browser credential cache, not a cookie | Sent only over HTTPS |
| The staff token typed into the dashboard | Kept in the Streamlit server's session memory | Never stored in the browser |

**The finding:** Streamlit adds `Secure` only when it terminates TLS itself
(`server.sslCertFile`). Behind Nginx it sees plain HTTP, so straight from Streamlit the
cookie is `Path=/; SameSite=Lax` (observed). Nginx adds the flag to every cookie it
passes to the browser (`proxy_cookie_flags ~ secure` in
[deploy/nginx/snippets/secure-cookies.conf](../deploy/nginx/snippets/secure-cookies.conf)).
Through Nginx the same cookie arrives as `Path=/; SameSite=Lax; Secure` on both sites
(observed).

- **`HttpOnly` is deliberately absent:** the page's JavaScript must read the XSRF token
  to send it back in a header. That is Streamlit's design, and the token is not a
  login.
- **`SameSite=Lax`** is kept as Streamlit sets it. The pages are not embedded in other
  sites (`X-Frame-Options: DENY`, `frame-ancestors 'none'`).
- **If a feature later adds a login cookie**, for example Streamlit's own
  authentication: it gets `Secure` from Nginx automatically, but it also needs
  `HttpOnly`, `SameSite`, and a configured `server.cookieSecret`. Review it then.

## 8. Monitoring certificate expiry

Let's Encrypt **no longer sends expiry reminder emails**, and lifetimes are getting
shorter. Monitor the certificate the site **actually serves**, from **outside** the
server, every day. A file on disk can look fine while Nginx still serves an old one.

[scripts/check_certificate.py](../scripts/check_certificate.py) connects like a browser
(full chain and hostname verification) and reports the days left. Standard library only.

```bash
python -m scripts.check_certificate support.example.com                    # warn under 21 days
python -m scripts.check_certificate admin.support.example.com --port 8443  # over the tunnel or VPN
```

| Exit code | Meaning | Action |
|---|---|---|
| `0` | Valid for more than `--warn-days` (default 21) | None |
| `1` | Valid, but expires within `--warn-days` | Renewal should already have happened (it starts about 30 days before expiry): check `certbot renew` and its timer |
| `2` | Invalid (untrusted, wrong name, expired) or unreachable | Urgent: customers see errors, or soon will |

Run it from another machine (or a hosted cron) once a day, alerting on any non-zero exit:

```cron
15 7 * * * cd /opt/support-chatbot && python3 -m scripts.check_certificate support.example.com || /usr/local/bin/alert "certificate check failed: support.example.com"
```

An external uptime service with certificate monitoring does the same job. Use either
one, but have one. Also watch certbot itself: `systemctl status certbot.timer`, and
`/var/log/letsencrypt/letsencrypt.log` after each renewal.

For a manual look: `echo | openssl s_client -connect support.example.com:443 -servername support.example.com 2>/dev/null | openssl x509 -noout -issuer -enddate -ext subjectAltName`.

## 9. Local verification (development only)

[deploy/dev/make-dev-certificate.sh](../deploy/dev/make-dev-certificate.sh) creates a
**development-only**, self-signed, 7-day certificate. Its subject reads *DEVELOPMENT
ONLY - not trusted*. It exists to test the mechanics locally. It is not trusted by
browsers and is **not evidence of public HTTPS readiness**.

Checked on 2026-10-19 on Docker (WSL 2), `PUBLIC_HOSTNAME=localhost`, with these
development certificates only:

| Check | Result |
|---|---|
| Configuration | `nginx -t` successful with the ACME location, the redirect location and the cookie flags |
| ACME webroot | A token file placed where certbot puts it was served on port 80 as `text/plain` for the public name, and also for the dashboard's name (`200`). Unknown token `404`, directory listing `404` |
| Redirects | Other paths for the public name: `308` to `https://localhost:18443/chat?x=1` (path and query kept). Other names: closed (444). No HSTS header over HTTP |
| Cookies | Straight from Streamlit (customer and dashboard): `_streamlit_xsrf=…; Path=/; SameSite=Lax`. Through Nginx: `…; Path=/; SameSite=Lax; Secure`. The API sets no cookies |
| Renewal through the deploy hook | A new development certificate installed as if renewed: the served certificate's serial changed, Nginx was **not restarted** (same start time), the page answered `200`, and the previous pair was kept in `certs/previous/` |
| Hook refusals | A key that does not match the certificate, and an already expired certificate: both refused (exit 1); the served certificate unchanged |
| Expiry monitoring | Against the live listener with the development certificate as CA: `RENEW SOON … 7.0 days left` (exit 1, default 21-day warning); with `--warn-days 3`: `OK` (exit 0); without the CA file: `INVALID … self-signed certificate` (exit 2) |
| Tests | `tests/test_check_certificate.py` (10, including a real TLS server on 127.0.0.1); HTTPS checks in `tests/test_deployment_config.py` |

**Not verified, because it needs the real server and approval:** issuance and renewal
against Let's Encrypt (staging or production), DNS, the firewall, IPv6, the dashboard
over a tunnel with a trusted certificate, and browser behaviour with HSTS.

## 10. Go-live checklist

1. DNS records and firewall as in [section 2](#2-dns-prerequisites); `dig` shows the
   server for every name.
2. `ADMIN_HOSTNAME` set to a real name, and staff access chosen (tunnel and hosts
   entry, or VPN).
3. **Approval to request certificates.** Staging issuance succeeds, then production.
4. `python -m scripts.check_certificate <each name>` from outside: `OK`, issuer Let's
   Encrypt, both names listed.
5. `sudo certbot renew --dry-run` succeeds; running the deploy hook by hand
   ([section 5](#5-renewal)) succeeds, and the site still answers.
6. Daily expiry monitoring alerting someone, from another machine.
7. Only then send customers to the site: HSTS will pin them to HTTPS for a year.
8. Delete the placeholder (`deploy/nginx/certs/previous/`) once the real certificate
   is served.
