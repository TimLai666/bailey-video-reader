"""MCP server for claude-real-video.

Exposes the crv pipeline over the Model Context Protocol so any MCP client
(Claude Desktop, Claude Code, Cursor, ...) can ask for a video to be watched:
scene-aware deduplicated keyframes plus a timestamped transcript, processed
entirely on the user's machine.

Run directly:            crv-mcp
Claude Code:             claude mcp add crv -- crv-mcp
Claude Desktop config:   {"mcpServers": {"crv": {"command": "crv-mcp"}}}

Requires the optional dependency group:  pip install 'claude-real-video[mcp]'
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import sys
import time
from pathlib import Path

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _ServerClass, Image
except ImportError:
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _ServerClass, Image
    except ImportError:  # pragma: no cover
        print(
            "The MCP server needs the optional 'mcp' dependency.\n"
            "Install it with: pip install 'claude-real-video[mcp]'",
            file=sys.stderr,
        )
        sys.exit(1)

from . import core

# Analyses are cached per source under one root so repeated questions about the
# same video (or a follow-up get_frames call) never re-download or re-extract.
CACHE_ROOT = Path(os.environ.get("CRV_MCP_CACHE", "~/.cache/crv-mcp")).expanduser()

# Frame images are resized before being returned inline: MCP responses travel
# through the model's context window, so full-resolution frames would blow the
# token budget for zero visual gain.
FRAME_MAX_SIDE = 768
DEFAULT_FRAME_BATCH = 12
TRANSCRIPT_CHAR_CAP = 20_000

mcp_app = _ServerClass("claude-real-video")


def _out_dir_for(source: str) -> Path:
    return CACHE_ROOT / hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def _sorted_frames(frames_dir: Path) -> list[Path]:
    def key(p: Path):
        nums = re.findall(r"\d+", p.stem)
        return (int(nums[-1]) if nums else 0, p.name)

    return sorted(
        (p for p in frames_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}),
        key=key,
    )


def _frame_image(path: Path) -> Image:
    from PIL import Image as PILImage

    with PILImage.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((FRAME_MAX_SIDE, FRAME_MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
    return Image(data=buf.getvalue(), format="jpeg")


def _read_capped(path: Path, cap: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…[transcript truncated at {cap} characters — read {path} for the rest]"


@mcp_app.tool()
def watch_video(
    source: str,
    max_frames: int = DEFAULT_FRAME_BATCH,
    language: str = "auto",
    transcribe: bool = True,
) -> list:
    """Actually watch a video: download (URL) or read (local path), extract
    scene-aware deduplicated keyframes and a timestamped transcript, all
    locally. Returns the transcript plus the first batch of keyframes as
    images; use get_frames for more frames from the same video.

    Args:
        source: Video URL (YouTube, Instagram, ...) or a local file path.
        max_frames: How many keyframes to return inline (1-24, default 12).
        language: Transcript language hint, e.g. "auto", "en", "zh".
        transcribe: Set false to skip audio transcription (frames only).
    """
    max_frames = max(1, min(24, max_frames))
    out_dir = _out_dir_for(source)
    manifest = out_dir / "MANIFEST.txt"

    was_cached = manifest.exists()
    if was_cached:
        result_note = "cached analysis (already processed earlier)"
    else:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        r = core.process(
            source,
            str(out_dir),
            lang=language,
            do_transcribe=transcribe,
            overwrite=True,
        )
        result_note = f"duration {r.duration}s, {r.frame_count} keyframes kept"

    # Index outside the cached/fresh split: a video whose indexing failed last
    # time (or that predates the memory feature) gets backfilled on the next
    # call instead of staying invisible to search_memory forever. remember()
    # replaces rather than duplicates, so re-indexing a cached analysis is free.
    if not os.environ.get("CRV_NO_MEMORY"):
        try:
            from . import memory as _memory
            if not was_cached or _memory.lookup(source) is None:
                _memory.remember(out_dir, source=source, tool="crv",
                                 params={"language": language, "transcribe": transcribe})
        except Exception as e:  # noqa: BLE001
            # Swallowing this silently made the user believe the video was
            # remembered when it was not — say so in the response instead.
            result_note += f" | note: not indexed for search ({e})"

    frames = _sorted_frames(out_dir / "frames")
    transcript_path = out_dir / "transcript.txt"

    parts: list = []
    header = [
        f"claude-real-video analysis of: {source}",
        f"({result_note})",
        f"keyframes: {len(frames)} total, showing 1-{min(max_frames, len(frames))} "
        f"(call get_frames with start_index for more)",
        f"output dir: {out_dir}",
    ]
    if transcript_path.exists():
        header.append("--- timestamped transcript ---")
        header.append(_read_capped(transcript_path, TRANSCRIPT_CHAR_CAP))
    else:
        header.append("(no transcript: video has no audio, transcription disabled, "
                      "or whisper is not installed)")
    parts.append("\n".join(header))
    parts.extend(_frame_image(p) for p in frames[:max_frames])
    return parts


@mcp_app.tool()
def get_frames(source: str, start_index: int = 1, count: int = DEFAULT_FRAME_BATCH) -> list:
    """Fetch more keyframes from a video already analysed with watch_video.

    Args:
        source: The same URL or path given to watch_video.
        start_index: 1-based index of the first frame to return.
        count: How many frames to return (1-24).
    """
    count = max(1, min(24, count))
    out_dir = _out_dir_for(source)
    frames_dir = out_dir / "frames"
    if not frames_dir.is_dir():
        return [f"No analysis found for this source — call watch_video first. (looked in {out_dir})"]
    frames = _sorted_frames(frames_dir)
    if not frames:
        return ["The analysis exists but holds no frames — re-run watch_video."]
    start = max(1, start_index) - 1
    batch = frames[start : start + count]
    if not batch:
        return [f"start_index {start_index} is past the end — this video has {len(frames)} keyframes."]
    parts: list = [
        f"keyframes {start + 1}-{start + len(batch)} of {len(frames)} for: {source}"
    ]
    parts.extend(_frame_image(p) for p in batch)
    return parts


@mcp_app.tool()
def search_memory(query: str, limit: int = 12, kind: str = "any") -> list:
    """Search everything crv has ever watched — spoken words and on-screen text —
    and get back the video, the timestamp and the matching line. Use this instead
    of watch_video when the answer might already be on disk, or when the question
    spans several videos ("which video mentioned pricing?").

    Args:
        query: What to look for, in any language.
        limit: Max hits to return (1-50).
        kind: "any", "speech" (spoken), "onscreen" (text on screen), or "note".
    """
    from . import memory as _memory

    if not (query or "").strip():
        return ["Give me something to look for — an empty query is not a wildcard here."]
    limit = max(1, min(50, limit))
    try:
        hits = _memory.search(query, limit=limit, kind=None if kind == "any" else kind)
    except _memory.SchemaTooNew as e:
        return [f"Memory index unavailable: {e}"]
    if not hits:
        seen = _memory.stats()["videos"]
        return [f'No match for "{query}" across {seen} watched video(s). '
                f"Call watch_video on a new source, or try a shorter phrase."]
    lines = [f'{len(hits)} hit(s) for "{query}":']
    current = None
    for h in hits:
        if h.source != current:
            lines.append(f"\n{h.source}   (analysis: {h.out_dir})")
            current = h.source
        lines.append(f"  [{h.stamp()}] {h.kind}: {h.text}")
    return ["\n".join(lines)]


@mcp_app.tool()
def list_watched() -> list:
    """List every video crv has already watched, newest first, with how long it
    was and how many searchable lines it produced. Cheap way to check whether a
    video needs watching again before spending time on it.
    """
    from . import memory as _memory

    rows = _memory.videos(limit=100)
    if not rows:
        return ["Nothing watched yet — call watch_video on a URL or a local file."]
    out = [f"{len(rows)} most recent video(s) in memory ({_memory.stats()['lines']} searchable lines):"]
    for v in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["watched_at"]))
        dur = f"{v['duration']:.0f}s" if v["duration"] else "unknown length"
        out.append(f"  {when}  {dur}  {v['lines']} lines  {v['source']}")
    return ["\n".join(out)]


@mcp_app.tool()
def get_transcript(source: str, max_chars: int = TRANSCRIPT_CHAR_CAP) -> list:
    """Return the full timestamped transcript of a video already analysed, without
    re-sending any frames. Use when you only need the words (quoting, searching a
    long talk) and the images would waste context.

    Args:
        source: The same URL or path given to watch_video.
        max_chars: Truncate beyond this many characters (default 20000).
    """
    out_dir = _out_dir_for(source)
    path = out_dir / "transcript.txt"
    if not path.exists():
        return [f"No transcript for this source — call watch_video first, or the video has no speech. (looked in {out_dir})"]
    cap = max(1000, min(200_000, int(max_chars)))   # was unbounded upward
    return [f"transcript for: {source}\n\n" + _read_capped(path, cap)]


def main() -> None:
    mcp_app.run()


if __name__ == "__main__":
    main()
