"""The reference client of `force api` (SPEC §54): the tests drive Force through it, and a frontend author can read
it as the smallest complete client. `ApiClient(cockpit)` runs the server in this process on pipes;
`ApiClient(argv=["force", "api"])` talks to a real one."""
from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import threading
import time


class ApiClient:
    def __init__(self, cockpit=None, argv=None, run=None, app_argv=None):
        self.ids = itertools.count(1)
        self.replies: dict[int, dict] = {}
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.cv = threading.Condition()
        if argv is not None:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
            self.w, r = self.proc.stdin, self.proc.stdout
        else:
            from force.api import Api
            self.proc = None
            r_in, w_in = os.pipe()
            r_out, w_out = os.pipe()
            self.w = os.fdopen(w_in, "w", buffering=1)
            server_in, server_out = os.fdopen(r_in, "r"), os.fdopen(w_out, "w", buffering=1)
            self.server = Api(cockpit, server_in, server_out, run=run, app_argv=app_argv)
            threading.Thread(target=self._serve, args=(server_out,), daemon=True).start()
            r = os.fdopen(r_out, "r")
        threading.Thread(target=self._read, args=(r,), daemon=True).start()

    def _serve(self, out):
        try:
            self.server.serve()
        finally:
            out.close()

    def _read(self, r):
        for line in r:
            msg = json.loads(line)
            if "event" in msg:
                self.events.put(msg)
            else:
                with self.cv:
                    self.replies[msg.get("id")] = msg
                    self.cv.notify_all()

    def call(self, op: str, timeout: float = 20, **fields) -> dict:
        """One request; the whole reply ({"id", "ok"} or {"id", "error"})."""
        rid = next(self.ids)
        self.w.write(json.dumps({"id": rid, "op": op, **fields}) + "\n")
        self.w.flush()
        end = time.monotonic() + timeout
        with self.cv:
            while rid not in self.replies:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError(op)
                self.cv.wait(left)
            return self.replies.pop(rid)

    def event(self, kind: str, timeout: float = 10, where=lambda e: True) -> dict:
        """The next event of `kind` that `where` accepts (others of that wait are dropped)."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(kind)
            try:
                e = self.events.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(kind)
            if e.get("event") == kind and where(e):
                return e

    def close(self):
        try:
            self.w.close()
        except OSError:
            pass
        if self.proc is not None:
            self.proc.wait(timeout=10)
