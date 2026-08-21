#!/usr/bin/env python3
"""
Build docs.db — the prebuilt hybrid-search database for the module knowledge base.

Scans modules/**/<ver>/ into:
  - modules       : one row per documented module version
  - docs          : one row per doc unit (start / usage-section / agent file / data-synth)
  - docs_fts      : FTS5 (BM25) lexical index, rowid == docs.id
  - vec_docs      : sqlite-vec vectors, rowid == docs.id
  - meta          : build provenance + model guard

This is BUILD-TIME tooling (runs in CI). Not shipped to consumers.

Usage:
  python indexer.py --corpus ../modules --out docs.db
  python indexer.py --corpus ../modules --out sample.db --limit 200   # quick iteration
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import struct
import sys
import time

import sqlite_vec

from embed import MODEL_NAME, MODEL_DIM, embed as embed_texts

SCHEMA_VERSION = "1"
# The corpus mixes three version-dir shapes (see AGENTS.md): minor 'N.N.x' (e.g. 3.6.x),
# major-only 'N.x' (e.g. 3.x), and legacy '8.x-N.x' / '9.x-N.x'. Accept all of them.
VERSION_RE = re.compile(r"^(?:\d+\.x-\d+\.x|\d+\.\d+\.x|\d+\.x)$")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def version_key(v):
    """Sort key so the newest version of a module sorts last, across all dir shapes."""
    v = v or ""
    m = re.match(r"^\d+\.x-(\d+)\.x$", v)          # legacy 8.x-1.x -> module major 1
    if m:
        return (int(m.group(1)), 0)
    m = re.match(r"^(\d+)\.(\d+)\.x$", v)          # 3.6.x
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d+)\.x$", v)                 # major-only 3.x
    if m:
        return (int(m.group(1)), 0)
    return (-1, -1)


# --------------------------------------------------------------------------- #
# Corpus -> rows                                                               #
# --------------------------------------------------------------------------- #

def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def split_usage(text):
    """usage.md is short/long summary + use-cases separated by '---'. One unit each."""
    parts = [p.strip() for p in re.split(r"^\s*---\s*$", text, flags=re.M)]
    return [p for p in parts if p]


def first_heading(md):
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return None


def collect_docs(version_dir):
    """Yield (doc_type, doc_path, title, body) units for one version dir."""
    # start.md
    start = read(os.path.join(version_dir, "agent", "start.md"))
    if start and start.strip():
        yield ("start", "agent/start.md", first_heading(start) or "start", start.strip())

    # usage.md -> one unit per '---' section
    usage = read(os.path.join(version_dir, "usage.md"))
    if usage and usage.strip():
        for i, sec in enumerate(split_usage(usage)):
            yield ("usage", f"usage.md#{i}", first_heading(sec) or f"usage {i}", sec)

    # every agent/**/*.md except start.md (already emitted)
    agent_dir = os.path.join(version_dir, "agent")
    for root, _d, files in os.walk(agent_dir):
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, version_dir).replace(os.sep, "/")
            if rel == "agent/start.md":
                continue
            body = read(full)
            if body and body.strip():
                yield ("agent", rel, first_heading(body) or rel, body.strip())


def data_synth(d):
    """A compact searchable sentence synthesized from data.json metadata.

    Includes the enriched `project_description` (the real Drupal.org project-page body)
    when present — it is far richer search signal than the info.yml one-liner.
    """
    bits = [d.get("name") or "", d.get("description") or "", d.get("project_description") or ""]
    kw = d.get("keywords") or []
    cats = (d.get("categories") or []) + (d.get("subcategories") or [])
    if kw:
        bits.append("Keywords: " + ", ".join(kw))
    if cats:
        bits.append("Categories: " + ", ".join(cats))
    return ". ".join(b for b in bits if b)


def scan_corpus(corpus_root, limit=None):
    """Return (modules, docs) as lists of dicts. docs carry a temp module ref index."""
    modules, docs = [], []
    # Group version dirs by machine_name to compute is_latest.
    by_name = {}
    for data_path in glob.iglob(os.path.join(corpus_root, "**", "data.json"), recursive=True):
        vdir = os.path.dirname(data_path)
        version = os.path.basename(vdir)
        if not VERSION_RE.match(version):
            continue
        try:
            with open(data_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        machine = d.get("data_name") or d.get("name")
        if not machine:
            continue
        by_name.setdefault(machine, []).append((version, vdir, d))

    names = sorted(by_name)
    if limit:
        # For quick iteration, take the most-installed modules (by their latest
        # version's active_installs) so the sample is representative, not all 'a*'.
        def installs(machine):
            return max((e[2].get("active_installs") or 0) for e in by_name[machine])
        names = sorted(names, key=installs, reverse=True)[:limit]
        names.sort()  # stable ordering for deterministic ids

    mod_id = 0
    doc_id = 0
    for machine in names:
        entries = sorted(by_name[machine], key=lambda e: version_key(e[0]))
        latest_version = entries[-1][0]
        for version, vdir, d in entries:
            mod_id += 1
            rel_path = os.path.relpath(vdir, corpus_root).replace(os.sep, "/")
            modules.append({
                "id": mod_id,
                "machine_name": machine,
                "name": d.get("name") or machine,
                "description": d.get("description") or "",
                "version": version,
                "is_latest": 1 if version == latest_version else 0,
                "active_installs": d.get("active_installs") or 0,
                "categories": json.dumps(d.get("categories") or []),
                "keywords": json.dumps(d.get("keywords") or []),
                "project_url": d.get("project_url") or "",
                "rel_path": rel_path,
                "data_json": json.dumps(d, separators=(",", ":")),
            })
            units = list(collect_docs(vdir))
            synth = data_synth(d)
            if synth:
                units.append(("data", "data.json", d.get("name") or machine, synth))
            for doc_type, doc_path, title, body in units:
                doc_id += 1
                docs.append({
                    "id": doc_id,
                    "module_id": mod_id,
                    "machine_name": machine,
                    "name": d.get("name") or machine,
                    "version": version,
                    "doc_type": doc_type,
                    "doc_path": doc_path,
                    "title": title or "",
                    "body": body,
                    "keywords": d.get("keywords") or [],
                    "categories": (d.get("categories") or []) + (d.get("subcategories") or []),
                })
    return modules, docs


# --------------------------------------------------------------------------- #
# Embeddings                                                                    #
# --------------------------------------------------------------------------- #

def embed_passages(bodies):
    t0 = time.time()
    vecs = embed_texts(bodies)  # (N, MODEL_DIM) unit-normalized float32
    log(f"  embedded {len(bodies)} units ({time.time() - t0:.1f}s)")
    return vecs


# --------------------------------------------------------------------------- #
# DB build                                                                      #
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE modules (
  id INTEGER PRIMARY KEY, machine_name TEXT NOT NULL, name TEXT NOT NULL,
  description TEXT, version TEXT NOT NULL, is_latest INTEGER NOT NULL,
  active_installs INTEGER DEFAULT 0, categories TEXT, keywords TEXT,
  project_url TEXT, rel_path TEXT NOT NULL, data_json TEXT
);
CREATE INDEX idx_modules_machine ON modules(machine_name);
CREATE TABLE docs (
  id INTEGER PRIMARY KEY, module_id INTEGER NOT NULL REFERENCES modules(id),
  machine_name TEXT NOT NULL, version TEXT NOT NULL, doc_path TEXT NOT NULL,
  doc_type TEXT NOT NULL, title TEXT, body TEXT NOT NULL
);
CREATE INDEX idx_docs_module ON docs(module_id);
CREATE VIRTUAL TABLE docs_fts USING fts5(
  machine_name, name, keywords, categories, title, body,
  content='', tokenize='porter unicode61'
);
CREATE TABLE meta (
  schema_version TEXT, corpus_commit TEXT, model_name TEXT, model_dim INTEGER,
  built_at INTEGER, doc_count INTEGER, module_count INTEGER
);
"""


