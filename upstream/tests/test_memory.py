"""Tests for the cross-video memory index.

The two behaviours worth pinning down are the ones that were measured to be
wrong at first:
  * a two-character Chinese query must find its line (FTS5's trigram tokenizer
    alone returns nothing for queries shorter than 3 characters),
  * re-indexing the same source must replace its rows, not double them.
"""
import json

from claude_real_video import memory


def _fixture(tmp_path, source="/tmp/fake.mp4", speech=None, onscreen_line=True):
    out = tmp_path / "crv-out"
    out.mkdir(exist_ok=True)
    segs = speech if speech is not None else [
        {"start": 12.0, "end": 15.0, "text": "我們的定價策略是先免費再收費"},
        {"start": 30.0, "end": 33.0, "text": "this part is about pricing tiers"},
    ]
    (out / "transcript.json").write_text(json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    manifest = [f"source: {source}", "duration: 45s | frames: 10"]
    if onscreen_line:
        manifest.append("[00:12-00:15] text: 定價策略 (0.90) — frame_001.jpg")
    (out / "MANIFEST.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return out


def test_indexes_speech_and_onscreen(tmp_path):
    db = tmp_path / "m.db"
    out = _fixture(tmp_path)
    r = memory.remember(out, db_path=db)
    assert r["speech"] == 2
    assert r["onscreen"] == 1
    assert memory.stats(db_path=db)["videos"] == 1


def test_two_character_chinese_query_finds_its_line(tmp_path):
    """Regression: trigram FTS returns 0 for 2-char queries, so search() falls
    back to a substring scan. 定價 must find 定價策略."""
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    hits = memory.search("定價", db_path=db)
    assert hits, "two-character Chinese query found nothing"
    assert any("定價" in h.text for h in hits)


def test_longer_query_and_english_still_work(tmp_path):
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    assert memory.search("定價策略", db_path=db)
    assert memory.search("pricing", db_path=db)
    assert memory.search("這句話不存在於任何影片", db_path=db) == []


def test_hits_carry_a_usable_timestamp(tmp_path):
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    hit = next(h for h in memory.search("定價策略", db_path=db) if h.kind == "speech")
    assert hit.stamp() == "00:12"
    assert hit.source == "/tmp/fake.mp4"


def test_kind_filter(tmp_path):
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    assert all(h.kind == "speech" for h in memory.search("定價", kind="speech", db_path=db))
    assert all(h.kind == "onscreen" for h in memory.search("定價", kind="onscreen", db_path=db))


def test_reindexing_replaces_instead_of_duplicating(tmp_path):
    db = tmp_path / "m.db"
    out = _fixture(tmp_path)
    memory.remember(out, db_path=db)
    first = memory.stats(db_path=db)["lines"]
    memory.remember(out, db_path=db)
    assert memory.stats(db_path=db)["lines"] == first
    assert memory.stats(db_path=db)["videos"] == 1


def test_lookup_and_forget(tmp_path):
    db = tmp_path / "m.db"
    out = _fixture(tmp_path)
    memory.remember(out, db_path=db)
    assert memory.lookup("/tmp/fake.mp4", db_path=db)
    assert memory.lookup("/tmp/never-seen.mp4", db_path=db) is None
    assert memory.forget("/tmp/fake.mp4", db_path=db) is True
    assert memory.lookup("/tmp/fake.mp4", db_path=db) is None
    assert memory.forget("/tmp/fake.mp4", db_path=db) is False


def test_query_with_fts_syntax_characters_does_not_crash(tmp_path):
    """A user typing quotes, % or an asterisk must not blow up FTS or the LIKE
    fallback (% and _ are wildcards there)."""
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    for q in ['"', "100%", "a_b", "*", "AND OR NOT", "-"]:
        memory.search(q, db_path=db)  # must not raise


def test_missing_transcript_json_falls_back_to_txt(tmp_path):
    db = tmp_path / "m.db"
    out = tmp_path / "crv-out"
    out.mkdir()
    (out / "transcript.txt").write_text("plain line one\nplain line two\n", encoding="utf-8")
    (out / "MANIFEST.txt").write_text("source: /tmp/plain.mp4\nduration: 9s | frames: 3\n", encoding="utf-8")
    r = memory.remember(out, db_path=db)
    assert r["speech"] == 2
    assert memory.search("plain line", db_path=db)


def test_same_filename_in_different_folders_are_different_videos(tmp_path):
    """Regression, measured: two videos both called clip.mp4 in different folders
    shared one key, so the second was skipped and the user was pointed at the
    first video's analysis."""
    db = tmp_path / "m.db"
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "clip.mp4").write_bytes(b"a")
    (b / "clip.mp4").write_bytes(b"b")
    out_a = _fixture(a, source=str(a / "clip.mp4"))
    out_b = _fixture(b, source=str(b / "clip.mp4"))
    memory.remember(out_a, source=str(a / "clip.mp4"), db_path=db)
    memory.remember(out_b, source=str(b / "clip.mp4"), db_path=db)
    assert memory.stats(db_path=db)["videos"] == 2
    assert memory.lookup(str(a / "clip.mp4"), db_path=db)["out_dir"] == str(out_a.resolve())
    assert memory.lookup(str(b / "clip.mp4"), db_path=db)["out_dir"] == str(out_b.resolve())


def test_relative_and_absolute_path_are_the_same_video(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    (tmp_path / "clip.mp4").write_bytes(b"x")
    out = _fixture(tmp_path, source=str(tmp_path / "clip.mp4"))
    memory.remember(out, source=str(tmp_path / "clip.mp4"), db_path=db)
    monkeypatch.chdir(tmp_path)
    assert memory.lookup("clip.mp4", db_path=db) is not None
    assert memory.stats(db_path=db)["videos"] == 1


def test_params_are_stored_so_a_caller_can_tell_options_apart(tmp_path):
    """The CLI skips re-analysis only when the recorded options match, so the
    options have to survive a round trip."""
    db = tmp_path / "m.db"
    out = _fixture(tmp_path)
    memory.remember(out, params={"scene": 0.3, "lang": "auto"}, db_path=db)
    row = memory.lookup("/tmp/fake.mp4", db_path=db)
    assert json.loads(row["params"]) == {"lang": "auto", "scene": 0.3}


def test_v1_index_upgrades_without_losing_rows(tmp_path):
    """An index written by the first release must survive the params column."""
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table meta (key text primary key, value text);
        insert into meta values('schema_version','1');
        create table videos (id integer primary key, source text unique not null,
            out_dir text not null, title text, duration real, frame_count integer,
            tool text, watched_at real not null);
        create table lines (id integer primary key, video_id integer not null,
            t_start real, t_end real, kind text not null, text text not null);
        create virtual table lines_fts using fts5(text, content='lines', content_rowid='id', tokenize='trigram');
        insert into videos(source,out_dir,tool,watched_at) values('/tmp/old.mp4','/tmp/old-out','crv',1.0);
        insert into lines(video_id,t_start,t_end,kind,text) values(1,1.0,2.0,'speech','舊索引裡的定價策略');
        insert into lines_fts(rowid,text) select id,text from lines;
        """
    )
    con.commit()
    con.close()
    assert memory.stats(db_path=db)["videos"] == 1          # triggers the migration
    assert memory.lookup("/tmp/old.mp4", db_path=db) is not None
    assert memory.search("定價", db_path=db)                 # old rows still searchable


def test_empty_query_is_not_a_wildcard(tmp_path):
    """Regression, from the codex review: an empty query fell through to a LIKE
    '%%' scan and listed the whole index."""
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    assert memory.search("", db_path=db) == []
    assert memory.search("   ", db_path=db) == []


def test_limit_is_clamped(tmp_path):
    """-1 means 'no limit' to SQLite, which would dump the database."""
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    assert len(memory.search("定價", limit=-1, db_path=db)) <= 200
    assert len(memory.search("定價", limit=10**9, db_path=db)) <= 200


def test_refuses_to_downgrade_a_newer_index(tmp_path):
    """A build that only knows v2 must not write its version over a v99 index —
    that would silently break the newer side sharing the same file."""
    import sqlite3
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    con = sqlite3.connect(db)
    con.execute("insert or replace into meta values('schema_version','99')")
    con.commit()
    con.close()
    try:
        memory.stats(db_path=db)
    except memory.SchemaTooNew:
        pass
    else:
        raise AssertionError("a newer schema was silently downgraded")


def test_prune_keeps_the_newest_and_reclaims(tmp_path):
    db = tmp_path / "m.db"
    for i in range(4):
        d = tmp_path / f"v{i}"
        d.mkdir()
        out = _fixture(d, source=f"/tmp/v{i}.mp4")
        memory.remember(out, source=f"/tmp/v{i}.mp4", db_path=db)
    assert memory.stats(db_path=db)["videos"] == 4
    r = memory.prune(keep=2, db_path=db)
    assert r["removed"] == 2
    assert memory.stats(db_path=db)["videos"] == 2
    assert memory.lookup("/tmp/v3.mp4", db_path=db) is not None   # newest survived
    assert memory.lookup("/tmp/v0.mp4", db_path=db) is None       # oldest gone


def test_updating_a_line_keeps_the_index_in_sync(tmp_path):
    """External-content FTS needs an UPDATE trigger; without it search would
    still return the old words after an edit."""
    import sqlite3
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    con = sqlite3.connect(db)
    con.execute("update lines set text='換成完全不同的句子' where kind='speech' and t_start=12.0")
    con.commit()
    con.close()
    assert memory.search("換成完全不同", db_path=db)
    assert not [h for h in memory.search("先免費再收費", db_path=db) if h.t_start == 12.0]


def test_videos_listing_is_paginated(tmp_path):
    db = tmp_path / "m.db"
    for i in range(3):
        d = tmp_path / f"p{i}"
        d.mkdir()
        memory.remember(_fixture(d, source=f"/tmp/p{i}.mp4"), source=f"/tmp/p{i}.mp4", db_path=db)
    assert len(memory.videos(limit=2, db_path=db)) == 2
    assert len(memory.videos(limit=2, offset=2, db_path=db)) == 1


def test_free_reanalysis_clears_pro_facets_via_cascade(tmp_path):
    """Round-three blocker: the shared layer used UPDATE for an existing source,
    so Pro's shots/events/rhythm stayed attached to the new analysis. remember()
    now replaces the row and ON DELETE CASCADE clears every child table —
    including ones this module has never heard of."""
    import sqlite3
    db = tmp_path / "m.db"
    out = _fixture(tmp_path)
    memory.remember(out, source="/tmp/fake.mp4", tool="crv-pro", db_path=db)
    con = memory._connect(db)
    con.execute("create table if not exists shots (id integer primary key, "
                "video_id integer not null references videos(id) on delete cascade, camera text)")
    vid = con.execute("select id from videos where source='/tmp/fake.mp4'").fetchone()["id"]
    con.execute("insert into shots(video_id, camera) values (?, 'zoom-in')", (vid,))
    con.commit()
    con.close()
    memory.remember(out, source="/tmp/fake.mp4", tool="crv", db_path=db)   # free re-run
    con = sqlite3.connect(db)
    left = con.execute("select count(*) from shots").fetchone()[0]
    con.close()
    assert left == 0, "stale Pro facets survived a free-edition re-analysis"


def test_prune_negative_keep_reports_reality(tmp_path):
    db = tmp_path / "m.db"
    memory.remember(_fixture(tmp_path), db_path=db)
    r = memory.prune(keep=-1, db_path=db)
    assert r == {"removed": 1, "kept": 0}


def test_migration_failure_rolls_back_and_releases_the_lock(tmp_path, monkeypatch):
    """Round three: an exception mid-migration left the connection inside the
    transaction, holding the write lock until GC. _connect must roll back,
    close, and re-raise — and a second connect must work immediately."""
    import sqlite3
    db = tmp_path / "m.db"
    real_statements = memory._SCHEMA_STATEMENTS
    monkeypatch.setattr(memory, "_SCHEMA_STATEMENTS",
                        real_statements + ("this is not sql",))
    try:
        memory.stats(db_path=db)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("broken migration did not raise")
    monkeypatch.setattr(memory, "_SCHEMA_STATEMENTS", real_statements)
    assert memory.stats(db_path=db)["videos"] == 0   # lock released, migration retried
