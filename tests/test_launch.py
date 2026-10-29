"""End-to-end test: local git repo -> launcher -> real MCP client over HTTP -> new commit -> auto-refresh.

Run with the Python that has graphifyy[mcp] installed:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import launch  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


async def mcp_call(url: str, key: str, tool: str, args: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {key}"}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                return "".join(getattr(c, "text", "") for c in result.content)


class UnitTests(unittest.TestCase):
    def test_config_problems(self):
        self.assertEqual(len(launch.Config({}).problems()), 2)
        self.assertIn("SSH", " ".join(launch.Config({"REPO_URL": "git@github.com:a/b.git", "GRAPHIFY_API_KEY": "k"}).problems()))
        self.assertEqual(launch.Config({"REPO_URL": "https://github.com/a/b", "GRAPHIFY_API_KEY": "k"}).problems(), [])

    def test_refresh_interval(self):
        self.assertEqual(launch.Config({"REFRESH_MINUTES": "30"}).refresh_seconds, 1800)
        self.assertEqual(launch.Config({"REFRESH_MINUTES": "0"}).refresh_seconds, 0)
        self.assertEqual(launch.Config({}).refresh_seconds, 3600)

    def test_token_never_leaks(self):
        self.assertEqual(launch.redact("fatal: https://user:ghp_secret@github.com/x denied ghp_secret", ["ghp_secret"]),
                         "fatal: https://***@github.com/x denied ***")
        cfg = launch.Config({"GIT_TOKEN": "ghp_secret"})
        args = launch.Git(cfg)._auth_args()
        self.assertEqual(args[0], "-c")
        self.assertNotIn("ghp_secret", args[1], "token is base64-encoded in a per-command header")


class EndToEnd(unittest.TestCase):
    def test_serves_graph_and_refreshes_after_a_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "src"
            src.mkdir()
            (src / "app.py").write_text("def handle_order(order):\n    return compute_total(order)\n\ndef compute_total(order):\n    return sum(order)\n")
            git(src, "init", "-q", "-b", "main")
            git(src, "add", "-A")
            git(src, "commit", "-qm", "first")

            port = free_port()
            env = {**os.environ, "REPO_URL": f"file://{src}", "GRAPHIFY_API_KEY": "secret-key", "PORT": str(port),
                   "DATA_DIR": str(tmp / "data"), "REFRESH_SECONDS": "3"}
            proc = subprocess.Popen([sys.executable, str(ROOT / "launch.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                url = f"http://127.0.0.1:{port}/mcp"
                deadline = time.time() + 60
                while True:
                    try:
                        urllib.request.urlopen(urllib.request.Request(url, method="POST", data=b"{}"), timeout=2)
                    except urllib.error.HTTPError as e:
                        self.assertEqual(e.code, 401, "without the key the server refuses")
                        break
                    except OSError:
                        if time.time() > deadline or proc.poll() is not None:
                            self.fail("server did not start: " + (proc.stdout.read() if proc.poll() is not None else ""))
                        time.sleep(0.5)

                stats1 = asyncio.run(mcp_call(url, "secret-key", "graph_stats", {}))
                hit = asyncio.run(mcp_call(url, "secret-key", "query_graph", {"question": "handle_order"}))
                self.assertIn("handle_order", hit)
                self.assertNotIn("ship_order", hit)

                # New commit: the refresh loop must pick it up and the server must reload without a restart.
                (src / "shipping.py").write_text("from app import handle_order\n\ndef ship_order(order):\n    return handle_order(order)\n")
                git(src, "add", "-A")
                git(src, "commit", "-qm", "second")
                deadline = time.time() + 60
                while True:
                    hit2 = asyncio.run(mcp_call(url, "secret-key", "query_graph", {"question": "ship_order"}))
                    if "ship_order" in hit2:
                        break
                    if time.time() > deadline:
                        self.fail("graph was not refreshed after the new commit")
                    time.sleep(1)
                stats2 = asyncio.run(mcp_call(url, "secret-key", "graph_stats", {}))
                self.assertNotEqual(stats1, stats2)
                self.assertIsNone(proc.poll(), "the server kept running through the refresh")
            finally:
                proc.terminate()
                out, _ = proc.communicate(timeout=20)
                self.log = out
            self.assertIn("graph ready:", out)
            self.assertIn("refresh: graph updated", out)
            self.assertNotIn("secret-key", out, "the API key never appears in logs")
            print("\n--- launcher log ---\n" + out)


if __name__ == "__main__":
    unittest.main()
