# Hosting the API on Vultr

One Vultr instance runs the API behind Caddy (automatic HTTPS). The database stays on Tiger Cloud. GitHub Actions deploys every push to `main` that touches the API, once a teammate approves it, so after the one-time setup below nobody needs Docker, SSH keys or Vultr access on their own laptop to ship a change.

```text
push to main ─► GitHub Actions: tests ─► build image ─► push to GHCR ─► approval ─► ssh deploy@server
                                                                             │
                Vultr instance ◄─────────────────────────────────────────────┘
                  sojourner-deploy: pull image, start it, health check, roll back if unhealthy
                  Caddy :443 ─► api :8000 ─► Tiger Cloud
```

## Files

| File | What it is |
| --- | --- |
| `cloud-init.yaml` | Pasted into Vultr when creating the instance. Installs Docker, makes SSH key-only, creates the `deploy` user, the secrets file and the deploy script |
| `docker-compose.yml` | API + Caddy. Copied to the server on every deploy |
| `Caddyfile` | HTTPS and the 50 MB upload cap. Copied to the server on every deploy |
| `known_hosts` | The server's SSH host key, pinned. CI refuses to deploy to a server that presents any other key. See step 7 |
| `../../../.github/workflows/deploy-cloud.yml` | The CI pipeline |

## One-time setup

Takes about 30 minutes. You need: a Vultr account, admin on the GitHub repo (for the `production` environment and its secret), a domain name you can add a DNS record to, and the Tiger Cloud admin URL.

### 1. Make the CI deploy key

On any machine:

```sh
ssh-keygen -t ed25519 -N '' -C sojourner-ci -f sojourner-ci
```

This makes `sojourner-ci` (private) and `sojourner-ci.pub` (public). The private half goes into GitHub in step 7, then you delete both files.

### 2. Create the firewall group

Vultr console → Network → Firewall → Add Firewall Group. Inbound IPv4 rules (add the same for IPv6):

| Protocol | Port | Source | Why |
| --- | --- | --- | --- |
| TCP | 443 | Anywhere | HTTPS |
| TCP | 80 | Anywhere | Let's Encrypt certificate checks; Caddy redirects the rest to HTTPS |
| TCP | 22 | Anywhere | SSH. GitHub's runners use changing IP ranges, so SSH can't be limited to team IPs. It accepts keys only, and the CI key can only run the deploy script |

### 3. Create the instance

Vultr console → Deploy:

- **Type:** Cloud Compute, shared CPU. The smallest plan with at least 1 GB RAM is enough.
- **Location:** close to the team, or close to your Tiger Cloud region.
- **Image:** Ubuntu 24.04 LTS x64.
- **SSH Keys:** add your own personal public key. This is how admins log in as `root`; passwords are turned off.
- **Firewall Group:** the one from step 2.
- **Additional features → Cloud-Init User-Data:** paste all of `cloud-init.yaml`, after replacing `REPLACE_WITH_CI_PUBLIC_KEY` with the contents of `sojourner-ci.pub` (one line, starting `ssh-ed25519`). Edit the pasted copy; don't commit the change.

Note the instance's IPv4 address once it's running.

### 4. Point a domain at it

Add a DNS `A` record, e.g. `api.yourdomain.com` → the instance IP. Caddy requests the HTTPS certificate on the first deploy, which only works once this record resolves.

### 5. Fill in the secrets on the server

```sh
ssh root@<instance IP>
cloud-init status --wait          # wait for setup to finish; should print "status: done"
nano /etc/sojourner/cloud.env
```

| Key | Value |
| --- | --- |
| `DATABASE_URL_APP` | `postgres://sojourner_app:<password>@<host>:<port>/tsdb?sslmode=require` |
| `DATABASE_URL_DASHBOARD` | `postgres://demo_dashboard:<password>@<host>:<port>/tsdb?sslmode=require` (role made in step 6) |
| `TOKEN_PEPPER` | `openssl rand -hex 24`. Also save it in your password manager: `create_token.py` needs the same value |
| `ALLOWED_ORIGINS` | The dashboard's origin, e.g. `https://sojourner.example.com`. Comma-separate several |
| `API_DOMAIN` | The domain from step 4, e.g. `api.yourdomain.com` |

The deploy script refuses to run while any of these is empty.

