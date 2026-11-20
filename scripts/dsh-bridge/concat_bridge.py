#!/usr/bin/env python3
"""Concat bridge for the DSH harness: an MCP stdio server over Concat's socket.

The editor already speaks JSON-RPC on a loopback port (Settings > Automation).
This file turns that socket into MCP tools, so an agent in DSH can drive the
editor without a Rust toolchain: no build step, Python 3 standard library only.

    python3 concat_bridge.py                 # MCP server on stdin/stdout
    python3 concat_bridge.py --call clip_add '{"path":"/p","mediaId":"m1"}'
    python3 concat_bridge.py --list-tools

Where it looks for the port, in order:
    --endpoint host:port, $CONCAT_ENDPOINT, automation.json, 127.0.0.1:7420
`automation.json` is written by the Concat window when Automation is on; the
token beside the address is passed to the first `auth` line, as Concat requires.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "concat", "version": "0.1.0"}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7420
CONFIG_NAME = "automation.json"

#: The window writes its listing into the config directory it chose.
def config_candidates():
    home = os.path.expanduser("~")
    seen = []
    for root in (os.environ.get("CONCAT_CONFIG"), os.environ.get("XDG_CONFIG_HOME"),
                 os.path.join(home, ".config"), home):
        if root:
            seen.append(os.path.join(root, "concat", CONFIG_NAME))
            seen.append(os.path.join(root, CONFIG_NAME))
    env = os.environ.get("CONCAT_API_FILE")
    if env:
        seen.insert(0, env)
    return seen


class ToolError(Exception):
    """A refusal worth showing the model: it becomes an MCP error result."""


# --------------------------------------------------------------------------
# JSON schemas, written out because Concat's serde types are strict
# --------------------------------------------------------------------------

def string(description, **extra):
    return dict(type="string", description=description, **extra)


def number(description, **extra):
    return dict(type="number", description=description, **extra)


def integer(description, **extra):
    return dict(type="integer", description=description, **extra)


def boolean(description, **extra):
    return dict(type="boolean", description=description, **extra)


def array(items, description, **extra):
    return dict(type="array", items=items, description=description, **extra)


def obj(properties, required=(), description=None, **extra):
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    schema.update(extra)
    return schema


def ref(name):
    """A shared definition, by `#/definitions/<name>`."""
    return {"$ref": "#/definitions/" + name}


DEFINITIONS = {
    "video": obj(
        {
            "width": integer("Frame width in pixels."),
            "height": integer("Frame height in pixels."),
            "rateNum": integer("Frame rate numerator; 30000 with rateDen 1001 is 29.97 fps."),
            "rateDen": integer("Frame rate denominator."),
        },
        ("width", "height", "rateNum", "rateDen"),
        "Frame size and rate.",
    ),
    "transform": obj(
        {
            "scale": number("Scale; 1.0 is the frame as it came in."),
            "offsetX": number("Horizontal offset, as a fraction of the frame width."),
            "offsetY": number("Vertical offset, as a fraction of the frame height."),
            "rotation": number("Rotation in degrees."),
            "stretchX": number("Horizontal stretch; 1.0 leaves it alone."),
            "stretchY": number("Vertical stretch; 1.0 leaves it alone."),
        },
        description="Position, size and rotation on the frame.",
    ),
    "effect": obj(
        {
            "id": string("Catalogue id, e.g. `concat.warm` (see effects_list)."),
            "params": {"type": "object", "description": "The effect's own parameters."},
        },
        ("id",),
        "One effect entry, as a clip patch carries it.",
    ),
    "text_style": obj(
        {
            "content": string("The words this title shows. Required; a patch replaces the whole style."),
            "fontFamily": string("Font family; absent leaves the editor's default."),
            "fontSize": number("Size as a fraction of the frame height, e.g. 0.08."),
            "fontWeight": number("Weight; 400 is regular, 800 is heavy."),
            "italic": boolean("Italic."),
            "align": string("One of `left`, `center`, `right`.", enum=["left", "center", "right"]),
            "color": string("Colour as `#rrggbb`."),
            "opacity": number("0 to 1."),
            "background": string("Background colour as `#rrggbb`, or empty for none."),
        },
        ("content",),
        "A title's whole style. Concat stores styles whole, so read the clip first when patching.",
    ),
    "caption": obj(
        {
            "start": number("Start in seconds."),
            "end": number("End in seconds; must be after the start."),
            "text": string("The words to show."),
        },
        ("start", "end", "text"),
        "One caption.",
    ),
    "clip_move": obj(
        {
            "clipId": string("The clip to move."),
            "start": number("New start in seconds."),
            "trackId": string("Destination track; omit to keep the clip's track."),
        },
        ("clipId", "start"),
        "One clip's new place.",
    ),
}


# --------------------------------------------------------------------------
# Tools: name, what it does, and how its arguments become a Concat request
# --------------------------------------------------------------------------

class Tool:
    def __init__(self, name, title, description, kind, target, schema,
                 read_only=False, destructive=False):
        self.name = name
        self.title = title
        self.description = description
        self.kind = kind          # "method", "edit" or a special handler name
        self.target = target      # Concat method, or the command's `op`
        self.schema = schema
        self.read_only = read_only
        self.destructive = destructive

    def as_mcp(self):
        schema = dict(self.schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        if DEFINITIONS:
            schema["definitions"] = DEFINITIONS
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        }


def method_tool(name, title, description, method, properties, required=(),
                read_only=False):
    return Tool(name, title, description, "method", method,
                obj(properties, required), read_only)


def edit_tool(name, title, description, op, properties, required=("path",),
              destructive=False):
    return Tool(name, title, description, "edit", op,
                obj(properties, required), destructive=destructive)


TOOLS = []


def add(tool):
    TOOLS.append(tool)
    return tool


# --- what is running -------------------------------------------------------

add(Tool(
    "concat_status", "Bridge status",
    "What this bridge reached: the address and port it dialled, where the token "
    "came from, the Concat build on the other end, and whether it answered. "
    "Call it first when a call fails.",
    "status", None,
    obj({}),
    read_only=True,
))

add(method_tool(
    "concat_version", "Concat version",
    "What this Concat speaks: API version, build, capabilities, and the address "
    "the editor listens on.",
    "version", {}, read_only=True,
))

# --- projects --------------------------------------------------------------

add(method_tool(
    "project_list", "Recent projects",
    "The projects this machine opened most recently, newest first: each one's "
    "folder `path`, name, frame size and rate. A project is a folder, and every "
    "other project tool names it by that `path`.",
    "project.list", {}, read_only=True,
))

add(method_tool(
    "project_create", "Create a project",
    "Creates a project folder named `name` inside the existing directory "
    "`location` and opens it. `video` sets the frame and rate (1920x1080 at "
    "30 fps when absent). The folder is `location/name`, which is the `path` "
    "later tools take.",
    "project.create",
    {"location": string("An existing directory the project folder goes inside."),
     "name": string("The new project's folder name."),
     "video": ref("video")},
    ("location", "name"),
))

add(method_tool(
    "project_open", "Open a project",
    "Opens a project folder so it can be read and edited. Opening one already "
    "open returns it as it stands, unsaved edits included. Refused when the "
    "Concat window has that project open.",
    "project.open",
    {"path": string("The project folder.")},
    ("path",),
))

add(method_tool(
    "project_save", "Save a project",
    "Writes an open project's document to its folder; edits stay in memory "
    "until this is called. `name` renames the project as it saves.",
    "project.save",
    {"path": string("The project folder."),
     "name": string("A new name for the project; optional.")},
    ("path",),
))

add(method_tool(
    "project_close", "Close a project",
    "Closes an open project. With `save` true it is saved first; otherwise "
    "unsaved edits are dropped.",
    "project.close",
    {"path": string("The project folder."),
     "save": boolean("Save before closing; default false.")},
    ("path",),
))

add(method_tool(
    "project_get", "Get the whole project",
    "An open project's full state: every timeline, clip, media item and "
    "setting, as the editor holds it. Large; read it when you need the ids of "
    "tracks and clips to edit them.",
    "project.get",
    {"path": string("The project folder.")},
    ("path",),
    read_only=True,
))

add(method_tool(
    "project_set_video", "Set frame size and rate",
    "Sets the active timeline's frame (width, height in pixels) and rate "
    "(rateNum/rateDen frames a second, e.g. 30000/1001 for 29.97) as one "
    "undoable edit.",
    "project.setVideo",
    {"path": string("The project folder."), "video": ref("video")},
    ("path", "video"),
))

# --- media -----------------------------------------------------------------

add(method_tool(
    "media_probe", "Probe a media file",
    "What is inside a video, audio or image file: duration in seconds, streams, "
    "frame size and rate, codecs. Changes no project.",
    "media.probe",
    {"path": string("Absolute path of the file to probe.")},
    ("path",),
    read_only=True,
))

add(method_tool(
    "media_import", "Import media",
    "Adds a file (an absolute path) to an open project's media bin, as dropping "
    "it on the window does. The new item's id is the summary's `createdId`; "
    "place it on the timeline with clip_add.",
    "media.import",
    {"path": string("The project folder."),
     "file": string("Absolute path of the media file to import.")},
    ("path", "file"),
))

# --- history ---------------------------------------------------------------

add(method_tool(
    "edit_apply", "Apply an edit",
    "Applies any one edit command to an open project as one undoable step, with "
    "the same limits and refusals as the window. `command` is an object tagged "
    "by `op` (`addClip`, `trimClip`, `splitClips`, `moveClips`, `updateClip`, "
    "`addTextClip`, `addTrack`, ...). A `batch` command applies several as one "
    "step: all land or none does. A refusal changes nothing.",
    "edit.apply",
    {"path": string("The project folder."),
     "command": {"type": "object",
                 "description": "The command, tagged by `op`."}},
    ("path", "command"),
))

add(method_tool(
    "edit_undo", "Undo",
    "Steps an open project's history back one edit (a batch is one edit).",
    "edit.undo",
    {"path": string("The project folder.")},
    ("path",),
))

add(method_tool(
    "edit_redo", "Redo",
    "Steps an open project's history forward one edit.",
    "edit.redo",
    {"path": string("The project folder.")},
    ("path",),
))

# --- clips -----------------------------------------------------------------

add(edit_tool(
    "clip_add", "Add a clip to the timeline",
    "Places an imported media item on a track at `start` seconds. Without "
    "`trackId` the editor picks a track that fits; without `start` it is "
    "appended. Returns the summary, with the new clip's id in `createdId`.",
    "addClip",
    {"path": string("The project folder."),
     "mediaId": string("Media bin id, from media_import or project_get."),
     "trackId": string("Track to place it on; omit to let Concat choose."),
     "start": number("Start in seconds; omit to append.")},
    ("path", "mediaId"),
))

add(edit_tool(
    "clips_add_sequence", "Add clips one after another",
    "Places several media items in order, each starting where the last ends. "
    "A still or unknown item takes five seconds.",
    "addClipsInSequence",
    {"path": string("The project folder."),
     "mediaIds": array(string("A media bin id."), "Media bin ids, in order."),
     "trackId": string("Track to place them on; omit to let Concat choose."),
     "start": number("Where the first clip starts; omit to append.")},
    ("path", "mediaIds"),
))

add(edit_tool(
    "clip_trim", "Trim a clip's edge",
    "Moves one edge of a clip by `delta` seconds: negative shortens, positive "
    "holds the last frame. With `ripple` the clips after it move with it.",
    "trimClip",
    {"path": string("The project folder."),
     "clipId": string("The clip to trim."),
     "edge": string("Which edge: `start` or `end`.", enum=["start", "end"]),
     "delta": number("Seconds; negative shortens, positive lengthens."),
     "ripple": boolean("Move the following clips by the same amount.")},
    ("path", "clipId", "edge", "delta"),
))

add(edit_tool(
    "clips_split", "Split clips",
    "Cuts each clip at `time` seconds, leaving two where there was one.",
    "splitClips",
    {"path": string("The project folder."),
     "clipIds": array(string("A clip id."), "Clips to split."),
     "time": number("Cut point in seconds.")},
    ("path", "clipIds", "time"),
))

add(edit_tool(
    "clips_move", "Move clips",
    "Moves clips to new start times, optionally to another track. All the moves "
    "land as one undoable edit.",
    "moveClips",
    {"path": string("The project folder."),
     "moves": array(ref("clip_move"), "Where each clip goes.")},
    ("path", "moves"),
))

add(edit_tool(
    "clips_remove", "Remove clips",
    "Removes clips. With `ripple` the clips after them close the gap.",
    "removeClips",
    {"path": string("The project folder."),
     "clipIds": array(string("A clip id."), "Clips to remove."),
     "ripple": boolean("Close the gap behind them.")},
    ("path", "clipIds"),
    destructive=True,
))

add(edit_tool(
    "clip_update", "Change a clip's properties",
    "Patches a clip: volume, opacity, fades, crop, speed, masks, effects, "
    "transitions, text, and the flip flags. Only the fields you send change; "
    "`null` clears a field (a crop, a transition, a text style).",
    "updateClip",
    {"path": string("The project folder."),
     "clipId": string("The clip to patch."),
     "patch": {"type": "object",
               "description": "Fields to change: volume, opacity, fadeIn, fadeOut, "
                              "muted, flipH, flipV, crop, transitionIn, videoEffects, "
                              "masks, text, preservePitch."}},
    ("path", "clipId", "patch"),
))

add(edit_tool(
    "clip_transform", "Move, scale or rotate a clip",
    "Sets a clip's position, scale, rotation and stretch as one edit.",
    "setClipTransform",
    {"path": string("The project folder."),
     "clipId": string("The clip to transform."),
     "scale": number("Scale; 1.0 is the frame as it came in."),
     "offsetX": number("Horizontal offset as a fraction of frame width."),
     "offsetY": number("Vertical offset as a fraction of frame height."),
     "rotation": number("Rotation in degrees."),
     "stretchX": number("Horizontal stretch."),
     "stretchY": number("Vertical stretch.")},
    ("path", "clipId"),
))

add(edit_tool(
    "clip_speed", "Set a clip's speed",
    "Plays a clip faster or slower; 2.0 is double speed, 0.5 is half. With "
    "`ripple` the timeline closes or opens behind it.",
    "setClipSpeed",
    {"path": string("The project folder."),
     "clipId": string("The clip to retime."),
     "speed": number("Speed factor, above 0."),
     "ripple": boolean("Move the following clips to match the new duration.")},
    ("path", "clipId", "speed"),
))

add(edit_tool(
    "clip_frame", "Scale a clip to fill the frame",
    "How a clip sits in the frame: `fit` shows all of it, `fill` crops the "
    "edges, `stretch` fills without keeping the shape.",
    "frameClip",
    {"path": string("The project folder."),
     "clipId": string("The clip to frame."),
     "framing": string("`fit`, `fill` or `stretch`.",
                       enum=["fit", "fill", "stretch"]),
     "at": number("Start of the move in seconds; optional.")},
    ("path", "clipId", "framing"),
))

# --- text ------------------------------------------------------------------

add(edit_tool(
    "title_add", "Add a title",
    "Adds a title clip at `start` seconds, on a track of its own above the "
    "picture (`above` false puts it on the current track). `style` carries the "
    "words in `content` plus their look; `offsetY` moves it up or down the "
    "frame as a fraction of frame height (negative is higher).",
    "addTextClip",
    {"path": string("The project folder."),
     "start": number("Start in seconds."),
     "duration": number("How long it stays, in seconds."),
     "above": boolean("Put it on a track above the picture; default true."),
     "trackId": string("A specific track; optional."),
     "offsetY": number("Vertical position, as a fraction of frame height."),
     "style": ref("text_style")},
    ("path", "start", "style"),
))

add(edit_tool(
    "title_style_words", "Style part of a title",
    "Styles the words between `start` and `end` seconds of a title: a colour, a "
    "weight, a background. The rest of the title keeps its style.",
    "styleTextRange",
    {"path": string("The project folder."),
     "clipId": string("The title clip."),
     "start": number("First character of the range."),
     "end": number("Character after the last one to style."),
     "patch": {"type": "object",
               "description": "Run style fields: color, fontWeight, italic, "
                              "underline, strikethrough, background, fontSize."}},
    ("path", "clipId", "start", "end", "patch"),
))

add(edit_tool(
    "title_clear_word_styles", "Clear a title's word styles",
    "Drops the per-word styling of a title, or of a range inside it, back to the "
    "title's own style.",
    "clearTextStyles",
    {"path": string("The project folder."),
     "clipId": string("The title clip."),
     "start": number("First character; optional."),
     "end": number("Character after the last; optional.")},
    ("path", "clipId"),
))

add(Tool(
    "captions_add", "Add captions",
    "Adds several captions as one undoable step: each caption becomes a title "
    "on its own track, timed from `start` to `end`. `style` is the shared look "
    "(`content` is filled in per caption); `offsetY` places them (0.35 is the "
    "lower third).",
    "captions", None,
    obj({"path": string("The project folder."),
         "captions": array(ref("caption"), "The captions, in time order."),
         "style": {"type": "object",
                   "description": "Shared style for every caption: fontSize, "
                                  "fontWeight, color, align, background..."},
         "offsetY": number("Vertical position as a fraction of frame height; "
                           "default 0.35.")},
        ("path", "captions")),
))

# --- animation -------------------------------------------------------------

add(edit_tool(
    "keyframe_set", "Set a keyframe",
    "Sets one property of a clip at one time, so it changes over the clip: "
    "`scale`, `opacity`, `offsetX`, `offsetY`, `rotation`. `value` is the "
    "property's own value; `ease` is an optional cubic-bezier as "
    "[x1, y1, x2, y2].",
    "setClipKey",
    {"path": string("The project folder."),
     "clipId": string("The clip to animate."),
     "property": string("scale, opacity, offsetX, offsetY or rotation."),
     "at": number("Time in seconds, inside the clip."),
     "value": number("The property's value at that time."),
     "ease": array(number("A control point."),
                   "Optional cubic-bezier easing: [x1, y1, x2, y2].")},
    ("path", "clipId", "property", "at", "value"),
))

add(edit_tool(
    "keyframe_clear", "Remove a keyframe",
    "Removes the keyframe of one property at one time, leaving the others.",
    "clearClipKey",
    {"path": string("The project folder."),
     "clipId": string("The clip."),
     "property": string("Which property."),
     "at": number("Which keyframe, in seconds.")},
    ("path", "clipId", "property", "at"),
))

# --- tracks and layers -----------------------------------------------------

add(edit_tool(
    "track_add", "Add a track",
    "Adds a track above (default) or below the existing ones. `kind` is "
    "video, audio or text when the editor needs it to know.",
    "addTrack",
    {"path": string("The project folder."),
     "bottom": boolean("Add it below instead of above."),
     "name": string("The track's name; optional.")},
    ("path",),
))

add(edit_tool(
    "effect_layer_add", "Add an effect layer",
    "Adds an effect over a span of the timeline, on its own track: a "
    "temperature wash, a vignette, a blur. `effectId` comes from effects_list.",
    "addLayerClip",
    {"path": string("The project folder."),
     "start": number("Start in seconds."),
     "duration": number("Length in seconds."),
     "effectId": string("Catalogue id, e.g. `concat.warm`."),
     "name": string("A name for the layer; optional."),
     "trackId": string("A specific track; optional.")},
    ("path", "start", "duration", "effectId"),
))

# --- catalogues ------------------------------------------------------------

add(method_tool(
    "effects_list", "List effects",
    "The catalogue of effects, filters, audio processors, transitions and "
    "generators, with their parameters. Call it before clip_update or "
    "effect_layer_add so the ids and params are right.",
    "catalogue.list",
    {"kind": string("One kind: `effect`, `filter`, `audio`, `transition` or "
                    "`generator`; omit for all.")},
    read_only=True,
))

add(method_tool(
    "template_list", "List templates",
    "The project templates this machine offers, with the slots each one fills.",
    "template.list", {}, read_only=True,
))

add(method_tool(
    "template_instantiate", "Make a project from a template",
    "Creates a project from a template, filling its slots with the media you "
    "name, and opens it.",
    "template.instantiate",
    {"template": string("The template's path, from template_list."),
     "location": string("Directory the new project folder goes inside."),
     "name": string("The new project's folder name."),
     "fills": array({"type": "object",
                     "description": "{mediaId, file} pairs."},
                    "Slot fills.")},
    ("template", "location", "name"),
))

# --- export ----------------------------------------------------------------

add(method_tool(
    "export_start", "Start an export",
    "Starts rendering an open project to a file and returns at once with a job "
    "id. Ask export_status or export_wait how it is going. `codec` is h264 or "
    "h265 (or the editor's software fallback); `crf` is quality, lower is "
    "better, 20 is a good default.",
    "export.run",
    {"path": string("The project folder."),
     "output": string("File to write, with its extension."),
     "codec": string("h264 or h265; optional."),
     "crf": integer("Quality: 0-51, lower is better; default 20."),
     "preset": string("Encoder speed/size trade-off; optional."),
     "audioCodec": string("Audio codec; optional."),
     "audioBitrate": integer("Audio bitrate in bits a second; optional.")},
    ("path", "output"),
))

add(method_tool(
    "export_status", "Export status",
    "One export job's state: running or finished, its stage, how far through, "
    "and the file it writes when done.",
    "export.status",
    {"job": string("The job id, from export_start.")},
    ("job",),
    read_only=True,
))

add(Tool(
    "export_wait", "Wait for an export",
    "Polls an export until it ends or `timeoutSeconds` passes, returning the "
    "job as it stands. A still-running job says so; call again.",
    "export_wait", None,
    obj({"job": string("The job id, from export_start."),
         "timeoutSeconds": number("How long to wait; default 60, at most 600.")},
        ("job",)),
    read_only=True,
))

add(method_tool(
    "export_cancel", "Cancel an export",
    "Asks a running export to stop, and reports the job as it stands.",
    "export.cancel",
    {"job": string("The job id, from export_start.")},
    ("job",),
))

add(Tool(
    "preview_frame", "Render a frame",
    "Renders one frame of an open project to a PNG and returns it as an image, "
    "so you can see what an edit did. Without width/height the timeline's own "
    "frame is scaled to fit 1280 pixels.",
    "preview", None,
    obj({"path": string("The project folder."),
         "time": number("Which frame, in seconds."),
         "width": integer("Picture width in pixels; optional."),
         "height": integer("Picture height in pixels; optional."),
         "output": string("Write the PNG here instead of returning it; optional.")},
        ("path", "time")),
    read_only=True,
))

# --- masks -----------------------------------------------------------------

add(edit_tool(
    "clip_masks", "Set a clip's masks",
    "Replaces a clip's masks: shapes, images or video used as a stencil, with a "
    "mode (`add`, `subtract`, `intersect`), feathering and inversion.",
    "updateClip",
    {"path": string("The project folder."),
     "clipId": string("The clip."),
     "masks": array({"type": "object",
                     "description": "One mask: id, source, mode, feather, invert."},
                    "The clip's new masks.")},
    ("path", "clipId", "masks"),
))

add(edit_tool(
    "mask_key_set", "Keyframe a mask",
    "Sets one of a mask's numbers at a time, so the mask moves or changes over "
    "the clip: `feather`, `scale`, `offsetX`, `offsetY`, `rotation`.",
    "setMaskKey",
    {"path": string("The project folder."),
     "clipId": string("The clip."),
     "maskId": string("The mask, by its id."),
     "number": string("Which number: feather, scale, offsetX, offsetY, rotation."),
     "at": number("Time in seconds, inside the clip."),
     "value": number("The value at that time.")},
    ("path", "clipId", "maskId", "number", "at", "value"),
))

BY_NAME = {tool.name: tool for tool in TOOLS}


# --------------------------------------------------------------------------
# Finding the editor
# --------------------------------------------------------------------------

class Endpoint:
    def __init__(self, host, port, token, source):
        self.host = host
        self.port = port
        self.token = token
        self.source = source

    def __str__(self):
        return f"{self.host}:{self.port}"


def read_listing(path):
    """One automation.json, or None when it is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            listing = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(listing, dict):
        return None
    return listing


