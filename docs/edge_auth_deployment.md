# Edge Authentication & Zero-Trust Deployment Guide

This guide documents the production deployment of `jobs.archnet.lol` (with `jobs.aslan.net` supported as migration/secondary host) using Cloudflare Tunnel, Cloudflare Access, and internal Gateway authorization under the **Free-Only** tier constraints.

---

## 1. Free-Only Cost and Eligibility Prerequisites

The autonomous pipeline relies exclusively on free, non-subscription zero-trust services:
- **Cloudflare Free Plan**: Domain management and DNSSEC ($0/month).
- **Cloudflare Zero Trust (Access)**: Up to 50 users on the Free Plan ($0/month).
- **Cloudflare Tunnel (`cloudflared`)**: Unlimited outbound-only tunnels on the Free Plan ($0/month).
- **Identity Providers (Google OAuth / GitHub OAuth)**: Free developer applications with upstream MFA ($0/month).
- **ntfy**: Self-hosted notification server on existing Hetzner infrastructure ($0/month).

> ⚠️ **Strict Constraint**: Never upgrade to paid Cloudflare plans, buy notification subscriptions, or weaken security policies to bypass any limits. If an external service constraint cannot be satisfied under the free tier, stop and report the gate blocked.

---

## 2. Complete Zone Export & DNS Baseline

Before adding or moving any domain to Cloudflare:
1. **Never rely on Cloudflare automated record discovery**: Automated discovery scans only common prefixes and routinely misses critical mail (MX), verification (TXT/CAA), and non-standard DNS records.
2. **Export Full BIND Zone**:
   - Access domain registrar or current DNS host (e.g., current authoritative nameserver).
   - Export the complete standard BIND zone file (`aslan.net.zone`).
   - Audit and preserve:
     - **Mail Records**: All `MX`, `TXT` (`v=spf1 ...`), `_dmarc`, and DKIM records (`*._domainkey`).
     - **Security & Verifications**: `CAA` records (restricting certificate authorities), registrar/search engine verification `TXT` records.
     - **Delegations & Subdomains**: Existing hosts (e.g., `archnet.lol` mappings, VPN endpoints).
3. **Import into Cloudflare**:
   - Import the BIND zone file directly via Cloudflare Dashboard: **DNS** → **Records** → **Import and Export**.
   - Verify every record against the baseline export file before delegating nameservers.

---

## 3. Safe DNSSEC Migration Protocol

Migrating a domain with active DNSSEC requires strict sequencing to avoid a global DNS resolution blackout:

```
[Phase A: Registrar]      [Phase B: Cutover]         [Phase C: Cloudflare]      [Phase D: Registrar]
Remove old DS Record  ->  Wait DS TTL Expiration  ->  Update Nameservers    ->  Enable DNSSEC &
at domain registrar       (typically 24-48 hrs)       to Cloudflare             Publish new DS Record
```

### Step 1: Pre-Migration DS Deletion
1. Check existing DS record:
   ```bash
   dig DS aslan.net +trace
   ```
2. Log in to the domain registrar.
3. Remove/delete the existing DS record.
4. **Wait for registrar DS TTL to fully expire** (typically 24 to 48 hours). Do NOT switch nameservers while an obsolete DS record is published, or validating resolvers will fail to resolve `aslan.net` (SERVFAIL).

### Step 2: Nameserver Cutover
1. Once `dig DS aslan.net` returns `NXDOMAIN` or `NODATA`, update authoritative nameservers at the registrar to Cloudflare's assigned nameservers.
2. Wait for NS propagation:
   ```bash
   dig NS aslan.net +trace
   ```

### Step 3: Post-Cutover DNSSEC Activation
1. In Cloudflare Dashboard: **DNS** → **Settings** → **Enable DNSSEC**.
2. Cloudflare generates new DS parameters:
   - **Key Tag** (e.g. `2371`)
   - **Algorithm** (e.g. `13` - ECDSA Curve P-256 with SHA-256)
   - **Digest Type** (e.g. `2` - SHA-256)
   - **Digest** (64-character hex string)
3. Log in to domain registrar and add the new Cloudflare DS record.
4. Verify validation:
   ```bash
   dig DS aslan.net +dnssec
   ```

---

## 4. Cloudflare Tunnel Configuration

Cloudflare Tunnel creates an outbound-only connection from the Hetzner server to Cloudflare's edge. No public ports (`80`, `443`, `8000`, `6080`, `5900`) are opened on the server.

### 1. Tunnel Creation
```bash
cloudflared tunnel create jobs-aslan-net
```

