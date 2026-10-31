"""Keeping the scoreboard when the container doesn't.

A Hugging Face Space restarts whenever it's rebuilt, goes to sleep or is bumped, and takes its
filesystem with it. The game's scoreboard lives in one SQLite file, so this syncs that file to a
Hugging Face dataset: pulled once on boot, pushed every few minutes and once on the way out.

It's deliberately optional. Without `huggingface_hub` installed, or without PROBIE_DATASET and a
token in the environment, the game runs exactly as before and keeps its scores only for as long as
the container lives. Nothing here ever uploads a document or anything a player wrote: the scoreboard
holds outcomes, not content.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

ENV_REPO = "PROBIE_DATASET"
ENV_TOKEN = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
FILENAME = "game.sqlite"
EVERY_SECONDS = 300


def token() -> str | None:
    for name in ENV_TOKEN:
        value = os.environ.get(name)
        if value:
            return value
    return None


def repo() -> str | None:
    value = (os.environ.get(ENV_REPO) or "").strip()
    return value or None


def _api():
    try:
        from huggingface_hub import HfApi
    except ImportError:
        return None
    if not repo() or not token():
        return None
    return HfApi(token=token())


def why_off() -> str | None:
    """Why syncing isn't running, in a sentence, or None when it is."""
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        return "huggingface_hub isn't installed, so the scoreboard is only kept while this container lives."
    if not repo():
        return f"{ENV_REPO} isn't set, so the scoreboard is only kept while this container lives."
    if not token():
        return "No Hugging Face token is set, so the scoreboard is only kept while this container lives."
    return None


def looks_like_a_database(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return True
    except sqlite3.Error:
        return False


def pull(path: str | Path, *, out=print) -> bool:
    """Fetch the saved scoreboard, if there is one and the local file is empty. True if it landed."""
    api = _api()
    path = Path(path)
    if api is None or (path.exists() and path.stat().st_size > 0):
        return False
    try:
        from huggingface_hub import hf_hub_download
        got = hf_hub_download(repo_id=repo(), filename=FILENAME, repo_type="dataset", token=token())
    except Exception as exc:  # a missing file, no network, a bad token: all the same answer
        if out:
            out(f"No saved scoreboard to restore ({type(exc).__name__}). Starting fresh.")
        return False
    candidate = Path(got)
    if not looks_like_a_database(candidate):
        if out:
            out("The saved scoreboard didn't read as a database. Starting fresh.")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(candidate.read_bytes())
    if out:
        out(f"Restored the scoreboard from {repo()}.")
    return True


def push(path: str | Path, *, out=None) -> bool:
    """Save the scoreboard. True if it went up."""
    api = _api()
    path = Path(path)
    if api is None or not path.exists():
        return False
    try:
        api.create_repo(repo_id=repo(), repo_type="dataset", private=True, exist_ok=True)
        api.upload_file(path_or_fileobj=str(path), path_in_repo=FILENAME, repo_id=repo(), repo_type="dataset",
                        commit_message="Probie scoreboard")
        return True
    except Exception as exc:
        if out:
            out(f"Couldn't save the scoreboard ({type(exc).__name__}).")
        return False


class Keeper:
    """Pushes the scoreboard on a timer, and once more when the server stops."""

    def __init__(self, path: str | Path, every: float = EVERY_SECONDS, out=None):
        self.path, self.every, self.out = Path(path), every, out
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.last = 0.0

    @property
    def on(self) -> bool:
        return _api() is not None

    def start(self) -> None:
        if not self.on or self.thread:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stopping.wait(self.every):
            self.save()

    def save(self) -> bool:
        if not self.on:
            return False
        went = push(self.path, out=self.out)
        if went:
            self.last = time.time()
        return went

    def stop(self) -> None:
        self.stopping.set()
        self.save()