def parse_endpoint(text):
    """`host:port`, `[v6]:port`, or a bare port."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
    elif text.count(":") == 1:
        host, port = text.split(":", 1)
    else:
        return None
    try:
        return host or DEFAULT_HOST, int(port)
    except ValueError:
        return None


def resolve(cli_endpoint=None, cli_token=None, verbose=False):
    """The address to dial, and where it came from."""
    override_token = cli_token if cli_token is not None else os.environ.get("CONCAT_TOKEN")

    for text in (cli_endpoint, os.environ.get("CONCAT_ENDPOINT")):
        if not text:
            continue
        parsed = parse_endpoint(text.strip())
        if parsed:
            host, port = parsed
            return Endpoint(host, port, override_token or "",
                            f"setting ({text.strip()})")
        raise ToolError(f"cannot read the endpoint {text!r}; write it as host:port")

    for path in config_candidates():
        listing = read_listing(path)
        if not listing:
            continue
        address = listing.get("address")
        parsed = parse_endpoint(address) if isinstance(address, str) else None
        if not parsed:
            continue
        host, port = parsed
        # An explicit token in the environment or on the command line wins: the
        # listing is a cache, and a stale one must not silently win.
        token = override_token if override_token is not None else str(listing.get("token", ""))
        return Endpoint(host, port, token,
                        os.path.join("listing", os.path.basename(os.path.dirname(path)) or path))

    return Endpoint(DEFAULT_HOST, DEFAULT_PORT, override_token or "", "default")


def listing_search_note():
    return ", ".join(config_candidates()[:3])


# --------------------------------------------------------------------------
# The socket
# --------------------------------------------------------------------------

MAX_LINE = 4 * 1024 * 1024


class Concat:
    """One JSON-RPC call at a time over Concat's loopback socket."""

    def __init__(self, endpoint, timeout=30.0):
        self.endpoint = endpoint
        self.timeout = timeout

    def call(self, request):
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_LINE:
            raise ToolError("the request is larger than Concat's 4 MB line limit")
        try:
            sock = socket.create_connection((self.endpoint.host, self.endpoint.port),
                                            timeout=self.timeout)
        except OSError as error:
            raise ToolError(
                f"cannot reach Concat at {self.endpoint} ({error.strerror or error}). "
                "Turn on Settings > Automation in Concat, or point CONCAT_ENDPOINT "
                "at the address its Automation sheet shows."
            )
        try:
            sock.settimeout(self.timeout)
            auth = {"jsonrpc": "2.0", "id": 1, "method": "auth",
                    "params": {"token": self.endpoint.token}}
            sock.sendall(json.dumps(auth, separators=(",", ":")).encode("utf-8") + b"\n")
            answer = self._read_line(sock)
            if answer is None:
                raise ToolError("Concat closed the connection before answering the token")
            hello = json.loads(answer)
            if "error" in hello:
                detail = hello["error"].get("message", "wrong token")
                raise ToolError(
                    f"Concat refused the token ({detail}). Copy the token from "
                    "Settings > Automation into CONCAT_TOKEN, or delete "
                    "automation.json and read the new one."
                )
            sock.sendall(payload + b"\n")
            line = self._read_line(sock)
            if line is None:
                raise ToolError("Concat closed the connection without answering")
            return json.loads(line)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _read_line(sock):
        buffer = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buffer.extend(chunk)
            end = buffer.find(b"\n")
            if end >= 0:
                return bytes(buffer[:end])
            if len(buffer) > MAX_LINE:
                raise ToolError("Concat's answer is larger than 4 MB")


