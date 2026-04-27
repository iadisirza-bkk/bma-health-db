# Security Hardening — BMA Health DB

Last updated: 2026-04-27 (Schema v3 + 7-of-12 hardening gaps closed)

## Threat Model

| Actor | Read access | Write access | Mechanism |
|-------|-------------|--------------|-----------|
| End user (browser) | `public.mv_*` aggregates only (k-anon ≥ 5) | ❌ | API endpoint + k-anon |
| Researcher (API key) | Same as end user | ❌ | API key + same MVs |
| Admin (login + 2FA TBD) | + Admin UI views | ✅ Upload CSV → ETL | Admin panel |
| DBA (psql + dba_user) | ✅ ALL tables (private + public) | ✅ ALL | DB password (rotate) |
| Server attacker (RCE on FastAPI) | **Limited to api_user perms** | **Limited to api_user perms** | role-based DB auth |
| Disk attacker (steal pgdata) | Everything in pgdata | Yes | needs physical/volume access |

**The single most important boundary**: FastAPI runs as `api_user` (role `bma_api_reader`),
which has **NO access to `private.*` schema** even with SQL injection. Verified:
```
$ psql -U api_user -d bma_health -c "SELECT COUNT(*) FROM private.patient"
ERROR: permission denied for schema private
```

## Layer-by-Layer

### Layer 1: Database Roles (PostgreSQL ACL)

| Role | private.* | public.* | Use |
|------|:---------:|:--------:|-----|
| `bma_dba_admin` | ALL | ALL | DBAs (audit-logged) |
| `bma_etl_writer` | INSERT/UPDATE/SELECT | USAGE + EXECUTE refresh fn | ETL pipeline |
| `bma_api_reader` | ❌ NO ACCESS | SELECT | FastAPI |
| `postgres` | ALL (superuser) | ALL | Emergency only |

Login users:
- `etl_user` ← `bma_etl_writer` (used by `DATABASE_URL_WRITER`)
- `api_user` ← `bma_api_reader` (used by `DATABASE_URL_READER`)
- `dba_user` ← `bma_dba_admin` (manual ops)

Row-level security (RLS) on `private.patient`:
```sql
CREATE POLICY exclude_erased ON private.patient
  FOR SELECT USING (NOT is_erased);  -- PDPA right-to-be-forgotten
```

### Layer 2: Network

```
Browser → Cloudflare (HTTPS, WAF, DDoS) → tunnel → FastAPI :9002 → DB :5433 (localhost-only)
```

- Postgres binds `127.0.0.1:5433` only — not externally reachable.
- API behind Cloudflare tunnel — public DNS but mTLS to origin.
- pg_hba.conf: `hostssl` rules require SSL/TLS; non-SSL only from `127.0.0.1`.

### Layer 3: Connection-Level

| Auth | Protocol | Where |
|------|----------|-------|
| SCRAM-SHA-256 | TCP+SSL | All non-postgres users |
| `trust` (uid 0 only) | local socket | postgres superuser inside container |
| X-API-Key header | HTTPS | All `/api/v2/*` |
| Username + Password + CSRF | HTTPS | `/admin/*` |

Encrypted on the wire (`ssl=on`, `ssl_cert_file`, `ssl_key_file` configured).
Self-signed cert in dev → swap for proper CA-issued cert in production.

### Layer 4: Application — k-anonymity in MVs

Every `public.mv_*` enforces `HAVING COUNT(DISTINCT patient_id) >= 5`.
Frontend can never see cells with fewer than 5 patients.

Example:
```sql
CREATE MATERIALIZED VIEW public.mv_disease_district AS
SELECT district_code, source_code, disease_key, persons_at_risk
FROM ...
GROUP BY district_code, source_code, disease_key
HAVING COUNT(DISTINCT patient_id) >= 5;
```

### Layer 5: PDPA / Privacy

- IDCARD never stored raw → SHA-256 hash in `private.patient.idcard_hash`
- `private.erasure_request` table for right-to-be-forgotten
- `is_erased` flag + RLS policy hides erased rows from non-DBA queries
- 13-byte ID hash collision is negligible (2^104 namespace)

### Layer 6: Audit + Logging

PostgreSQL native logging (pgaudit unavailable in alpine image):
- `log_statement = 'mod'` — every INSERT/UPDATE/DELETE/DDL logged
- `log_connections = on` — every login attempt
- `log_disconnections = on` — session termination
- `log_min_duration_statement = 1000` — slow queries >1s
- `log_line_prefix = '%t [%p] %u@%d '` — timestamp + pid + user + db

