# SIRE MCP Server

A custom MCP (Model Context Protocol) server that exposes the SIRE multi-strategy Azure AI Search logic as MCP tools. This lets any MCP-compatible client (VS Code Copilot, Claude Desktop, etc.) invoke the same search pipeline used by the SIRE Voice Agent.

## Architecture

```
┌──────────────────────┐       ┌──────────────────────┐
│  MCP Client          │       │  Direct Python       │
│  (Copilot / Claude)  │       │  (main.py / voice)   │
│                      │       │                      │
│  search_user ────────┤       ├──── search_user      │
│  search_group ───────┤       ├──── search_group     │
│  search_any ─────────┤       │                      │
│  compare_search ─────┤       │                      │
│  search_diagnostics ─┤       │                      │
│  get_index_info ─────┤       │                      │
└─────────┬────────────┘       └──────────┬───────────┘
          │                               │
          └───────────┬───────────────────┘
                      ▼
         ┌─────────────────────────┐
         │  search_client.py       │
         │  SIRESearchClient       │
         │  (multi-strategy RRF)   │
         └────────────┬────────────┘
                      ▼
         ┌─────────────────────────┐
         │  Azure AI Search        │
         │  group-slot-mapping     │
         │  user-slot-mapping      │
         └─────────────────────────┘
```

Both paths share the **exact same** `SIRESearchClient` and `SearchConfig` — the MCP server is a thin protocol wrapper, not a reimplementation.

## Tools

| Tool | Description |
|------|-------------|
| `search_group` | Multi-strategy group search (exact, fuzzy, number-normalisation) |
| `search_user` | Multi-strategy user search (exact, fuzzy, phonetic, field-targeted) |
| `search_any` | Search both indexes simultaneously |
| `search_diagnostics` | Full diagnostic output with strategy weights and scoring config |
| `compare_search` | Run each strategy independently to A/B test individual contributions |
| `get_index_info` | Show configured indexes, fields, and analyzers |

## Resources

| URI | Description |
|-----|-------------|
| `sire://config/search` | Current search endpoint and index configuration |
| `sire://config/scoring` | Scoring weights and confidence thresholds |

## Setup

```bash
# Install dependencies (from project root)
pip install -r mcp_server/requirements.txt
```

## Running

### Stdio (for VS Code / Copilot Chat)

Already configured in `.vscode/mcp.json`. Just open VS Code and the server will start automatically when an MCP client connects.

```bash
# Manual test
python -m mcp_server
```

### SSE (for remote / multi-client)

```bash
python -m mcp_server --sse --port 8080
```

## Comparison Testing

Verify the MCP server returns identical results to the direct Python client:

```bash
python test_mcp_vs_direct.py
```

## Environment Variables

Same as the main project — loaded from `.env`:

| Variable | Required | Default |
|----------|----------|---------|
| `AZURE_SEARCH_ENDPOINT` | Yes | — |
| `AZURE_SEARCH_API_KEY` | Yes | — |
| `AZURE_SEARCH_GROUP_INDEX` | No | `group-slot-mapping-index` |
| `AZURE_SEARCH_USER_INDEX` | No | `user-slot-mapping-index` |
| `AZURE_SEARCH_API_VERSION` | No | `2024-07-01` |
| `MCP_SERVER_PORT` | No | `8080` |
