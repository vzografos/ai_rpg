# LXC on Proxmox — 1-page runbook (M6.4)

Deploy the ai_rpg web app as a hardened Docker container inside a Proxmox LXC
container. No Python, no node, no build tooling on the host (constraint C1).

## 1. Provision (once)

- Proxmox → Create CT: Debian 12 (bookworm), 2 CPU / 2 GiB / 16 GiB disk.
  Bind-mount a storage dir for backups if you have one.
- Inside the CT:

  ```sh
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker $USER   # then log out/in
  ```

## 2. Ship the image (once per release)

On your build machine (anywhere with Docker and this repo):

```sh
make build                          # builds ai-rpg:dev
docker save ai-rpg:dev | gzip > ai-rpg-dev.tar.gz
scp ai-rpg-dev.tar.gz <ct-host>:/tmp/
```

On the CT:

```sh
docker load < /tmp/ai-rpg-dev.tar.gz
```

## 3. Configure (once)

On the CT, next to wherever you keep app config (e.g. `/srv/ai-rpg`):

```sh
mkdir -p /srv/ai-rpg/docker && cd /srv/ai-rpg
cp <repo>/docker/compose.yml docker/compose.yml
cp <repo>/.env.example .env && $EDITOR .env
```

Keep the compose file under `docker/`: its relative paths are part of the
contract (`env_file: ../.env` resolves to `/srv/ai-rpg/.env`, and the build
context `..` to `/srv/ai-rpg`). The `prod` service runs the image loaded above,
so do not pass `--build` on the CT.

`.env` — fill in the model endpoint (any OpenAI-compatible server):

```sh
LLM_BASE_URL=http://<host-or-sibling>:8080/v1
LLM_API_KEY=...
LLM_MODEL=...
LLM_TIMEOUT=120
```

Local model option: start the sibling with `--profile ollama` and pull the
weights once
(`docker compose -f docker/compose.yml --profile ollama exec ollama ollama pull llama3.1:8b`),
or point `LLM_BASE_URL` at a vLLM/Ollama on another box (that is what
production does).

## 4. Run

```sh
docker compose -f docker/compose.yml --profile prod up -d --no-build prod
curl -s localhost:8080/api/status | head -c 200   # expect: milestone + progress
```

The `prod` service is hardened: non-root (uid 10001), read-only rootfs,
`/tmp` as tmpfs, `no-new-privileges`, no bind mounts, and the SQLite DB on the
`rpg-data` named volume (the only state).

## 5. Operate

```sh
docker compose -f docker/compose.yml --profile prod logs -f prod     # follow
docker compose -f docker/compose.yml --profile prod restart prod     # restart
# after loading a new image tarball (replaces the ai-rpg:dev tag):
docker compose -f docker/compose.yml --profile prod up -d --no-build --force-recreate prod
```

Backup = the data volume:

```sh
docker run --rm -v ai-rpg_rpg-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/rpg-data-$(date +%F).tar.gz -C /data .
```

Restore: `docker compose -f docker/compose.yml --profile prod down`, replace
the volume contents, then `docker compose -f docker/compose.yml --profile prod
up -d --no-build prod`.
Wipe-and-restart: `docker compose -f docker/compose.yml --profile prod down -v`
then the same `up -d --no-build prod` (loses the world).

## 6. Verify after any change

1. `curl -s localhost:8080/api/status` → JSON with `"milestone"` and `progress`.
2. Open `http://<ct>:8080/` in a browser → the single-page app loads.
3. Play one turn; reload; the session resumes (proves the volume works).
