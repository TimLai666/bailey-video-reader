"""Lite fused timeline — frames woven into the transcript on one clock.

The sectioned MANIFEST leaves the frame↔speech join to the reading LLM; on
long videos that occasionally misaligns (a frame quoted against the wrong
line). This precomputes the join: each transcript segment lists the frames
that fall inside it, and silences longer than GAP_S become frame-only spans.
Frames + speech only — the multi-track fusion (voice, emotion, camera, speed)
is a crv Pro feature.
"""
from __future__ import annotations

GAP_S = 1.5


def _mmss(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:04.1f}"


def build_spans(duration: float, frames: list[dict],
                segments: list[dict] | None) -> list[dict]:
    """[{start, end, speech, speaker, frames:[{file, t}]}] covering the video.
    `frames` entries need "file" and "t" (seconds)."""
    spans: list[dict] = []
    if segments:
        # Whisper sometimes hallucinates a segment far past the video's end on
        # music-only audio; don't let a phantom line swallow the timeline.
        # duration may be int-truncated upstream, so allow 1s of tolerance.
        limit = duration + 1.0
        segments = [s for s in segments if float(s["start"]) < limit]
        cursor = 0.0
        for s in sorted(segments, key=lambda s: (float(s["start"]), float(s["end"]))):
            start, end = float(s["start"]), min(float(s["end"]), limit)
            if start - cursor >= GAP_S:
                spans.append({"start": cursor, "end": start, "speech": None,
                              "speaker": None})
            spans.append({"start": start, "end": max(end, start),
                          "speech": (s.get("text") or "").strip() or None,
                          "speaker": s.get("speaker")})
            cursor = max(cursor, end)
        if duration - cursor >= GAP_S:
            spans.append({"start": cursor, "end": duration, "speech": None,
                          "speaker": None})
    if not spans:
        spans = [{"start": 0.0, "end": max(duration, 0.0), "speech": None,
                  "speaker": None}]
    for sp in spans:
        sp["frames"] = []
    for f in sorted(frames, key=lambda f: f["t"]):
        hit = next((sp for sp in spans if sp["start"] <= f["t"] < sp["end"]), None)
        if hit is None:  # a crack between segments — attach to the closest span
            hit = min(spans, key=lambda sp: min(abs(f["t"] - sp["start"]),
                                                abs(f["t"] - sp["end"])))
        hit["frames"].append(f)
    return spans


def manifest_lines(duration: float, frames: list[dict],
                   segments: list[dict] | None) -> list[str]:
    """The `--- timeline ---` MANIFEST block (only worth emitting when there is
    a transcript to weave — callers skip it otherwise)."""
    spans = build_spans(duration, frames, segments)
    lines = ["--- timeline ---",
             "(reader: this is the primary read — frames are already placed inside "
             "the speech span they belong to, so cite these timestamps instead of "
             "matching frames to lines yourself)"]
    for sp in spans:
        head = f"[{_mmss(sp['start'])}-{_mmss(sp['end'])}]"
        if sp["speech"]:
            head += f" {sp['speaker']}: " if sp.get("speaker") else " "
            head += f"「{sp['speech']}」"
        else:
            head += " (no speech)"
        lines.append(head)
        if sp["frames"]:
            lines.append("    frames: " + "  ".join(
                f"{f['file']} @{f['t']:.1f}s" for f in sp["frames"]))
    return lines
