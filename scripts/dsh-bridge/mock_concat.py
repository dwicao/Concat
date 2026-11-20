#!/usr/bin/env python3
"""A stand-in Concat window for testing the bridge without the editor.

It speaks exactly what `concat-server` speaks — one `auth` line, then one
JSON-RPC request per connection, one JSON line back — and answers the few
methods the bridge uses. Every request it serves is appended to a record file
as one JSON object, so a test can check what the bridge actually sent.

    python3 mock_concat.py --port 0 --token test-token --record /tmp/seen.jsonl

The chosen port is printed on the first line, so a caller can read it.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading

#: A real 1x1 PNG, so preview_frame returns something decodable.
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
       "IQAAAABJRU5ErkJggg==")

VERSION = {
    "api": "0.2",
    "concat": "0.0.0-mock",
    "capabilities": ["events"],
    "dirs": {"config": "/tmp/concat", "cache": "/tmp/concat-cache", "data": "/tmp/concat",
             "state": "/tmp/concat-state"},
}

PROJECT = {
    "name": "Mock",
    "path": "/tmp/Mock",
    "settings": {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1},
    "tracks": [{"id": "t1", "kind": "video"}],
    "media": [],
    "clips": {},
    "timelines": [],
}

SUMMARY = {
    "path": "/tmp/Mock",
    "name": "Mock",
    "revision": 1,
    "createdId": "c1",
    "changed": ["clips"],
    "timeline": "tl1",
}


def answer(method, params):
    if method == "version":
        return VERSION
    if method == "project.get":
        return PROJECT
    if method in ("project.create", "project.open", "project.list", "project.save",
                  "project.close", "project.setVideo", "edit.undo", "edit.redo"):
        if method == "project.list":
            return {"projects": [PROJECT]}
        return SUMMARY
    if method == "media.import":
        return {"path": "/tmp/Mock", "item": {"id": "m1", "name": "a.mp4", "kind": "video"}}
    if method == "media.probe":
        return {"duration": 4.0, "video": {"width": 1920, "height": 1080}}
    if method == "edit.apply":
        return SUMMARY
    if method == "catalogue.list":
        return {"effects": [{"id": "concat.warm", "name": "Warm"}]}
    if method == "template.list":
        return {"templates": []}
    if method == "export.run":
        return {"job": "job-1", "output": params.get("output")}
    if method == "export.status":
        return {"job": params.get("job"), "state": "done", "stage": "done", "fraction": 1.0}
    if method == "export.cancel":
        return {"job": params.get("job"), "state": "cancelled"}
    if method == "preview.frame":
        return {"png": PNG, "width": params.get("width", 320), "height": params.get("height", 180),
                "time": params.get("time")}
    return None


def serve_once(conn, token, record):
    with conn, open(record, "a", encoding="utf-8") as log:
        buffer = b""
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buffer += chunk
        line, _, rest = buffer.partition(b"\n")
        try:
            hello = json.loads(line)
        except ValueError:
            return
        presented = hello.get("params", {}).get("token", hello.get("token", ""))
        if presented != token:
            conn.sendall(json.dumps({
                "jsonrpc": "2.0", "id": hello.get("id"),
                "error": {"code": -32001, "message": "wrong token"},
            }).encode() + b"\n")
            return
        conn.sendall(json.dumps({"jsonrpc": "2.0", "id": hello.get("id"),
                                 "result": {"ok": True}}).encode() + b"\n")

        request = rest
        while b"\n" not in request:
            chunk = conn.recv(65536)
            if not chunk:
                return
            request += chunk
        line, _, _ = request.partition(b"\n")
        try:
            call = json.loads(line)
        except ValueError:
            return
        log.write(json.dumps(call) + "\n")
        log.flush()
        method = call.get("method")
        # The same two shapes the real server takes: a JSON-RPC envelope, or
        # the bare request with its fields inline (concat-api/src/rpc.rs).
        params = call.get("params")
        if not isinstance(params, dict):
            params = {key: value for key, value in call.items()
                      if key not in ("method", "jsonrpc", "id")}
        result = answer(method, params)
        if result is None:
            reply = {"jsonrpc": "2.0", "id": call.get("id"),
                     "error": {"code": -32601, "message": f"no method {method!r}"}}
        else:
            reply = {"jsonrpc": "2.0", "id": call.get("id"), "result": result}
        conn.sendall(json.dumps(reply).encode() + b"\n")


def serve(sock, token, record, stop):
    while not stop.is_set():
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=serve_once, args=(conn, token, record), daemon=True).start()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--token", default="test-token-0123456789abcdef")
    parser.add_argument("--record", default="/tmp/concat-mock.jsonl")
    parser.add_argument("--project", default=None, help="name the open project reports")
    args = parser.parse_args(argv)

    if args.project is not None:
        globals()["PROJECT"] = dict(PROJECT, name=args.project)

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(16)
    print(f"{args.host}:{sock.getsockname()[1]}", flush=True)

    stop = threading.Event()
    try:
        serve(sock, args.token, args.record, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())