def serialize_vec(vec):
    # store as float32 blob for sqlite-vec
    return struct.pack("%sf" % len(vec), *(float(x) for x in vec))


def build(corpus_root, out_path, limit=None, corpus_commit=""):
    log(f"[scan] {corpus_root}")
    t0 = time.time()
    modules, docs = scan_corpus(corpus_root, limit=limit)
    log(f"[scan] {len(modules)} module versions, {len(docs)} doc units "
        f"({time.time() - t0:.1f}s)")

    if os.path.exists(out_path):
        os.remove(out_path)
    con = sqlite3.connect(out_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(SCHEMA)
    con.execute(
        f"CREATE VIRTUAL TABLE vec_docs USING vec0(embedding float[{MODEL_DIM}])"
    )

    con.executemany(
        "INSERT INTO modules VALUES "
        "(:id,:machine_name,:name,:description,:version,:is_latest,"
        ":active_installs,:categories,:keywords,:project_url,:rel_path,:data_json)",
        modules,
    )
    con.executemany(
        "INSERT INTO docs VALUES "
        "(:id,:module_id,:machine_name,:version,:doc_path,:doc_type,:title,:body)",
        docs,
    )
    con.executemany(
        "INSERT INTO docs_fts(rowid, machine_name, name, keywords, categories, title, body) "
        "VALUES (?,?,?,?,?,?,?)",
        [(d["id"], d["machine_name"], d["name"], " ".join(d["keywords"]),
          " ".join(d["categories"]), d["title"], d["body"]) for d in docs],
    )
    con.commit()
    log(f"[fts] indexed {len(docs)} units")

    log(f"[embed] {MODEL_NAME} over {len(docs)} units ...")
    vecs = embed_passages([d["body"] for d in docs])
    con.executemany(
        "INSERT INTO vec_docs(rowid, embedding) VALUES (?, ?)",
        [(docs[i]["id"], serialize_vec(v)) for i, v in enumerate(vecs)],
    )
    con.commit()
    log(f"[embed] stored {len(vecs)} vectors")

    con.execute(
        "INSERT INTO meta VALUES (?,?,?,?,?,?,?)",
        (SCHEMA_VERSION, corpus_commit, MODEL_NAME, MODEL_DIM,
         int(t0), len(docs), len(modules)),
    )
    con.commit()
    con.execute("VACUUM")
    con.close()
    size_mb = os.path.getsize(out_path) / 1e6
    log(f"[done] {out_path} — {size_mb:.1f} MB, {len(modules)} modules, {len(docs)} docs "
        f"in {time.time() - t0:.0f}s total")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.join(os.path.dirname(__file__), "..", "modules"))
    ap.add_argument("--out", default="docs.db")
    ap.add_argument("--limit", type=int, default=None, help="only first N modules (by name)")
    ap.add_argument("--corpus-commit", default=os.environ.get("GITHUB_SHA", ""))
    args = ap.parse_args()
    build(os.path.abspath(args.corpus), os.path.abspath(args.out),
          limit=args.limit, corpus_commit=args.corpus_commit)


if __name__ == "__main__":
    main()
