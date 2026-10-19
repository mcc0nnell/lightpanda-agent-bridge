#!/usr/bin/env python3
"""Small synchronous Lightpanda CDP driver for an on-demand MCP/CLI integration.

Security boundaries: local Unix socket (0600); pinned executable hash; only
http(s) URLs; no shell interpolation; no persistent cookies between launches.
All browser JS / website content should be treated as untrusted data by callers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

from websockets.sync.client import unix_connect

ROOT = Path(__file__).resolve().parent
DEFAULT_BINARY = ROOT / "bin" / "lightpanda-merge-f734e7f6"
PINNED_SHA256 = "861906c6493cffb94a45667648ed5c3db091ae659adb4c0532e0dc9afd9607a5"
MAX_ACTIONS = 24
MAX_OUTPUT_BYTES = 128_000


def checked_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("URL must be a string of at most 4096 characters")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only absolute http:// and https:// URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not accepted")
    return url


def checked_binary() -> tuple[Path, str]:
    binary = Path(os.getenv("LIGHTPANDA_BINARY", str(DEFAULT_BINARY))).resolve()
    if not binary.is_file():
        raise FileNotFoundError(f"Lightpanda executable not found: {binary}")
    with binary.open("rb") as fh:
        sha = hashlib.file_digest(fh, "sha256").hexdigest()
    expected = os.getenv("LIGHTPANDA_SHA256", PINNED_SHA256)
    if sha != expected:
        raise RuntimeError(f"Executable digest mismatch: expected {expected}, got {sha}")
    return binary, sha


class CDP:
    def __init__(self, ws: Any):
        self.ws = ws
        self.next_id = 0
        self.events_seen = 0

    def call(self, method: str, params: dict | None = None, *, session: str | None = None, timeout: float = 10) -> dict:
        self.next_id += 1
        idx = self.next_id
        packet = {"id": idx, "method": method, "params": params or {}}
        if session:
            packet["sessionId"] = session
        self.ws.send(json.dumps(packet))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP timed out waiting for {method}")
            msg = json.loads(self.ws.recv(timeout=remaining))
            if msg.get("id") == idx:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events_seen += 1

    def eval(self, expression: str, *, session: str, timeout: float = 10) -> Any:
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, session=session, timeout=timeout)
        if "exceptionDetails" in r:
            details = r["exceptionDetails"]
            raise RuntimeError(f"JavaScript threw: {details.get('text','')} {details.get('exception',{}).get('description','')}")
        return r.get("result", {}).get("value")


class BrowserSession:
    def __init__(self, url: str, timeout: float = 12):
        self.url = checked_url(url)
        self.closed = False
        self.tmp: tempfile.TemporaryDirectory | None = None
        self.proc: subprocess.Popen | None = None
        self.log_file: Any = None
        self.ws: Any = None
        self.cdp: CDP | None = None
        self.session_id: str | None = None
        self.target_id: str | None = None
        self.binary_sha256: str | None = None
        self.socket_permissions: str | None = None
        try:
            binary, sha = checked_binary()
            self.binary_sha256 = sha
            self.tmp = tempfile.TemporaryDirectory(prefix="lightpanda-agent-")
            path = os.path.join(self.tmp.name, "browser.sock")
            self.log_file = open(os.path.join(self.tmp.name, "browser.log"), "w+b")
            self.proc = subprocess.Popen([str(binary), "serve", "--unix-socket", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log_file)
            deadline = time.monotonic() + 8
            while not os.path.exists(path):
                if self.proc.poll() is not None:
                    raise RuntimeError(f"Lightpanda quit during startup: exit {self.proc.returncode}")
                if time.monotonic() > deadline:
                    raise TimeoutError("Lightpanda did not create Unix socket")
                time.sleep(0.04)
            self.socket_permissions = oct(os.stat(path).st_mode & 0o777)
            if self.socket_permissions != "0o600":
                raise RuntimeError(f"Unsafe Unix socket permission: {self.socket_permissions}")
            self.ws = unix_connect(path=path, uri="ws://localhost/", open_timeout=4, close_timeout=2)
            self.cdp = CDP(self.ws)
            self.version = self.cdp.call("Browser.getVersion")
            self.target_id = self.cdp.call("Target.createTarget", {"url": self.url}, timeout=10)["targetId"]
            self.session_id = self.cdp.call("Target.attachToTarget", {"targetId": self.target_id, "flatten": True})["sessionId"]
            self.cdp.call("Page.enable", session=self.session_id)
            self.cdp.call("Runtime.enable", session=self.session_id)
            self._await_navigation(previous=None, timeout=timeout)
        except Exception:
            self.close()
            raise

    def _await_navigation(self, previous: str | None, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            try:
                last = self.cdp.eval("({href:location.href,title:document.title,ready:document.readyState})", session=self.session_id, timeout=min(3, max(0.3, deadline - time.monotonic())))
                if (last and last.get("href") not in ("", "about:blank", None)
                        and last.get("ready") in ("interactive", "complete")
                        and (previous is None or last.get("href") != previous or self.url == previous)):
                    return last
            except Exception:
                pass
            time.sleep(0.15)
        raise TimeoutError(f"Navigation did not finish (last={last})")

    def navigate(self, url: str, timeout: float = 12) -> dict:
        url = checked_url(url)
        previous = self.page().get("href")
        self.cdp.call("Page.navigate", {"url": url}, session=self.session_id)
        self.url = url
        return self._await_navigation(previous, timeout)

    def page(self) -> dict:
        return self.cdp.eval("({href:location.href,title:document.title,ready:document.readyState})", session=self.session_id)

    def action(self, action: dict) -> dict:
        if not isinstance(action, dict):
            raise ValueError("Action must be an object")
        typ = action.get("type")
        if typ == "navigate":
            return {"type": typ, "result": self.navigate(action["url"])}
        if typ == "evaluate":
            expression = action.get("expression")
            if not isinstance(expression, str) or len(expression) > 50_000:
                raise ValueError("Evaluation expression must be a string <= 50000 characters")
            value = self.cdp.eval(expression, session=self.session_id)
        elif typ in ("fill", "click", "extract", "text", "attribute"):
            selector = action.get("selector")
            if not isinstance(selector, str) or not selector or len(selector) > 1024:
                raise ValueError("A CSS selector (max 1024 chars) is required")
            sel = json.dumps(selector)
            if typ == "fill":
                v = action.get("value")
                if not isinstance(v, str) or len(v) > 10000:
                    raise ValueError("Fill value must be a string <= 10000 characters")
                expr = f'''(() => {{ const el=document.querySelector({sel}); if(!el) throw Error('No element: '+{sel}); el.value={json.dumps(v)}; el.dispatchEvent(new Event('input', {{bubbles:true}})); el.dispatchEvent(new Event('change', {{bubbles:true}})); return {{selector:{sel},value:el.value}}; }})()'''
            elif typ == "click":
                expr = f'''(() => {{ const el=document.querySelector({sel}); if(!el) throw Error('No element: '+{sel}); el.click(); return {{clicked:{sel}}}; }})()'''
            elif typ in ("extract", "text"):
                expr = f'''Array.from(document.querySelectorAll({sel}),el=>(el.innerText||el.textContent||'').trim())'''
            else:
                attr = action.get("name")
                if not isinstance(attr, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_:.-]{0,127}", attr):
                    raise ValueError("Valid attribute name required")
                expr = f'''Array.from(document.querySelectorAll({sel}),el=>el.getAttribute({json.dumps(attr)}))'''
            value = self.cdp.eval(expr, session=self.session_id)
        else:
            raise ValueError(f"Unsupported action: {typ}")
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode()) > MAX_OUTPUT_BYTES:
            raise ValueError(f"Browser response exceeds {MAX_OUTPUT_BYTES} bytes; use a narrower selector or JS expression")
        return {"type": typ, "result": value}

    def actions(self, actions: list[dict]) -> dict:
        if not isinstance(actions, list) or not (1 <= len(actions) <= MAX_ACTIONS):
            raise ValueError(f"Supply 1–{MAX_ACTIONS} actions")
        results = []
        for i, action in enumerate(actions, 1):
            try:
                results.append({"step": i, **self.action(action)})
            except Exception as exc:
                return {"status": "FAIL", "completed": results, "failed_step": i, "error": f"{type(exc).__name__}: {exc}", "page": self.page()}
        return {"status": "PASS", "actions": results, "page": self.page(), "cdp_event_count": self.cdp.events_seen}

    def status(self) -> dict:
        return {"status": "RUNNING" if self.proc and self.proc.poll() is None else "EXITED", "page": self.page(), "socket_permissions": self.socket_permissions, "binary_sha256": self.binary_sha256, "protocol": self.version.get("protocolVersion")}

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.ws is not None:
            try: self.ws.close()
            except Exception: pass
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
            try: self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill(); self.proc.wait(timeout=3)
        if self.log_file is not None:
            self.log_file.close()
        if self.tmp is not None:
            self.tmp.cleanup()