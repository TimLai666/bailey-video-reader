#!/usr/bin/env python3
"""Bounded local-media preprocessing queue; never claims the videos were watched."""
from __future__ import annotations
import argparse
import fcntl
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import reader

ROOT = Path(__file__).resolve().parent
SCHEMA = "bailey-video-batch/1"
MAX_WORKERS = 2
ESTIMATED_MB_PER_WORKER = 900
RESERVE_MB = 256


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    reader.write_json(tmp, data)
    tmp.replace(path)


def digest_json(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def available_memory_mb():
    """Best effort; cgroup headroom takes precedence when visible."""
    values, sources = [], []
    try:
        text = Path("/proc/meminfo").read_text()
        values.append(int(re.search(r"MemAvailable:\s+(\d+)", text).group(1)) / 1024)
        sources.append("/proc/meminfo MemAvailable")
    except (OSError, AttributeError, ValueError):
        pass
    try:
        limit = int(Path("/sys/fs/cgroup/memory.max").read_text())
        current = int(Path("/sys/fs/cgroup/memory.current").read_text())
        values.append(max(0, limit-current) / 1024**2)
        sources.append("cgroup v2 headroom")
    except (OSError, ValueError):
        pass
    return (min(values) if values else None), sources


def choose_workers(requested):
    if not 1 <= requested <= MAX_WORKERS:
        raise ValueError("Workers must be 1 or 2; higher concurrency is not validated")
    available, sources = available_memory_mb()
    admitted = requested
    reason = "requested worker limit"
    if available is None and requested > 1:
        admitted, reason = 1, "memory headroom unknown; conservative single worker"
    elif available is not None and available < RESERVE_MB + requested * ESTIMATED_MB_PER_WORKER:
        if available < RESERVE_MB + ESTIMATED_MB_PER_WORKER:
            raise RuntimeError("Insufficient estimated memory headroom for one worker")
        admitted, reason = 1, "memory guard reduced concurrency"
    return admitted, {"requested": requested, "admitted": admitted, "reason": reason,
                      "available_mb_at_start": available, "sources": sources,
                      "estimated_mb_per_worker": ESTIMATED_MB_PER_WORKER, "reserve_mb": RESERVE_MB,
                      "warning": "Admission estimate, not a hard memory sandbox or guarantee"}


def process_tree_rss_mb(pids):
    seen, pending = set(), list(pids)
    total = 0
    page = os.sysconf("SC_PAGE_SIZE")
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            fields = Path(f"/proc/{pid}/statm").read_text().split()
            total += int(fields[1]) * page
            child_ids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            pending.extend(int(x) for x in child_ids)
        except (OSError, ValueError, IndexError):
            pass
    return total / 1024**2


def read_inputs(args):
    values = list(args.inputs)
    if args.manifest:
        path = Path(args.manifest).resolve()
        data = json.loads(path.read_text())
        entries = data.get("inputs") if isinstance(data, dict) else data
        if not isinstance(entries, list) or not all(isinstance(v, str) for v in entries):
            raise ValueError("Input manifest must be a JSON string array, or {\"inputs\": [...]} ")
        values.extend(str((path.parent / value).resolve()) if "://" not in value and not Path(value).is_absolute()
                      else value for value in entries)
    if not values:
        raise ValueError("Supply local input paths or --manifest")
    return values


def runtime_context(args):
    reader.verify_upstream()
    model = Path(args.model).resolve()
    if not (model / "model.bin").is_file():
        raise ValueError("Batch requires an explicitly prepared local model")
    return {"reader_sha256": reader.sha256(ROOT / "reader.py"),
            "webm_duration_sha256": reader.sha256(ROOT / "capture_queue" / "webm_duration.py"),
            "batch_reader_sha256": reader.sha256(Path(__file__)),
            "upstream_manifest_sha256": reader.sha256(ROOT / "upstream-source.json"),
            "model_files": {p.name: reader.sha256(p) for p in model.glob("*") if p.is_file()},
            "versions": {name: importlib.metadata.version(name) for name in
                         ("faster-whisper", "ctranslate2", "onnxruntime", "av", "Pillow")},
            "python": sys.version.split()[0],
            "ffmpeg": reader.run(["ffmpeg", "-version"]).stdout.splitlines()[0],
            "options": {key: getattr(args, key) for key in
                        ("language", "start", "end", "width", "interval", "scene", "max_frames", "threads", "audio_stream")}}


def prepare_item(index, value, context):
    if "://" in value:
        raise ValueError("URL input is unsupported; acquire permitted local media separately")
    source = Path(value).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Input must be a regular local file")
    sidecars = [p for ext in (".srt", ".vtt") for p in source.parent.glob(source.stem + "*" + ext)
                if p.stem == source.stem or p.stem.startswith(source.stem + ".")]
    identity = {"source_sha256": reader.sha256(source), "source_bytes": source.stat().st_size,
                "sidecars": {p.name: reader.sha256(p) for p in sorted(sidecars)}, "context": context}
    key = digest_json(identity)
    stem = re.sub(r"[^a-zA-Z0-9._-]", "_", source.stem)[:50] or "media"
    return {"index": index, "input": str(source), "key": key, "identity": identity,
            "directory": f"{index:04}-{stem}-{key[:16]}", "status": "pending",
            "analysis_status": "not_performed", "understanding_claim": "none; preprocessing only"}


def validated_output(path, item):
    """Reuse only a completed, exact-context job with every artifact intact."""
    path = Path(path)
    try:
        m = json.loads((path / "manifest.json").read_text())
        stamp = json.loads((path / "batch-job.json").read_text())
        if not isinstance(m, dict) or not isinstance(stamp, dict):
            return False
        if m.get("status") != "complete" or stamp.get("key") != item["key"]:
            return False
        if stamp.get("manifest_sha256") != reader.sha256(path / "manifest.json"):
            return False
        if m["source"]["sha256"] != item["identity"]["source_sha256"]:
            return False
        if m.get("reader_sha256") != item["identity"]["context"]["reader_sha256"]:
            return False
        context = item["identity"]["context"]
        if m.get("webm_duration_sha256") != context["webm_duration_sha256"]:
            return False
        if m.get("model_files") != context["model_files"] or m.get("versions") != context["versions"]:
            return False
        if m.get("python_version") != context["python"] or m.get("ffmpeg_version") != context["ffmpeg"]:
            return False
        if not isinstance(m.get("parameters"), dict) or any(m["parameters"].get(k) != v for k, v in context["options"].items()):
            return False
        captions = json.loads((path / "captions.json").read_text())
        if not isinstance(captions, dict) or not isinstance(captions.get("tracks"), list):
            return False
        sidecars = {t["source_filename"]: t["source_sha256"] for t in captions["tracks"] if t["origin"] == "supplied_sidecar"}
        if sidecars != item["identity"]["sidecars"]:
            return False
        artifacts = m.get("artifact_sha256", {})
        if not isinstance(artifacts, dict):
            return False
        if not all(name in artifacts for name in ("asr.json", "captions.json", "frames.json", "alignment.json")):
            return False
        for name, expected in artifacts.items():
            target = (path / name).resolve()
            if not target.is_relative_to(path.resolve()) or not target.is_file() or reader.sha256(target) != expected:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def identity_still_matches(item):
    try:
        current = prepare_item(item["index"], item["input"], item["identity"]["context"])
        return current["key"] == item["key"]
    except (OSError, ValueError, KeyError):
        return False


def next_run_path(runs):
    numbers = [int(m.group(1)) for p in runs.glob("run-*.json")
               if (m := re.fullmatch(r"run-(\d+)\.json", p.name))]
    number = max(numbers, default=0) + 1
    while (runs / f"run-{number:04}.json").exists():
        number += 1
    return runs / f"run-{number:04}.json"


def stop_worker(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def run_batch(args):
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".batch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another batch already owns this output directory")
        return _run_batch_locked(args)


def coverage(path):
    path = Path(path)
    m = json.loads((path / "manifest.json").read_text())
    a = json.loads((path / "asr.json").read_text())
    c = json.loads((path / "captions.json").read_text())
    f = json.loads((path / "frames.json").read_text())
    return {"input_modality": m.get("input_modality"), "source_duration_sec": m["source_duration"],
            "processed_window": m["window"], "frame_count": len(f["frames"]),
            "largest_sample_gap_sec": f.get("largest_sample_gap_sec"), "asr_status": a["status"],
            "asr_segment_count": len(a["segments"]), "caption_track_count": len(c["tracks"]),
            "caption_issue_count": len(c["issues"]), "unclassified_audio_gap_count": len(a.get("gaps", [])),
            "low_confidence_segment_count": sum(bool(s.get("quality_flags")) for s in a["segments"]),
            "reader_elapsed_seconds": m["elapsed_seconds"], "reader_peak_rss_mb": m["peak_rss_mb"],
            "limitations": "Sampled images/estimated speech/captions only; assistant interpretation and verification remain undone"}


def child_command(item, args, output):
    cmd = [sys.executable, str(ROOT / "reader.py"), item["input"], "-o", str(output), "--model", str(Path(args.model).resolve())]
    for key in ("language", "start", "end", "width", "interval", "scene", "max_frames", "threads", "audio_stream"):
        value = getattr(args, key)
        if value is not None:
            cmd += ["--" + key.replace("_", "-"), str(value)]
    return cmd


def _run_batch_locked(args):
    start = time.monotonic()
    values = read_inputs(args)
    workers, guard = choose_workers(args.workers)
    if args.threads < 1:
        raise ValueError("CPU threads per worker must be positive")
    if args.job_timeout_seconds <= 0:
        raise ValueError("Per-item timeout must be positive")
    requested_threads = args.threads
    cpu_count = os.cpu_count() or 1
    args.threads = min(requested_threads, max(1, cpu_count // workers))
    root = Path(args.output).resolve()
    if root.exists() and any(p.name != ".batch.lock" for p in root.iterdir()):
        if not args.resume:
            raise ValueError("Batch output is not empty; use a new directory or explicit --resume")
        prior = json.loads((root / "batch.json").read_text())
        if not isinstance(prior, dict) or prior.get("schema") != SCHEMA:
            raise ValueError("Not a recognized batch output directory")
    root.mkdir(parents=True, exist_ok=True)
    (root / "jobs").mkdir(exist_ok=True)
    context = runtime_context(args)
    items = []
    for i, value in enumerate(values, 1):
        try:
            items.append(prepare_item(i, value, context))
        except (OSError, ValueError) as e:
            items.append({"index": i, "input": value, "status": "failed_preflight", "error": str(e),
                          "analysis_status": "not_performed", "understanding_claim": "none"})
    report = {"schema": SCHEMA, "created_host_utc": datetime.now(timezone.utc).isoformat(),
              "status": "running", "workers": workers, "memory_guard": guard,
              "cpu_budget": {"logical_cpus": cpu_count, "requested_threads_per_worker": requested_threads,
                             "effective_threads_per_worker": args.threads, "maximum_active_inference_threads": workers * args.threads},
              "runtime_context": context, "items": items, "analysis_status": "not_performed",
              "scope": "Local media preprocessing only. No URL acquisition or claim of completed viewing/understanding.",
              "job_timeout_seconds": args.job_timeout_seconds,
              "aggregate_peak_rss_mb_sampled": round(process_tree_rss_mb([os.getpid()]), 2), "rss_sample_interval_seconds": .05}
    pending = []
    for item in items:
        if item["status"] != "pending":
            continue
        job_root = root / "jobs" / item["directory"]
        old = sorted(job_root.glob("attempt-*/evidence"), reverse=True) if args.resume else []
        usable = next((path for path in old if validated_output(path, item)), None)
        if usable:
            item.update(status="reused_verified", output=str(usable.relative_to(root)), coverage=coverage(usable))
            continue
        job_root.mkdir(exist_ok=True)
        attempt = 1 + len(list(job_root.glob("attempt-*")))
        attempt_root = job_root / f"attempt-{attempt:04}"
        while attempt_root.exists():
            attempt += 1
            attempt_root = job_root / f"attempt-{attempt:04}"
        attempt_root.mkdir()
        item["output"] = str((attempt_root / "evidence").relative_to(root))
        pending.append(item)
    active = []
    interrupted = False
    try:
        while pending or active:
            while pending and len(active) < workers:
                item = pending.pop(0)
                output = root / item["output"]
                if not identity_still_matches(item):
                    item.update(status="failed_input_changed", error="Source or captions changed after preflight; rerun with the new inputs")
                    continue
                log = (output.parent / "worker.log").open("w", encoding="utf-8")
                try:
                    process = subprocess.Popen(child_command(item, args, output), stdout=log, stderr=subprocess.STDOUT,
                                               stdin=subprocess.DEVNULL, start_new_session=True)
                except OSError as e:
                    log.close()
                    item.update(status="failed_start", error=str(e))
                    continue
                item.update(status="running", process_id=process.pid)
                active.append({"item": item, "process": process, "log": log, "start": time.monotonic()})
            rss = process_tree_rss_mb([p["process"].pid for p in active] + [os.getpid()])
            report["aggregate_peak_rss_mb_sampled"] = max(report["aggregate_peak_rss_mb_sampled"], round(rss, 2))
            for job in list(active):
                code = job["process"].poll()
                if code is None and time.monotonic()-job["start"] > args.job_timeout_seconds:
                    stop_worker(job["process"])
                    code = job["process"].returncode
                    job["timed_out"] = True
                if code is None:
                    continue
                job["log"].close()
                item = job["item"]
                item["worker_wall_seconds"] = round(time.monotonic()-job["start"], 3)
                item["exit_code"] = code
                output = root / item["output"]
                if code == 0:
                    try:
                        m = json.loads((output / "manifest.json").read_text())
                        if m.get("status") != "complete":
                            raise ValueError("Worker exited without a complete manifest")
                        if not identity_still_matches(item):
                            raise ValueError("Source or captions changed during processing")
                        reader.write_json(output / "batch-job.json", {"key": item["key"],
                            "manifest_sha256": reader.sha256(output / "manifest.json")})
                        if not validated_output(output, item):
                            raise ValueError("Output identity/hash validation failed")
                        item.update(status="preprocessed", coverage=coverage(output))
                    except (OSError, ValueError, KeyError, TypeError) as e:
                        item.update(status="failed_validation", error=str(e))
                else:
                    item.update(status="timed_out" if job.get("timed_out") else "failed",
                                error=f"Per-item timeout exceeded ({args.job_timeout_seconds}s)" if job.get("timed_out")
                                else (output.parent / "worker.log").read_text(errors="replace")[-3000:])
                active.remove(job)
            report["elapsed_seconds"] = round(time.monotonic()-start, 3)
            atomic_json(root / "batch.json", report)
            if pending or active:
                time.sleep(.05)
    except KeyboardInterrupt:
        interrupted = True
        for item in pending:
            item["status"] = "not_started"
    finally:
        for job in active:
            stop_worker(job["process"])
            job["log"].close()
            job["item"]["status"] = "interrupted" if interrupted else "failed_batch_interruption"
        failed = sum(i["status"] not in ("preprocessed", "reused_verified") for i in items)
        report.update(status="interrupted" if interrupted else ("finished_with_failures" if failed else "preprocessing_complete"),
                      elapsed_seconds=round(time.monotonic()-start, 3),
                      counts={"total": len(items), "preprocessed": sum(i["status"] == "preprocessed" for i in items),
                              "reused_verified": sum(i["status"] == "reused_verified" for i in items), "failed_or_incomplete": failed})
        atomic_json(root / "batch.json", report)
        runs = root / "runs"
        runs.mkdir(exist_ok=True)
        atomic_json(next_run_path(runs), report)
    return report


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="*")
    p.add_argument("--manifest", help="JSON list of local paths or an object with inputs; paths are relative to this JSON file")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--workers", type=int, default=1, choices=(1, 2))
    p.add_argument("--resume", action="store_true")
    p.add_argument("--job-timeout-seconds", type=float, default=3600, help="terminate an individual stuck worker after this many seconds")
    p.add_argument("--model", default=str(ROOT / "models" / "faster-whisper-small"))
    p.add_argument("--language", default="auto")
    p.add_argument("--start", type=float, default=0)
    p.add_argument("--end", type=float)
    p.add_argument("--width", type=int, default=1600)
    p.add_argument("--interval", type=float, default=1)
    p.add_argument("--scene", type=float, default=.15)
    p.add_argument("--max-frames", type=int, default=1200)
    p.add_argument("--threads", type=int, default=4, help="CPU threads per worker; two workers multiply this budget")
    p.add_argument("--audio-stream", type=int)
    return p


if __name__ == "__main__":
    try:
        result = run_batch(parser().parse_args())
        print(json.dumps({key: result[key] for key in ("status", "elapsed_seconds", "workers", "counts", "aggregate_peak_rss_mb_sampled")}))
        sys.exit(0 if result["status"] == "preprocessing_complete" else 2)
    except (ValueError, OSError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