def call_tool(name, arguments):
    """Run one tool, returning the MCP result object."""
    tool = BY_NAME[name]
    con = Concat(resolve())

    if tool.kind == "status":
        return text_result(status_view(con))
    if tool.name == "captions_add":
        return text_result(con.call(captions_request(arguments)))
    if tool.name == "clip_masks":
        return text_result(con.call(edit_request(
            {"path": arguments["path"], "clipId": arguments["clipId"],
             "patch": {"masks": arguments["masks"]}}, "updateClip")))
    if tool.name == "export_wait":
        return text_result(wait_for_export(con, arguments))
    if tool.name == "preview_frame":
        return preview_result(con, arguments)
    if tool.kind == "method":
        return text_result(con.call(with_method(arguments, tool.target)))
    if tool.kind == "edit":
        return text_result(con.call(edit_request(arguments, tool.target)))
    raise ToolError(f"the bridge does not know the tool {name!r}")


def with_method(arguments, method):
    request = dict(arguments or {})
    request["method"] = method
    return request


def edit_request(arguments, op):
    fields = dict(arguments or {})
    path = fields.pop("path", None)
    if not path:
        raise ToolError("invalid arguments: missing field `path`")
    fields["op"] = op
    return {"method": "edit.apply", "path": path, "command": fields}