### 2. Tunnel Ingress Configuration (`/etc/cloudflared/config.yml`)
```yaml
tunnel: <TUNNEL_UUID>
credentials-file: /etc/cloudflared/<TUNNEL_UUID>.json

ingress:
  # Canonical dashboard and API endpoint (targets internal gateway)
  - hostname: jobs.archnet.lol
    service: http://gateway:80

  # Offline/mobile notification endpoint (targets ntfy)
  - hostname: notify.jobs.archnet.lol
    service: http://ntfy:80

  # Secondary / migration aliases
  - hostname: jobs.aslan.net
    service: http://gateway:80

  - hostname: notify.jobs.aslan.net
    service: http://ntfy:80

  - service: http_status:404
```

### 3. DNS Routing
```bash
cloudflared tunnel route dns jobs-aslan-net jobs.archnet.lol
cloudflared tunnel route dns jobs-aslan-net notify.jobs.archnet.lol
cloudflared tunnel route dns jobs-aslan-net jobs.aslan.net
cloudflared tunnel route dns jobs-aslan-net notify.jobs.aslan.net
```

---

## 5. Cloudflare Access Application Setup

To protect `jobs.archnet.lol` (and `jobs.aslan.net`) with Zero-Trust authentication:

1. **Create Access Application**:
   - Application Type: **Self-hosted**
   - Application Name: `Job Applier Dashboard`
   - Application Domain: `jobs.archnet.lol` (with additional domain `jobs.aslan.net`)
   - Session Duration: **8 hours**
2. **Identity Providers (IdP)**:
   - Configure **Google OAuth** or **GitHub OAuth**. Both accounts must have hardware or app-based MFA enabled.
   - Do NOT enable Email One-Time PIN (OTP) or open domain registrations.
3. **Access Policy**:
   - Action: **Allow**
   - Rule type: **Include**
   - Selector: **Emails** → Exact operator email (`ervin.popescu10@gmail.com`).
   - Wildcards (`*@domain.com`) are strictly forbidden.
4. **Retrieve Application AUD Tag**:
   - In Cloudflare Zero Trust: **Access** → **Applications** → `Job Applier Dashboard` → **Edit** → **Overview**.
   - Copy the **Application Audience (AUD) Tag** (64-character hex string).
   - Set in `.env`: `CF_ACCESS_AUD=<HEX_STRING>`.
   - Set team domain in `.env`: `CF_ACCESS_TEAM_DOMAIN=aged-sunset-0292.cloudflareaccess.com` (or prefix `aged-sunset-0292`; automatically normalized).

---

## 6. Origin Security Architecture

The server deploys a defense-in-depth architecture:

```
[Cloudflare Edge]
       │
       │ (Outbound-only Tunnel)
       ▼
[cloudflared]
       │
       ▼ (internal-net)
   [gateway] (Caddy:80)
   ├── /browser* ────► [forward_auth: web:8000/api/auth/viewer-gate]
   │                   ├── (HTTP) ──────► [runtime:6080] (noVNC Static)
   │                   └── (WebSocket) ──► [web:8000/browser/websockify]
   │                                       (Strict 5-min server lifetime) ──► [runtime:6080]
   └── / (all other) ─► [web:8000] (FastAPI: JWT verify, exact allowlist, CSRF, Origin)
```

### Security Guarantees:
1. **Strict RS256 Verification**: Only cryptographically signed JWTs from Cloudflare Access (`alg: RS256`) are accepted. Algorithm confusion attacks (`none`, `HS256`) are rejected with `401`.
2. **Exact Allowlist**: Tokens are validated against `CF_ACCESS_ALLOWED_IDENTITIES`. Unlisted accounts are rejected with `403`.
3. **Fail-Closed Startup**: If `CF_ACCESS_ENABLED=true`, FastAPI refuses to start if `CF_ACCESS_AUD`, `CF_ACCESS_TEAM_DOMAIN`, or `CF_ACCESS_ALLOWED_IDENTITIES` is missing or contains wildcards.
4. **Legacy Route Protection**: Requests to `/job-applier` cannot bypass authentication. They require full JWT validation before redirecting (HTTP 308) to canonical root `/`.
5. **Gateway noVNC Authorization**: Caddy validates all `/browser*` requests through `/api/auth/viewer-gate` before proxying.
6. **Server-Side Viewer Lifetime**: The viewer WebSocket connection is terminated server-side at `min(300, token_exp - now)` seconds.
7. **SSE Expiry**: The Server-Sent Events stream emits `event: expired` and cleanly terminates when the Access token expires, prompting client reauthentication.
8. **CSRF & Origin Verification**: Mutating HTTP requests (`POST`, `PUT`, `DELETE`) and WebSocket upgrades enforce exact Origin matching `PUBLIC_ORIGIN` (default: `https://jobs.archnet.lol`). Cross-origin mutations are rejected with `403`.
9. **No Credentialed Cross-Origin CORS**: Cross-origin requests with credentials are denied; origins are strictly canonical.
10. **Minimal Private Health**: `/api/health` returns only minimal status (`{"status": "ok", "service": "job-applier"}`) and exposes no internal configuration.
