# Lightpanda Agent Bridge

A small **local MCP stdio bridge** for [Lightpanda](https://github.com/lightpanda-io/browser) over **Unix-domain CDP WebSockets**. It exposes seven tools for agents to open web pages, keep sessions alive, interact with the DOM, navigate, read status, and close browsers.

This is an independent integration project, **not a fork of the Lightpanda browser**. The runtime was tested against the upstream merge build of [PR #4023](https://github.com/lightpanda-io/browser/pull/4023), contributed via [mcc0nnell/browser](https://github.com/mcc0nnell/browser).

## Status

- **Local integration PASS**, October 18, 2026: actual Lightpanda process, local HTTP fixture, live CDP WebSocket, persistent session, fill/click/extract/navigate, 0600 Unix socket and clean shutdown.
- **External website navigation not validated** in the originating container: its DNS/network egress was restricted.
- **Not a ChatGPT-installed plugin.** This is a stdio MCP server. A separate supported remote MCP endpoint or Secure MCP Tunnel and user setup are required for direct ChatGPT tool availability.
- Binary **not committed**. Build provenance and executable SHA-256 are retained in [`evidence/run-manifest.json`](evidence/run-manifest.json).

## Install (Linux x86-64, glibc; Python 3.10+)

```bash
git clone https://github.com/mcc0nnell/lightpanda-agent-bridge.git
cd lightpanda-agent-bridge
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
# Requires GitHub CLI (gh), authenticated to download upstream Actions artifacts:
./scripts/install-pinned.sh
python3 tests/test_mcp_integration.py
```

The installer downloads the upstream successful GitHub Actions artifact for commit `f734e7f6aebc945fb8340284bfddd43244bf49ff`, checks the expected executable digest, and leaves it at `bin/lightpanda-merge-f734e7f6`. If GitHub expires the artifact, obtain or build a compatible Lightpanda binary and supply its path and expected SHA-256 via `LIGHTPANDA_BINARY` and `LIGHTPANDA_SHA256`. The bridge refuses to launch an unverified binary.

For local MCP stdio operation:

```bash
python3 -u lightpanda_mcp.py
```

See [`examples/mcp-config.example.json`](examples/mcp-config.example.json) for a stdio-capable MCP client's configuration; replace absolute paths.

## Evidence recorder (Milestone 0.2)

The bridge records browser actions as **sanitized, integrity-checked receipts** and can replay the same declarative workflow with newly supplied parameters. Results are `PASS` (all observed hashes match), `FAIL` (action failed or observation differs), or `BLOCKED` (cannot run or requires inputs). These are **not cryptographic signatures or independent proof** of website truth.

Two new MCP tools are `lightpanda_record` and `lightpanda_replay`:

```json
{"name":"lightpanda_record","arguments":{"url":"https://example.org/","actions":[{"type":"fill","selector":"#q","value":"{{search}}"},{"type":"click","selector":"#go"},{"type":"extract","selector":"#items li"}],"inputs":{"search":"example"}}}
```

The result includes a `receipt_id`. To verify the same workflow later:

```json
{"name":"lightpanda_replay","arguments":{"receipt_id":"<receipt-id-from-record>","inputs":{"search":"example"}}}
```

Records live in `~/.local/state/lightpanda-agent-bridge/runs/` (override with `LIGHTPANDA_RUN_DIR`). The directory and receipt files use permissions `0700` and `0600`. Receipts retain action types, selectors, URLs without query strings/fragments, hashed observations, timing, and a SHA-256 integrity checksum. Raw page text, raw JavaScript expressions, and supplied input values are **not persisted**. Plain literal fill values and JavaScript expressions are replaced with parameter placeholders; replay requires explicit values. URLs with query strings or fragments are also replaced with placeholders.

**Important privacy boundaries:** CSS selectors and URL paths without query strings can still carry sensitive information; avoid supplying secrets there. SHA-256 hashes of low-entropy outputs can be guessed, and an attacker with write access to a receipt can recalculate its checksum. Do not treat a receipt as an attestation, audit signature, or protection against a malicious local user. This tool must remain local/trusted until strict destination policy, authentication, isolation and approvals are implemented.

The recorder currently executes our own CDP action model; it does **not** yet integrate PandaScript or Chromium. Browser comparison and remote exposure are future milestones.

## Tools

| Tool | Purpose |
| --- | --- |
| `lightpanda_open` | Open HTTP(S) URL; preserve named session |
| `lightpanda_actions` | Execute ordered `navigate`, `fill`, `click`, `extract`, `text`, `attribute`, or `evaluate` actions |
| `lightpanda_status` | Inspect active sessions and browser status |
| `lightpanda_close` | Close a browser, remove its Unix socket |
| `lightpanda_run` | One-shot browser task with automatic cleanup |
| `lightpanda_record` | Record actions and a sanitized evidence receipt |
| `lightpanda_replay` | Replay a receipt; compare hashed observations |

Example `lightpanda_actions` tool arguments:

```json
{"session_name":"research","actions":[{"type":"fill","selector":"#q","value":"light"},{"type":"click","selector":"#go"},{"type":"extract","selector":"#items li"}]}
```

The `fill` and `click` operations execute DOM JavaScript, **not native user input**. Evaluate actions allow arbitrary JavaScript execution in the visited page; treat page content and output as untrusted. Allow only trusted agents to invoke this tool locally. This project does **not** offer authentication or multi-user isolation and should not be exposed as an unauthenticated public service.

## Verification

```bash
python3 -m py_compile lightpanda_mcp.py lightpanda_service.py lightpanda_evidence.py tests/test_mcp_integration.py
python3 -m unittest discover -s tests -p "test_*unit.py" -v
python3 tests/test_mcp_integration.py
```

The test launches a real local HTTP fixture and verifies the MCP JSON-RPC interface and browser lifecycle. It overwrites the local test result file (if present). Captured evidence from the original run is at [`evidence/mcp-integration-result.json`](evidence/mcp-integration-result.json); source run: [upstream GitHub Actions #39102731049](https://github.com/lightpanda-io/browser/actions/runs/39102731049).

## Boundaries

- Navigation is limited to HTTP(S), **but localhost and private/internal addresses are not blocked**; do not deploy this to untrusted callers, or treat the URL scheme check as SSRF protection.
- Unix socket mode `0600` limits other operating-system users but does not isolate same-UID processes.
- At most three concurrent sessions; at most 24 actions per call; response size capped.
- Requires a Lightpanda build with the `--unix-socket` flag. An older nightly predating the merge will not work.
- No saved browsing profiles, password storage, access-token injection, proxy bypass, or permanent ChatGPT registration.

License: Apache-2.0. See [LICENSE](LICENSE).