def captions_request(arguments):
    """One batch of titles, as the editor's MCP server builds it."""
    path = (arguments or {}).get("path")
    captions = (arguments or {}).get("captions")
    if not path:
        raise ToolError("invalid arguments: missing field `path`")
    if not isinstance(captions, list) or not captions:
        raise ToolError("invalid arguments: `captions` is a list of {start, end, text}")
    offset = arguments.get("offsetY", 0.35)
    style = dict(arguments.get("style") or
                 {"fontFamily": "Hanken Grotesk", "fontSize": 0.04, "fontWeight": 600})
    style.pop("runs", None)
    commands = []
    for index, caption in enumerate(captions):
        try:
            start = float(caption["start"])
            end = float(caption["end"])
            text = caption["text"]
        except (KeyError, TypeError, ValueError):
            raise ToolError(f"invalid arguments: caption {index} needs a start, an end and a text")
        if not (start >= 0.0 and end > start):
            raise ToolError(f"invalid arguments: caption {index} ends at {end} s, "
                            f"not after its start at {start} s")
        one = dict(style)
        one["content"] = text
        commands.append({"op": "addTextClip", "trackId": None, "above": True,
                         "start": start, "duration": end - start,
                         "style": one, "offsetY": offset})
    return {"method": "edit.apply", "path": path,
            "command": {"op": "batch", "commands": commands}}


