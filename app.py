"""
MikroTik Fleet Controller
Web dashboard to monitor and control multiple MikroTik routers over
the RouterOS API (8728 / 8729-SSL) or SSH (22).

Run:  FLEET_PASSWORD=changeme uvicorn app:app --host 0.0.0.0 --port 8080
"""
import asyncio
import json
import os
import secrets
import socket
import ssl
import threading
import time
import re
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, Optional

import librouteros
import paramiko
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
DB_FILE = Path(os.environ.get("FLEET_DB", BASE / "routers.json"))
UI_USER = os.environ.get("FLEET_USER", "admin")
UI_PASS = os.environ.get("FLEET_PASSWORD", "")
BACKUP_DIR = Path(os.environ.get("FLEET_BACKUPS", BASE / "backups"))
TIMEOUT = float(os.environ.get("FLEET_TIMEOUT", "8"))
pool = ThreadPoolExecutor(max_workers=int(os.environ.get("FLEET_WORKERS", "32")))
_lock = threading.Lock()

app = FastAPI(title="MikroTik Fleet Controller")
security = HTTPBasic()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


# --------------------------------------------------------------------- auth
def auth(creds: HTTPBasicCredentials = Depends(security)):
    if not UI_PASS:
        raise HTTPException(500, "Set FLEET_PASSWORD before starting the server.")
    ok = secrets.compare_digest(creds.username, UI_USER) and secrets.compare_digest(
        creds.password, UI_PASS
    )
    if not ok:
        raise HTTPException(401, "Bad credentials", headers={"WWW-Authenticate": "Basic"})
    return creds.username


# ------------------------------------------------------------------ storage
class Router(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    host: str
    method: Literal["api", "ssh"] = "api"
    port: Optional[int] = None  # default chosen by method
    username: str = "admin"
    password: str = ""
    api_ssl: bool = False
    verify_ssl: bool = False
    group: str = ""
    ssh_port: Optional[int] = None  # used for SFTP backup downloads on API routers


def load() -> dict[str, Router]:
    if not DB_FILE.exists():
        return {}
    data = json.loads(DB_FILE.read_text() or "[]")
    return {r["id"]: Router(**r) for r in data}


def save(routers: dict[str, Router]):
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps([r.model_dump() for r in routers.values()], indent=2))
    os.chmod(tmp, 0o600)  # file holds router passwords
    tmp.replace(DB_FILE)


def public(r: Router) -> dict:
    d = r.model_dump()
    d["password"] = "********" if r.password else ""
    return d


# --------------------------------------------------------------- transports
def default_port(r: Router) -> int:
    if r.port:
        return r.port
    if r.method == "ssh":
        return 22
    return 8729 if r.api_ssl else 8728


def api_connect(r: Router):
    kw = dict(username=r.username, password=r.password, host=r.host,
              port=default_port(r), timeout=TIMEOUT)
    if r.api_ssl:
        ctx = ssl.create_default_context()
        if not r.verify_ssl:  # RouterOS often uses self-signed certs
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        kw["ssl_wrapper"] = lambda s: ctx.wrap_socket(s, server_hostname=r.host)
    return librouteros.connect(**kw)


def parse_api_command(cmd: str) -> tuple[str, dict]:
    """
    Accepts either API style  '/ip/address/print'
    or CLI style              '/ip address add address=10.0.0.1/24 interface=ether2'
    Returns (path, {attr: value}).
    """
    words, args = [], {}
    for tok in cmd.strip().split():
        if "=" in tok and not tok.startswith("/"):
            k, v = tok.split("=", 1)
            args[k.lstrip("=")] = v.strip('"')
        else:
            words.extend(p for p in tok.split("/") if p)
    if not words:
        raise ValueError("Empty command")
    return "/" + "/".join(words), args


def api_run(r: Router, cmd: str) -> str:
    path, args = parse_api_command(cmd)
    api = api_connect(r)
    try:
        rows = list(api(path, **args))
    finally:
        api.close()
    if not rows:
        return "(ok, no output)"
    return "\n".join(
        "  ".join(f"{k}={v}" for k, v in row.items()) for row in rows
    )