Application-level:
- `private.audit_log` (action/actor/target/details JSONB)
- `private.import_batch` (every CSV upload)
- `private.erasure_request` (PDPA requests)
- `public.mv_refresh_log` (every MV refresh)

### Layer 7: WAL Archiving (Point-in-Time Recovery)

```
archive_mode = on
archive_command = 'cp %p /var/lib/postgresql/wal_archive/%f'
wal_level = replica
```

Volume `pgwal` mounted, archive writes succeed. **Production todo**: rsync this
volume to encrypted offsite (S3 with SSE, or NAS with LUKS).

## ✅ Closed Gaps (this session)

1. **DATABASE_URL split** — `DATABASE_URL_READER` (api_user) vs `DATABASE_URL_WRITER` (etl_user)
2. **Strong passwords** — 32-char base64 random for api_user/etl_user/dba_user
3. **ADMIN_PASSWORD rotated** — 24-char random
4. **Native logging** — log_statement=mod equivalent of pgaudit
5. **pg_hba.conf** — SCRAM-SHA-256 only, IP-restricted, hostssl required
6. **SSL/TLS** — server cert + key, sslmode=require enforced
7. **WAL archiving** — local archive volume + archive_mode=on

## ⏳ Open Gaps (require external infra)

### Gap 5: Cloudflare Access for `/admin/*`
**Status**: not configured
**How**: in Cloudflare dashboard → Zero Trust → Access → Add Application
- Application path: `api-health.splash-wonderland.com/admin/*`
- Identity provider: Google Workspace / SSO of choice
- Policies: allow specific email domains
- Effect: `/admin/*` requires Cloudflare login before reaching FastAPI

### Gap 6: Encrypt Docker volume
**Status**: not encrypted (Docker Desktop default)
**How (macOS dev)**: enable FileVault for the host disk
**How (Linux prod)**: LUKS-encrypted volume mounted under Docker root

### Gap 10: Penetration test (OWASP Top 10)
**Status**: not performed
**Scope**:
- A01 Broken Access Control — verify api_user really can't reach private
- A02 Cryptographic — TLS config, cert validation
- A03 Injection — parameterized queries (already use psycopg2 %s)
- A04 Insecure Design — review auth flow
- A05 Misconfiguration — review postgresql.conf, .env perms
- A07 Auth/Session — bcrypt admin password, secure cookies
- A08 Software integrity — pip install verification
- A09 Logging/Monitoring — verify Layer 6 captures attacks
- A10 SSRF — review any URL fetches

### Gap 11: Privacy Impact Assessment (PIA) per PDPA
**Status**: not performed
**Required**:
- Data inventory (what we collect, why, retention period)
- Lawful basis (consent? public interest? legitimate interest?)
- Data subject rights flow (access / erasure / rectification)
- Cross-border transfer assessment (Cloudflare US/EU egress)
- Risk register + mitigation
- Sign-off by DPO

### Gap 12: Incident Response Plan
**Status**: ad-hoc
**Required**:
- Roles & responsibilities (who calls who)
- Severity classification (P0..P3)
- Detection: alerting on logs (slow queries, failed logins, RLS violations)
- Containment: how to isolate compromised infra
- Eradication + Recovery
- Post-incident review template
- 72-hour PDPA breach notification timeline

## Operational Runbook

### Rotating credentials
```bash
NEW_PWD=$(openssl rand -base64 32)
docker exec bma-health-db psql -U postgres -d bma_health \
  -c "ALTER USER api_user PASSWORD '$NEW_PWD'"
# Update DATABASE_URL_READER in .env, restart FastAPI
```

### Granting access to a new analyst
```sql
CREATE USER analyst_jane PASSWORD '<random>' IN ROLE bma_api_reader;
-- They can now connect with DATABASE_URL=postgresql://analyst_jane:.../bma_health
-- and SELECT public.* only.
```

### Emergency revoke
```sql
ALTER USER api_user PASSWORD 'expired';   -- forces all FastAPI conns to fail
```

### Reading audit logs
```bash
docker logs bma-health-db --since 1h 2>&1 | grep -E "FATAL|ERROR|connection"
docker exec bma-health-db psql -U postgres -d bma_health \
  -c "SELECT * FROM private.audit_log ORDER BY occurred_at DESC LIMIT 20"
```

## References
- PostgreSQL: <https://www.postgresql.org/docs/current/auth-pg-hba-conf.html>
- pgaudit: <https://www.pgaudit.org/>
- PDPA Thailand: <https://www.pdpc.or.th/>
- OWASP Top 10: <https://owasp.org/Top10/>
