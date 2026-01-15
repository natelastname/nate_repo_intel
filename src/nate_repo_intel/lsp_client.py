# -*- coding: utf-8 -*-
"""
Created on 2026-01-14T17:26:51-05:00

@author: nate
"""
import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from loguru import logger

KIND_LABELS = {
    2: "namespace",
    3: "package",
    4: "class",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    23: "type parameter",
}


@dataclass
class _DocState:
    version: int
    text: str


class LspClient:
    def __init__(self, cmd: list[str], root: Path) -> None:
        self.root = root.resolve()
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            cwd=str(self.root),
        )

        self._next_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()

        self._doc_state: dict[str, _DocState] = {}  # uri -> state

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        self.root_uri = self.root.as_uri()
        self._initialize()

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("LSP stdin closed")

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.proc.stdin.write(header)
        self.proc.stdin.write(body)
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout_s: float = 30.0) -> Any:
        req_id = self._next_request_id()
        q: queue.Queue[dict[str, Any]] = queue.Queue()

        with self._pending_lock:
            self._pending[req_id] = q

        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

        msg = q.get(timeout=timeout_s)
        if "error" in msg:
            raise RuntimeError(f"LSP error for {method}: {msg['error']}")
        return msg.get("result")

    def ensure_open(self, file_path: Path) -> str:
        uri = file_path.resolve().as_uri()
        if uri in self._doc_state:
            return uri

        text = file_path.read_text(encoding="utf-8")
        self._doc_state[uri] = _DocState(version=1, text=text)

        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )
        return uri

    def update_document(self, file_path: Path, *, text: str | None = None) -> str:
        uri = file_path.resolve().as_uri()
        if uri not in self._doc_state:
            return self.ensure_open(file_path)

        state = self._doc_state[uri]
        if text is None:
            text = file_path.read_text(encoding="utf-8")

        state.version += 1
        state.text = text

        # Full sync (simple + reliable)
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": state.version},
                "contentChanges": [{"text": text}],
            },
        )
        return uri

    def close_document(self, file_path: Path) -> None:
        uri = file_path.resolve().as_uri()
        if uri not in self._doc_state:
            return
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self._doc_state.pop(uri, None)

    def _reader_loop(self) -> None:
        if self.proc.stdout is None:
            return

        buf = self.proc.stdout
        while True:
            headers: dict[str, str] = {}
            while True:
                line = buf.readline()
                if not line:
                    return
                line = line.decode("ascii", errors="ignore").strip()
                if not line:
                    break
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            n_str = headers.get("content-length")
            if not n_str:
                continue

            n = int(n_str)
            body = buf.read(n)
            if not body:
                return

            msg = json.loads(body.decode("utf-8"))
            if not isinstance(msg, dict):
                continue

            if "id" in msg and ("result" in msg or "error" in msg):
                msg_id = msg["id"]
                with self._pending_lock:
                    q = self._pending.pop(msg_id, None)
                if q is not None:
                    q.put(msg)

    def _initialize(self) -> None:
        _ = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root_uri,
                "workspaceFolders": [{"uri": self.root_uri, "name": self.root.name}],
                "capabilities": {
                    "textDocument": {
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}
                    }
                },
            },
        )
        self.notify("initialized", {})

    def close(self) -> None:
        try:
            _ = self.request("shutdown", {}, timeout_s=5.0)
        except Exception:
            pass
        try:
            self.notify("exit", {})
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
