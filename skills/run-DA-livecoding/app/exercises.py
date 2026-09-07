"""Exercise loading and validation for Qubika SQL Interview.

An exercise is a folder under exercises/ with:
    exercise.json   {"title", "difficulty", "tables": [{"name", "csv"}], "sample_rows"}
    statement.md    the prompt shown to the candidate
    schema.sql      CREATE TABLE statements, ;-separated
    data/*.csv      seed data, one file per table (header row required)
    solution.sql    reference answer for the interviewer (never served)
"""
import csv
import json
import os
from dataclasses import dataclass

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
EXERCISES_DIR = os.path.join(PROJECT_DIR, "exercises")


class ExerciseError(Exception):
    pass


@dataclass
class Exercise:
    name: str
    title: str
    difficulty: str
    focus: str  # interviewer-facing technique note; NEVER sent to the candidate
    statement_md: str
    schema_sql: str
    tables: list  # [{"name": str, "csv_path": str}]
    sample_rows: int


def list_exercise_names(exercises_dir=EXERCISES_DIR):
    if not os.path.isdir(exercises_dir):
        return []
    return sorted(
        d for d in os.listdir(exercises_dir)
        if os.path.isfile(os.path.join(exercises_dir, d, "exercise.json"))
    )


def load_exercise(name, exercises_dir=EXERCISES_DIR):
    folder = os.path.join(exercises_dir, name)
    meta_path = os.path.join(folder, "exercise.json")
    if not os.path.isfile(meta_path):
        raise ExerciseError(f"exercise '{name}' not found ({meta_path})")
    with open(meta_path, encoding="utf-8") as f:
        try:
            meta = json.load(f)
        except json.JSONDecodeError as e:
            raise ExerciseError(f"{name}: invalid exercise.json: {e}")
    for key in ("title", "tables"):
        if key not in meta:
            raise ExerciseError(f"{name}: exercise.json missing '{key}'")
    if not meta["tables"]:
        raise ExerciseError(f"{name}: 'tables' is empty")

    statement_md = _read(folder, "statement.md", name)
    schema_sql = _read(folder, "schema.sql", name)
    schema_flat = " ".join(schema_sql.lower().split())

    tables = []
    for t in meta["tables"]:
        if "name" not in t or "csv" not in t:
            raise ExerciseError(f"{name}: each table needs 'name' and 'csv'")
        if f"create table {t['name'].lower()}" not in schema_flat:
            raise ExerciseError(f"{name}: table '{t['name']}' not found in schema.sql")
        csv_path = os.path.join(folder, t["csv"])
        if not os.path.isfile(csv_path):
            raise ExerciseError(f"{name}: missing CSV {csv_path}")
        with open(csv_path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), None)
        if not header or not any(h.strip() for h in header):
            raise ExerciseError(f"{name}: CSV {t['csv']} has no header row")
        tables.append({"name": t["name"], "csv_path": csv_path})

    return Exercise(
        name=name,
        title=meta["title"],
        difficulty=meta.get("difficulty", ""),
        focus=meta.get("focus", ""),
        statement_md=statement_md,
        schema_sql=schema_sql,
        tables=tables,
        sample_rows=int(meta.get("sample_rows", 5)),
    )


def load_exercises(names=None, exercises_dir=EXERCISES_DIR):
    available = list_exercise_names(exercises_dir)
    if names is None:
        names = available
    missing = [n for n in names if n not in available]
    if missing:
        raise ExerciseError(
            f"unknown exercise(s): {', '.join(missing)}. "
            f"Available: {', '.join(available) or '(none)'}"
        )
    return [load_exercise(n, exercises_dir) for n in names]


def table_samples(exercise):
    """First N rows of each table's CSV, for display (no engine round-trip)."""
    out = []
    for t in exercise.tables:
        with open(t["csv_path"], newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = []
            for row in reader:
                rows.append(row)
                if len(rows) >= exercise.sample_rows:
                    break
        out.append({"name": t["name"], "columns": header, "rows": rows})
    return out


def _read(folder, filename, name):
    path = os.path.join(folder, filename)
    if not os.path.isfile(path):
        raise ExerciseError(f"{name}: missing {filename}")
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        raise ExerciseError(f"{name}: {filename} is empty")
    return content
