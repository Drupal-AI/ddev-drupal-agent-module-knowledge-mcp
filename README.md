# DDEV - Drupal Agent Module Knowledge (MCP)

A [DDEV](https://ddev.com) add-on that runs an **MCP server** serving an agent-consumable
knowledge base of ~10,300 Drupal contrib modules. Your AI client (Claude Code, Claude Desktop)
connects over HTTP and gets **hybrid search** (lexical BM25 + semantic) plus compact per-module
docs - so an agent can understand and operate a module without reading its source.

The knowledge base and its build method live in the companion repo
**[`agent-module-documentation`](https://github.com/Drupal-AI/agent-module-documentation)** (see its `mcp/DESIGN.md` for the full architecture).

## What it installs

- A small HTTP **MCP service** in a container, exposed through the DDEV router at
  `https://<project>.ddev.site:9131/mcp`.
- A prebuilt search database (`docs.db`, ~128 MB gzipped) downloaded from this repo's latest
  **GitHub Release** — no indexing or model download on your machine.

Tools exposed: `search_modules`, `get_module`, `list_docs`, `read_doc`, `list_categories`.

## Install

```bash
ddev add-on get Drupal-AI/ddev-drupal-agent-module-knowledge-mcp
ddev restart
```

Then register it with your AI client (runs on the host):

```bash
# Claude Code
claude mcp add --transport http agent-module-docs https://<project>.ddev.site:9131/mcp
```

```jsonc
// Claude Desktop — claude_desktop_config.json
{ "mcpServers": { "agent-module-docs": {
    "transport": "http",
    "url": "https://<project>.ddev.site:9131/mcp"
} } }
```

## Layout

```
install.yaml                              # DDEV add-on manifest (downloads the DB)
docker-compose.agent-module-docs.yaml     # the HTTP MCP service, router-exposed
server/
├── server.py         # hybrid MCP server (Streamable HTTP)
├── embed.py          # shared model2vec embedder (build == query)
├── indexer.py        # build-time: corpus -> docs.db
├── requirements.txt  # mcp, sqlite-vec, model2vec, numpy (no torch/onnx)
└── Dockerfile        # python:3.12-slim + PRE-BAKED model, runs offline
.github/workflows/release.yml             # build DB + image, publish release
tests/test.bats                           # add-on install + endpoint smoke test
```

## Security

- The service runs non-root; the DB is opened read-only; doc bodies are served from the DB
  (no filesystem traversal surface).
- The endpoint is published through the DDEV router (host-reachable, not public). Add a bearer
  token before exposing it beyond your machine.