While you're logged in, note the server's host key fingerprint for step 7:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

### 6. Prepare the Tiger Cloud database

This assumes the base schema is already applied and `sojourner_app` has a login and password (`backend/db/README.md`, setup steps 5 and 6); `DATABASE_URL_APP` uses that password. Then, from `backend/cloud` on your laptop, with the admin URL in `TIMESCALE_SERVICE_URL`:

```sh
psql "$TIMESCALE_SERVICE_URL" -f sql/cloud_schema.sql                     # API tables; re-runnable
psql "$TIMESCALE_SERVICE_URL" -c 'CREATE ROLE demo_dashboard LOGIN IN ROLE demo_reader'
psql "$TIMESCALE_SERVICE_URL" -c '\password demo_dashboard'              # use this in DATABASE_URL_DASHBOARD
```

If your Tiger Cloud plan supports IP allow-lists, allow the instance's IP (and your own, for admin work).

### 7. Add the GitHub settings

The CI SSH key is an **environment secret**, not a repository secret. A repository secret can be read by a workflow on any branch, so anyone with write access could push a branch whose workflow prints it. An environment secret is only released to a job that names the environment and passes its rules.

**Create the environment.** Repo → Settings → Environments → New environment, named `production`:

- **Deployment branches and tags:** "Selected branches and tags", add `main` only. A workflow run from any other branch can't enter the environment, so it never sees the secret.
- **Required reviewers:** add at least one teammate. Every deploy then waits in the Actions tab until a reviewer approves it. Tick "Prevent self-review" if you want a second person on every deploy. With a four-person team, expect to ping someone.

**Environment secrets** (on the `production` environment page → Add environment secret):

| Name | Value |
| --- | --- |
| `VULTR_SSH_KEY` | The whole contents of the private key file `sojourner-ci`, including the `BEGIN` and `END` lines |

**Pin the server's host key in the repo.** The host key is not stored in GitHub. It is public, not a credential, so it's committed as `backend/cloud/vultr/known_hosts`, one line in the form `<instance IP> ssh-ed25519 <key>`. Anyone can read exactly what CI trusts, and changing it goes through PR review like any other file. The deploy step copies this file and connects with `StrictHostKeyChecking=yes`, so a server presenting a different key is refused before anything is sent. From the repo root:

```sh
ssh-keyscan -t ed25519 <instance IP> 2>/dev/null | grep '^<instance IP> ' > backend/cloud/vultr/known_hosts
ssh-keygen -lf backend/cloud/vultr/known_hosts
```

The fingerprint printed must match the one you noted in step 5. This is what stops CI from deploying to an impostor, so compare it character by character. Commit the file on a branch and merge it by PR.

The line is keyed by IP, so `VULTR_HOST` below must be that same IP, not a hostname. A hostname or a different IP fails the deploy with `Host key verification failed`.

**Repository variables** (Repo → Settings → Secrets and variables → Actions → Variables tab). These are not secret and must stay at repository level: the workflow's job-level check for `VULTR_HOST` runs before the job enters the environment, so it can't see environment variables.

| Name | Value |
| --- | --- |
| `VULTR_HOST` | The instance IP, exactly as it appears at the start of `known_hosts`. The deploy job is skipped while this is unset |
| `VULTR_API_DOMAIN` | The domain from step 4. Optional: enables a public `/healthz` check after each deploy |

If `VULTR_SSH_KEY` was already added as a repository secret, delete it from the Secrets tab after adding it to the environment. Nothing reads a `VULTR_KNOWN_HOSTS` secret any more. If one exists from an earlier setup, at repository or environment level, delete it so nobody mistakes it for the pin.

**Protect `main`** (repo owner only): Settings → Branches → add a rule for `main` requiring a pull request with one approval. The environment trusts whatever is on `main`, including `deploy-cloud.yml` itself, so this is what stops one person from changing the workflow and deploying it alone. Keep write access to people you trust.

Then delete `sojourner-ci` and `sojourner-ci.pub` from your machine.

### 8. First deploy

Repo → Actions → deploy-cloud → Run workflow (on `main`). Approve the deploy job when it waits (the run → Review deployments). When it's green:

```sh
curl https://api.yourdomain.com/healthz        # {"ok":true}
```

### 9. Make a device token

From `backend/cloud` on your laptop, with the Tiger Cloud admin URL and the same `TOKEN_PEPPER` as the server:

