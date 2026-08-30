# Docker Guide

This project can be packaged into a Docker image and run against your existing,
external PostgreSQL database. PostgreSQL is **not** run inside the container —
the app connects to it over the network using the `DB_*` variables in `.env`.

---

## 1. Architecture

```
┌─────────────────────────────────────────────┐
│              Docker container                │
│  ┌─────────────────────────────────────────┐│
│  │  gunicorn (radio_database.wsgi)         ││
│  │  port 8000                              ││
│  └─────────────────────────────────────────┘│
│  volumes: /app/artifacts, /app/logs         │
└────────────────────┬────────────────────────┘
                     │  DB_HOST (external)
                     ▼
        ┌───────────────────────────┐
        │  External PostgreSQL      │
        │  (existing server + DB)   │
        └───────────────────────────┘
```

- **Web server:** gunicorn (`radio_database.wsgi:application`) on port `8000`.
- **Static files:** built into the image (Tailwind CSS is compiled, then
  `collectstatic` copies assets into `STATIC_ROOT`) and served by whitenoise.
- **Media/files:** `artifacts/` and `logs/` are mounted as volumes so data
  survives image rebuilds.
- **Database:** external PostgreSQL, configured entirely via environment
  variables.

## 2. Prerequisites

- Docker Engine (and Docker Compose plugin) installed on the build machine.
- The existing PostgreSQL server is reachable from the container's network.
- A `.env` file at the project root (already present in this repo).

## 3. `.env` Configuration

The container reads its configuration from `.env`. The relevant variables:

| Variable               | Purpose                                              | Example                    |
|------------------------|------------------------------------------------------|----------------------------|
| `DB_NAME`              | PostgreSQL database name                             | `radios`                   |
| `DB_USER`              | PostgreSQL user                                      | `radiogod`                 |
| `DB_PASSWORD`          | PostgreSQL password                                  | *(set in `.env`)*          |
| `DB_HOST`              | PostgreSQL host (must be reachable from the container)| `docker-server`           |
| `DB_PORT`              | PostgreSQL port                                      | `5432`                     |
| `DOCKER_SERVER_IP`     | IP of the Docker server (used for tagging/pushing)   | `10.0.0.7`                 |
| `DOCKER_REGISTRY_PORT` | Registry port on the Docker server                   | `5000`                     |
| `DEBUG`                | `true`/`false` (defaults to `true`)                  | `true`                     |
| `ALLOWED_HOSTS`        | Comma-separated hostnames/IPs allowed to connect     | `localhost,127.0.0.1,10.0.0.7` |
| `SECRET_KEY`           | Django secret key (strongly recommended in prod)     | *(optional)*               |
| `ARTIFACTS_STORE_TYPE` | `samba` (network share) or `local`                   | `samba`                    |
| `ARTIFACTS_STORE_HOST` | SMB share host/IP                                    | `10.0.0.9`                 |
| `ARTIFACTS_STORE_FOLDER` | SMB share name                                     | `artifacts`                |
| `ARTIFACTS_USER`       | SMB share username                                   | `radio`                    |
| `ARTIFACTS_PASSWORD`   | SMB share password                                   | *(set in `.env`)*          |

> **Important:** the container resolves `DB_HOST` internally. If `DB_HOST` is a
> hostname like `docker-server`, `docker-compose.yml` maps it to
> `DOCKER_SERVER_IP` via `extra_hosts`. If PostgreSQL lives on a different
> machine, set `DB_HOST` to that machine's IP or use `host.docker.internal`
> (Linux requires `--add-host=host.docker.internal:host-gateway`).

> **Artifacts store:** `artifacts/` (manuals, OET documents, test reports,
> images) is a CIFS named volume that mounts the SMB share
> `//ARTIFACTS_STORE_HOST/ARTIFACTS_STORE_FOLDER` directly inside the Docker
> VM. Docker Desktop for Windows cannot bind-mount a mapped network drive
> (e.g. `Z:`), so the share is mounted via the `local` volume driver instead.
> See §7 for details.

## 4. Build the Image

```bash
docker build -t radio-tracker:latest .
```

The build:
1. Installs Python dependencies from `requirements.txt`.
2. Compiles Tailwind CSS (`python manage.py tailwind build`).
3. Runs `collectstatic`.
4. Installs the Playwright Chromium browser, Google Chrome (x86_64 only),
   and Xvfb for the FCC OET exhibit fallback.

