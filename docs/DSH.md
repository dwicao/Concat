# Concat in DSH

[DSH](https://github.com/deepseek-ai/deepseek-harness) is a coding agent that runs on a phone, a laptop or a server. This page has two independent halves; you can use either alone.

1. **The bridge** gives the model Concat's tools. It is a small Python program that speaks MCP to the agent and Concat's socket API to the editor. No build step, no Rust, and it works in any MCP client.
2. **The dsh-go plugin** adds a status line, two composer capsules and a small Concat panel to the phone app's native interface. It is a nicety: it reads the editor's address and drops a prompt into the composer. It never edits a project by itself.

Everything lives in [`scripts/dsh-bridge/`](../scripts/dsh-bridge/).

## What you need

- Concat itself, running, with **Settings → Automation → Enable** turned on. That writes `automation.json` (address, token, pid) into your config directory and starts the socket server.
- `python3` (standard library only) for the bridge.
- For the plugin: dsh-go with a local engine, and the plugin installed (see below).

The bridge reads `automation.json` on **every** call, so a new address or a re-minted token is picked up without restarting anything. `CONCAT_ENDPOINT=host:port` overrides the address and `CONCAT_TOKEN` overrides the token.

## The bridge

```sh
python3 scripts/dsh-bridge/concat_bridge.py --list-tools
python3 scripts/dsh-bridge/concat_bridge.py --call concat_status '{}'
python3 scripts/dsh-bridge/concat_bridge.py --call media_probe '{"path":"/sdcard/DCIM/clip.mp4"}'
```

It speaks MCP over stdio (newline-delimited JSON-RPC 2.0, protocol `2024-11-05`), so any client can start it:

```json
{
  "mcpServers": {
    "concat": {
      "command": "python3",
      "args": ["/path/to/Concat/scripts/dsh-bridge/concat_bridge.py"]
    }
  }
}
```

In a DSH profile, add one insert entry to `cordis.patch.yml`. The `insert:`
list is what the loader wants; a bare `- id:` entry only *overrides* an entry
that already exists, and is rejected with `entry "mcp-concat" not found`:

```yaml
- insert:
    - id: mcp-concat
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: concat
        transport: stdio
        command: /usr/bin/python3
        args:
          - /sdcard/Projects/Concat/scripts/dsh-bridge/concat_bridge.py
        toolCallTimeoutMs: 700000
```

Then restart the engine. The tools appear as `mcp__concat__*`. To check the
layer composes without booting:

```sh
dsh --profile web --dump-config | grep -A9 mcp-concat
```

The bridge is deliberately a **thin** client: one TCP connection per call, one request per connection, no session. That matches what Concat's socket server does, and it means a stale connection can never wedge the editor.

### What it adds over `--mcp`

Concat's own `--mcp` server already covers the same ground (see [MCP.md](MCP.md)); the bridge exists for the places where the agent cannot start a Rust binary:

- phones and small proot/Ubuntu environments with no toolchain to build `concat`;
- any MCP client that only knows how to run a script;
- hosts where `automation.json` is the only thing that can be shared between the editor (a GUI app) and the agent (a different sandbox).

The bridge's tool list is generated from the same request shapes as the in-app API, so the two do not drift in meaning; the bridge just does not compile the Rust schema.

## The dsh-go plugin

The plugin lives in this repository at `plugins/dsh-go-local-concat/` and is
linked into the dsh-go profile, so the app loads the repo copy and edits to it
are versioned with everything else:

```sh
dsh plugin --profile web add /sdcard/Projects/Concat/plugins/dsh-go-local-concat
dsh plugin --profile web list    # shows local-concat@link:.../plugins/dsh-go-local-concat
```

`dsh plugin add` takes a path, so point it at the repo rather than copying the
folder into `$DSH_HOME/dsh-go/plugins/`. A copy there drifts from the repo the
moment either one is edited.

It contributes:

| Where | What |
| --- | --- |
| Status bar | `Concat · <project>` when the editor answers, `Concat · offline` when it does not |
| Composer capsule | **Cut this** — reads the clipboard path and inserts a prepared edit brief |
| Composer capsule | **Edit brief** — inserts the general editing brief |
| Panel (session tab / features) | address, version, open project, and buttons to refresh, insert the brief, or copy the address |

The plugin only ever **reads** the editor and **inserts text**. It has no edit tools of its own: the user sends the prompt, the model calls the Concat tools, and the result comes back in the chat. That is the whole design - the model is the editor's hands, the plugin is its dashboard.

Change the plugin's `lib/index.js` and restart the engine in the app's **Engine** page; the manifest is re-read without a restart.

## Trying it without Concat

`mock_concat.py` is a stand-in that speaks the same socket protocol:

```sh
python3 scripts/dsh-bridge/mock_concat.py --port 7420 --token test-token-0123456789abcdef &
CONCAT_ENDPOINT=127.0.0.1:7420 CONCAT_TOKEN=test-token-0123456789abcdef \
  python3 scripts/dsh-bridge/concat_bridge.py --call concat_status '{}'
```

Two test scripts run the whole chain and assert the wire shapes:

```sh
python3 scripts/dsh-bridge/test_bridge.py   # bridge <-> mock, as MCP and as CLI
node scripts/dsh-bridge/test_plugin.mjs     # plugin <-> mock, with a fake dshGo
```

The second one needs no engine: it drives the plugin's handlers the way the app
does, and checks the strip's 24-character limit, the offline message, and the
inserted brief.

## Limits

- The bridge is line-based JSON, so a tool result larger than 4 MB is refused. `preview_frame` returns one PNG; that is the only large result.
- One call at a time. `export_wait` polls the job and returns progress, so a long export does not need a second call.
- The plugin polls every 10 s (panel) and 20 s (status bar). Each poll is one TCP connection; if Concat is closed, the plugin says so and stops retrying loudly.
- Neither half runs FFmpeg or touches a project file. Everything goes through Concat's own API, so the same undo history and the same limits apply.