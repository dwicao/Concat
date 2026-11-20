# dsh-bridge

A small MCP server that gives an AI agent Concat's editing tools, plus a stand-in
editor to test against.

```
concat_bridge.py     MCP stdio server + a plain CLI (--call, --list-tools)
mock_concat.py       a fake Concat that speaks the same socket protocol
test_bridge.py       end-to-end checks: bridge -> mock, wire shapes included
test_plugin.mjs      drives the dsh-go plugin against the mock (node)
```

Standard library only; `python3` is the whole dependency.

## Run it as an MCP server

```sh
python3 concat_bridge.py
```

It prints nothing on stdout except MCP frames, so it can be wired straight into a
client:

```json
{
  "mcpServers": {
    "concat": {
      "command": "python3",
      "args": ["/sdcard/Projects/Concat/scripts/dsh-bridge/concat_bridge.py"]
    }
  }
}
```

DSH users add one entry to `~/.dsh/profiles/web/cordis.patch.yml` and restart the
engine; see [`docs/DSH.md`](../../docs/DSH.md) in the repository root.

## Run it as a command

```sh
python3 concat_bridge.py --list-tools
python3 concat_bridge.py --call concat_status '{}'
python3 concat_bridge.py --call media_probe '{"path":"/sdcard/DCIM/VID_0001.mp4"}'
```

Exit code is `1` when the tool reports an error, so it composes with `&&` and
`set -e`. Arguments are JSON; when omitted, `{}` is sent.

`--endpoint host:port` and `--token …` override what the bridge finds on its own.

## How it finds Concat

1. `CONCAT_ENDPOINT` / `CONCAT_TOKEN` if set.
2. `automation.json` under `CONCAT_CONFIG`, `$XDG_CONFIG_HOME`, `~/.config` or `~`
   (Concat writes it when Settings → Automation is enabled; it holds the address,
   the token, the pid and the Concat version).
3. `127.0.0.1:7420`, with whatever token the environment carries.

Every call re-reads the listing, so restarting Concat or re-minting its token
needs no bridge restart.

## Wire protocol

One TCP connection per call. The first line is an `auth` frame carrying the token;
the second is the request. Concat answers one line per frame, and the bridge
closes the socket. Requests use the bare JSON shape (`{"method": …, …fields}`),
which is what Concat's socket server documents; the reply carries `"id": null`.

Limits worth knowing: a line is capped at 4 MB, and a tool call that takes longer
than 30 s (or 700 s for the MCP client, set in the profile) is reported rather
than left hanging.

## Tools

`concat_status` first; it tells you whether the editor is there and which project
is open. After that the names read like the API:

- projects: `project_list`, `project_create`, `project_open`, `project_save`,
  `project_close`, `project_get`, `project_set_video`
- media: `media_probe`, `media_import`
- editing: `edit_apply`, `edit_undo`, `edit_redo`, `clip_add`,
  `clips_add_sequence`, `clip_trim`, `clips_split`, `clips_move`, `clips_remove`,
  `clip_update`, `clip_transform`, `clip_speed`, `clip_frame`
- text: `title_add`, `title_style_words`, `title_clear_word_styles`,
  `captions_add`
- animation: `keyframe_set`, `keyframe_clear`
- organisation: `track_add`, `effect_layer_add`, `effects_list`, `template_list`,
  `template_instantiate`
- export: `export_start`, `export_status`, `export_wait`, `export_cancel`
- looking: `preview_frame`

`--list-tools` prints the same list with input schemas. `preview_frame` returns an
image content block (a PNG), not base64 text, so the agent can look at the frame.

## Test it

```sh
python3 test_bridge.py
```

The suite starts `mock_concat.py` on a free port, points the bridge at it through
`automation.json`, and drives the bridge both as an MCP client and as a CLI. It
checks the auth handshake, tool listing, a few real edit shapes (that `edit_apply`
inlines the `op`, that `clip_masks` nests under `patch`, that `captions_add`
becomes one batch), image results, export polling and error propagation.

`mock_concat.py` is also useful on its own:

```sh
python3 mock_concat.py --port 7420 --record /tmp/concat.jsonl
```

It prints the address it listens on and appends every request it receives to the
record file, which is the quickest way to see what a client actually sends.

## Not included

No Rust, no FFmpeg, no build step. The bridge does not edit media; it forwards
requests to a running Concat, which does.