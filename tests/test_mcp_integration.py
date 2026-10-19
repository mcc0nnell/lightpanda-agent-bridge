#!/usr/bin/env python3
"""End-to-end check of the MCP bridge, persistent browser session and one-shot mode."""
from __future__ import annotations
import http.server
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import tempfile

HERE = Path(__file__).resolve().parent.parent
FIXTURE = (HERE / "sample.html").read_bytes()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            payload = FIXTURE
        elif self.path == "/second":
            payload = b'<html><head><title>Second page</title></head><body><h1 id="heading">Second page</h1></body></html>'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *args):
        pass

def main():
    web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{web.server_address[1]}"
    recording_dir = tempfile.TemporaryDirectory(prefix="lightpanda-mcp-test-")
    env = {**os.environ, "LIGHTPANDA_RUN_DIR": recording_dir.name}
    server = subprocess.Popen(
        [sys.executable, "-u", str(HERE / "lightpanda_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env,
    )
    events = []
    seq = 0
    def rpc(method, params=None):
        nonlocal seq
        seq += 1
        server.stdin.write((json.dumps({"jsonrpc":"2.0","id":seq,"method":method,"params":params or {}}) + "\n").encode())
        server.stdin.flush()
        ready, _, _ = select.select([server.stdout], [], [], 30)
        if not ready:
            raise TimeoutError(f"MCP response timeout: {method}")
        line = server.stdout.readline()
        if not line:
            raise RuntimeError("MCP server exited early")
        response = json.loads(line)
        assert response.get("id") == seq, response
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        return response["result"]
    def tool(name, args):
        response = rpc("tools/call", {"name":name, "arguments":args})
        data = json.loads(response["content"][0]["text"])
        events.append({"tool": name, "error": response.get("isError", False), "result": data})
        return data
    try:
        init = rpc("initialize", {"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"lightpanda-test","version":"1"}})
        assert init["serverInfo"]["name"] == "lightpanda-local"
        names = {item["name"] for item in rpc("tools/list")["tools"]}
        assert names == {"lightpanda_open","lightpanda_actions","lightpanda_status","lightpanda_close","lightpanda_run","lightpanda_record","lightpanda_replay"}
        opened = tool("lightpanda_open", {"url":url + "/"})
        assert opened["status"] == "RUNNING" and opened["socket_permissions"] == "0o600"
        assert opened["page"]["title"] == "Lightpanda On-Demand Demo"
        first = tool("lightpanda_actions", {"actions":[
            {"type":"fill","selector":"#q","value":"pipe"},
            {"type":"click","selector":"#go"},
            {"type":"extract","selector":"#items li"},
            {"type":"evaluate","expression":"document.title"}
        ]})
        assert first["status"] == "PASS" and first["actions"][2]["result"] == ["SignPipe", "TalkPipe"], first
        second = tool("lightpanda_actions", {"actions":[
            {"type":"fill","selector":"#q","value":"light"},
            {"type":"click","selector":"#go"},
            {"type":"text","selector":"#items li"}
        ]})
        assert second["status"] == "PASS" and second["actions"][-1]["result"] == ["Lightpanda"], second
        nav = tool("lightpanda_actions", {"actions":[
            {"type":"navigate","url":url + "/second"},
            {"type":"attribute","selector":"#heading","name":"id"},
            {"type":"extract","selector":"#heading"}
        ]})
        assert nav["status"] == "PASS" and nav["actions"][-1]["result"] == ["Second page"], nav
        status = tool("lightpanda_status", {})
        assert status["session_count"] == 1
        closed = tool("lightpanda_close", {})
        assert closed["status"] == "CLOSED" and closed["socket_removed"]
        once = tool("lightpanda_run", {"url":url + "/second","actions":[{"type":"evaluate","expression":"document.title"}]})
        assert once["status"] == "PASS" and once["actions"][0]["result"] == "Second page"
        recorded = tool("lightpanda_record", {"url": url + "/", "actions": [
            {"type":"fill","selector":"#q","value":"{{search}}"},
            {"type":"click","selector":"#go"},
            {"type":"extract","selector":"#items li"}
        ], "inputs": {"search": "pipe"}})
        assert recorded["outcome"] == "PASS" and recorded["required_replay_inputs"] == ["search"], recorded
        recording = (Path(recording_dir.name) / (recorded["receipt_id"] + ".json")).read_text()
        assert "SignPipe" not in recording and '"pipe"' not in recording, recording
        replayed = tool("lightpanda_replay", {"receipt_id": recorded["receipt_id"], "inputs": {"search": "pipe"}})
        assert replayed["outcome"] == "PASS", replayed
        mismatch = tool("lightpanda_replay", {"receipt_id": recorded["receipt_id"], "inputs": {"search": "light"}})
        assert mismatch["outcome"] == "FAIL", mismatch
        blocked = tool("lightpanda_replay", {"receipt_id": recorded["receipt_id"]})
        assert blocked["outcome"] == "BLOCKED", blocked
        invalid = rpc("tools/call", {"name":"lightpanda_open","arguments":{"url":"file:///etc/passwd"}})
        assert invalid.get("isError") and "Only absolute http" in invalid["content"][0]["text"]
        assert rpc("ping") == {}
        result = {
            "status":"PASS",
            "bridge_protocol":init["protocolVersion"],
            "tools":sorted(names),
            "persistent_session":True,
            "navigation_same_browser":True,
            "multi_turn_interaction":True,
            "one_shot":True,
            "unix_socket_cleanup":True,
            "invalid_url_rejected":True,
            "evidence_recorded":True,
            "replay_pass_fail_blocked":True,
            "no_raw_page_content_in_receipt":True,
            "results":events,
        }
        (HERE / "mcp-integration-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k:v for k,v in result.items() if k != "results"}, indent=2))
    finally:
        server.stdin.close()
        try:
            server.wait(timeout=6)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        stderr = server.stderr.read().decode(errors="replace")
        if stderr:
            print("MCP stderr:", stderr[-1200:])
        web.shutdown()
        web.server_close()
        recording_dir.cleanup()

if __name__ == "__main__":
    main()
