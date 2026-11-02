# MikroTik Fleet Controller

Web dashboard to monitor and control many MikroTik routers at once, over the
RouterOS API (8728, or API-SSL on 8729) or SSH. Each router picks its own method.

## Features
- Router inventory with groups (stored in `routers.json`, mode 600)
- Live status: online/offline, identity, RouterOS version, board, uptime, CPU, memory, latency
- Run any command on one or many selected routers in parallel; output per router
- Quick actions: interfaces, IPs, routes, DHCP leases, log, export (SSH), backup, update check, reboot
- Update RouterOS: select routers → **Update RouterOS…** to check each against MikroTik's upgrade server, then install updates on just the out-of-date ones (each downloads and reboots). Check first; Install only acts on routers behind the latest version
- Manage users: select routers → **Manage users…** to keep a named set of accounts, create/ensure one account (optionally with an SSH public key), and remove all others. Preview shows every change before you apply it; the login account is always kept and the new account is created before any deletion
- Live interface bandwidth: click **traffic** on any router for per-interface Rx/Tx rates, a 60-sample history chart, and total counters (2/5/10s refresh)
- Backups: **Save backup** creates an unencrypted `.backup` named `<identity>-<date>-<time>` (falls back to the IP if the identity can't be read) on each selected router, downloads it to the server over SFTP (`backups/<router-id>/`), and lists it in the Backups panel for download

## Run
    pip install -r requirements.txt
    FLEET_PASSWORD=choose-a-password uvicorn app:app --host 0.0.0.0 --port 8080

Open http://SERVER:8080 and log in as `admin` / your FLEET_PASSWORD.

Docker:

    docker build -t mtfleet .
    docker run -d -p 8080:8080 -e FLEET_PASSWORD=... -v mtfleet:/data mtfleet

## Run as a service (systemd)

Easiest: from the project directory, run the installer, which sets up the venv,
a service account, the password file, and the systemd unit:

    sudo bash deploy/install-service.sh

Then browse to http://<server>:8080. Manage it with:

    systemctl status mtfleet
    systemctl restart mtfleet
    journalctl -u mtfleet -f        # live logs

Manual setup instead: copy `deploy/mtfleet.env.example` to `/etc/mtfleet.env`
(set FLEET_PASSWORD, then `chmod 600`), copy `deploy/mtfleet.service` to
`/etc/systemd/system/` (edit the paths if yours differ), then
`systemctl daemon-reload && systemctl enable --now mtfleet`.

The password lives in `/etc/mtfleet.env` (mode 600), not in the unit file.

If the service fails with `status=200/CHDIR`, the service account can't traverse
into the app directory (home dirs are often mode 750). Fix it with:

    sudo chmod o+x /home/<youruser>
    sudo systemctl restart mtfleet

The installer now does this automatically.

## Router prep
API:  `/ip service enable api` (or `api-ssl` with a certificate)
SSH:  `/ip service enable ssh` — also needed on API routers for backup downloads (SFTP). Set a custom SSH port in the router's settings if it isn't 22.
Recommended: a dedicated user and restrict the service to your server's IP:

    /user group add name=fleet policy=read,write,api,ssh,reboot,policy,test,sensitive
    /user add name=fleet group=fleet password=...
    /ip service set api address=10.0.0.5/32

## Commands
- **API routers** accept CLI style `/ip address add address=10.0.0.1/24 interface=ether2`
  or API style `/ip/address/print`. Query filters (`where`) aren't supported over API — use SSH for those.
- **SSH routers** accept any RouterOS CLI, including scripts and `where` clauses.

## When a router is set to API vs SSH
Each router is reached by the method you choose when adding it. If a router is set to
**API** but the `api` service is disabled (or your IP isn't allowed), the dashboard now
shows a clear message telling you to enable it or switch the router to SSH, instead of a
raw socket error. **Manage users** and **backups** both upload/import over SSH, so for
those features SSH must be reachable on the router (for API routers, via the backup SSH
port). User-management commands check RouterOS's reply and report any per-router failure.

## Settings (env vars)
| Var | Default | |
|---|---|---|
| FLEET_PASSWORD | (required) | Web UI password |
| FLEET_USER | admin | Web UI username |
| FLEET_DB | ./routers.json | Inventory file |
| FLEET_BACKUPS | ./backups | Where downloaded backups are kept |
| FLEET_TIMEOUT | 8 | Per-router connect timeout (s) |
| FLEET_WORKERS | 32 | Max parallel connections |

## Security notes
- Router passwords are stored in plain text in `routers.json`; protect the host.
- The UI uses HTTP Basic auth — put it behind HTTPS (nginx/Caddy reverse proxy) or a VPN, never expose it raw to the internet.
- SSH host keys are auto-accepted on first connect.
- Backups are saved unencrypted (so they restore on any router) and contain your full config, including user accounts. Treat the `backups/` folder as sensitive.