> The image is large (~1.5–2 GB) because it includes Chromium, Chrome, and
> tesseract OCR. If you do not need the FCC OET exhibit fallback, you can
> remove the `playwright install` line, the Google Chrome step, and the
> `tesseract-ocr`/`libgl1`/`libglib2.0-0`/`xvfb`/`dbus-x11` packages from the
> `Dockerfile`.

## 5. Run Locally

### Option A — Docker Compose (recommended)

```bash
docker compose up --build -d
```

- Serves on http://localhost:8000
- Mounts `./artifacts` → `/app/artifacts` and `./logs` → `/app/logs`.
- Automatically applies migrations on startup.

### Option B — Docker run

```bash
docker run --rm \
  --env-file .env \
  -p 8000:8000 \
  -v "$PWD/artifacts:/app/artifacts" \
  -v "$PWD/logs:/app/logs" \
  --add-host docker-server:10.0.0.7 \
  radio-tracker:latest
```

Replace `10.0.0.7` with your PostgreSQL/Docker server IP.

### Verify

Open http://localhost:8000/radios/ (redirects from `/`). Data should load from
the existing PostgreSQL database and the Tailwind CSS should render.

## 6. Push to the Local Docker Server

The Docker server IP is read from `.env` (`DOCKER_SERVER_IP`).

```bash
./deploy.sh
```

This builds and pushes the image tagged as:

```
<DOCKER_SERVER_IP>:<DOCKER_REGISTRY_PORT>/radio-tracker:latest
# e.g. 10.0.0.7:5000/radio-tracker:latest
```

`deploy.sh` builds a single-architecture `linux/amd64` image (your server's
architecture) using `docker build --platform`, even though your Mac is arm64.
Docker Desktop handles the QEMU cross-compilation automatically.

Manual equivalent:

```bash
docker build --platform linux/amd64 -t 10.0.0.7:5000/radio-tracker:latest .
docker push 10.0.0.7:5000/radio-tracker:latest
```

> **Why build and push separately?** The `docker push` step runs through the
> Docker daemon, which respects the `insecure-registries` setting. Using
> `docker buildx build --push` with a custom builder bypasses that config and
> fails against an HTTP registry.

### Insecure (HTTP) registry

If your local registry serves over plain HTTP (no TLS), Docker must be told the
registry is insecure. On the **Docker server** edit
`/etc/docker/daemon.json`:

```json
{
  "insecure-registries": ["10.0.0.7:5000"]
}
```

Then restart Docker (`sudo systemctl restart docker`). On macOS, add the
registry under **Docker Desktop → Settings → Docker Engine**.

### No registry running?

If the server has no registry container, use `docker save`/`docker load` (see
the commented fallback at the bottom of `deploy.sh`).

## 7. Run on the Docker Server

First, make sure the server's `.env` has `DB_HOST` pointing at the PostgreSQL
server. If PostgreSQL runs on the same machine as Docker, use:

```ini
DB_HOST=host.docker.internal
```

Otherwise set it to the PostgreSQL machine's IP.

### Linux / macOS

```bash
docker run -d \
  --name radio-tracker \
  --restart unless-stopped \
  --env-file .env \
  -p 8000:8000 \
  -v "$PWD/artifacts:/app/artifacts" \
  -v "$PWD/logs:/app/logs" \
  127.0.0.1:5000/radio-tracker:latest
```

### Windows (PowerShell)

```powershell
# Create the mount folders once (copy artifacts/ from the Mac if you want
# the manuals / test-reports / images served):
New-Item -ItemType Directory -Force artifacts, logs | Out-Null

docker run -d `
  --name radio-tracker `
  --restart unless-stopped `
  --env-file .env `
  -p 8000:8000 `
  -v "C:/Users/kaver/apps/radio-tracker/artifacts:/app/artifacts" `
  -v "C:/Users/kaver/apps/radio-tracker/logs:/app/logs" `
  127.0.0.1:5000/radio-tracker:latest
```

Adjust the `C:/Users/kaver/...` paths to your actual folder. If you keep
`DB_HOST=docker-server` in `.env`, add `--add-host "docker-server:10.0.0.7"`
(replacing the IP with the PostgreSQL host's IP).

### Verify

```bash
docker logs -f radio-tracker
```

The entrypoint runs `migrate` on startup; once gunicorn prints it is listening
on `:8000`, open http://localhost:8000/radios/ (on the server) or
http://<server-ip>:8000/radios/ from another machine.

Or, copy `docker-compose.prod.yml` and `.env` to the server and run:

```bash
docker compose -f docker-compose.prod.yml up -d
```

The prod compose file is image-only (no `build`), so the server never needs
the source code.

### Updating to a new build

After `./deploy.sh` pushes a new `latest` image, the server's local copy is
**not** refreshed automatically. Pull and recreate:

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --force-recreate
```