def ssh_run(r: Router, cmd: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(r.host, port=default_port(r), username=r.username, password=r.password,
              timeout=TIMEOUT, banner_timeout=TIMEOUT, auth_timeout=TIMEOUT,
              look_for_keys=False, allow_agent=False)
    try:
        _, out, err = c.exec_command(cmd, timeout=TIMEOUT * 4)
        text = out.read().decode(errors="replace") + err.read().decode(errors="replace")
    finally:
        c.close()
    return text.strip() or "(ok, no output)"


def run(r: Router, cmd: str) -> str:
    return api_run(r, cmd) if r.method == "api" else ssh_run(r, cmd)


def ssh_mutate(r: Router, cmd: str):
    """Run a RouterOS command that should produce no output on success.
    Any output means RouterOS rejected it, so raise it as an error."""
    out = ssh_run(r, cmd)
    if out and out != "(ok, no output)":
        raise RuntimeError(out)


# connection errors we translate into method-specific guidance
_CONN_ERRORS = (ConnectionRefusedError, ConnectionResetError, TimeoutError, socket.timeout)


def friendly_error(r: Router, e: Exception) -> str:
    msg = str(e)
    is_conn = isinstance(e, _CONN_ERRORS) or type(e).__name__ in (
        "NoValidConnectionsError", "ConnectionRefusedError") or any(
        s in msg.lower() for s in ("refused", "timed out", "timeout",
                                    "unable to connect", "no route", "not known"))
    if not is_conn:
        # RuntimeError is our own clean, pre-formatted message; pass it through
        return msg if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
    port = default_port(r)
    if r.method == "api":
        svc = "api-ssl" if r.api_ssl else "api"
        return (f"Can't reach the RouterOS API at {r.host}:{port}. This router is set to "
                f"API, but the '{svc}' service looks disabled or blocked. On the router run "
                f"'/ip service enable {svc}' (and allow this server's IP), or switch this "
                f"router to SSH.  [{type(e).__name__}]")
    return (f"Can't reach SSH at {r.host}:{port}. Enable it with '/ip service enable ssh' "
            f"or check the host/port.  [{type(e).__name__}]")


def ssh_kv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def get_status(r: Router) -> dict:
    t0 = time.time()
    if r.method == "api":
        api = api_connect(r)
        try:
            res = list(api("/system/resource/print"))[0]
            ident = list(api("/system/identity/print"))[0].get("name", "")
            try:
                rb = list(api("/system/routerboard/print"))[0]
            except Exception:
                rb = {}
        finally:
            api.close()
        free, total = res.get("free-memory", 0), res.get("total-memory", 1)
        mem = round(100 * (1 - int(free) / int(total))) if total else None
    else:
        res = ssh_kv(ssh_run(r, "/system resource print"))
        ident = ssh_kv(ssh_run(r, "/system identity print")).get("name", "")
        try:
            rb = ssh_kv(ssh_run(r, "/system routerboard print"))
        except Exception:
            rb = {}
        mem = None
        try:
            f = float(res["free-memory"].rstrip("KMGiB"))
            t = float(res["total-memory"].rstrip("KMGiB"))
            mem = round(100 * (1 - f / t))
        except Exception:
            pass
    return {
        "online": True,
        "identity": ident,
        "version": res.get("version"),
        "board": res.get("board-name"),
        # RouterBOOT: what the board runs vs. what its installed RouterOS bundles.
        # None on CHR/x86 (routerboard: no) or if it couldn't be read.
        "firmware": (rb.get("current-firmware") or None) if as_bool(rb.get("routerboard", "yes")) else None,
        "firmware_upgrade": (rb.get("upgrade-firmware") or None) if as_bool(rb.get("routerboard", "yes")) else None,
        "uptime": res.get("uptime"),
        "cpu": as_int(str(res.get("cpu-load", "")).rstrip("% ")) if res.get("cpu-load") not in (None, "") else None,
        "mem": mem,
        "latency_ms": round((time.time() - t0) * 1000),
    }


# ------------------------------------------------------------------ backups
def sftp_open(r: Router):
    """SFTP rides on the router's SSH service, for API routers too."""
    port = default_port(r) if r.method == "ssh" else (r.ssh_port or 22)
    t = paramiko.Transport((r.host, port))
    t.banner_timeout = TIMEOUT
    t.connect(username=r.username, password=r.password)
    return t, paramiko.SFTPClient.from_transport(t)


def router_identity(r: Router) -> str:
    """The router's /system identity name, or '' if it can't be read."""
    try:
        if r.method == "api":
            api = api_connect(r)
            try:
                return list(api("/system/identity/print"))[0].get("name", "")
            finally:
                api.close()
        return ssh_kv(ssh_run(r, "/system identity print")).get("name", "")
    except Exception:
        return ""


def backup_and_fetch(r: Router) -> dict:
    # Name after the router's identity; fall back to its IP/hostname
    label = router_identity(r) or r.host
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", label)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.") or r.host.replace(":", "-")
    name = f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"

    # 1. create the backup on the router (unencrypted so it restores anywhere)
    def save_cmd(extra: str):
        if r.method == "api":
            return api_run(r, f"/system/backup/save name={name}{extra}")
        return ssh_run(r, f"/system backup save name={name}{extra}")
    try:
        save_cmd(" dont-encrypt=yes")
    except Exception:
        save_cmd("")  # older RouterOS without dont-encrypt

    # 2. pull it down over SFTP
    try:
        t, sftp = sftp_open(r)
    except Exception as e:
        raise RuntimeError(f"Backup saved on router as {name}.backup, but download "
                           f"failed (is SSH enabled on the router?): {e}")
    try:
        dest_dir = BACKUP_DIR / r.id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{name}.backup"
        last = None
        for _ in range(10):  # file can take a moment to appear on slow flash
            for remote in (f"{name}.backup", f"flash/{name}.backup"):
                try:
                    sftp.get(remote, str(dest))
                    if dest.stat().st_size > 0:
                        return {
                            "text": f"Saved {dest.name} ({dest.stat().st_size // 1024} KB)",
                            "download": f"/api/backups/{r.id}/{dest.name}",
                        }
                except (IOError, OSError) as e:
                    last = e
            time.sleep(1)
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"Backup created on router but file not found via SFTP: {last}")
    finally:
        sftp.close()
        t.close()


