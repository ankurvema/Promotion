#!/usr/bin/env python3
"""Publish scheduled Instagram carousels stored in this repo.

Uses the official Instagram API (Instagram Login). Instagram fetches each
slide from raw.githubusercontent.com, so the repo must be public.

Commands:
  run             publish every carousel that is due (used by GitHub Actions)
  check           validate all post folders without contacting Instagram
  refresh-tokens  extend each account's token and save it back as a repo secret
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

API_VERSION = os.environ.get("IG_API_VERSION", "v26.0")
GRAPH_URL = f"https://graph.instagram.com/{API_VERSION}"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = ROOT / "accounts"

IMAGE_EXTS = frozenset({".jpg", ".jpeg"})
NON_JPEG_EXTS = frozenset({".png", ".heic", ".webp", ".gif"})
MIN_SLIDES, MAX_SLIDES = 2, 10  # API carousel limits
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT = 30
POLL_INTERVAL = 5
POLL_TIMEOUT = 300
LIVE_CHECK_TRIES = 8  # feed checks after a publish error (Instagram lags behind its own 403)
LIVE_CHECK_INTERVAL = 10
RETRIES = 3

log = logging.getLogger("publish")


class PublishError(RuntimeError):
    """A post could not be published."""


class RateLimitError(PublishError):
    """Instagram throttled us; safe to retry on a later run."""


RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})


# --------------------------------------------------------------------------
# Repo content
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    path: Path
    enabled: bool
    start_date: date
    tz: ZoneInfo
    post_hour: int

    @property
    def _suffix(self) -> str:
        return re.sub(r"[^A-Z0-9]", "_", self.name.upper())

    @property
    def token_var(self) -> str:
        return f"IG_TOKEN_{self._suffix}"

    @property
    def user_id_var(self) -> str:
        return f"IG_USER_ID_{self._suffix}"

    @property
    def log_path(self) -> Path:
        return self.path / "posted.json"

    def post_dirs(self) -> list[Path]:
        posts = self.path / "posts"
        if not posts.is_dir():
            return []
        return sorted(p for p in posts.iterdir() if p.is_dir())


def _slide_order(path: Path) -> tuple[int, int, str]:
    """Sort 1.jpg, 2.jpg ... 10.jpg numerically, anything else by name after."""
    return (0, int(path.stem), "") if path.stem.isdigit() else (1, 0, path.name.lower())


@dataclass(frozen=True, slots=True)
class Post:
    id: str
    folder: Path
    slides: tuple[Path, ...]
    caption: str

    @classmethod
    def load(cls, folder: Path) -> Post:
        slides = tuple(
            sorted(
                (p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS),
                key=_slide_order,
            )
        )
        caption_file = folder / "caption.txt"
        caption = (
            caption_file.read_text(encoding="utf-8").strip()
            if caption_file.is_file()
            else ""
        )
        return cls(folder.name, folder, slides, caption)

    def problems(self) -> list[str]:
        issues: list[str] = []
        wrong_format = sorted(
            p.name for p in self.folder.iterdir() if p.suffix.lower() in NON_JPEG_EXTS
        )
        if wrong_format:
            issues.append(f"convert to JPEG: {', '.join(wrong_format)}")
        if not MIN_SLIDES <= len(self.slides) <= MAX_SLIDES:
            issues.append(
                f"has {len(self.slides)} slides, needs {MIN_SLIDES}-{MAX_SLIDES}"
            )
        issues.extend(
            f"{s.name} is over 8 MB"
            for s in self.slides
            if s.stat().st_size > MAX_IMAGE_BYTES
        )
        if not self.caption:
            issues.append("missing caption.txt")
        if len(self.caption) > MAX_CAPTION_CHARS:
            issues.append(f"caption is {len(self.caption)} chars (max {MAX_CAPTION_CHARS})")
        hashtags = len(re.findall(r"(?<!\w)#\w+", self.caption))
        if hashtags > MAX_HASHTAGS:
            issues.append(f"{hashtags} hashtags (max {MAX_HASHTAGS})")
        return issues


def load_accounts(only: list[str] | None = None) -> list[Account]:
    accounts = []
    for cfg in sorted(ACCOUNTS_DIR.glob("*/config.toml")):
        name = cfg.parent.name
        if only and name not in only:
            continue
        with cfg.open("rb") as f:
            data = tomllib.load(f)
        accounts.append(
            Account(
                name=name,
                path=cfg.parent,
                enabled=bool(data.get("enabled", True)),
                start_date=date.fromisoformat(str(data["start_date"])),
                tz=ZoneInfo(data["timezone"]),
                post_hour=int(data["post_hour"]),
            )
        )
    if only and (missing := set(only) - {a.name for a in accounts}):
        raise SystemExit(f"Unknown account(s): {', '.join(sorted(missing))}")
    return accounts


def read_state(account: Account) -> dict:
    """Load posted.json: {"posted": [...], "pending": {...} | absent}."""
    if not account.log_path.is_file():
        return {"posted": []}
    state = json.loads(account.log_path.read_text(encoding="utf-8"))
    state.setdefault("posted", [])
    return state


def read_log(account: Account) -> list[dict]:
    return read_state(account)["posted"]


def write_state(account: Account, state: dict) -> None:
    if not state.get("pending"):
        state.pop("pending", None)
    account.log_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def is_due(account: Account, entries: list[dict], now: datetime) -> bool:
    """Due once per local day, at the first run on/after post_hour."""
    local = now.astimezone(account.tz)
    if local.date() < account.start_date or local.hour < account.post_hour:
        return False
    today = local.date().isoformat()
    return all(e.get("local_date") != today for e in entries)


def raw_url(path: Path, repo: str, ref: str) -> str:
    rel = path.relative_to(ROOT).as_posix()
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{quote(rel)}"


# --------------------------------------------------------------------------
# Instagram API
# --------------------------------------------------------------------------


def _json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


class InstagramClient:
    def __init__(self, user_id: str, token: str) -> None:
        self._user_id = user_id
        self._token = token
        self._http = requests.Session()

    def _call(self, method: str, path: str, *, retries: int = RETRIES, **params: str) -> dict:
        params["access_token"] = self._token
        payload = {"params": params} if method == "GET" else {"data": params}
        error = "unknown error"
        for attempt in range(1, retries + 1):
            try:
                resp = self._http.request(
                    method, f"{GRAPH_URL}/{path}", timeout=HTTP_TIMEOUT, **payload
                )
            except requests.RequestException as exc:
                # Don't log the exception text: it can contain the URL + token.
                error = f"network error ({type(exc).__name__})"
            else:
                body = _json(resp)
                if resp.ok:
                    return body
                err = body.get("error", {})
                error = f"HTTP {resp.status_code}: {err.get('message', resp.reason)}"
                if err.get("code") in RATE_LIMIT_CODES or "request limit" in error:
                    raise RateLimitError(f"{method} {path} throttled: {error}")
                if resp.status_code < 500 and not err.get("is_transient"):
                    break  # client error; retrying won't help
            if attempt < retries:
                time.sleep(2**attempt)
        raise PublishError(f"{method} {path} failed: {error}")

    def create_slide(self, image_url: str) -> str:
        return self._call(
            "POST", f"{self._user_id}/media", image_url=image_url, is_carousel_item="true"
        )["id"]

    def create_carousel(self, children: list[str], caption: str) -> str:
        return self._call(
            "POST",
            f"{self._user_id}/media",
            media_type="CAROUSEL",
            children=",".join(children),
            caption=caption,
        )["id"]

    def wait_until_ready(self, container_id: str) -> None:
        deadline = time.monotonic() + POLL_TIMEOUT
        while True:
            info = self._call("GET", container_id, fields="status_code,status")
            code = info.get("status_code")
            if code == "FINISHED":
                return
            if code in {"ERROR", "EXPIRED"}:
                raise PublishError(f"container {container_id} {code}: {info.get('status', '')}")
            if time.monotonic() > deadline:
                raise PublishError(f"container {container_id} not ready after {POLL_TIMEOUT}s")
            time.sleep(POLL_INTERVAL)

    def container_status(self, container_id: str) -> str:
        info = self._call("GET", container_id, fields="status_code")
        return info.get("status_code", "")

    def find_live_media(self, caption: str) -> str | None:
        """Media id of a recent post whose caption matches, or None.

        The source of truth for "is this already published?", independent of
        container status or our own log (which a crashed run may not have saved).
        """
        data = self._call("GET", f"{self._user_id}/media", fields="id,caption", limit="50")
        wanted = caption.strip()
        return next(
            (m["id"] for m in data.get("data", []) if (m.get("caption") or "").strip() == wanted),
            None,
        )

    def publish(self, container_id: str) -> str:
        """Publish once. Never retried here: a lost response may still have gone live,
        so the caller must check container_status before trying again."""
        return self._call(
            "POST", f"{self._user_id}/media_publish", retries=1, creation_id=container_id
        )["id"]


def ensure_reachable(urls: list[str]) -> None:
    """Fail early with a clear message if Instagram won't be able to fetch a slide."""
    for url in urls:
        try:
            status = requests.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True).status_code
        except requests.RequestException as exc:
            raise PublishError(f"cannot reach {url} ({type(exc).__name__})") from None
        if status != 200:
            raise PublishError(
                f"{url} returned HTTP {status}; is the repo public and the file pushed?"
            )