def wait_for_export(con, arguments, poll=0.25):
    timeout = arguments.get("timeoutSeconds", 60.0)
    try:
        timeout = max(0.0, min(float(timeout), 600.0))
    except (TypeError, ValueError):
        raise ToolError("invalid arguments: `timeoutSeconds` is a number of seconds")
    job = {"method": "export.status", "job": arguments["job"]}
    until = time.monotonic() + timeout
    while True:
        status = con.call(job)
        if status.get("state") != "running":
            return status
        if time.monotonic() >= until:
            status["note"] = "still running: call export_wait again, or export_status"
            return status
        time.sleep(poll)


def preview_result(con, arguments):
    arguments = dict(arguments or {})
    wants_file = arguments.get("output") not in (None, "")
    sized = arguments.get("width") not in (None, "") and arguments.get("height") not in (None, "")
    if not wants_file and not sized:
        view = con.call({"method": "project.get", "path": arguments.get("path")})
        settings = view.get("settings") or {}
        width = float(settings.get("width") or 1920)
        height = float(settings.get("height") or 1080)
        scale = min(1280.0 / max(width, height), 1.0)
        arguments["width"] = max(1, round(width * scale))
        arguments["height"] = max(1, round(height * scale))
    arguments["method"] = "preview.frame"
    result = con.call(arguments)
    png = result.get("png")
    if not png:
        return text_result(result)
    return {
        "content": [
            {"type": "image", "data": png, "mimeType": "image/png"},
            {"type": "text",
             "text": f"The frame at {arguments.get('time')} s, "
                     f"{result.get('width')}x{result.get('height')}."},
        ],
        "structuredContent": {"width": result.get("width"),
                              "height": result.get("height"),
                              "time": arguments.get("time")},
    }


