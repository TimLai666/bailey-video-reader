"""Persistent memory across analyses: watch once, ask again later.

Why this exists: crv already writes a timestamped transcript (transcript.json)
and per-second on-screen text (MANIFEST.txt), but every question meant finding
the right output folder by hand and re-reading it. Worse, asking "which video
did someone talk about pricing in" had no answer at all — each analysis was an
island.

This module keeps one local index of everything crv has ever watched, so:
  * re-watching the same source is skipped (the analysis is already on disk),
  * a plain-language question searches every video at once and answers with
    the source, the timestamp and the line itself.

Design notes:
  * SQLite + FTS5 with the **trigram** tokenizer. The default tokenizers split
    on whitespace, which finds nothing in Chinese/Japanese; trigram matches
    substrings, so 「定價」 hits inside 「我們的定價策略」. Requires SQLite 3.34+;
    if the build lacks trigram we fall back to unicode61 (English still works)
    and record which tokenizer was used.
  * Everything is local: one file, no server, no embeddings, no network.
  * Nothing here is destructive — indexing the same analysis twice replaces
    that video's rows instead of duplicating them.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(os.environ.get("CRV_MEMORY_DB", "~/.crv/memory.db")).expanduser()
SCHEMA_VERSION = 2


def canonical_source(source: str) -> str:
    """The key a video is remembered under.

    Local paths are resolved to an absolute path. Measured bug this fixes:
    two different videos both called `clip.mp4` in different folders were the
    same key, so the second one was skipped and the user was pointed at the
    *first* video's analysis. URLs are left exactly as typed — normalising them
    (stripping query strings, say) would merge genuinely different videos.
    """
    s = (source or "").strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        return s
    try:
        p = Path(s).expanduser()
        if p.exists():
            return str(p.resolve())
    except OSError:
        pass
    return s


class SchemaTooNew(RuntimeError):
    """The index was written by a newer crv. Refuse rather than downgrade it."""


# One statement per entry: executescript() would end the migration transaction.
_SCHEMA_STATEMENTS = (
    """create table if not exists videos (
            id          integer primary key,
            source      text unique not null,   -- URL or absolute local path
            out_dir     text not null,
            title       text,
            duration    real,
            frame_count integer,
            tool        text,                   -- 'crv' or 'crv-pro'
            params      text,                   -- json of the options that change the analysis
            watched_at  real not null
        )""",
    """create table if not exists lines (
            id       integer primary key,
            video_id integer not null references videos(id) on delete cascade,
            t_start  real,
            t_end    real,
            kind     text not null,             -- 'speech' | 'onscreen' | 'note'
            text     text not null
        )""",
    "create index if not exists lines_video on lines(video_id)",
    """create virtual table if not exists lines_fts using fts5(
            text, content='lines', content_rowid='id', tokenize='{tok}'
        )""",
    """create trigger if not exists lines_ai after insert on lines begin
            insert into lines_fts(rowid, text) values (new.id, new.text);
        end""",
    """create trigger if not exists lines_ad after delete on lines begin
            insert into lines_fts(lines_fts, rowid, text) values('delete', old.id, old.text);
        end""",
    # Without this, any later UPDATE of lines.text leaves the FTS index pointing
    # at the old words (external-content tables are not automatic).
    """create trigger if not exists lines_au after update on lines begin
            insert into lines_fts(lines_fts, rowid, text) values('delete', old.id, old.text);
            insert into lines_fts(rowid, text) values (new.id, new.text);
        end""",
)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    con = sqlite3.connect(str(path), timeout=15.0)   # wait out another writer
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=ON")            # off by default; the cascade needs it
    con.execute("pragma busy_timeout=15000")
    # Transcripts are personal. Don't leave them at whatever umask says — and
    # fix up an index created earlier under a looser umask, not only fresh ones.
    try:
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
    except OSError:
        pass
    if fresh and not os.environ.get("CRV_MEMORY_QUIET"):
        # Tell the user once, the first time the index exists. Silently starting
        # to keep transcripts and on-screen text forever is not acceptable just
        # because it is local. (No claim about permissions here: the chmod above
        # can fail on exotic filesystems and we would not know.)
        import sys as _sys
        print(
            f"note: crv now keeps a local index of what it watched at {path} "
            f"(transcripts + on-screen text) so you can "
            f'ask across videos later with crv-ask "…". '
            f"Turn it off with CRV_NO_MEMORY=1; clear it with crv-ask --prune 0.",
            file=_sys.stderr,
        )
    try:
        _migrate(con)
    except BaseException:
        con.close()   # do not leak a connection holding the write lock
        raise
    return con


def _tokenizer(con: sqlite3.Connection) -> str:
    """trigram if this SQLite build has it (needed for CJK), else unicode61."""
    try:
        con.execute("create virtual table if not exists _tok_probe using fts5(x, tokenize='trigram')")
        con.execute("drop table if exists _tok_probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


def _migrate(con: sqlite3.Connection) -> None:
    con.execute("create table if not exists meta (key text primary key, value text)")
    row = con.execute("select value from meta where key='schema_version'").fetchone()
    found = int(row["value"]) if row else None
    if found == SCHEMA_VERSION:
        return
    if found is not None and found > SCHEMA_VERSION:
        # A newer crv (or the Pro edition) owns this file. Writing our older
        # schema number back would silently downgrade it and corrupt the
        # contract for the newer side, so refuse instead.
        raise SchemaTooNew(
            f"this memory index is schema v{found}, this build only knows v{SCHEMA_VERSION} — "
            f"upgrade crv (pip install -U claude-real-video crv-pro) or point CRV_MEMORY_DB elsewhere"
        )
    # ⚠️ Do not use executescript() here: it commits any open transaction first
    # (measured: in_transaction goes True → False), so a BEGIN IMMEDIATE would be
    # dropped before the first CREATE and the DDL would not be atomic with the
    # version key. Statements are issued one at a time inside the lock instead.
    con.execute("begin immediate")                   # one migrator at a time
    try:
        # Re-read under the lock: another process may have migrated while we
        # waited — and it may have been a NEWER build, which must be refused
        # exactly like the first check did. A bare equality check here would
        # let an old build overwrite a newer schema's version key (round three).
        again = con.execute("select value from meta where key='schema_version'").fetchone()
        now = int(again["value"]) if again else 0
        if now > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"this memory index is schema v{now}, this build only knows v{SCHEMA_VERSION} — "
                f"upgrade crv (pip install -U claude-real-video crv-pro) or point CRV_MEMORY_DB elsewhere"
            )
        if now == SCHEMA_VERSION:
            con.execute("commit")                    # another process migrated while we waited
            return
        if found is not None:  # upgrading an existing index: add what v2 needs, keep the rows
            cols = {r[1] for r in con.execute("pragma table_info(videos)")}
            if "params" not in cols:
                con.execute("alter table videos add column params text")
        tok = _tokenizer(con)
        for stmt in _SCHEMA_STATEMENTS:
            con.execute(stmt.format(tok=tok))
        # Triggers never backfill an external-content FTS table, so anything that
        # existed before this migration is invisible to search until we rebuild.
        # If the rebuild fails, the migration fails: committing the version key
        # over a broken index would make old rows unsearchable forever, and no
        # later connection would retry (the version already reads as current).
        con.execute("insert into lines_fts(lines_fts) values('rebuild')")
        con.execute("insert or replace into meta(key,value) values('schema_version',?)", (str(SCHEMA_VERSION),))
        con.execute("insert or replace into meta(key,value) values('tokenizer',?)", (tok,))
        con.commit()
    except BaseException:
        # Roll back explicitly: relying on GC to release the write lock keeps
        # every other process waiting out its full 15s busy timeout.
        try:
            con.execute("rollback")
        except sqlite3.OperationalError:
            pass
        raise


# ---------------------------------------------------------------- parsing

_MANIFEST_PERCEPTION = re.compile(
    r"^\[(\d+):(\d+)-(\d+):(\d+)\]\s+([a-z_-]+):\s+(.*?)(?:\s+\(\d\.\d+\))?(?:\s+—\s+\S+)?$"
)


def _parse_transcript(out_dir: Path) -> list[tuple[float | None, float | None, str, str]]:
    """Prefer transcript.json (has timestamps); fall back to transcript.txt."""
    rows: list[tuple[float | None, float | None, str, str]] = []
    j = out_dir / "transcript.json"
    if j.exists():
        try:
            data = json.loads(j.read_text(encoding="utf-8", errors="replace"))
            segs = data.get("segments") if isinstance(data, dict) else data
            for s in segs or []:
                text = (s.get("text") or "").strip()
                if text:
                    rows.append((s.get("start"), s.get("end"), "speech", text))
            if rows:
                return rows
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass  # fall through to the plain text file
    t = out_dir / "transcript.txt"
    if t.exists():
        for line in t.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                rows.append((None, None, "speech", line))
    return rows


def _parse_manifest(out_dir: Path) -> tuple[dict, list[tuple[float, float, str, str]]]:
    """Pull header facts plus the timestamped perception lines out of MANIFEST.txt."""
    facts: dict = {}
    rows: list[tuple[float, float, str, str]] = []
    m = out_dir / "MANIFEST.txt"
    if not m.exists():
        return facts, rows
    for line in m.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("source:") and "source" not in facts:
            facts["source"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("duration:") and "duration" not in facts:
            d = re.search(r"duration:\s*(\d+(?:\.\d+)?)s", stripped)
            if d:
                facts["duration"] = float(d.group(1))
            f = re.search(r"frames:\s*(\d+)", stripped)
            if f:
                facts["frame_count"] = int(f.group(1))
        else:
            hit = _MANIFEST_PERCEPTION.match(stripped)
            if hit:
                m1, s1, m2, s2, kind, text = hit.groups()
                text = text.strip()
                if text:
                    rows.append((
                        int(m1) * 60 + int(s1),
                        int(m2) * 60 + int(s2),
                        "onscreen" if kind == "text" else "note",
                        f"{kind}: {text}" if kind != "text" else text,
                    ))
    return facts, rows


# ---------------------------------------------------------------- public API


@dataclass
class Hit:
    source: str
    out_dir: str
    kind: str
    t_start: float | None
    text: str
    watched_at: float

    def stamp(self) -> str:
        if self.t_start is None:
            return "--:--"
        return f"{int(self.t_start) // 60:02d}:{int(self.t_start) % 60:02d}"


def remember(out_dir: str | Path, source: str | None = None, tool: str = "crv",
             params: dict | None = None, db_path: Path | None = None) -> dict:
    """Index one finished analysis. Returns a small summary dict.

    Safe to call repeatedly: a source that is already indexed has its old rows
    replaced, so re-running an analysis updates the memory instead of doubling it.
    """
    out_dir = Path(out_dir).expanduser().resolve()
    facts, perception = _parse_manifest(out_dir)
    src = canonical_source(source or facts.get("source") or str(out_dir))
    params_json = json.dumps(params, sort_keys=True, ensure_ascii=False) if params else None
    speech = _parse_transcript(out_dir)
    rows = speech + perception
    con = _connect(db_path)
    try:
        con.execute("begin immediate")   # take the writer lock before we look
        # Replace, don't update: deleting the row lets ON DELETE CASCADE clear
        # every child table — lines here, plus the Pro facet tables (shots/
        # events/rhythm) when the Pro edition added them. An UPDATE kept the old
        # facets attached to the new analysis, so a free-edition re-run served
        # stale Pro data under a fresh out_dir (found in review round three).
        con.execute("delete from videos where source=?", (src,))
        cur = con.execute(
            "insert into videos(source,out_dir,duration,frame_count,tool,params,watched_at) "
            "values(?,?,?,?,?,?,?)",
            (src, str(out_dir), facts.get("duration"), facts.get("frame_count"), tool,
             params_json, time.time()),
        )
        vid = cur.lastrowid
        con.executemany(
            "insert into lines(video_id,t_start,t_end,kind,text) values(?,?,?,?,?)",
            [(vid, a, b, k, t) for a, b, k, t in rows],
        )
        con.commit()
    finally:
        con.close()
    return {"source": src, "out_dir": str(out_dir), "lines": len(rows),
            "speech": len(speech), "onscreen": len(perception)}


def lookup(source: str, db_path: Path | None = None) -> dict | None:
    """Has this source been watched before? Returns its row, or None."""
    con = _connect(db_path)
    try:
        row = con.execute("select * from videos where source=?", (canonical_source(source),)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def search(query: str, limit: int = 12, kind: str | None = None,
           db_path: Path | None = None) -> list[Hit]:
    """Full-text search across every indexed video.

    Two passes on purpose. FTS5's trigram tokenizer indexes 3-character
    sequences, so a two-character query like 「定價」 matches nothing at all
    (measured: 定價策略 → 2 hits, 定價 → 0). Two-character words are the common
    case in Chinese, so when FTS returns nothing we fall back to a substring
    scan. The index is local and line-sized, so the scan is cheap; correctness
    beats purity here.
    """
    query = (query or "").strip()
    if not query:
        return []                                   # an empty query used to dump the whole index
    limit = max(1, min(200, int(limit)))
    con = _connect(db_path)
    try:
        base = (
            "select v.source, v.out_dir, l.kind, l.t_start, l.text, v.watched_at "
            "from {src} join videos v on v.id = l.video_id where {cond}"
        )
        # FTS5 treats bare punctuation as syntax; quoting makes any user string safe.
        sql = base.format(src="lines_fts f join lines l on l.id = f.rowid",
                          cond="lines_fts match ?")
        params: list = ['"' + query.replace('"', '""') + '"']
        if kind:
            sql += " and l.kind = ?"
            params.append(kind)
        sql += " order by rank limit ?"
        params.append(limit)
        # The unicode61 fallback tokenizer drops punctuation, so an FTS hit is
        # only a candidate — keep it if the text really contains what was typed.
        hits = [Hit(**dict(r)) for r in con.execute(sql, params)
                if query.casefold() in (r["text"] or "").casefold()]
        if hits:
            return hits

        sql = base.format(src="lines l", cond="l.text like ? escape '\\'")
        like = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = [f"%{like}%"]
        if kind:
            sql += " and l.kind = ?"
            params.append(kind)
        sql += " order by l.video_id desc, l.t_start limit ?"
        params.append(limit)
        return [Hit(**dict(r)) for r in con.execute(sql, params)]
    finally:
        con.close()


def videos(limit: int = 200, offset: int = 0, db_path: Path | None = None) -> list[dict]:
    limit = max(1, min(1000, int(limit)))
    con = _connect(db_path)
    try:
        return [dict(r) for r in con.execute(
            "select v.*, (select count(*) from lines where video_id=v.id) as lines "
            "from videos v order by watched_at desc limit ? offset ?", (limit, max(0, int(offset))))]
    finally:
        con.close()


def stats(db_path: Path | None = None) -> dict:
    con = _connect(db_path)
    try:
        tok = con.execute("select value from meta where key='tokenizer'").fetchone()
        return {
            "db": str(Path(db_path) if db_path else DB_PATH),
            "videos": con.execute("select count(*) from videos").fetchone()[0],
            "lines": con.execute("select count(*) from lines").fetchone()[0],
            "tokenizer": tok["value"] if tok else "unknown",
        }
    finally:
        con.close()


def prune(keep: int = 500, db_path: Path | None = None) -> dict:
    """Drop the oldest videos beyond `keep`, then reclaim the space.

    The index grows without bound otherwise — every video adds its transcript
    and one line per second of on-screen text, and the substring fallback is a
    table scan, so an unbounded index makes *misses* slower over time.
    """
    keep = max(0, int(keep))
    con = _connect(db_path)
    try:
        con.execute("begin immediate")
        old = [r["id"] for r in con.execute(
            "select id from videos order by watched_at desc limit -1 offset ?", (keep,))]
        for vid in old:
            con.execute("delete from videos where id=?", (vid,))   # cascade clears children
        remaining = con.execute("select count(*) from videos").fetchone()[0]
        con.commit()
        con.execute("insert into lines_fts(lines_fts) values('optimize')")
        con.commit()
    finally:
        con.close()
    con = sqlite3.connect(str(Path(db_path) if db_path else DB_PATH), timeout=15.0)
    try:
        con.execute("vacuum")
    finally:
        con.close()
    # `kept` is what actually remains, not the argument — another process can
    # insert between our commit and the count, and the argument may be -1.
    return {"removed": len(old), "kept": remaining}


def forget(source: str, db_path: Path | None = None) -> bool:
    """Drop one video from the index. Does not touch any files on disk."""
    con = _connect(db_path)
    try:
        row = con.execute("select id from videos where source=?", (canonical_source(source),)).fetchone()
        if not row:
            return False
        con.execute("delete from lines where video_id=?", (row["id"],))
        con.execute("delete from videos where id=?", (row["id"],))
        con.commit()
        return True
    finally:
        con.close()


# ---------------------------------------------------------------- CLI


def main() -> None:
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        prog="crv-ask",
        description="Ask across every video crv has watched. Answers cite the video and the timestamp.",
    )
    ap.add_argument("query", nargs="*", help="What to look for, e.g. crv-ask 定價 策略")
    ap.add_argument("-n", "--limit", type=int, default=12, help="Max hits (default 12)")
    ap.add_argument("--kind", choices=["speech", "onscreen", "note"], default=None,
                    help="Only search spoken words, on-screen text, or perception notes")
    ap.add_argument("--list", action="store_true", help="List every watched video instead of searching")
    ap.add_argument("--stats", action="store_true", help="Show where the index lives and how big it is")
    ap.add_argument("--remember", metavar="OUT_DIR", default=None,
                    help="Index an existing analysis folder (normally automatic)")
    ap.add_argument("--forget", metavar="SOURCE", default=None, help="Remove one video from the index")
    ap.add_argument("--prune", type=int, metavar="KEEP", default=None,
                    help="Keep only the newest KEEP videos, then reclaim disk space")
    a = ap.parse_args()

    if a.stats:
        s = stats()
        print(f"index: {s['db']}\nvideos: {s['videos']}  lines: {s['lines']}  tokenizer: {s['tokenizer']}")
        return
    if a.remember:
        r = remember(a.remember)
        print(f"indexed {r['lines']} lines ({r['speech']} spoken, {r['onscreen']} on-screen) — {r['source']}")
        return
    if a.forget:
        print("removed" if forget(a.forget) else "not in the index")
        return
    if a.prune is not None:
        r = prune(a.prune)
        print(f"removed {r['removed']} video(s), kept the newest {r['kept']}")
        return
    if a.list:
        rows = videos()
        if not rows:
            print("nothing watched yet")
            return
        for v in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["watched_at"]))
            dur = f"{v['duration']:.0f}s" if v["duration"] else "?"
            print(f"{when}  {dur:>6}  {v['lines']:>5} lines  {v['tool']:<8} {v['source']}")
        return

    query = " ".join(a.query).strip()
    if not query:
        ap.error("give me something to look for, or use --list / --stats")
    hits = search(query, limit=a.limit, kind=a.kind)
    if not hits:
        print(f"no match for {query!r} in {stats()['videos']} watched video(s)")
        return
    last = None
    for h in hits:
        if h.source != last:
            print(f"\n{h.source}")
            last = h.source
        print(f"  [{h.stamp()}] {h.kind:<8} {h.text[:160]}")


if __name__ == "__main__":
    main()
