# Deployment

Job Applier is distributed as a multi-architecture container image. CI builds and publishes the image; each server owns its runtime configuration and persistent data. No server hostname, address, username, path, or SSH key needs to be committed to the repository.

## Requirements

- Docker Engine 24 or newer
- Docker Compose v2
- An `amd64` or `arm64` Linux server
- A reverse proxy, VPN, or SSH tunnel for remote access

The dashboard contains personal information and persistent browser sessions. It binds to `127.0.0.1` by default and should not be exposed directly to the public internet.

## Deploy a published image

Create a deployment directory and download the generic configuration:

```bash
mkdir -p job-applier && cd job-applier
curl -fsSLO https://raw.githubusercontent.com/ervinpopescu/job-applier/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/ervinpopescu/job-applier/main/.env.example -o .env
```

Edit `.env` and set at least `GOOGLE_API_KEY`. Forks should also change `JOB_APPLIER_IMAGE` to their own GHCR package:

```dotenv
JOB_APPLIER_IMAGE=ghcr.io/owner/job-applier:latest
GOOGLE_API_KEY=replace-me
JOB_APPLIER_BIND_ADDRESS=127.0.0.1
JOB_APPLIER_PORT=8000
```

Start the application:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Open it through an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 user@server
```

Then visit <http://127.0.0.1:8000> locally.

The first startup creates privacy-safe candidate and resume templates in the persistent data volume. Complete the candidate profile in the dashboard. To replace the master resume JSON while preserving container ownership:

```bash
docker compose exec -T app sh -c 'cat > /app/data/master_resume.json' < master_resume.json
```

## Build locally instead of using GHCR

Clone the repository and build the exact source checkout:

```bash
git clone https://github.com/ervinpopescu/job-applier.git
cd job-applier
cp .env.example .env
# Edit .env
docker compose up -d --build
```

The Docker build excludes repository `data/`, `output/`, `.env`, and `.browser_profile` content. Personal local data is therefore never copied into the image.

## Persistent data and backups

Compose creates three named volumes:

- `job-applier-data` — profile, master resume, SQLite database, and tracker data
- `job-applier-output` — generated application packages and PDFs
- `job-applier-browser-profile` — persistent Playwright sessions

Upgrades preserve all three volumes:

```bash
docker compose pull
docker compose up -d --remove-orphans
```

Create an application-level portable backup from the dashboard or with:

```bash
curl -f http://127.0.0.1:8000/api/export -o job-applier-backup.zip
```

Removing the Compose stack with `docker compose down` preserves data. Adding `--volumes` permanently deletes the volumes and must only be used when intentionally resetting the installation.

## Browser automation in containers

The image includes Playwright Chromium and its Linux dependencies. Automated runs fall back to headless mode when no display is available. Interactive platform login windows and desktop Chrome session synchronization require a graphical session and are not available in a normal headless server container. Import an existing application backup/session or place the service behind a private remote desktop environment when interactive login is required.

Chromium receives a 1 GiB shared-memory allocation through Compose. Increase `shm_size` for highly concurrent automation.

## Reverse proxy and authentication

Keep `JOB_APPLIER_BIND_ADDRESS=127.0.0.1` when using Caddy, Nginx, Traefik, Cloudflare Tunnel, or Tailscale. Terminate HTTPS and require authentication at that layer. The application currently does not provide its own multi-user authentication boundary.

Do not proxy the dashboard publicly without access control. It can expose resumes, contact details, generated documents, and authenticated browser state.

## Automatic private-host deployment

The CI/CD workflow supports updating a host either through containerized Docker Compose or through a native systemd user service via an internal runner (such as Actions Runner Controller). Host details are read exclusively from GitHub Secrets and Variables, keeping the repository completely anonymous.

### 1. Host Target Models

- **Systemd User Service (default in CI)**: Runs directly on the server (e.g. `systemctl --user restart job-applier.service`), pulling the latest code, rebuilding the Angular frontend, and verifying `/api/health`.
- **Docker Compose**: Pulls the published container image and updates the Compose service stack.

### 2. Prepare the server

Deploy once using the steps above. The deployment user must:

- own the deployment directory;
- have access to Docker Compose, preferably through rootless Docker;
- be able to pull the GHCR image.

For a private GHCR package, authenticate once on the server using a read-only package token:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u USERNAME --password-stdin
```

### 2. Create a dedicated SSH key

Generate a deployment-only key locally and add its public half to the deployment user's `~/.ssh/authorized_keys`:

```bash
ssh-keygen -t ed25519 -f ./job-applier-deploy -C job-applier-deploy
```

Capture and verify the server host key locally. For a nonstandard SSH port, keep the bracketed host and port produced by `ssh-keyscan`:

```bash
ssh-keyscan -p PORT HOST > known_hosts
ssh-keygen -H -f known_hosts
```

Verify the fingerprint out of band before trusting it. Store the hashed contents of `known_hosts`, not the generated `.old` file.

### 3. Configure GitHub

Create a GitHub Environment named `production`. Add these **Environment secrets**:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | DNS name or address |
| `DEPLOY_USER` | Dedicated deployment user |
| `DEPLOY_PORT` | SSH port |
| `DEPLOY_PATH` | Directory containing `compose.yaml` and `.env` |
| `DEPLOY_SSH_KEY` | Entire private deployment key |
| `DEPLOY_KNOWN_HOSTS` | Verified, preferably hashed, known-hosts entry |

Add this repository **Actions variable** (job-level conditions are evaluated before Environment variables become available):

```text
DEPLOY_ENABLED=true
```

The workflow contains only secret names. It never contains the host, username, port, or deployment path. Do not enable verbose SSH logging because a resolved IP address may not be covered by GitHub's exact-value masking.

The deploy job runs only for pushes to `main`, pulls the immutable `sha-<full commit>` image, restarts the Compose service, and waits for `/api/health` to respond. Protect the `production` Environment with required reviewers if deployments need approval.

## Publishing from a fork

The workflow publishes these GHCR tags:

- `latest` for the default branch;
- `sha-<full commit SHA>` for immutable deployments;
- the Git tag for releases such as `v1.2.0`.

GitHub packages may initially be private. Make the package public for anonymous `docker compose pull`, or document the read-only GHCR login required by your users.