### Artifacts share (CIFS named volume)

The container mounts `artifacts_share` — a CIFS volume that connects to
`//ARTIFACTS_STORE_HOST/ARTIFACTS_STORE_FOLDER` (from `.env`) directly inside
the Docker VM. This is required because Docker Desktop for Windows cannot
bind-mount a mapped network drive (`Z:`).

```powershell
# .env needs ARTIFACTS_STORE_HOST / _FOLDER / _USER / _PASSWORD set
docker compose -f docker-compose.prod.yml up -d --force-recreate
docker exec radio-tracker sh -c 'ls /app/artifacts'
```

> If the CIFS volume fails to mount (the Docker VM must support `mount.cifs`),
> fall back to mounting the share in WSL2 and bind-mounting its path, or mirror
> the share to a local folder with robocopy and bind-mount that instead.

On macOS (local dev), mount the share at `./artifacts` with:

```bash
./mount-artifacts.sh            # mount at ./artifacts
./mount-artifacts.sh --unmount  # unmount
```

## 8. Everyday Operations

| Action                     | Command                                                          |
|----------------------------|------------------------------------------------------------------|
| View logs                  | `docker logs -f radio-tracker`                                    |
| Open a shell in container  | `docker exec -it radio-tracker /bin/sh`                           |
| Run a Django command       | `docker exec -it radio-tracker python manage.py <command>`        |
| Create an admin user       | `docker exec -it radio-tracker python manage.py createsuperuser`  |
| Run migrations manually    | `docker exec -it radio-tracker python manage.py migrate`          |
| Restart                    | `docker restart radio-tracker`                                    |
| Stop / remove              | `docker rm -f radio-tracker`                                      |

## 9. How It Works (file map)

| File                    | Purpose                                                        |
|-------------------------|----------------------------------------------------------------|
| `Dockerfile`            | Builds the image (deps → Tailwind → static → Playwright).       |
| `docker-entrypoint.sh`  | Runs migrations + collectstatic, then starts gunicorn.          |
| `docker-compose.yml`    | Local run with volumes, env, port, and `extra_hosts` mapping.   |
| `docker-compose.prod.yml` | Server run — image-only; bind-mounts `./artifacts` (share). |
| `mount-artifacts.sh`    | Mounts/unmounts the artifacts SMB share on macOS.              |
| `deploy.sh`             | Builds, tags, and pushes the image to the Docker server.        |
| `.dockerignore`         | Excludes secrets, venv, data, and logs from the build context.  |

## 10. Troubleshooting

- **`no matching manifest for linux/amd64/v4 ... no match for platform`** —
  the image was built for a different CPU architecture (e.g. arm64). Rebuild
  with `./deploy.sh` (which targets `linux/amd64`), then pull again on the
  server. If you built a multi-arch image, pull explicitly with
  `docker pull --platform linux/amd64 <image>`.
- **`OperationalError: could not translate host name "docker-server"`** — the
  container cannot resolve `DB_HOST`. Use an IP for `DB_HOST`, or ensure
  `extra_hosts`/`--add-host` maps the hostname to the correct IP.
- **`password authentication failed`** — confirm `DB_PASSWORD` in `.env` and
  that the Postgres server allows remote connections (`pg_hba.conf`).
- **403 / `DisallowedHost`** — add the server IP/hostname to `ALLOWED_HOSTS`
  in `.env` (comma-separated).
- **`https: server gave HTTP response`** during push — the registry is HTTP;
  configure `insecure-registries` (see §6).
- **Static/CSS missing** — rebuild so `tailwind build` + `collectstatic` run,
  or run `docker exec -it radio-tracker python manage.py collectstatic --noinput`.
- **Playwright / OCR failures** — ensure the image was built with the
  `playwright install chromium --with-deps` step intact. The FCC OET browser
  fallback runs **headed** under Xvfb when `FCC_PLAYWRIGHT_HEADLESS=0` (the
  prod compose file sets this). If FCC itself returns 503, the fallback still
  skips records — that's an FCC outage, not a container problem.

## 11. Security Notes

- **`DEBUG=true`** is convenient for a home/lab server (it also serves media
  files from `artifacts/`). For any internet-exposed deployment, set
  `DEBUG=false` and serve media through a real web server or object storage.
- **`SECRET_KEY`** falls back to an insecure development key if unset. Set a
  unique value in `.env` before exposing the app beyond your LAN.
- **`.env` is never baked into the image** (it is excluded by `.dockerignore`).
  Keep it out of source control.