def backup_path(rid: str, fname: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9]+", rid) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.backup", fname):
        raise HTTPException(400, "Bad backup name")
    p = BACKUP_DIR / rid / fname
    if not p.is_file():
        raise HTTPException(404, "Backup not found")
    return p


async def in_thread(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(pool, fn, *a)


async def safe(fn, *a) -> dict:
    try:
        return {"ok": True, "result": await in_thread(fn, *a)}
    except Exception as e:  # report per-router failures, don't abort the batch
        # a[0] is the Router in every call site, so give method-aware guidance
        if a and isinstance(a[0], Router):
            return {"ok": False, "error": friendly_error(a[0], e)}
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------- API
@app.get("/")
def index(_=Depends(auth)):
    # Stamp each CSS/JS link with the file's modified time so browsers always
    # fetch the new version after an update (no stale cache / hard refresh needed).
    html = (BASE / "static" / "index.html").read_text()
    for f in [*(BASE / "static").glob("*.css"), *(BASE / "static").glob("*.js")]:
        html = html.replace(f'/static/{f.name}"', f'/static/{f.name}?v={int(f.stat().st_mtime)}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/routers")
def list_routers(_=Depends(auth)):
    return [public(r) for r in load().values()]


@app.post("/api/routers")
def add_router(r: Router, _=Depends(auth)):
    with _lock:
        rs = load()
        r.id = uuid.uuid4().hex[:8]
        rs[r.id] = r
        save(rs)
    return public(r)


@app.put("/api/routers/{rid}")
def update_router(rid: str, r: Router, _=Depends(auth)):
    with _lock:
        rs = load()
        if rid not in rs:
            raise HTTPException(404)
        if r.password in ("", "********"):
            r.password = rs[rid].password  # keep existing password
        r.id = rid
        rs[rid] = r
        save(rs)
    return public(r)


@app.delete("/api/routers/{rid}")
def delete_router(rid: str, _=Depends(auth)):
    with _lock:
        rs = load()
        rs.pop(rid, None)
        save(rs)
    return {"ok": True}


# ------------------------------------------------------------------ traffic
# One line per interface over SSH, so we don't have to parse table output.
SSH_IFACE_SCRIPT = (
    ':foreach i in=[/interface find] do={:put ([/interface get $i name]."\\t".'
    '[/interface get $i type]."\\t".[/interface get $i running]."\\t".'
    '[/interface get $i disabled]."\\t".[/interface get $i rx-byte]."\\t".'
    '[/interface get $i tx-byte]."\\t".[/interface get $i comment])}'
)


def as_bool(v) -> bool:
    return v is True or str(v).lower() in ("true", "yes")


def as_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def iface_counters(r: Router) -> list[dict]:
    """Byte counters per interface. The browser turns two samples into a rate."""
    if r.method == "api":
        api = api_connect(r)
        try:
            rows = list(api("/interface/print"))
            if rows and "rx-byte" not in rows[0]:
                rows = list(api.rawCmd("/interface/print", "=stats="))
        finally:
            api.close()
    else:
        rows = []
        for line in ssh_run(r, SSH_IFACE_SCRIPT).splitlines():
            p = line.rstrip("\r").split("\t")
            if len(p) < 6:
                continue
            rows.append(dict(zip(
                ["name", "type", "running", "disabled", "rx-byte", "tx-byte", "comment"], p)))
    return [{
        "name": x.get("name", ""),
        "type": x.get("type", ""),
        "comment": x.get("comment", "") or "",
        "running": as_bool(x.get("running")),
        "disabled": as_bool(x.get("disabled")),
        "rx": as_int(x.get("rx-byte")),
        "tx": as_int(x.get("tx-byte")),
    } for x in rows]


@app.get("/api/routers/{rid}/traffic")
async def traffic(rid: str, _=Depends(auth)):
    r = load().get(rid)
    if not r:
        raise HTTPException(404)
    res = await safe(iface_counters, r)
    if not res["ok"]:
        raise HTTPException(502, res["error"])
    # timestamp taken after the read so rate = Δbytes / Δt is accurate
    return {"t": time.time(), "interfaces": res["result"]}


@app.get("/api/status")
async def status_all(_=Depends(auth)):
    rs = list(load().values())
    results = await asyncio.gather(*(safe(get_status, r) for r in rs))
    return {
        r.id: (res["result"] if res["ok"] else {"online": False, "error": res["error"]})
        for r, res in zip(rs, results)
    }


class Exec(BaseModel):
    router_ids: list[str]
    command: str


@app.post("/api/exec")
async def exec_cmd(body: Exec, _=Depends(auth)):
    rs = load()
    targets = [rs[i] for i in body.router_ids if i in rs]
    if not targets:
        raise HTTPException(400, "No valid routers selected")
    results = await asyncio.gather(*(safe(run, r, body.command) for r in targets))
    return {r.id: res for r, res in zip(targets, results)}


# Quick actions: (api command, ssh command)
ACTIONS = {
    "interfaces": ("/interface/print", "/interface print terse"),
    "addresses": ("/ip/address/print", "/ip address print terse"),
    "routes": ("/ip/route/print", "/ip route print terse"),
    "dhcp-leases": ("/ip/dhcp-server/lease/print", "/ip dhcp-server lease print terse"),
    "log": ("/log/print", "/log print"),
    "backup": (None, None),  # handled by backup_and_fetch
    "export": (None, "/export"),
    "check-updates": ("/system/package/update/check-for-updates",
                      "/system package update check-for-updates"),
    "reboot": ("/system/reboot", "/system reboot"),
}


class Action(BaseModel):
    router_ids: list[str]
    action: str


@app.post("/api/action")
async def action(body: Action, _=Depends(auth)):
    if body.action not in ACTIONS:
        raise HTTPException(400, "Unknown action")
    rs = load()
    targets = [rs[i] for i in body.router_ids if i in rs]
    api_cmd, ssh_cmd = ACTIONS[body.action]

    def do(r: Router):
        if body.action == "backup":
            return backup_and_fetch(r)
        if r.method == "api":
            if api_cmd is None:
                raise RuntimeError("Not available over API — switch this router to SSH")
            if body.action == "reboot":
                try:
                    return api_run(r, api_cmd)
                except Exception:
                    return "Reboot sent (connection dropped as expected)"
            return api_run(r, api_cmd)
        if body.action == "reboot":
            # RouterOS asks for confirmation on reboot; :execute avoids the prompt
            return ssh_run(r, ':execute "/system reboot"') or "Reboot sent"
        return ssh_run(r, ssh_cmd)

    results = await asyncio.gather(*(safe(do, r) for r in targets))
    return {r.id: res for r, res in zip(targets, results)}


# ------------------------------------------------------------------ users
NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


def list_users(r: Router) -> list[dict]:
    if r.method == "api":
        api = api_connect(r)
        try:
            rows = list(api("/user/print"))
        finally:
            api.close()
        return [{"name": x.get("name", ""), "group": x.get("group", ""),
                 "disabled": as_bool(x.get("disabled"))} for x in rows if x.get("name")]
    users = []
    for line in ssh_run(r, "/user print terse").splitlines():
        m = re.search(r"name=(\S+)", line)
        if not m:
            continue
        g = re.search(r"group=(\S+)", line)
        flags = line.split("name=")[0]
        users.append({"name": m.group(1).strip('"'),
                      "group": g.group(1).strip('"') if g else "",
                      "disabled": " X " in f" {flags} "})
    return users


def reconcile_users(r: Router, keep: list[str], ensure: dict | None, apply: bool) -> dict:
    """Keep the named accounts (plus the login account), optionally create one,
    and remove every other account. Create happens before any removal."""
    keep_set = {k.strip() for k in keep if k.strip()}
    if ensure and ensure.get("name"):
        keep_set.add(ensure["name"].strip())
    keep_set.add(r.username)  # never delete the account the app logs in with

    users = list_users(r)
    names = {u["name"] for u in users}
    to_remove = sorted(u["name"] for u in users if u["name"] not in keep_set)
    to_create = ([ensure["name"]] if ensure and ensure.get("name")
                 and ensure["name"] not in names else [])

    plan = {"existing": sorted(names), "kept": sorted(n for n in names if n in keep_set),
            "to_remove": to_remove, "to_create": to_create, "applied": apply}
    if ensure and ensure.get("sshkey") and ensure.get("name"):
        plan["key_preview"] = f"Will set SSH key on {ensure['name']}"
    if not apply:
        plan["text"] = _user_plan_text(plan, preview=True)
        return plan

    created, removed, errors = [], [], []

    # per-method primitives that raise on failure
    if r.method == "api":
        api = api_connect(r)

        def add_user():
            api("/user/add", name=ensure["name"], password=ensure.get("password", ""),
                group=ensure.get("group", "full"))

        def remove_user(name):
            ids = {x.get("name"): x.get(".id") for x in api("/user/print")}
            if ids.get(name):
                api("/user/remove", **{".id": ids[name]})

        close = api.close
    else:
        def add_user():
            pw = ensure.get("password", "")
            ssh_mutate(r, f'/user add name={ensure["name"]} password="{pw}" '
                          f'group={ensure.get("group", "full")}')

        def remove_user(name):
            ssh_mutate(r, f'/user remove [find name="{name}"]')

        close = lambda: None

    try:
        if to_create:
            try:
                add_user()
                created.append(ensure["name"])
            except Exception as e:
                errors.append(f"create {ensure['name']}: {type(e).__name__}: {e}")
        # only delete accounts once the account we want to keep is confirmed present,
        # so a failed create can never strip a router down
        ensure_present = (not ensure) or ensure["name"] in names or ensure["name"] in created
        if ensure_present:
            for name in to_remove:
                try:
                    remove_user(name)
                    removed.append(name)
                except Exception as e:
                    errors.append(f"remove {name}: {type(e).__name__}: {e}")
        elif to_remove:
            errors.append(f"skipped removing {', '.join(to_remove)} because "
                          f"{ensure['name']} could not be created")
    finally:
        close()

    key_status = ""
    if ensure and ensure.get("sshkey") and ensure.get("name") and (
            ensure["name"] in names or ensure["name"] in created):
        try:
            add_ssh_key(r, ensure["name"], ensure["sshkey"])
            key_status = f"SSH key imported for {ensure['name']}"
        except Exception as e:
            errors.append(f"SSH key for {ensure['name']}: {friendly_error(r, e)}")

    plan.update(created=created, removed=removed, key=key_status, errors=errors)
    text = _user_plan_text(plan, preview=False)
    if errors:
        # raise so the router shows as failed, but keep the full summary in the message
        raise RuntimeError(text + "\n\nFAILED:\n- " + "\n- ".join(errors))
    plan["text"] = text
    return plan


def add_ssh_key(r: Router, username: str, pubkey: str):
    """Upload a public key over SFTP and import it for the user. Idempotent:
    any existing keys for that user are removed first."""
    fname = f"{username}.pub"
    t, sftp = sftp_open(r)
    try:
        with sftp.open(fname, "w") as f:
            f.write(pubkey.strip() + "\n")
    finally:
        sftp.close()
        t.close()
    if r.method == "api":
        api = api_connect(r)
        try:
            for k in list(api("/user/ssh-keys/print")):
                if k.get("user") == username and k.get(".id"):
                    api("/user/ssh-keys/remove", **{".id": k[".id"]})
            api("/user/ssh-keys/import", user=username, **{"public-key-file": fname})
        finally:
            api.close()
    else:
        ssh_run(r, f"/user ssh-keys remove [find user={username}]")
        ssh_run(r, f"/user ssh-keys import user={username} public-key-file={fname}")


def _user_plan_text(p: dict, preview: bool) -> str:
    lines = [f"Existing users: {', '.join(p['existing']) or '(none)'}"]
    if preview:
        lines.append(f"Will keep:   {', '.join(p['kept']) or '(none)'}")
        lines.append(f"Will create: {', '.join(p['to_create']) or '(none)'}")
        lines.append(f"Will remove: {', '.join(p['to_remove']) or '(none)'}")
        if p.get("key_preview"):
            lines.append(p["key_preview"])
    else:
        lines.append(f"Created: {', '.join(p.get('created', [])) or '(none)'}")
        lines.append(f"Removed: {', '.join(p.get('removed', [])) or '(none)'}")
        if p.get("key"):
            lines.append(p["key"])
    return "\n".join(lines)


class UserCleanup(BaseModel):
    router_ids: list[str]
    keep: list[str] = ["admin"]
    ensure_name: str = ""
    ensure_password: str = ""
    ensure_group: str = "full"
    ensure_sshkey: str = ""
    apply: bool = False


@app.post("/api/users/cleanup")
async def users_cleanup(body: UserCleanup, _=Depends(auth)):
    for n in [*body.keep, body.ensure_name]:
        if n and not NAME_RE.match(n):
            raise HTTPException(400, f"Invalid user name: {n!r}")
    if '"' in body.ensure_password:
        raise HTTPException(400, "Password cannot contain a double-quote character")
    key = body.ensure_sshkey.strip()
    if key and not re.match(r"^(ssh-(rsa|ed25519|dss)|ecdsa-)", key):
        raise HTTPException(400, "SSH key must be an OpenSSH public key (e.g. 'ssh-rsa AAAA...')")
    if key and ("\n" in key or "\r" in key):
        raise HTTPException(400, "SSH key must be a single line")
    rs = load()
    targets = [rs[i] for i in body.router_ids if i in rs]
    if not targets:
        raise HTTPException(400, "No valid routers selected")
    ensure = ({"name": body.ensure_name.strip(), "password": body.ensure_password,
               "group": body.ensure_group.strip() or "full", "sshkey": key}
              if body.ensure_name.strip() else None)
    results = await asyncio.gather(
        *(safe(reconcile_users, r, body.keep, ensure, body.apply) for r in targets))
    return {r.id: res for r, res in zip(targets, results)}


# ------------------------------------------------------------------ updates
def check_update(r: Router) -> dict:
    """Ask the router to check MikroTik's upgrade server and report versions."""
    if r.method == "api":
        api = api_connect(r)
        try:
            list(api("/system/package/update/check-for-updates"))
            row = list(api("/system/package/update/print"))[0]
        finally:
            api.close()
    else:
        ssh_run(r, "/system package update check-for-updates")
        row = ssh_kv(ssh_run(r, "/system package update print"))
    installed = row.get("installed-version") or row.get("installed", "")
    latest = row.get("latest-version") or row.get("latest", "")
    status = row.get("status", "")
    # "available" when the server returned a newer version than installed
    available = bool(latest) and bool(installed) and latest != installed
    return {"installed": installed, "latest": latest, "channel": row.get("channel", ""),
            "status": status, "available": available}


def update_router(r: Router, apply: bool) -> dict:
    info = check_update(r)
    if not apply:
        info["text"] = _update_text(info, None)
        return info
    if not info["available"]:
        info["action"] = "skipped"
        info["text"] = _update_text(info, "Already up to date — nothing to install.")
        return info
    # install downloads the packages and reboots the router to apply them.
    # The session drops during reboot, which is the expected outcome.
    note = f"Update started: {info['installed']} → {info['latest']}. Router is downloading and will reboot."
    try:
        if r.method == "api":
            api = api_connect(r)
            try:
                list(api("/system/package/update/install"))
            finally:
                api.close()
        else:
            # :execute returns at once so we don't block on the download+reboot
            ssh_run(r, ':execute "/system package update install"')
    except _CONN_ERRORS + (Exception,) as e:
        if isinstance(e, _CONN_ERRORS) or type(e).__name__ in (
                "NoValidConnectionsError", "ConnectionResetError"):
            pass  # connection dropped as the router rebooted — expected
        else:
            raise
    info["action"] = "installing"
    info["text"] = _update_text(info, note)
    return info


def _update_text(info: dict, action_note: str | None) -> str:
    lines = [f"Installed: {info['installed'] or '?'}    Latest: {info['latest'] or '?'}"
             + (f"    ({info['channel']})" if info.get("channel") else "")]
    if info.get("status"):
        lines.append(f"Status: {info['status']}")
    if action_note:
        lines.append(action_note)
    elif info["available"]:
        lines.append(f"Update available: {info['installed']} → {info['latest']}")
    else:
        lines.append("Up to date")
    return "\n".join(lines)


class UpdateReq(BaseModel):
    router_ids: list[str]
    apply: bool = False


@app.post("/api/updates")
async def updates(body: UpdateReq, _=Depends(auth)):
    rs = load()
    targets = [rs[i] for i in body.router_ids if i in rs]
    if not targets:
        raise HTTPException(400, "No valid routers selected")
    results = await asyncio.gather(*(safe(update_router, r, body.apply) for r in targets))
    return {r.id: res for r, res in zip(targets, results)}


# ------------------------------------------------------- RouterBOOT firmware
# RouterBOOT (the bootloader firmware) is upgraded separately from RouterOS.
# Each RouterOS release bundles a firmware version ("upgrade-firmware"); the
# board keeps running its old firmware ("current-firmware") until you run
# /system routerboard upgrade AND reboot.
FW_LOG_MATCH = "firmware upgraded"   # RouterOS logs "Firmware upgraded successfully, please reboot..."
FW_CONFIRM_WAIT = float(os.environ.get("FLEET_FW_WAIT", "60"))


def firmware_info(r: Router) -> dict:
    if r.method == "api":
        api = api_connect(r)
        try:
            row = list(api("/system/routerboard/print"))[0]
        finally:
            api.close()
    else:
        row = ssh_kv(ssh_run(r, "/system routerboard print"))
    is_rb = as_bool(row.get("routerboard", "yes"))
    current = str(row.get("current-firmware", "") or "")
    upgrade = str(row.get("upgrade-firmware", "") or "")
    return {
        "routerboard": is_rb,
        "model": row.get("model") or row.get("board-name") or "",
        "firmware_type": row.get("firmware-type", ""),
        "current": current,
        "upgrade": upgrade,
        "available": is_rb and bool(current) and bool(upgrade) and current != upgrade,
    }


def fw_log_count(r: Router) -> int:
    """How many 'firmware upgraded' lines are in the router's log right now."""
    if r.method == "api":
        api = api_connect(r)
        try:
            rows = list(api("/log/print"))
        finally:
            api.close()
        return sum(FW_LOG_MATCH in str(x.get("message", "")).lower() for x in rows)
    out = ssh_run(r, '/log print where message~"irmware upgraded"')
    return sum(FW_LOG_MATCH in l.lower() for l in out.splitlines())


def _fw_text(info: dict, note: str | None) -> str:
    if not info["routerboard"]:
        return "Not a RouterBOARD (CHR / x86) — there is no RouterBOOT firmware to upgrade."
    lines = [f"Model: {info['model'] or '?'}" + (f"    ({info['firmware_type']})" if info["firmware_type"] else ""),
             f"Current firmware: {info['current'] or '?'}    Bundled with RouterOS: {info['upgrade'] or '?'}"]
    if note:
        lines.append(note)
    elif info["available"]:
        lines.append(f"Firmware upgrade available: {info['current']} → {info['upgrade']}")
    else:
        lines.append("Firmware matches the installed RouterOS — nothing to do")
    return "\n".join(lines)


def update_firmware(r: Router, apply: bool, reboot: bool) -> dict:
    info = firmware_info(r)
    if not apply or not info["available"]:
        info["text"] = _fw_text(info, "Already current — skipped." if apply and info["routerboard"] else None)
        if apply:
            info["action"] = "skipped"
        return info

    before = fw_log_count(r)
    # write the new RouterBOOT to flash (the API never prompts; over SSH,
    # :execute avoids the interactive "Do you really want to upgrade?" prompt)
    if r.method == "api":
        api = api_connect(r)
        try:
            list(api("/system/routerboard/upgrade"))
        finally:
            api.close()
    else:
        ssh_run(r, ':execute "/system routerboard upgrade"')

    # only reboot once the router confirms the firmware was written
    deadline = time.time() + FW_CONFIRM_WAIT
    confirmed = False
    while time.time() < deadline:
        time.sleep(3)
        if fw_log_count(r) > before:
            confirmed = True
            break
    if not confirmed:
        info["action"] = "unconfirmed"
        info["text"] = _fw_text(info, (
            f"Upgrade command sent, but the router didn't log 'Firmware upgraded successfully' "
            f"within {int(FW_CONFIRM_WAIT)}s, so it was NOT rebooted. Check /log on the router "
            f"and reboot it yourself once the upgrade is confirmed."))
        return info

    note = f"Firmware written: {info['current']} → {info['upgrade']}."
    if reboot:
        try:
            if r.method == "api":
                api_run(r, "/system/reboot")
            else:
                ssh_run(r, ':execute "/system reboot"')
        except Exception:
            pass  # connection drops as the router goes down — expected
        note += " Rebooting now to load it (takes a minute or two)."
        info["action"] = "rebooting"
    else:
        note += " It takes effect at the next reboot."
        info["action"] = "pending-reboot"
    info["text"] = _fw_text(info, note)
    return info


class FirmwareReq(BaseModel):
    router_ids: list[str]
    apply: bool = False
    reboot: bool = True


@app.post("/api/firmware")
async def firmware(body: FirmwareReq, _=Depends(auth)):
    rs = load()
    targets = [rs[i] for i in body.router_ids if i in rs]
    if not targets:
        raise HTTPException(400, "No valid routers selected")
    results = await asyncio.gather(
        *(safe(update_firmware, r, body.apply, body.reboot) for r in targets))
    return {r.id: res for r, res in zip(targets, results)}


@app.get("/api/backups")
def list_backups(_=Depends(auth)):
    rs = load()
    out = []
    if BACKUP_DIR.exists():
        for d in BACKUP_DIR.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.backup"):
                st = f.stat()
                out.append({
                    "router_id": d.name,
                    "router": rs[d.name].name if d.name in rs else f"(deleted {d.name})",
                    "file": f.name, "size": st.st_size, "mtime": st.st_mtime,
                    "url": f"/api/backups/{d.name}/{f.name}",
                })
    return sorted(out, key=lambda x: -x["mtime"])


@app.get("/api/backups/{rid}/{fname}")
def download_backup(rid: str, fname: str, _=Depends(auth)):
    return FileResponse(backup_path(rid, fname), filename=fname,
                        media_type="application/octet-stream")


@app.delete("/api/backups/{rid}/{fname}")
def delete_backup(rid: str, fname: str, _=Depends(auth)):
    backup_path(rid, fname).unlink()
    return {"ok": True}
