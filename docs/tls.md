# TLS at the edge — acme.sh, deploy scripts, hot reload

Phone-facing TLS terminates at Kamailio (`tls:PUBIP:5061`, `kamailio/tls.cfg`).
The edge↔home hop stays cleartext inside the WireGuard/Pangolin tunnel — there
is deliberately no TLS there.

## Components (all in `deploy/`)

| File | Role |
|---|---|
| `acme/run.sh` | acme.sh container entrypoint: issues the cert if missing (DNS-01 via nsupdate/RFC2136 or the Technitium API), installs it into the container's `/tls`, registers the deploy hook, then runs the acme cron every 12 h. |
| `acme/deploy-certs.sh` | `--reloadcmd` target: writes a timestamped release directory under `/tls/releases/`, flips the stable `current` symlink atomically and re-points `sip.crt`/`sip.key` through it. Kamailio never reads a new certificate with an old key. Keeps the five newest releases. |
| `watch-certs.sh` | `cert-reloader` container: watches `/certs/.renewed` and calls `kamcmd -s unix:/run/kamailio/kamailio_ctl tls.reload`. No docker.sock, no restart. |
| `acme-deploy-hook.sh` | Standalone fallback for non-compose setups: sync certs from an acme.sh volume into Kamailio's TLS dir and reload only when files changed. |

`kamailio/tls.cfg` expects the live pair at `/etc/kamailio/tls/sip.{crt,key}`
(container paths; host paths are whatever you mount there).

## Configuration

Copy `deploy/acme.env.example` and set at least:

- `ACME_EMAIL`, `ACME_DOMAINS` (wildcard **and** per-tenant `sip.<client>` SANs —
  Let's Encrypt wildcards cover one label only), `ACME_CERT_NAME` = the **first**
  `ACME_DOMAINS` entry.
- DNS-01 target: `NSUPDATE_SERVER` + a TSIG key file mounted as a docker secret
  (`NSUPDATE_KEY=/run/secrets/technitium_tsig`), or the Technitium API
  (`ACME_DNS_PROVIDER=dns_technitium`, `Technitium_Server`, `Technitium_Token`).

Secrets never enter this repo:

- the TSIG file is gitignored and the pre-commit guard refuses `*technitium_tsig*`;
- `deploy/*.env`, `deploy/acme.env` and any `.env` are refused;
- certificate/key material (`*.crt`, `*.key`, `*.pem`) is refused.

## Staging → live promotion

`ACME_SERVER=letsencrypt_test` writes a *staging* certificate that run.sh will
happily re-install on every start (it only checks that a cert exists). To
promote:

1. set `ACME_SERVER=letsencrypt`, `ACME_FORCE_ISSUE=1`;
2. recreate the acme container (`docker compose up -d --force-recreate acme`);
3. watch `docker logs` for `Cert success`, verify with `openssl s_client`;
4. set `ACME_FORCE_ISSUE=0` again (otherwise every recreate re-issues and can
   hit Let's Encrypt duplicate-certificate limits).

## Renewal visibility

Renewal failures are logged by the acme container but not alerted. Add an
external check on `notAfter` (e.g. `openssl x509 -enddate`) and watch for
`tls.reload failed` in the cert-reloader logs; the watcher now retries a failed
reload instead of dropping it.

## Adding a tenant's TLS name

1. Append `,sip.<client>.home.example.net` to `ACME_DOMAINS`.
2. One-shot reissue: `ACME_FORCE_ISSUE=1`, recreate the acme container (`docker compose up -d --force-recreate acme`), watch
   `docker logs <acme>` for `Cert success`.
3. Set `ACME_FORCE_ISSUE=0` again.
4. The deploy hook + cert-reloader install and hot-reload the new certificate
   (no Kamailio restart; new handshakes pick it up).

## Verify

```sh
openssl s_client -connect <PUBIP>:5061 \
  -servername sip.<client>.home.example.net -verify_return_error </dev/null \
  2>&1 | grep -E 'Verify return|subject=|issuer='
# expect: Verify return code: 0, LE issuer, the tenant SAN in the chain
```

## Failure modes worth knowing

- **`no tls engine` at startup**: the `tls` module failed to load — check the
  `WITH_TLS` define and that `tls.cfg` + cert files exist before enabling it.
- **Renewal fired but handshakes still show the old cert**: `tls.cfg` paths and
  the mounted directory disagree; `tls.reload` only re-reads what `tls.cfg`
  names.
- **Cert expired after a rebuild**: `create-db.sh` resets the DB, not TLS, but
  restoring an old `certs/` snapshot can reintroduce an expired pair — compare
  `notAfter` before restoring.
