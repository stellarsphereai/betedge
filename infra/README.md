# BetEdge NY — Lightsail deployment

Single-box, single-tenant deploy on AWS Lightsail. Reachable only via
Tailscale (no public ingress except SSH).

## One-time setup

### 1. Provision Lightsail

In the AWS Lightsail console (us-east-1):

| Setting | Value |
|---|---|
| Platform | Linux/Unix |
| Blueprint | OS Only → **Ubuntu 22.04 LTS** |
| Instance plan | **$10 / month** (1 vCPU, 2 GB RAM, 60 GB SSD, 3 TB transfer) |
| Hostname | `betedge` |
| Static IP | Yes (free if attached) |
| Automatic snapshots | Daily at 04:00 UTC |

After it boots, open the Lightsail networking tab and confirm SSH (port 22)
is open. Everything else stays closed — Tailscale handles the rest.

### 2. Generate a Tailscale auth key

Visit <https://login.tailscale.com/admin/settings/keys> and click **Generate
auth key**. Settings:

- Reusable: off
- Ephemeral: off
- Tags: leave blank
- Expiration: 90 days

Copy the key (starts with `tskey-auth-...`). You won't see it again.

### 3. Run the setup script on the box

SSH in (`ssh ubuntu@<your-lightsail-static-ip>`), then:

```bash
curl -fsSL https://raw.githubusercontent.com/stellarsphereai/betedge/main/infra/setup.sh -o setup.sh
chmod +x setup.sh
TAILSCALE_AUTH_KEY='tskey-auth-...' ./setup.sh
```

The script will:

1. Install Python 3.13, Node 20, Caddy, Tailscale, sqlite3, ufw
2. Join the box to your tailnet (hostname `betedge`)
3. Generate a GitHub deploy key, **print it, and exit**
4. You add the key as a read-only deploy key at
   <https://github.com/stellarsphereai/betedge/settings/keys/new>
5. Re-run the same command — it picks up where it left off, clones the repo,
   builds, sets up systemd + Caddy, and locks down the firewall

### 4. Migrate secrets and current DB from your laptop

```bash
# from your laptop, inside the betedge-ny directory
scp .env             ubuntu@<lightsail-ip>:/tmp/.env
scp backend/betedge.db ubuntu@<lightsail-ip>:/tmp/betedge.db
```

Then on the box:

```bash
sudo install -o betedge -g betedge -m 600 /tmp/.env       /opt/betedge/backend/.env
sudo install -o betedge -g betedge -m 644 /tmp/betedge.db /opt/betedge/backend/betedge.db
sudo systemctl restart betedge.service
```

### 5. Verify

```bash
# on the box
curl -s http://127.0.0.1:8002/quota
systemctl status betedge.service caddy.service
sudo journalctl -u betedge -n 50 --no-pager
```

From any device on your tailnet, open `http://betedge/` (or
`http://<tailscale-ip>/`). Dashboard should load.

## Day-to-day

### Deploy a new version

From your laptop:

```bash
git -C ~/betedge-ny push origin main
```

On the box:

```bash
sudo /opt/betedge/infra/deploy.sh
```

(Or set up a cron / GitHub Action to trigger this — left as future work.)

### Logs

```bash
sudo journalctl -u betedge -f          # backend live
sudo journalctl -u caddy -f            # caddy live
sudo tail -f /var/log/caddy/access.log # http access log
```

### Backups

Lightsail snapshots run nightly at 04:00 UTC and retain ~7 days. SQLite is
small enough that the snapshot covers it. If you want offsite backups too,
add to crontab:

```cron
0 5 * * *  aws s3 cp /opt/betedge/backend/betedge.db s3://your-bucket/betedge/$(date +\%F).db
```

(Configure AWS CLI on the box with an IAM key that has `s3:PutObject` on the
target bucket only.)

### Scheduler

The 23:55 NY cron (closing-line sweep + P&L + self-eval) runs in-process
inside the FastAPI app. It auto-starts because `SCHEDULER_ENABLED=true` in
`.env`. Confirm with:

```bash
curl -s http://127.0.0.1:8002/scheduler/status | python3 -m json.tool
```

## Why Tailscale over public HTTPS

A raw Lightsail IP can't get a Let's Encrypt cert (no domain). Three
alternatives existed: HTTP + Basic auth (cleartext over the public
internet), self-signed cert (browser warnings every load), or Tailscale
(no public ingress, real WireGuard encryption, 0 cost). Tailscale won.

If you ever want it on a real domain with HTTPS, point a CNAME at the
tailnet hostname (`betedge.your-tailnet.ts.net`) and enable Tailscale HTTPS
— Caddy will fetch a cert via Tailscale's ACME proxy. Until then: tailnet
only.
