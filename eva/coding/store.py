"""Persistent coding knowledge and experience store (standard-library SQLite)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{1,}", text.lower()) if t not in {
        "this", "that", "with", "from", "when", "then", "have", "will", "your", "into", "using", "code", "file"
    }}


class CodingStore:
    """Stores collected knowledge separately from validated knowledge."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'general',
                category TEXT NOT NULL DEFAULT 'reference',
                source TEXT NOT NULL DEFAULT 'local',
                license TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL DEFAULT 'unverified',
                sha256 TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(sha256, source)
            );
            CREATE TABLE IF NOT EXISTS experiences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'unknown',
                outcome TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'unknown',
                evidence TEXT NOT NULL DEFAULT '',
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS repair_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experience_id INTEGER NOT NULL REFERENCES experiences(id),
                proposed_fix TEXT NOT NULL,
                result TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verified_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                trigger_pattern TEXT NOT NULL,
                fix_procedure TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'general',
                evidence_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'verified',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_language ON knowledge(language);
            CREATE INDEX IF NOT EXISTS idx_experience_category ON experiences(category);
            CREATE INDEX IF NOT EXISTS idx_experience_created ON experiences(created_at);
            """)

    def add_knowledge(self, title: str, body: str, *, language: str = "general",
                      category: str = "reference", source: str = "local",
                      license: str = "unknown", status: str = "unverified") -> int:
        if not title.strip() or not body.strip():
            raise ValueError("Knowledge title and body must not be empty.")
        if status not in {"unverified", "tested", "verified", "outdated", "rejected"}:
            raise ValueError("Invalid knowledge status.")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connect() as con:
            row = con.execute("SELECT id FROM knowledge WHERE sha256=? AND source=?", (digest, source)).fetchone()
            if row:
                con.execute("UPDATE knowledge SET title=?, body=?, language=?, category=?, license=?, status=?, updated_at=? WHERE id=?",
                            (title, body, language.lower(), category.lower(), license, status, now, row["id"]))
                return int(row["id"])
            cur = con.execute("INSERT INTO knowledge(title,body,language,category,source,license,status,sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (title.strip(), body, language.lower(), category.lower(), source, license, status, digest, now, now))
            return int(cur.lastrowid)

    def search_knowledge(self, query: str, *, language: str | None = None, limit: int = 8,
                         include_unverified: bool = True) -> list[dict[str, Any]]:
        terms = _tokens(query)
        if not terms:
            return []
        clauses, params = [], []
        if language and language.lower() not in {"general", "unknown"}:
            clauses.append("language IN (?, 'general')")
            params.append(language.lower())
        if not include_unverified:
            clauses.append("status='verified'")
        sql = "SELECT * FROM knowledge" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        scored = []
        for row in rows:
            searchable = f"{row['title']} {row['body']} {row['category']} {row['language']}"
            found = _tokens(searchable)
            overlap = len(terms & found)
            if not overlap:
                continue
            # Prefer verified material, then title matches and coverage.
            title_terms = _tokens(row["title"])
            score = overlap / max(1, len(terms)) + 0.35 * len(terms & title_terms)
            score += {"verified": 0.35, "tested": 0.15, "unverified": 0.0, "outdated": -0.5, "rejected": -2.0}.get(row["status"], 0)
            if row["status"] in {"outdated", "rejected"}:
                continue
            item = dict(row)
            item["score"] = round(score, 3)
            scored.append(item)
        return sorted(scored, key=lambda x: (-x["score"], x["title"].lower()))[:max(1, min(limit, 50))]

    def record_experience(self, task: str, *, language: str = "unknown", outcome: str,
                          category: str = "unknown", evidence: str = "",
                          context: dict[str, Any] | None = None) -> int:
        if outcome not in {"success", "failure", "partial", "unresolved"}:
            raise ValueError("outcome must be success, failure, partial, or unresolved")
        with self._connect() as con:
            cur = con.execute("INSERT INTO experiences(task,language,outcome,category,evidence,context_json,created_at) VALUES(?,?,?,?,?,?,?)",
                              (task, language.lower(), outcome, category.lower(), evidence,
                               json.dumps(context or {}, ensure_ascii=False), time.time()))
            return int(cur.lastrowid)

    def record_repair_attempt(self, experience_id: int, proposed_fix: str, *, result: str, evidence: str = "") -> int:
        if result not in {"failed", "partial", "passed", "rejected"}:
            raise ValueError("Invalid repair result.")
        with self._connect() as con:
            if not con.execute("SELECT 1 FROM experiences WHERE id=?", (experience_id,)).fetchone():
                raise ValueError("Unknown experience id.")
            cur = con.execute("INSERT INTO repair_attempts(experience_id,proposed_fix,result,evidence,created_at) VALUES(?,?,?,?,?)",
                              (experience_id, proposed_fix, result, evidence, time.time()))
            return int(cur.lastrowid)

    def promote_rule(self, title: str, trigger_pattern: str, fix_procedure: str, *, language: str,
                     original_test_passed: bool, regression_tests_passed: bool,
                     reviewed: bool, evidence: dict[str, Any]) -> int:
        """Promote a rule only after explicit validation and review gates."""
        if not (original_test_passed and regression_tests_passed and reviewed):
            raise ValueError("Promotion denied: original test, regression tests, and human review are all required.")
        if not title.strip() or not trigger_pattern.strip() or not fix_procedure.strip():
            raise ValueError("Rule title, trigger and procedure are required.")
        with self._connect() as con:
            cur = con.execute("INSERT INTO verified_rules(title,trigger_pattern,fix_procedure,language,evidence_json,status,created_at) VALUES(?,?,?,?,?,'verified',?)",
                              (title, trigger_pattern, fix_procedure, language.lower(), json.dumps(evidence, ensure_ascii=False), time.time()))
            return int(cur.lastrowid)

    def search_experiences(self, query: str, *, language: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Retrieve similar prior cases, keeping failures visible as evidence rather than rules."""
        terms = _tokens(query)
        if not terms:
            return []
        sql = "SELECT * FROM experiences"
        params: list[Any] = []
        if language and language.lower() not in {"general", "unknown"}:
            sql += " WHERE language IN (?, 'unknown')"
            params.append(language.lower())
        sql += " ORDER BY created_at DESC LIMIT 2000"
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        scored = []
        for row in rows:
            searchable = f"{row['task']} {row['category']} {row['evidence']} {row['context_json']}"
            found = _tokens(searchable)
            overlap = len(terms & found)
            if overlap:
                item = dict(row)
                item["context"] = json.loads(item.pop("context_json"))
                item["score"] = round(overlap / max(1, len(terms)), 3)
                scored.append(item)
        return sorted(scored, key=lambda x: (-x["score"], -x["created_at"]))[:max(1, min(limit, 25))]

    def search_verified_rules(self, query: str, *, language: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        terms = _tokens(query)
        if not terms:
            return []
        with self._connect() as con:
            rows = con.execute("SELECT * FROM verified_rules WHERE status='verified'").fetchall()
        scored = []
        for row in rows:
            if language and language.lower() not in {"general", "unknown"} and row["language"] not in {language.lower(), "general"}:
                continue
            searchable = f"{row['title']} {row['trigger_pattern']} {row['fix_procedure']}"
            found = _tokens(searchable)
            overlap = len(terms & found)
            if overlap:
                item = dict(row)
                item["evidence"] = json.loads(item.pop("evidence_json"))
                item["score"] = round(overlap / max(1, len(terms)), 3)
                scored.append(item)
        return sorted(scored, key=lambda x: (-x["score"], x["title"].lower()))[:max(1, min(limit, 25))]

    def stats(self) -> dict[str, int]:
        with self._connect() as con:
            return {
                "knowledge_items": con.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
                "verified_knowledge": con.execute("SELECT COUNT(*) FROM knowledge WHERE status='verified'").fetchone()[0],
                "experiences": con.execute("SELECT COUNT(*) FROM experiences").fetchone()[0],
                "failures": con.execute("SELECT COUNT(*) FROM experiences WHERE outcome='failure'").fetchone()[0],
                "repair_attempts": con.execute("SELECT COUNT(*) FROM repair_attempts").fetchone()[0],
                "verified_rules": con.execute("SELECT COUNT(*) FROM verified_rules WHERE status='verified'").fetchone()[0],
            }
