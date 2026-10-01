"""Own generated WebM with optional DefaultDuration removed, never browser proof."""
import builtins
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reader
from capture_queue import queue_reader as queue
from capture_queue import webm_duration as repair


@pytest.fixture
def unspecified(tmp_path):
    path = tmp_path / "generated.webm"
    subprocess.run(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.2", "-c:v", "libvpx-vp9",
                    "-deadline", "realtime", "-cpu-used", "8", "-threads", "1", "-c:a", "libopus",
                    "-write_crc32", "0", "-live", "1", str(path)], check=True, capture_output=True)
    raw = path.read_bytes()
    # Replace the optional track DefaultDuration with an equal-size EBML Void.
    # No encoded packet bytes, offsets, parent sizes or CRCs are changed.
    position = raw.index(bytes.fromhex("23e383"))
    assert position < raw.index(bytes.fromhex("1f43b675"))
    assert raw[position + 3] & 128
    length = 4 + (raw[position + 3] & 127)
    assert 4 < length < 16
    changed = raw[:position] + bytes([0xec, 0x80 | (length - 2)]) + bytes(length - 2) + raw[position + length:]
    path.write_bytes(changed)
    assert "duration" not in reader.probe(path)["format"]
    return path


def test_generated_unspecified_video_duration_has_verified_audio_basis(unspecified):
    before = unspecified.read_bytes()
    duration, proof = repair.resolve_duration(unspecified, reader.probe(unspecified))
    detail = proof["unspecified_video_duration_verification"]
    assert 1.15 < duration < 1.3
    assert detail["source_unspecified_video_packet_durations"] == 6
    assert detail["source_raw_packet_sha256"] != detail["remux_raw_packet_sha256"]
    assert detail["decoded_timeline"]["streams"]["0"]["frame_count"] == 6
    assert detail["decoded_timeline"]["streams"]["1"]["samples"] == 57600
    assert detail["decoded_timeline"]["video_tail_hold_unverified_seconds"] > 0
    assert unspecified.read_bytes() == before
    assert proof["source_sha256"] == hashlib.sha256(before).hexdigest()


def test_reader_and_queue_accept_original_without_asr(unspecified, tmp_path, monkeypatch):
    original_import = builtins.__import__
    def guard(name, *args, **kwargs):
        assert name.split('.')[0] not in {"faster_whisper", "onnxruntime", "ctranslate2", "huggingface_hub", "av"}
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guard)
    out = tmp_path / "output"
    result = reader.process(reader.parser().parse_args([str(unspecified), "-o", str(out), "--skip-asr", "--interval", ".3"]))
    assert result["status"] == "complete_without_asr"
    assert json.loads((out/"asr.json").read_text())["segments"] == []
    assert (out/"audio_original.mka").stat().st_size > 0
    assert list((out/"frames").glob("*.jpg"))
    q = tmp_path/"queue"; queue.initialize(q); sid = str(uuid.uuid4())
    job = queue.register(q, sid, "own generated optional-duration fixture")
    state = queue.ready(q, job["job_id"], sid, str(unspecified), queue.hashfile(unspecified))
    assert state["state"] == "ready"


@pytest.mark.parametrize("field", ["duration_time", "pts_time", "dts_time", "data_hash", "size", "flags"])
def test_remux_must_preserve_known_duration_and_packet_identity(unspecified, monkeypatch, field):
    original = repair._run_strict
    def altered(command, deadline, **kwargs):
        callback = kwargs.get("on_line")
        if callback and "-show_packets" in command and str(command[-1]).endswith("duration-verified.webm"):
            def changed(line):
                # Audio duration is known in the source and must never be normalized away.
                if line.startswith(b"stream_index=1|"):
                    replacements = {"duration_time":b"0.021000", "pts_time":b"0.999000", "dts_time":b"0.999000",
                                    "data_hash":b"SHA256:"+b"0"*64, "size":b"1", "flags":b"__"}
                    line = re.sub(field.encode()+rb"=[^|]*", field.encode()+b"="+replacements[field], line)
                callback(line)
            kwargs["on_line"] = changed
        return original(command, deadline, **kwargs)
    monkeypatch.setattr(repair, "_run_strict", altered)
    with pytest.raises(ValueError, match="changed packet|endpoint|duration exceeds"):
        repair.resolve_duration(unspecified, reader.probe(unspecified))


def test_changed_decoded_timeline_is_rejected(unspecified, monkeypatch):
    original = repair._decoded_timeline
    def changed(path, *args):
        result = original(path, *args)
        if Path(path).name == "duration-verified.webm":
            result["streams"]["0"]["frame_timing_sha256"] = "0"*64
        return result
    monkeypatch.setattr(repair, "_decoded_timeline", changed)
    with pytest.raises(ValueError, match="decoded frame"):
        repair.resolve_duration(unspecified, reader.probe(unspecified))


def test_no_audio_basis_is_refused(unspecified):
    metadata = reader.probe(unspecified)
    metadata["streams"][1]["codec_type"] = "video"
    metadata["streams"][1]["codec_name"] = "vp9"
    with pytest.raises(ValueError, match="audio endpoint"):
        repair.resolve_duration(unspecified, metadata)


@pytest.mark.parametrize("field,value", [(b"pts_time",b"N/A"),(b"dts_time",b"N/A"),(b"duration_time",b"N/A")])
def test_missing_audio_duration_or_packet_pts_dts_is_refused(unspecified, monkeypatch, field, value):
    original = repair._run_strict
    def altered(command, deadline, **kwargs):
        callback = kwargs.get("on_line")
        if callback and "-show_packets" in command:
            kwargs["on_line"] = lambda line: callback(re.sub(field+rb"=[^|]*",field+b"="+value,line) if line.startswith(b"stream_index=1|") else line)
        return original(command, deadline, **kwargs)
    monkeypatch.setattr(repair, "_run_strict", altered)
    with pytest.raises(ValueError, match="incomplete|Only video"):
        repair.resolve_duration(unspecified, reader.probe(unspecified))


def test_truncation_still_refused(unspecified):
    raw=unspecified.read_bytes();unspecified.write_bytes(raw[:len(raw)*3//4])
    with pytest.raises(ValueError, match="warnings/errors|tool failed"):
        repair.resolve_duration(unspecified, reader.probe(unspecified))


def test_extra_decode_obeys_shared_deadline(unspecified, monkeypatch):
    original=repair._decoded_timeline
    def too_late(path, deadline, metadata):
        return original(path,time.monotonic()-1,metadata)
    monkeypatch.setattr(repair,"_decoded_timeline",too_late)
    with pytest.raises(TimeoutError):
        repair.resolve_duration(unspecified,reader.probe(unspecified))
