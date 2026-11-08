# TLS at the edge

Kamailio terminates phone-facing TLS on `tls:PUBIP:5061`; edge↔home stays
cleartext inside the tunnel.

## Components

| Path | Role |
|---|---|
| `acme/run.sh` | Issue/renew via DNS-01, install into `/tls` |
| `acme/deploy-certs.sh` | Atomic release dir, stable symlink, keep 5 |
| `acme/watch-certs.sh` | Watch `.renewed`, call `kamcmd tls.reload` |
| `acme/acme-deploy-hook.sh` | Non-compose fallback |

## Configure

Set in `.env`:

- `ACME_EMAIL`, `ACME_DOMAINS`, `ACME_CERT_NAME`. Include each
  `sip.<client>` SAN; wildcards cover one label only. `ACME_CERT_NAME` is the
  first `ACME_DOMAINS` entry.
- `NSUPDATE_SERVER` plus `${EDGE_HOME}/acme/technitium_tsig`, or
  `ACME_DNS_PROVIDER=dns_technitium` with `Technitium_Server` and
  `Technitium_Token`.

Enable TLS: add `-A WITH_TLS` to the Kamailio `entrypoint` in `compose.yaml`
and uncomment both TLS mounts. Recreate Kamailio.

## Promote staging

1. `ACME_SERVER=letsencrypt`, `ACME_FORCE_ISSUE=1`.
2. `docker compose up -d --force-recreate acme`.
3. Check `docker logs voip-acme` for `Cert success`.
4. Set `ACME_FORCE_ISSUE=0`.

## Renew and add a tenant

Renewal is automatic. `deploy-certs.sh` writes `sip.key` as `0600`; for
non-root Kamailio set `KAM_UID`/`KAM_GID` or use a shared group.

Add `,sip.<client>.home.example.net` to `ACME_DOMAINS`, force reissue, then
reset `ACME_FORCE_ISSUE=0`. `cert-reloader` installs and reloads without a
Kamailio restart.

## Verify

```sh
openssl s_client -connect <PUBIP>:5061 \
  -servername sip.<client>.home.example.net -verify_return_error </dev/null \
  2>&1 | grep -E 'Verify return|subject=|issuer='
```

Expect code 0 and the tenant SAN. Watch `notAfter` and `tls.reload failed`.
