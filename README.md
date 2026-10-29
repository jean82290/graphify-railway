# Graphify Team MCP Server on Railway

Give your whole team, and every AI coding assistant they use, one shared, always-up-to-date knowledge graph of your codebase.

This template deploys [Graphify](https://github.com/Graphify-Labs/graphify) as a hosted MCP server: it clones your repository, builds a code graph with Graphify (local AST parsing, **no LLM API key needed**), serves it over MCP Streamable HTTP behind an API key, and rebuilds it automatically when new commits land.

> Unofficial deployment wrapper. Not affiliated with Graphify Labs. Graphify is Apache-2.0 licensed by its authors.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/graphify)

## How it works

```
your Git repo ──clone/pull──▶ graphify extract --code-only ──▶ graph.json (volume)
                                                                   │  reloaded on change
Claude Code / Cursor / Codex ──HTTPS + API key──▶ /mcp  ◀── graphify.serve (HTTP)
```

1. On start, the service clones `REPO_URL` (public, or private with `GIT_TOKEN`) onto a Railway volume.
2. It builds the graph with `graphify extract --code-only`: tree-sitter AST, fully offline.
3. It starts `graphify.serve` over Streamable HTTP on `/mcp`. Every request needs `Authorization: Bearer <GRAPHIFY_API_KEY>`.
4. Every `REFRESH_MINUTES` it pulls the repository. If the commit changed, it rebuilds and swaps `graph.json` atomically; the server reloads it on the next request, so connected clients keep working.

## Setup

1. Deploy the template and set `REPO_URL`, for example `https://github.com/your-org/your-repo`.
2. Private repository: create a read-only token and set it as `GIT_TOKEN`. On GitHub: Settings → Developer settings → Fine-grained tokens → only this repository → Contents: Read-only.
3. Copy the service URL and `GRAPHIFY_API_KEY` (Railway → service → Variables), then connect your assistant.

### Claude Code

```bash
claude mcp add --transport http graphify https://YOUR-SERVICE.up.railway.app/mcp \
  --header "Authorization: Bearer YOUR_GRAPHIFY_API_KEY"
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "graphify": {
      "url": "https://YOUR-SERVICE.up.railway.app/mcp",
      "headers": { "Authorization": "Bearer YOUR_GRAPHIFY_API_KEY" }
    }
  }
}
```

Tools exposed by Graphify: `query_graph`, `get_node`, `get_neighbors`, `get_community`, `god_nodes`, `graph_stats`, `shortest_path`, `list_prs`, `get_pr_impact`, `triage_prs`.

## Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `REPO_URL` | yes | | HTTPS URL of the repository to map. |
| `GRAPHIFY_API_KEY` | yes | generated | Key every MCP client must send. |
| `GIT_TOKEN` | private repos | | Read-only token. Sent per command as an HTTP header, never stored in `.git/config` or logged. |
| `GIT_USERNAME` | no | `x-access-token` | Username paired with the token (use `oauth2` for GitLab). |
| `REPO_BRANCH` | no | default branch | Branch to map. |
| `REFRESH_MINUTES` | no | `60` | How often to pull and rebuild. `0` disables. |

The graph is stored on a volume mounted at `/data`, so a restart serves the last good graph immediately even if the repository is temporarily unreachable. If a refresh fails, the previous graph stays online.

## Security notes

- The server is reachable on the internet; the API key is what keeps your code map private. Rotate it by changing `GRAPHIFY_API_KEY` and redeploying.
- The graph contains names and relationships from your source code, not the full source text, but treat it as confidential if your code is.
- Use a token limited to read access on a single repository.

## Updating Graphify

Graphify releases very often. The version is pinned in the `Dockerfile` (`GRAPHIFY_VERSION`); bump it and redeploy to upgrade.

## Development

```bash
pip install "graphifyy[mcp]==0.9.97"
python -m unittest discover -s tests -v   # builds a real graph, talks MCP over HTTP, checks auto-refresh
```

## License

MIT for this wrapper. Graphify is Apache-2.0.
