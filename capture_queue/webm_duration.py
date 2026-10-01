"""Bounded duration recovery for local WebM with no container duration.

Only a warning-free, integrity-verified temporary remux can supply duration.
This cannot prove a recording continued beyond the bytes actually present.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import time

MAX_INPUT_BYTES = 256 * 1024**2
MAX_PACKET_BYTES = 64 * 1024**2  # Per scan, streamed; never retained as JSON.
MAX_PACKETS = 250_000
MAX_LINE_BYTES = 8192
MAX_STDERR_BYTES = 64 * 1024
MAX_DURATION = 7200
DEFAULT_TIMEOUT = 60.0  # Entire fallback, including hashes and all subprocesses.


class _MissingPacketDuration(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("WebM duration verification exceeded its total time budget")
    return remaining


@contextmanager
def _interruptible():
    """Let CLI SIGTERM/SIGINT unwind child-process and temporary-file cleanup."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    interrupted = False
    def interrupt(signum, frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            # selectors treats InterruptedError as a retryable syscall interruption.
            raise RuntimeError("WebM duration verification interrupted; no duration accepted")
    try:
        for sig in previous:
            signal.signal(sig, interrupt)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _hash(path, deadline, limit):
    digest = hashlib.sha256()
    count = 0
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            _remaining(deadline)
            count += len(block)
            require(count <= limit, "WebM validation file exceeded its byte limit")
            digest.update(block)
    return digest.hexdigest()


def _run_strict(command, deadline, *, max_stdout=2 * 1024**2, on_line=None, output_file=None, output_limit=None,
                allowed_stderr_lines=()):
    """Stream bounded output and reject warnings, including exit-0 truncation."""
    _remaining(deadline)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        result, pending, total = bytearray(), bytearray(), 0
        try:
            while selector.get_map() or process.poll() is None:
                remaining = _remaining(deadline)
                require(os.fstat(errors.fileno()).st_size <= MAX_STDERR_BYTES, "WebM diagnostics exceeded the byte limit")
                if output_file is not None and output_file.exists():
                    require(output_file.stat().st_size <= output_limit, "Temporary WebM exceeded the disk budget")
                for key, _ in selector.select(min(.05, remaining)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    require(total <= max_stdout, "WebM packet metadata exceeded the byte limit")
                    if on_line is None:
                        result.extend(chunk)
                    else:
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, pending = pending.partition(b"\n")
                            require(len(line) <= MAX_LINE_BYTES, "Oversized WebM packet record")
                            if line:
                                on_line(bytes(line))
                        require(len(pending) <= MAX_LINE_BYTES, "Oversized WebM packet record")
            if pending:
                on_line(bytes(pending))
            process.wait(timeout=_remaining(deadline))
            require(process.returncode == 0, "WebM verification tool failed; duration remains unverified")
            # FFmpeg can exit 0 while reporting 'File ended prematurely'.
            errors.seek(0)
            diagnostics = errors.read(MAX_STDERR_BYTES + 1)
            require(len(diagnostics) <= MAX_STDERR_BYTES, "WebM diagnostics exceeded the byte limit")
            require(all(line in allowed_stderr_lines for line in diagnostics.splitlines()),
                    "Media verification reported warnings/errors: " + diagnostics[-2000:].decode("utf-8", errors="replace"))
            if output_file is not None:
                require(output_file.is_file() and 0 < output_file.stat().st_size <= output_limit,
                        "Temporary WebM is empty or exceeded the disk budget")
            return bytes(result)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("WebM duration verification exceeded its total time budget") from error
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            selector.close()
            process.stdout.close()


def _packet_signature(path, deadline):
    streams = {}
    total, max_end = 0, 0.0

    def record(line):
        nonlocal total, max_end
        total += 1
        require(total <= MAX_PACKETS, "WebM packet count exceeded the verification limit")
        fields = dict(part.split(b"=", 1) for part in line.split(b"|") if b"=" in part)
        index = fields.get(b"stream_index", b"")
        require(re.fullmatch(rb"[0-9]+", index) is not None, "Invalid WebM packet stream index")
        require(re.fullmatch(rb"SHA256:[0-9a-f]{64}", fields.get(b"data_hash", b"")) is not None,
                "Missing WebM packet payload hash")
        try:
            pts, dts = (float(fields[key]) for key in (b"pts_time", b"dts_time"))
        except (KeyError, ValueError):
            raise ValueError("WebM packet timing is incomplete; duration remains unverified") from None
        if fields.get(b"duration_time") in (None, b"N/A"):
            raise _MissingPacketDuration("WebM packet duration is unspecified")
        try:
            duration = float(fields[b"duration_time"])
        except ValueError:
            raise ValueError("WebM packet duration is invalid") from None
        require(all(math.isfinite(v) for v in (pts, dts, duration)) and -1 <= pts <= MAX_DURATION
                and -1 <= dts <= MAX_DURATION and 0 < duration <= MAX_DURATION,
                "WebM packet timing is outside verification limits")
        max_end = max(max_end, pts + duration)
        require(max_end <= MAX_DURATION, "WebM packet timeline exceeded two hours")
        key = index.decode("ascii")
        if key not in streams:
            require(len(streams) < 32, "Too many WebM streams")
            streams[key] = {"count": 0, "digest": hashlib.sha256()}
        streams[key]["count"] += 1
        # Retain side-data semantics too (e.g. Opus discard padding), without storing packet JSON.
        streams[key]["digest"].update(line + b"\n")

    _run_strict(["/usr/bin/ffprobe", "-v", "warning", "-protocol_whitelist", "file,pipe",
                 "-show_packets", "-show_data_hash", "sha256", "-show_entries",
                 "packet=stream_index,pts_time,dts_time,duration_time,size,flags,data_hash", "-of", "compact=p=0:nk=0", str(path)],
                deadline, max_stdout=MAX_PACKET_BYTES, on_line=record)
    require(total > 0, "WebM contains no verifiable packets")
    return {"streams": {key: {"packet_count": value["count"], "packet_sha256": value["digest"].hexdigest()}
                        for key, value in streams.items()}, "max_packet_end_seconds": max_end}


def _variable_duration_packets(path, deadline, metadata, missing=None):
    """Compare all packet fields; normalize only source-unspecified video durations.

    Matroska permits an omitted video duration. It does not authorize invented PTS,
    DTS, payload, side data, or replacement of any duration that was already known.
    The bounded ordinal set is at most MAX_PACKETS integers, not packet JSON.
    """
    info = {str(s["index"]): s for s in metadata.get("streams", [])}
    require(0 < len(info) <= 32, "Missing/oversized WebM stream metadata")
    require(all(s.get("codec_name") in {"vp8", "vp9", "opus"} and s.get("codec_type") in {"video", "audio"}
                for s in info.values()), "Unspecified durations require VP8/VP9 video and Opus audio")
    source_scan = missing is None
    missing = set() if source_scan else set(missing)
    streams = {}; total = 0; max_end = audio_end = last_video_pts = -1.0

    def record(line):
        nonlocal total, max_end, audio_end, last_video_pts
        total += 1
        require(total <= MAX_PACKETS, "WebM packet count exceeded the verification limit")
        parts = line.split(b"|")
        fields = dict(part.split(b"=", 1) for part in parts if b"=" in part)
        index = fields.get(b"stream_index", b"").decode("ascii")
        require(index in info, "Unknown WebM packet stream")
        kind = info[index]["codec_type"]
        require(re.fullmatch(rb"SHA256:[0-9a-f]{64}", fields.get(b"data_hash", b"")) is not None,
                "Missing WebM packet payload hash")
        try:
            pts, dts = (float(fields[key]) for key in (b"pts_time", b"dts_time"))
        except (KeyError, ValueError):
            raise ValueError("WebM packet PTS/DTS is incomplete; duration remains unverified") from None
        require(all(math.isfinite(v) and -1 <= v <= MAX_DURATION for v in (pts, dts)),
                "WebM packet timing is outside verification limits")
        value = fields.get(b"duration_time")
        absent = value in (None, b"N/A")
        if absent:
            require(kind == "video" and info[index]["codec_name"] in {"vp8", "vp9"},
                    "Only video packet duration may be unspecified")
            if source_scan:
                missing.add(total)
            else:
                require(total in missing, "Remux removed an originally known packet duration")
        else:
            duration = float(value)
            require(math.isfinite(duration) and 0 < duration <= MAX_DURATION,
                    "WebM packet duration is outside verification limits")
        if total in missing:
            require(kind == "video", "Unspecified-duration packet identity changed")
            verified_duration = b"source-unspecified"
        else:
            verified_duration = value
            max_end = max(max_end, pts + duration)
            if kind == "audio":
                audio_end = max(audio_end, pts + duration)
        if kind == "video":
            last_video_pts = max(last_video_pts, pts)
        require(max_end <= MAX_DURATION, "WebM packet timeline exceeded two hours")
        item = streams.setdefault(index, {"count": 0, "missing": 0, "digest": hashlib.sha256(), "raw": hashlib.sha256()})
        item["count"] += 1
        item["missing"] += int(total in missing)
        item["raw"].update(line + b"\n")
        canonical = b"|".join(part for part in parts if not part.startswith(b"duration_time="))
        item["digest"].update(canonical + b"|verified_duration=" + verified_duration + b"\n")

    _run_strict(["/usr/bin/ffprobe", "-v", "warning", "-protocol_whitelist", "file,pipe",
                 "-show_packets", "-show_data_hash", "sha256", "-show_entries",
                 "packet=stream_index,pts_time,dts_time,duration_time,size,flags,data_hash", "-of", "compact=p=0:nk=0", str(path)],
                deadline, max_stdout=MAX_PACKET_BYTES, on_line=record)
    require(total > 0 and missing, "No source-unspecified video durations found")
    require(max(missing) <= total, "Remux lost packets with unspecified duration")
    require(audio_end > 0 and last_video_pts >= 0 and last_video_pts <= audio_end,
            "Opus audio endpoint does not cover the video timestamps")
    # An originally known video duration cannot extend beyond the reliable audio endpoint.
    require(max_end <= audio_end + .001, "Known video duration exceeds the audio endpoint")
    normalized = {"streams": {key: {"packet_count": value["count"], "packet_sha256": value["digest"].hexdigest(),
                                    "source_unspecified_duration_count": value["missing"]}
                              for key, value in streams.items()}, "max_packet_end_seconds": max_end,
                  "audio_packet_end_seconds": audio_end, "last_video_packet_pts_seconds": last_video_pts}
    raw = {key: value["raw"].hexdigest() for key, value in streams.items()}
    return normalized, missing, raw


def _decoded_timeline(path, deadline, metadata):
    """Decode every frame with bounded output, retaining exact source-clock signatures."""
    info = {str(s["index"]): s for s in metadata.get("streams", [])}
    streams = {}; total = 0; audio_end = -1.0; last_video_pts = -1.0

    def record(line):
        nonlocal total, audio_end, last_video_pts
        total += 1
        require(total <= MAX_PACKETS, "WebM decoded frame count exceeded the verification limit")
        fields = dict(part.split(b"=", 1) for part in line.split(b"|") if b"=" in part)
        index = fields.get(b"stream_index", b"").decode("ascii")
        require(index in info, "Unknown decoded WebM stream")
        kind = info[index]["codec_type"]
        require(fields.get(b"media_type") == kind.encode("ascii"), "Decoded WebM stream type changed")
        try:
            pts, dts, best = (float(fields[key]) for key in (b"pts_time", b"pkt_dts_time", b"best_effort_timestamp_time"))
        except (KeyError, ValueError):
            raise ValueError("Decoded WebM frame timestamps are incomplete") from None
        require(all(math.isfinite(v) and -1 <= v <= MAX_DURATION for v in (pts, dts, best)) and abs(pts-best) < .000001,
                "Decoded WebM frame timestamps are invalid")
        item = streams.setdefault(index, {"frame_count": 0, "first_pts_seconds": pts, "last_pts_seconds": pts,
                                          "samples": 0, "digest": hashlib.sha256()})
        require(pts >= item["last_pts_seconds"], "Decoded WebM frame timestamps moved backwards")
        item["frame_count"] += 1; item["last_pts_seconds"] = pts
        item["digest"].update(line + b"\n")
        if kind == "audio":
            rate = int(info[index].get("sample_rate", 0)); samples = int(fields.get(b"nb_samples", b"0"))
            require(info[index].get("codec_name") == "opus" and 0 < rate <= 192000 and 0 < samples <= rate,
                    "Decoded Opus sample timing is invalid")
            item["samples"] += samples
            audio_end = max(audio_end, pts + samples/rate)
        else:
            last_video_pts = max(last_video_pts, pts)

    _run_strict(["/usr/bin/ffprobe", "-v", "warning", "-protocol_whitelist", "file,pipe", "-show_frames",
                 "-show_entries", "frame=media_type,stream_index,pts_time,pkt_dts_time,best_effort_timestamp_time,nb_samples",
                 "-of", "compact=p=0:nk=0", str(path)], deadline, max_stdout=MAX_PACKET_BYTES, on_line=record)
    require(total > 0 and audio_end > 0 and 0 <= last_video_pts <= audio_end,
            "Decoded Opus audio endpoint does not cover the decoded video timestamps")
    return {"streams": {key: {**{k: v for k, v in value.items() if k != "digest"},
                              "frame_timing_sha256": value["digest"].hexdigest()} for key, value in streams.items()},
            "audio_decoded_end_seconds": audio_end, "last_video_frame_pts_seconds": last_video_pts,
            "video_tail_hold_unverified_seconds": audio_end-last_video_pts}


def resolve_duration(path, metadata, *, timeout_seconds=DEFAULT_TIMEOUT):
    """Return (seconds, optional verification provenance); normal files are unchanged."""
    value = metadata.get("format", {}).get("duration")
    if value not in (None, "N/A"):
        duration = float(value)
        require(math.isfinite(duration) and duration > 0, "Invalid media duration")
        return duration, None
    source = Path(path)
    require(source.suffix.lower() == ".webm" and "webm" in metadata.get("format", {}).get("format_name", ""),
            "Missing media duration; verified fallback supports local WebM only")
    require(math.isfinite(timeout_seconds) and 0 < timeout_seconds <= DEFAULT_TIMEOUT, "Invalid verification time budget")
    deadline = time.monotonic() + timeout_seconds
    require(not source.is_symlink() and stat.S_ISREG(source.stat().st_mode), "WebM source must be a regular local file")
    size = source.stat().st_size
    require(0 < size <= MAX_INPUT_BYTES, "Missing-duration WebM exceeds the 256 MiB verification limit")
    source_hash = _hash(source, deadline, MAX_INPUT_BYTES)
    output_limit = size * 5 // 4 + 8 * 1024**2
    with _interruptible(), tempfile.TemporaryDirectory(prefix="reader-webm-duration-") as temporary:
        temporary = Path(temporary)
        require(shutil.disk_usage(temporary).free >= output_limit + 32 * 1024**2,
                "Insufficient temporary disk space for verified WebM remux")
        derived = temporary / "duration-verified.webm"
        variable = None
        try:
            before = _packet_signature(source, deadline)
        except _MissingPacketDuration:
            before, missing, raw_before = _variable_duration_packets(source, deadline, metadata)
            frames_before = _decoded_timeline(source, deadline, metadata)
            require(abs(before["audio_packet_end_seconds"]-frames_before["audio_decoded_end_seconds"]) <= .1,
                    "Opus packet and decoded-sample endpoints disagree")
            variable = {"source_unspecified_video_packet_durations": len(missing),
                        "source_raw_packet_sha256": raw_before, "decoded_timeline": frames_before,
                        "duration_basis": "Container duration cross-checked against Opus packet and decoded-sample endpoints (100 ms tolerance)",
                        "video_duration_policy": "Only source-unspecified video durations are excluded from equality; remux-added values are not measured frame durations"}
        _run_strict(["/usr/bin/ffmpeg", "-nostdin", "-n", "-protocol_whitelist", "file,pipe", "-v", "warning",
                     "-xerror", "-err_detect", "explode", "-copyts", "-i", str(source), "-map", "0", "-c", "copy",
                     "-avoid_negative_ts", "disabled", str(derived)], deadline, max_stdout=1024,
                    output_file=derived, output_limit=output_limit)
        if variable is None:
            after = _packet_signature(derived, deadline)
        else:
            after, _, raw_after = _variable_duration_packets(derived, deadline, metadata, missing)
            require(frames_before == _decoded_timeline(derived, deadline, metadata),
                    "WebM remux changed decoded frame/sample timestamps")
            variable["remux_raw_packet_sha256"] = raw_after
        require(before == after, "WebM remux changed packet payload, timing or side data")
        raw = _run_strict(["/usr/bin/ffprobe", "-v", "warning", "-protocol_whitelist", "file,pipe",
                           "-show_entries", "format=duration", "-of", "json", str(derived)], deadline, max_stdout=4096)
        duration = float(json.loads(raw).get("format", {}).get("duration", 0))
        require(math.isfinite(duration) and 0 < duration <= MAX_DURATION, "Remux did not establish a bounded duration")
        require(abs(duration - before["max_packet_end_seconds"]) <= .1,
                "Remux duration disagrees with measured packet extent")
        derived_hash = _hash(derived, deadline, output_limit)
        require(source.stat().st_size == size and _hash(source, deadline, MAX_INPUT_BYTES) == source_hash,
                "Source WebM changed during duration verification")
    return duration, {"method": "temporary_lossless_remux_with_packet_verification", "source_sha256": source_hash,
                      "verifier_sha256": _hash(Path(__file__), deadline, MAX_INPUT_BYTES),
                      "temporary_remux_sha256": derived_hash, "seconds": duration, "packet_summary": before,
                      "original_file_modified": False, "asr_performed": False,
                      "limits": {"input_bytes": MAX_INPUT_BYTES, "total_timeout_seconds": timeout_seconds,
                                 "packets": MAX_PACKETS, "packet_metadata_bytes_per_scan": MAX_PACKET_BYTES},
                      **({"unspecified_video_duration_verification": variable} if variable is not None else {}),
                      "completeness_warning": "Valid bytes and a clean remux do not prove the recording's intended end was captured; a valid prefix can pass without an external expected duration/hash"}