```sh
DATABASE_URL_ADMIN="$TIMESCALE_SERVICE_URL" TOKEN_PEPPER=<the server's value> \
  python scripts/create_token.py create --name my-laptop \
  --scopes profiles:write sessions:write aggregates:read
```

## Day to day

- **Deploying:** merge to `main`. If the change touches `backend/cloud/`, `backend/db/tiger_store.py` or `requirements.txt`, GitHub Actions tests and builds it, then the deploy job waits for a `production` reviewer to approve it (Actions → the run → Review deployments). Anything else on `main` doesn't trigger a deploy. To deploy by hand: Actions → deploy-cloud → Run workflow, on `main`.
- **A deploy that fails its health check** is rolled back to the previous release automatically, and the workflow run goes red with the API's last log lines.
- **Rolling back by hand:** revert the bad commit on `main`, which deploys the reverted code. Or open an earlier green deploy-cloud run and choose "Re-run all jobs", which rebuilds and deploys that run's commit, after approval. A re-run uses the workflow file from that commit, so only runs from after the `production` environment and the committed `known_hosts` were added (run 4 onward) can deploy. Earlier runs can't reach the SSH key.
- **Logs and status**, as root on the server:

  ```sh
  sojourner-compose ps
  sojourner-compose logs -f api
  sojourner-compose logs caddy        # certificate problems show up here
  ```

- **Changing a server secret:** edit `/etc/sojourner/cloud.env`, then run `sojourner-compose up -d`, which recreates the API container with the new values. Re-running the latest deploy-cloud run also works, but needs an approval.
- **OS updates:** Ubuntu installs security updates automatically. Reboot now and then (`reboot`); the containers start again on their own.

## Security notes

- **The CI key can only deploy.** Its `authorized_keys` entry forces `sojourner-deploy`, which accepts one argument: an image from `ghcr.io/taswarmahbub/sojourner-api` pinned by digest. It can't open a shell, forward ports or run other commands. `sudo` is allowed for that one script only.
- **Registry access is short-lived.** The server pulls with the workflow's `GITHUB_TOKEN`, which expires when the job ends. The credentials are stored in a temporary directory for the pull and deleted afterwards. The image can stay private.
- **App secrets stay on the server.** The database URLs and `TOKEN_PEPPER` live only in `/etc/sojourner/cloud.env`. They aren't in the repo, GitHub, the image, or the cloud-init user data. The only credential in GitHub is the CI deploy key, in the `production` environment.
- **The host key pin is reviewed, not hidden.** `known_hosts` is a committed file, so a change to it needs an approved PR into `main`, and anyone can read it. A secret can't be read back, so nobody could check what CI actually trusted.
- **Deploys only come from `main`, with a reviewer's approval.** The CI key lives in the `production` environment, which only releases it to runs from `main` after a required reviewer approves. Workflows on other branches and pull requests, including from forks, can't read it.
- **The database admin URL is not in GitHub at all.** Only the server's env file holds app credentials, and the admin URL stays on admins' laptops.

## Updating the server setup

`cloud-init.yaml` only runs when an instance is created. Changes to the deploy script or SSH settings don't reach an existing server. To apply them, either create a new instance (steps 3 to 8 again; update `VULTR_HOST` and `known_hosts`), or copy the changed file onto the server by hand as root.

If the repository moves to another owner, update `ALLOWED_REPO` in the deploy script, on the server as well as in `cloud-init.yaml`.

## Not done yet

- Running on a real Vultr instance since 22 Nov 2026: deploy-cloud runs 4 to 6 pulled from GHCR, deployed over SSH with the pinned host key, and passed the public `/healthz` check. `/healthz` doesn't touch the database, so a green deploy says nothing about the routes against Tiger Cloud.
- Not yet exercised on the server: automatic rollback after a failed health check. That was tested only in an Ubuntu 24.04 container with a stand-in for Docker, along with the cloud-init schema, the SSH lockdown and image cleanup.
- Phase 2, only if needed: a Vultr Load Balancer in front of two instances (the API is stateless apart from its cache), and Vultr Object Storage to keep uploaded session files for re-ingestion.
- To check before ever moving the database to Vultr: whether Vultr Managed Databases for PostgreSQL offers the TimescaleDB extension.
