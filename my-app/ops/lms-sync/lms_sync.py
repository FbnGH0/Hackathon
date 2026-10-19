"""Local hourly course download -> normalized snapshot -> Butler inbox."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from core import Ledger, atomic_json, component, export_course
from secrets_store import load_secret, save_secret
from course_content import extract_material

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
LOG = logging.getLogger("lms-sync")


# Every Singapore polytechnic (and ITE) uses POLITEMall, the same Brightspace platform.
# Pick one with "school": "TP" (or SP / NP / NYP / RP / ITE) in config.local.json.
# Known addresses are pre-filled; for the rest, `login` starts at the shared POLITEMall
# portal and saves whichever school address you land on after signing in.
POLITEMALL_PORTAL = "https://politemall.polite.edu.sg"
POLY_PRESETS = {
    "TP": ("Temasek Polytechnic", "https://tplms.polite.edu.sg"),
    "RP": ("Republic Polytechnic", "https://rplms.polite.edu.sg"),
    "SP": ("Singapore Polytechnic", None),
    "NP": ("Ngee Ann Polytechnic", None),
    "NYP": ("Nanyang Polytechnic", None),
    "ITE": ("Institute of Technical Education", None),
}


def resolve_school(config):
    """Accept a short code ("TP") or full name; fill in the school name and known base_url."""
    raw = str(config.get("school", "")).strip()
    code = raw.upper()
    if code not in POLY_PRESETS:
        code = next((k for k, (name, _) in POLY_PRESETS.items() if name.lower() == raw.lower()), None)
    if code:
        name, known_url = POLY_PRESETS[code]
        config["school"] = name
        config["school_code"] = code
        if not config.get("base_url") and known_url:
            config["base_url"] = known_url
    elif not raw:
        raise ValueError('Set "school" in config.local.json, e.g. "TP", "SP", "NP", "NYP", "RP" or "ITE".')
    return config


def check_base_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query:
        raise ValueError("School base_url must be an HTTPS origin.")
    return f"https://{parsed.netloc}"


def load_config(path: Path):
    config = resolve_school(json.loads(path.read_text(encoding="utf-8-sig")))
    config["config_path"] = str(path)
    config["base_url"] = check_base_url(config["base_url"]) if config.get("base_url") else ""
    target = urlparse(config.get("butler_url", "http://127.0.0.1:3000"))
    if target.hostname not in ("localhost", "127.0.0.1", "::1") or target.scheme not in ("http", "https"):
        raise ValueError("Butler bridge must run on this computer.")
    for field in ("output_dir", "private_dir"):
        config[field] = str((path.parent / config[field]).resolve())
    if not 15 <= int(config.get("interval_minutes", 60)) <= 1440:
        raise ValueError("Sync interval must be between 15 and 1440 minutes.")
    if not 1 <= int(config.get("max_file_mb", 150)) <= 500:
        raise ValueError("max_file_mb must be between 1 and 500.")
    return config


@contextmanager
def single_run(private: Path):
    private.mkdir(parents=True, exist_ok=True)
    handle = (private / "sync.lock").open("a+b")
    handle.seek(0)
    if not handle.read(1):
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Another sync is already running.") from None
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def configure_bridge(config):
    private = Path(config["private_dir"])
    secret_path = private / "bridge.enc"
    env_path = HERE.parents[1] / "apps" / "web" / ".env.local"
    env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    token_line = next((l for l in env.splitlines() if l.startswith("LMS_SYNC_TOKEN=")), None)
    token = token_line.split("=", 1)[1].strip().strip('"') if token_line else secrets.token_urlsafe(48)
    if len(token) < 32:
        raise ValueError("Existing LMS_SYNC_TOKEN must have at least 32 characters.")
    additions = []
    if not token_line:
        additions.append("LMS_SYNC_TOKEN=" + token)
    if "BUTLER_DB_PATH=" not in env:
        additions.append('BUTLER_DB_PATH="' + (private / "butler.sqlite").as_posix() + '"')
    if "OLLAMA_SERVER_BASE_URL=" not in env and "DEEPSEEK_BASE_URL=" not in env:
        additions.append("OLLAMA_SERVER_BASE_URL=http://127.0.0.1:11434/v1")
    if "DEEPSEEK_API_KEY=" not in env:
        additions.append("DEEPSEEK_API_KEY=ollama")
    if "OLLAMA_SERVER_MODEL=" not in env:
        model = config.get("ai_model")
        if not model:
            try:
                with urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
                    installed = json.load(response)["models"]
                names = [m["name"] for m in installed if "completion" in m.get("capabilities", ["completion"]) and "embed" not in m["name"]]
                model = next((n for n in names if n == "qwen38-huihui:latest"), names[0] if names else None)
            except Exception:
                pass
        if model:
            additions.append("OLLAMA_SERVER_MODEL=" + model)
    if "LMS_ALLOWED_ORIGIN=" not in env:
        # Butler also accepts any https://*.polite.edu.sg school, so this only matters for non-POLITEMall schools.
        additions.append("LMS_ALLOWED_ORIGIN=" + (config["base_url"] or POLITEMALL_PORTAL))
    if "LMS_CONTENT_AI_ENABLED=" not in env:
        additions.append("LMS_CONTENT_AI_ENABLED=true")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if additions:
        env_path.write_text(env.rstrip() + "\n" + "\n".join(additions) + "\n", encoding="utf-8")
    save_secret(secret_path, {"token": token})
    LOG.info("Bridge configured. Restart Butler after changing its environment.")


def push(config, snapshot):
    token = os.environ.get("LMS_SYNC_TOKEN")
    if not token:
        token = load_secret(Path(config["private_dir"]) / "bridge.enc")["token"]
    body = json.dumps(snapshot, ensure_ascii=False).encode()
    req = Request(config["butler_url"].rstrip("/") + "/api/lms/ingest", data=body,
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
    # Redirects are forbidden so the bridge token cannot follow an external redirect.
    from urllib.request import HTTPRedirectHandler, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with build_opener(NoRedirect).open(req, timeout=90) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Butler rejected the snapshot.")
    LOG.info("Butler imported snapshot; notifications=%s, AI=%s", result["notifications"], result["aiStatus"])
    return result


def remember_base_url(config, base_url):
    """Write the detected school address back into config.local.json so sync uses it next time."""
    path = Path(config["config_path"])
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data["base_url"] = base_url
    atomic_json(path, data)
    config["base_url"] = base_url
    LOG.info("Detected your school's POLITEMall address: %s (saved to %s)", base_url, path.name)


async def signed_in(page):
    """True once Brightspace accepts this session (works whatever the homepage looks like)."""
    if await page.get_by_role("heading", name="My Courses", exact=True).count():
        return True
    try:
        return await page.evaluate("""async () => {
            const v = await fetch('/d2l/api/versions/', { credentials: 'include' });
            if (!v.ok) return false;
            const lp = (await v.json()).find(x => x.ProductCode === 'lp');
            if (!lp) return false;
            const r = await fetch(`/d2l/api/lp/${lp.LatestVersion}/users/whoami`, { credentials: 'include' });
            return r.ok;
        }""")
    except Exception:
        return False


async def login(config):
    from playwright.async_api import async_playwright
    expected = urlparse(config["base_url"]).hostname if config["base_url"] else None
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=config.get("browser_channel", "msedge"), headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto((config["base_url"] or POLITEMALL_PORTAL) + "/d2l/home")
        LOG.info("Complete the %s login in the new Edge window. The session will be saved encrypted automatically.", config["school"])
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                parsed = urlparse(page.url)
                host = parsed.hostname or ""
                on_school = host == expected if expected else (host.endswith(".polite.edu.sg") and host != urlparse(POLITEMALL_PORTAL).hostname)
                if parsed.scheme == "https" and on_school and parsed.path.startswith("/d2l/home") and await signed_in(page):
                    if not expected:
                        remember_base_url(config, f"https://{parsed.netloc}")
                    state = await context.storage_state()
                    save_secret(Path(config["private_dir"]) / "session.enc", state)
                    LOG.info("School session saved encrypted; no password was stored.")
                    return 0
                await asyncio.sleep(2)
            raise RuntimeError("Login was not completed within 10 minutes.")
        finally:
            await browser.close()


async def sync(config, metadata_only=False):
    if not config["base_url"]:
        raise SystemExit("Your school's POLITEMall address isn't known yet. Run `lms_sync.py login` first; it detects and saves it.")
    from brightspace import Brightspace, LoginRequired, AccessUnavailable
    from playwright.async_api import async_playwright
    private = Path(config["private_dir"])
    snapshot = {"school": config["school"], "baseUrl": config["base_url"], "timezone": config["timezone"],
                "syncedAt": datetime.now(timezone.utc).isoformat(), "status": "ok", "warnings": [],
                "courses": [], "enableAiBrief": config.get("enable_ai_brief", True)}
    ledger = Ledger(private / "sync.sqlite")
    try:
        session_path = private / "session.enc"
        if not session_path.exists() and not os.environ.get("LMS_ACCESS_TOKEN"):
            raise LoginRequired("Please run login before the first sync.")
        state = load_secret(session_path) if session_path.exists() else {"cookies": [], "origins": []}
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel=config.get("browser_channel", "msedge"), headless=True)
            context = await browser.new_context(storage_state=state)
            adapter = Brightspace(context, config)
            try:
                await adapter.initialize()
                courses = await adapter.courses()
                seen_folders = {}
                for source in courses:
                    try:
                        course, warnings = await adapter.course(source)
                        snapshot["warnings"].extend(warnings)
                        # Distinct enrollment IDs must never share an export directory.
                        name = component(course["moduleName"])
                        if name in seen_folders:
                            course["moduleName"] += f" [{course['id']}]"
                        seen_folders[name] = course["id"]
                        export_course(Path(config["output_dir"]), config["school"], course)
                        snapshot["courses"].append(course)
                    except AccessUnavailable as error:
                        snapshot["warnings"].append(f"{source['name']}: {error}")
                    await asyncio.sleep(0.5)
                if not metadata_only:
                    # Deliver the small facts first; large course ZIPs can finish in the background.
                    snapshot["status"] = "partial" if snapshot["warnings"] else "ok"
                    atomic_json(private / "last-snapshot.json", snapshot)
                    try:
                        push(config, snapshot)
                    except Exception:
                        LOG.info("Initial metadata delivery pending; downloads continue and final delivery retries.")
                    for course in snapshot["courses"]:
                        folder = export_course(Path(config["output_dir"]), config["school"], course)
                        for lesson in course["lessons"]:
                            for material in lesson["materials"]:
                                if not material["downloadable"]:
                                    continue
                                path = folder / lesson["directory"] / material["filename"]
                                key = f"{config['base_url']}:{course['id']}:{material['id']}"
                                try:
                                    ledger.relocate(key, material["modifiedAt"], path, Path(config["output_dir"]))
                                    if ledger.needs_download(key, material["modifiedAt"], path):
                                        data = await adapter.download(course["id"], material)
                                        temp = path.with_suffix(path.suffix + ".part")
                                        temp.write_bytes(data)
                                        temp.replace(path)
                                        ledger.downloaded(key, material["modifiedAt"], path)
                                        LOG.info("Downloaded %s / %s", course["moduleName"], material["title"])
                                    material["filePath"] = str(path.resolve())
                                    material.update(extract_material(path))
                                except AccessUnavailable as error:
                                    snapshot["warnings"].append(f"{course['moduleName']}: {material['title']} - {error}")
                        export_course(Path(config["output_dir"]), config["school"], course)
                    # Bound school-text payloads; retrieval reports truncation rather than inventing content.
                    remaining = 1_200_000
                    for course in snapshot["courses"]:
                        for lesson in course["lessons"]:
                            for material in lesson["materials"]:
                                text = material.get("text", "")
                                if len(text) > remaining:
                                    material["text"] = text[:remaining]
                                    material["textStatus"] = "truncated"
                                remaining -= len(material.get("text", ""))
                save_secret(session_path, await context.storage_state())
            finally:
                await browser.close()
    except LoginRequired as error:
        snapshot["status"] = "login_required"
        snapshot["warnings"].append(str(error))
    except AccessUnavailable as error:
        snapshot["status"] = "error"
        snapshot["warnings"].append(str(error))
    except Exception as error:
        snapshot["status"] = "error"
        # Browser errors sometimes include cookies/URLs: log only the type, not raw diagnostics.
        snapshot["warnings"].append(f"Sync failed ({type(error).__name__}); previous files were preserved.")
    finally:
        ledger.close()
    if snapshot["warnings"] and snapshot["status"] == "ok":
        snapshot["status"] = "partial"
    atomic_json(private / "last-snapshot.json", snapshot)
    LOG.info("Sync %s: %d courses, %d warnings", snapshot["status"], len(snapshot["courses"]), len(snapshot["warnings"]))
    try:
        push(config, snapshot)
        atomic_json(private / "status.json", {"syncedAt": snapshot["syncedAt"], "status": snapshot["status"], "bridge": "ok", "warnings": snapshot["warnings"]})
    except Exception as error:
        atomic_json(private / "status.json", {"syncedAt": snapshot["syncedAt"], "status": snapshot["status"], "bridge": "pending", "warnings": snapshot["warnings"]})
        LOG.error("Butler delivery pending (%s); snapshot retained for retry.", type(error).__name__)
        return 2
    return 0 if snapshot["status"] == "ok" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.local.json")
    parser.add_argument("command", choices=["setup", "login", "sync", "inspect", "watch", "status", "retry"])
    args = parser.parse_args()
    config = load_config(args.config.resolve())
    private = Path(config["private_dir"])
    private.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handlers = [RotatingFileHandler(private / "sync.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "setup":
        configure_bridge(config)
        return 0
    if args.command == "status":
        if not (private / "status.json").exists():
            print("No sync has run yet.")
        else:
            print((private / "status.json").read_text(encoding="utf-8"))
        return 0
    if args.command == "retry":
        push(config, json.loads((private / "last-snapshot.json").read_text(encoding="utf-8")))
        return 0
    if args.command == "login":
        with single_run(private):
            return asyncio.run(login(config))
    if args.command in ("sync", "inspect"):
        with single_run(private):
            return asyncio.run(sync(config, metadata_only=args.command == "inspect"))
    while True:
        try:
            with single_run(private):
                asyncio.run(sync(config))
        except RuntimeError:
            LOG.info("Sync skipped because another process is running.")
        time.sleep(config.get("interval_minutes", 60) * 60)


if __name__ == "__main__":
    sys.exit(main())