def create_container(client: InstagramClient, post: Post, repo: str, ref: str) -> str:
    urls = [raw_url(s, repo, ref) for s in post.slides]
    ensure_reachable(urls)
    children = []
    for url in urls:
        child = client.create_slide(url)
        client.wait_until_ready(child)
        children.append(child)
    carousel = client.create_carousel(children, post.caption)
    client.wait_until_ready(carousel)
    return carousel


def wait_until_live(client: InstagramClient, caption: str) -> str | None:
    """Poll the feed for a just-published post; Instagram can take a while to show it."""
    for attempt in range(LIVE_CHECK_TRIES):
        if attempt:
            time.sleep(LIVE_CHECK_INTERVAL)
        try:
            if media_id := client.find_live_media(caption):
                return media_id
        except PublishError:
            continue  # throttled lookup; try again
    return None


def finish_publish(client: InstagramClient, container_id: str, caption: str) -> str:
    """Publish a ready container exactly once and return its media id.

    media_publish can go live yet still return an error (e.g. a 403 "request limit"),
    so after any error the feed, not the response, decides whether it worked.
    """
    try:
        return client.publish(container_id)
    except PublishError as exc:
        log.warning("publish returned an error (%s); checking the feed", exc)
        if media_id := wait_until_live(client, caption):
            return media_id
        raise


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _run_account(account: Account, args: argparse.Namespace, now: datetime) -> None:
    if not account.enabled:
        log.info("[%s] disabled, skipping", account.name)
        return
    state = read_state(account)
    entries = state["posted"]
    pending = state.get("pending")
    if not (args.force or pending or is_due(account, entries, now)):
        log.info("[%s] nothing due right now", account.name)
        return

    done = {e["post"] for e in entries}
    posts = [d for d in account.post_dirs() if d.name not in done]
    if not posts:
        log.warning("[%s] no posts left to publish", account.name)
        return

    # Resume an interrupted publish first so the same post is never created twice.
    folder = next((d for d in posts if pending and d.name == pending["post"]), posts[0])
    post = Post.load(folder)
    if problems := post.problems():
        raise PublishError(f"post {post.id}: {'; '.join(problems)}")
    if args.dry_run:
        log.info("[%s] would publish %s (%d slides)", account.name, post.id, len(post.slides))
        return

    token = os.environ.get(account.token_var)
    user_id = os.environ.get(account.user_id_var)
    if not (token and user_id):
        raise PublishError(f"missing secrets {account.token_var} / {account.user_id_var}")
    client = InstagramClient(user_id, token)

    # Ask Instagram first: if this post is already live, just record it.
    live_id = client.find_live_media(post.caption)
    if live_id:
        log.warning("[%s] %s is already live (media %s); recording it", account.name, post.id, live_id)
        _record(account, state, entries, post, live_id, now)
        return

    container_id = ""
    if pending and pending["post"] == post.id:
        container_id = pending["container_id"]
        status = client.container_status(container_id)
        log.info("[%s] resuming %s: container %s is %s", account.name, post.id, container_id, status)
        if status != "FINISHED":
            container_id = ""
    if not container_id:
        container_id = create_container(client, post, args.repo, args.ref)
        # Persist before publishing: if publish errors, the next run resumes this container.
        state["pending"] = {"post": post.id, "container_id": container_id}
        write_state(account, state)

    media_id = finish_publish(client, container_id, post.caption)
    _record(account, state, entries, post, media_id, now)


