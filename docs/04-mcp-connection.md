# MCP connection guide (Phase 7)

PaperCLI ships a stdio Model-Context-Protocol (MCP) server so an
external agent session can drive the same core functions the `papers`
CLI exposes -- without re-implementing the contract.

## What it is

* Command: `papers mcp serve` (or `uv run papers mcp serve` from the
  project root).
* Transport: stdio, newline-delimited JSON-RPC 2.0 (MCP `initialize` /
  `initialized` / `tools/list` / `tools/call`). No third-party MCP
  dependency -- a small pure-Python implementation in
  `src/papercli/agent_contract/mcp_server.py`.
* Tool surface = the CLI contract, 1:1. Each tool converts its
  arguments to CLI argv and dispatches through the same `main()` the
  console command uses. The tool result is the CLI's JSON envelope
  (status ok/error, error codes, warnings). Because the server wraps
  the CLI handlers rather than re-declaring schemas, the tool surface
  cannot drift from the CLI contract.

### Tools (1:1 with CLI commands)

| MCP tool | CLI command it wraps |
|---|---|
| `search` | `papers search "Q" --providers ... --limit N` |
| `resolve` | `papers resolve "IDENTIFIER"` |
| `fetch` | `papers fetch "IDENTIFIER" --out ...` |
| `read` | `papers read FILE --pages ... --max-chars ...` |
| `ingest` | `papers ingest FILE --run RUN_ID` |
| `review_plan` | `papers review plan --brief ... --out ...` |
| `review_run` | `papers review run --plan ... [--detach]` |
| `review_status` | `papers review status RUN_ID` |
| `review_logs` | `papers review logs RUN_ID [--follow]` |
| `review_resume` | `papers review resume RUN_ID [--detach] [--brief ...]` |
| `review_cancel` | `papers review cancel RUN_ID` |
| `review_export` | `papers review export RUN_ID [--formats ...]` |
| `task_next` | `papers task next --run ... --worker ...` |
| `task_submit` | `papers task submit TASK_ID --input ...` |
| `evidence_list` | `papers evidence list --run ...` |
| `citations_audit` | `papers citations audit --run ...` |
| `doctor` | `papers doctor [--skip-network]` |
| `schema` | `papers schema [--list] [--validate ...]` |

## Connecting an agent session

Launch the server as a stdio child and speak the protocol over its
stdin/stdout. The server is a LONG-LIVED child: it blocks on stdin and
exits 0 when the parent closes the stream (the normal way an agent
session ends the child).

```python
import subprocess, json

# Run in the project root so `papers` resolves and the `.papercli`
# storage dir is found.
proc = subprocess.Popen(
    ["papers", "mcp", "serve"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, cwd=r".")

def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()

def read():
    return json.loads(proc.stdout.readline())

# 1. Handshake (MCP initialize).
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05"}})
print(read())                     # serverInfo + negotiated version

# 2. Notification (no response).
send({"jsonrpc": "2.0", "method": "initialized"})

# 3. Discover the tools.
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
print(read()["result"]["tools"])

# 4. Call a tool (offline path: schema list).
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "schema", "arguments": {"list": True}}})
resp = read()
envelope = json.loads(resp["result"]["content"][0]["text"])
print(envelope["status"])         # "ok"
```

### Reading a tool result

* `result.content[0].text` is the CLI JSON envelope string. Parse it
  and read `status` (`ok` / `error`), `data`, `error.code`,
  `warnings`.
* `result.isError` is `true` when the wrapped CLI call errored
  (an error envelope, or a nonzero exit with no envelope).
* A call to an UNKNOWN tool returns an error envelope with
  `error.code == "UNKNOWN_TOOL"` (isError true) -- never a crash.
* A call to an unknown JSON-RPC method returns a JSON-RPC error
  (`-32601 Method not found`); a malformed line returns `-32700
  parse error`.

## What the tools do / do not do

* `doctor` and `schema` run fully offline and are safe in tests.
* `search` / `resolve` / `fetch` still need the providers' network +
  credentials exactly as the CLI does (keyless Semantic Scholar degrades
  to a recorded skip, never a run failure). The server adds no network
  of its own.
* `review run --detach` launching a worker is NOT claiming the review
  is complete; inspect `review_status` for the actual state.
* `review_export` records a missing renderer dependency as
  `generation_status=missing` with a reason; it never silently omits a
  "completed" artifact.

## Security notes (spec 16)

* Tool results carry only the CLI's JSON envelopes; the CLI already
  redacts secrets, so the server adds no secret channel.
* Document content is untrusted data: the server never executes content
  found in papers, runs no browser automation, and sends no
  source documents to an undeclared endpoint.
* The server is a local stdio child; it makes no outbound connections
  itself.