def status_view(con):
    try:
        version = con.call({"method": "version"})
        answer = "answered"
    except ToolError as error:
        version, answer = None, str(error)
    return {
        "endpoint": str(con.endpoint),
        "endpointSource": con.endpoint.source,
        "token": "set" if con.endpoint.token else "empty (the editor allows no token on loopback)",
        "configFiles": [path for path in config_candidates() if os.path.exists(path)],
        "answered": answer,
        "concat": version,
    }


def text_result(value):
    return {
        "content": [{"type": "text", "text": json.dumps(value, indent=2, ensure_ascii=False)}],
        "structuredContent": value if isinstance(value, dict) else None,
    }


# --------------------------------------------------------------------------
# MCP over stdio
# --------------------------------------------------------------------------

def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def send_error(request_id, code, message):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def serve(stdin=None, stdout=None):
    """Read JSON-RPC lines until stdin ends, as MCP stdio expects."""
    for line in stdin or sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            send_error(None, -32700, "Parse error")
            continue
        if not isinstance(message, dict):
            send_error(None, -32600, "Invalid Request")
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            send({
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        "Drive the Concat video editor. Create or open a project, "
                        "import media, arrange clips, add titles and captions, "
                        "preview a frame, and export. Every call names a project "
                        "by its folder path."
                    ),
                },
            })
            continue
        if method in ("notifications/initialized", "notifications/cancelled"):
            continue
        if method == "ping":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            continue
        if method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id,
                  "result": {"tools": [tool.as_mcp() for tool in TOOLS]}})
            continue
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name not in BY_NAME:
                send_error(request_id, -32602, f"unknown tool {name!r}")
                continue
            try:
                result = call_tool(name, arguments)
            except ToolError as error:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            except (KeyError, TypeError, ValueError) as error:
                result = {"content": [{"type": "text",
                                       "text": f"invalid arguments: {error}"}],
                          "isError": True}
            else:
                result = dict(result)
                result.pop("isError", None)
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
            continue
        if request_id is not None:
            send_error(request_id, -32601, f"unknown method {method!r}")


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", help="host:port of the Concat editor")
    parser.add_argument("--token", help="the token from Settings > Automation")
    parser.add_argument("--call", metavar="TOOL", help="run one tool and print its result")
    parser.add_argument("arguments", nargs="?", help="JSON arguments for --call")
    parser.add_argument("--arguments", dest="arguments_option",
                        help="JSON arguments for --call")
    parser.add_argument("--list-tools", action="store_true", help="print the tool catalogue")
    args = parser.parse_args(argv)

    if args.list_tools:
        for tool in TOOLS:
            print(f"{tool.name:24s} {tool.title}")
        return 0
    if args.call:
        if args.endpoint:
            os.environ["CONCAT_ENDPOINT"] = args.endpoint
        if args.token:
            os.environ["CONCAT_TOKEN"] = args.token
        arguments = json.loads(args.arguments_option or args.arguments or "{}")
        result = call_tool(args.call, arguments)
        for item in result.get("content", []):
            if item.get("type") == "text":
                print(item["text"])
            else:
                print(f"<{item.get('type')} {len(item.get('data', ''))} bytes>")
        return 1 if result.get("isError") else 0

    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())