def _record(
    account: Account, state: dict, entries: list[dict], post: Post, media_id: str, now: datetime
) -> None:
    entries.append(
        {
            "post": post.id,
            "media_id": media_id,
            "published_at": now.isoformat(timespec="seconds"),
            "local_date": now.astimezone(account.tz).date().isoformat(),
        }
    )
    state.pop("pending", None)
    write_state(account, state)
    log.info("[%s] published %s -> media %s", account.name, post.id, media_id)


def cmd_run(args: argparse.Namespace) -> int:
    args.repo = os.environ.get("GITHUB_REPOSITORY")
    args.ref = os.environ.get("GITHUB_SHA")
    if not args.dry_run and not (args.repo and args.ref):
        log.error("GITHUB_REPOSITORY and GITHUB_SHA must be set (they are in GitHub Actions)")
        return 2

    now = datetime.now(timezone.utc)
    failures = 0
    for account in load_accounts(args.account):
        try:
            _run_account(account, args, now)
        except RateLimitError as exc:
            # Not a failure: the pending container is kept and retried next hour.
            log.warning("[%s] %s; will retry on the next run", account.name, exc)
        except PublishError as exc:
            log.error("[%s] %s", account.name, exc)
            failures += 1
    return 1 if failures else 0


def cmd_check(args: argparse.Namespace) -> int:
    bad = 0
    for account in load_accounts(args.account):
        done = {e["post"] for e in read_log(account)}
        dirs = account.post_dirs()
        for folder in dirs:
            if problems := Post.load(folder).problems():
                bad += 1
                log.error("[%s] %s: %s", account.name, folder.name, "; ".join(problems))
        pending = sum(d.name not in done for d in dirs)
        log.info("[%s] %d posts, %d pending", account.name, len(dirs), pending)
    log.info("all posts look good" if not bad else f"{bad} post(s) need fixing")
    return 1 if bad else 0


