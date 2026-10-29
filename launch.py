"""Graphify team MCP server for Railway.

Clones a Git repository, builds a code-only Graphify knowledge graph (local AST,
no LLM key needed), serves it over MCP Streamable HTTP behind an API key, and
keeps it fresh: every REFRESH_MINUTES it pulls the repository and rebuilds the
graph when the commit changed. The Graphify server reloads graph.json on its own
when the file changes, so clients never lose their connection.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

LOG_PREFIX = "[graphify-railway]"


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


class Config:
    def __init__(self, env: dict[str, str]):
        self.repo_url = env.get("REPO_URL", "").strip()
        self.branch = env.get("REPO_BRANCH", "").strip()
        self.token = env.get("GIT_TOKEN", "").strip()
        self.git_username = env.get("GIT_USERNAME", "").strip() or "x-access-token"
        self.api_key = env.get("GRAPHIFY_API_KEY", "").strip()
        self.port = int(env.get("PORT", "8080") or 8080)
        self.data_dir = Path(env.get("DATA_DIR", "/data"))
        refresh = env.get("REFRESH_SECONDS") or str(int(float(env.get("REFRESH_MINUTES", "60") or 60) * 60))
        self.refresh_seconds = max(0, int(float(refresh)))
        self.repo_dir = self.data_dir / "repo"
        self.build_dir = self.data_dir / "build"
        self.graph_path = self.data_dir / "graph.json"

    def problems(self) -> list[str]:
        out = []
        if not self.repo_url:
            out.append("REPO_URL is required, e.g. https://github.com/your-org/your-repo")
        elif urlparse(self.repo_url).scheme not in ("https", "http", "file"):
            out.append("REPO_URL must be an https:// URL (SSH URLs are not supported)")
        if not self.api_key:
            out.append("GRAPHIFY_API_KEY is required: the server is public, the key keeps your code map private")
        return out


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


class Git:
    """Runs git with the token passed as an HTTP header for each command only,
    so it is never written to .git/config on the volume or shown in logs."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _auth_args(self) -> list[str]:
        if not self.cfg.token:
            return []
        basic = base64.b64encode(f"{self.cfg.git_username}:{self.cfg.token}".encode()).decode()
        return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    def run(self, *args: str, cwd: Path | None = None) -> str:
        cmd = ["git", *self._auth_args(), *args]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            err = redact((proc.stderr or proc.stdout).strip(), [self.cfg.token])
            raise RuntimeError(f"git {args[0]} failed: {err[-600:]}")
        return proc.stdout.strip()

    def sync(self) -> str:
        """Clones or fast-forwards the repository; returns the checked-out commit."""
        repo = self.cfg.repo_dir
        if (repo / ".git").exists():
            ref = self.cfg.branch or "HEAD"
            self.run("fetch", "--depth", "1", "--quiet", "origin", ref, cwd=repo)
            self.run("reset", "--hard", "--quiet", "FETCH_HEAD", cwd=repo)
            self.run("clean", "-fdxq", cwd=repo)
        else:
            if repo.exists():
                shutil.rmtree(repo)
            args = ["clone", "--depth", "1", "--quiet"]
            if self.cfg.branch:
                args += ["--branch", self.cfg.branch]
            self.run(*args, self.cfg.repo_url, str(repo))
        return self.run("rev-parse", "HEAD", cwd=repo)


def build_graph(cfg: Config) -> str:
    """Builds the graph into the build dir, then atomically swaps the served graph.json."""
    started = time.time()
    env = {**os.environ, "GRAPHIFY_FORCE": "1"}  # deleted code may legitimately shrink the graph
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(cfg.repo_dir), "--code-only", "--out", str(cfg.build_dir)],
        env=env, capture_output=True, text=True, timeout=3600,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    built = cfg.build_dir / "graphify-out" / "graph.json"
    if proc.returncode != 0 or not built.exists():
        raise RuntimeError(f"graphify extract failed: {output.strip()[-800:]}")
    tmp = cfg.graph_path.with_suffix(".json.tmp")
    shutil.copyfile(built, tmp)
    os.replace(tmp, cfg.graph_path)
    summary = re.search(r"(\d+) nodes, (\d+) edges(?:, (\d+) communities)?", output)
    stats = f"{summary.group(1)} nodes, {summary.group(2)} edges" if summary else "graph built"
    return f"{stats} in {time.time() - started:.1f}s"


def start_server(cfg: Config) -> subprocess.Popen:
    # The API key goes through the environment, not the command line, so it does not show up in `ps`.
    env = {**os.environ, "GRAPHIFY_API_KEY": cfg.api_key}
    return subprocess.Popen(
        [sys.executable, "-m", "graphify.serve", str(cfg.graph_path),
         "--transport", "http", "--host", "0.0.0.0", "--port", str(cfg.port)],
        env=env,
    )


def refresh_loop(cfg: Config, git: Git, head: str, stop: threading.Event) -> None:
    while not stop.wait(cfg.refresh_seconds):
        try:
            new_head = git.sync()
            if new_head == head:
                log(f"refresh: no new commit ({head[:8]})")
                continue
            log(f"refresh: {head[:8]} -> {new_head[:8]}, rebuilding")
            log(f"refresh: graph updated, {build_graph(cfg)}")
            head = new_head
        except Exception as exc:  # keep serving the last good graph
            log(f"refresh failed, still serving the previous graph: {exc}")


def main() -> int:
    cfg = Config(dict(os.environ))
    problems = cfg.problems()
    if problems:
        for p in problems:
            log(f"setup: {p}")
        log("fix the variables in Railway (service > Variables), the service will restart")
        return 1

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    git = Git(cfg)
    log(f"syncing {redact(cfg.repo_url, [cfg.token])}{' (branch ' + cfg.branch + ')' if cfg.branch else ''}")
    try:
        head = git.sync()
        log(f"checked out {head[:8]}, building the code graph (local AST, no LLM)")
        log(f"graph ready: {build_graph(cfg)}")
    except Exception as exc:
        if cfg.graph_path.exists():
            head = ""
            log(f"could not refresh at startup, serving the graph from the volume: {exc}")
        else:
            log(f"startup failed: {exc}")
            return 1

    server = start_server(cfg)
    log(f"MCP server listening on port {cfg.port}, endpoint /mcp (Authorization: Bearer <GRAPHIFY_API_KEY>)")

    stop = threading.Event()
    if cfg.refresh_seconds > 0:
        threading.Thread(target=refresh_loop, args=(cfg, git, head, stop), daemon=True).start()
        log(f"auto-refresh every {cfg.refresh_seconds // 60 or cfg.refresh_seconds} {'min' if cfg.refresh_seconds >= 60 else 's'}")

    def shutdown(signum, _frame):
        log(f"signal {signum}, stopping")
        stop.set()
        server.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    code = server.wait()
    stop.set()
    return code


if __name__ == "__main__":
    sys.exit(main())
