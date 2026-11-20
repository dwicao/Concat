#!/usr/bin/env python3
"""End-to-end test of the DSH bridge against the mock Concat socket.

    python3 test_bridge.py

It starts mock_concat.py, points the bridge at it through a temporary
automation.json, and checks both the one-shot CLI and a real MCP stdio
session: initialize, tools/list, tools/call, and the error paths.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "concat_bridge.py")
MOCK = os.path.join(HERE, "mock_concat.py")

TOKEN = "test-token-0123456789abcdef"
CHECKS = []


def check(label, condition, detail=""):
    CHECKS.append((label, bool(condition), detail))
    print(f"{'ok  ' if condition else 'FAIL'} {label}{('  ' + detail) if not condition and detail else ''}")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_bridge(arguments, env):
    return subprocess.run([sys.executable, BRIDGE] + arguments, env=env,
                          capture_output=True, text=True, timeout=60)


def fields(call):
    """The request's own fields, whichever of the two wire shapes carried them."""
    params = call.get("params")
    if isinstance(params, dict):
        return params
    return {key: value for key, value in call.items()
            if key not in ("method", "jsonrpc", "id")}


def mcp_session(env, messages):
    """One MCP stdio conversation; returns the JSON-RPC replies in order."""
    proc = subprocess.Popen([sys.executable, BRIDGE], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    payload = "".join(json.dumps(message) + "\n" for message in messages)
    try:
        out, err = proc.communicate(payload, timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    return [json.loads(line) for line in out.splitlines() if line.strip()], err


def main():
    work = tempfile.mkdtemp(prefix="dsh-bridge-")
    record = os.path.join(work, "seen.jsonl")

    mock = subprocess.Popen([sys.executable, MOCK, "--port", "0", "--token", TOKEN,
                             "--record", record],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        address = mock.stdout.readline().strip()
        if not address:
            print("mock did not start:", mock.stderr.read())
            return 1
        host, _, port = address.rpartition(":")

        config = os.path.join(work, "concat")
        os.makedirs(config, exist_ok=True)
        with open(os.path.join(config, "automation.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "address": address, "token": TOKEN, "pid": 1,
                       "concat": "0.0.0-mock", "apiVersion": "0.2"}, handle)

        env = dict(os.environ)
        env["CONCAT_CONFIG"] = config
        env.pop("CONCAT_ENDPOINT", None)
        env.pop("CONCAT_TOKEN", None)

        # --- one-shot CLI ---
        result = run_bridge(["--call", "concat_status"], env)
        status = json.loads(result.stdout)
        check("status reaches the mock", status["answered"] == "answered", result.stdout + result.stderr)
        check("status found the listing", status["endpoint"] == address, str(status))

        result = run_bridge(["--call", "concat_version"], env)
        check("version comes back", "0.0.0-mock" in result.stdout, result.stdout + result.stderr)

        # --- each tool sends what Concat expects ---
        with open(record, "w", encoding="utf-8") as handle:
            handle.write("")
        result = run_bridge(["--call", "clip_add",
                             '{"path":"/tmp/Mock","mediaId":"m1","trackId":"t1","start":2.5}'], env)
        sent = [json.loads(line) for line in open(record, encoding="utf-8") if line.strip()]
        check("clip_add is one edit.apply",
              sent and sent[-1]["method"] == "edit.apply"
              and fields(sent[-1])["command"]["op"] == "addClip"
              and fields(sent[-1])["path"] == "/tmp/Mock"
              and fields(sent[-1])["command"]["start"] == 2.5,
              json.dumps(sent[-1] if sent else None))

        with open(record, "w", encoding="utf-8") as handle:
            handle.write("")
        result = run_bridge(["--call", "captions_add",
                             '{"path":"/tmp/Mock","captions":[{"start":0,"end":1.5,"text":"Hi"}],'
                             '"style":{"color":"#ffff00"}}'], env)
        sent = [json.loads(line) for line in open(record, encoding="utf-8") if line.strip()]
        command = (fields(sent[-1])["command"] if sent else {})
        check("captions become one batch",
              command.get("op") == "batch" and len(command.get("commands", [])) == 1,
              json.dumps(command))
        if command.get("commands"):
            one = command["commands"][0]
            check("caption carries its text and timing",
                  one["style"]["content"] == "Hi" and one["duration"] == 1.5
                  and one["style"]["color"] == "#ffff00" and one["offsetY"] == 0.35,
                  json.dumps(one))

        with open(record, "w", encoding="utf-8") as handle:
            handle.write("")
        result = run_bridge(["--call", "clip_masks",
                             '{"path":"/tmp/Mock","clipId":"c1","masks":[{"id":"heart","mode":"add"}]}'], env)
        sent = [json.loads(line) for line in open(record, encoding="utf-8") if line.strip()]
        patch = (fields(sent[-1])["command"] if sent else {})
        check("clip_masks becomes a patch",
              patch.get("op") == "updateClip" and patch.get("patch", {}).get("masks") == [{"id": "heart", "mode": "add"}],
              json.dumps(patch))

        with open(record, "w", encoding="utf-8") as handle:
            handle.write("")
        result = run_bridge(["--call", "preview_frame", '{"path":"/tmp/Mock","time":1.5}'], env)
        sent = [json.loads(line) for line in open(record, encoding="utf-8") if line.strip()]
        methods = [call["method"] for call in sent]
        check("preview sizes itself from the project",
              methods == ["project.get", "preview.frame"]
              and fields(sent[-1])["width"] == 1280 and fields(sent[-1])["height"] == 720,
              json.dumps(sent))

        # --- MCP stdio ---
        replies, err = mcp_session(env, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "project_create",
                        "arguments": {"location": "/tmp", "name": "Demo"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "clip_add", "arguments": {"path": "/tmp/Mock", "mediaId": "m1"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}},
        ])
        by_id = {reply.get("id"): reply for reply in replies}
        check("initialize answers",
              (by_id.get(1) or {}).get("result", {}).get("serverInfo", {}).get("name") == "concat",
              json.dumps(by_id.get(1)))
        tools = (by_id.get(2) or {}).get("result", {}).get("tools", [])
        check("tools/list offers the catalogue", len(tools) > 30, str(len(tools)))
        names = {tool["name"] for tool in tools}
        check("every tool has a schema",
              all(tool.get("inputSchema", {}).get("type") == "object" for tool in tools),
              str([tool["name"] for tool in tools if tool.get("inputSchema", {}).get("type") != "object"]))
        check("core tools present",
              {"project_create", "media_import", "clip_add", "captions_add",
               "export_start", "preview_frame"} <= names, str(sorted(names))[:200])
        created = (by_id.get(3) or {}).get("result", {})
        check("tools/call reaches the editor",
              "content" in created and not created.get("isError"), json.dumps(created))
        unknown = (by_id.get(5) or {}).get("error") or {}
        check("unknown tool is an error", unknown.get("code") == -32602, json.dumps(by_id.get(5)))

        # --- a wrong token explains itself ---
        bad_env = dict(env)
        bad_env["CONCAT_TOKEN"] = "not-the-token"
        result = run_bridge(["--call", "concat_version"], bad_env)
        check("a wrong token says how to fix it",
              "token" in (result.stdout + result.stderr).lower(),
              result.stdout + result.stderr)

        # --- nothing listening says how to fix that too ---
        dead_env = dict(env)
        dead_env["CONCAT_ENDPOINT"] = f"127.0.0.1:{free_port()}"
        result = run_bridge(["--call", "concat_version"], dead_env)
        check("a dead port says Settings > Automation",
              "Automation" in (result.stdout + result.stderr),
              result.stdout + result.stderr)
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    failed = [label for label, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())