def cmd_refresh_tokens(args: argparse.Namespace) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        log.error("GITHUB_REPOSITORY must be set")
        return 2
    failures = 0
    for account in load_accounts(args.account):
        token = os.environ.get(account.token_var)
        if not token:
            log.warning("[%s] %s not set, skipping", account.name, account.token_var)
            continue
        try:
            resp = requests.get(
                REFRESH_URL,
                params={"grant_type": "ig_refresh_token", "access_token": token},
                timeout=HTTP_TIMEOUT,
            )
            body = _json(resp)
            if not resp.ok or "access_token" not in body:
                msg = body.get("error", {}).get("message", resp.reason)
                raise PublishError(f"refresh failed: HTTP {resp.status_code}: {msg}")
            # Pass the token on stdin so it never appears in the process list.
            subprocess.run(
                ["gh", "secret", "set", account.token_var, "--repo", repo],
                input=body["access_token"],
                text=True,
                check=True,
                capture_output=True,
            )
        except requests.RequestException as exc:
            log.error("[%s] refresh failed: %s", account.name, type(exc).__name__)
            failures += 1
        except PublishError as exc:
            log.error("[%s] %s", account.name, exc)
            failures += 1
        except subprocess.CalledProcessError as exc:
            log.error("[%s] saving secret failed: %s", account.name, exc.stderr.strip())
            failures += 1
        else:
            days = int(body.get("expires_in", 0)) // 86400
            log.info("[%s] token refreshed, valid ~%d days", account.name, days)
    return 1 if failures else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--account", action="append", help="limit to this account folder (repeatable)"
    )

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="publish due carousels")
    run.add_argument("--dry-run", action="store_true", help="validate, don't publish")
    run.add_argument("--force", action="store_true", help="ignore the schedule, post next now")
    run.set_defaults(func=cmd_run)

    sub.add_parser("check", parents=[common], help="validate posts").set_defaults(func=cmd_check)
    sub.add_parser(
        "refresh-tokens", parents=[common], help="refresh access tokens"
    ).set_defaults(func=cmd_refresh_tokens)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
