"""
benchmarks/setup_benchmarks.py

Generates the 6 schema-drift benchmark scenarios MigraLoop is
evaluated against. Each case gets its own directory containing:
  - physical.db        the starting (drifted) database state, seeded
  - target_schema.sql   the ORM's intended schema (what the app expects)

Run via `make install` or directly:
    python benchmarks/setup_benchmarks.py
"""

import os
import sqlite3

BENCHMARKS_DIR = "benchmarks"

# Scoped down from the original 10-case list to the 6 highest-signal
# cases for a solo 3-day build: simple add, rename-with-data (the core
# "don't silently drop data" case), type cast, table split (tests the
# Guardian's cross-table content tracking), composite unique index
# (tests allow_row_count_decrease), and safe deprecation (tests
# intentional_drops). Numbering kept from the original 10 so it's
# traceable back to the full scenario list in the README.
CASES = {
    "01_simple_add": {
        "physical": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
        "target": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);",
        "seed": "INSERT INTO users (name) VALUES ('Alice'), ('Bob');",
    },
    "02_rename_column_with_data": {
        "physical": "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "target": "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
        "seed": "INSERT INTO users (user_name) VALUES ('Alice'), ('Bob');",
    },
    "03_type_migration": {
        "physical": "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount VARCHAR(255));",
        "target": "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER);",
        "seed": "INSERT INTO orders (amount) VALUES ('100'), ('250');",
    },
    "05_table_split": {
        "physical": (
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, street TEXT, city TEXT);"
        ),
        "target": (
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n"
            "CREATE TABLE addresses (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "street TEXT, city TEXT, FOREIGN KEY(user_id) REFERENCES users(id));"
        ),
        "seed": "INSERT INTO users (name, street, city) VALUES ('Alice', '123 Main St', 'NY');",
    },
    "08_composite_unique_index": {
        "physical": "CREATE TABLE tags (id INTEGER PRIMARY KEY, item_id INTEGER, tag_name TEXT);",
        "target": (
            "CREATE TABLE tags (id INTEGER PRIMARY KEY, item_id INTEGER, tag_name TEXT, "
            "UNIQUE(item_id, tag_name));"
        ),
        # deliberate duplicate row -- expects the migration to dedupe
        # before applying the unique constraint (allow_row_count_decrease)
        "seed": "INSERT INTO tags (item_id, tag_name) VALUES (1, 'urgent'), (1, 'urgent');",
    },
    "10_safe_deprecation": {
        "physical": (
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, legacy_hash TEXT);"
        ),
        "target": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
        # legacy_hash is expected to be intentionally dropped
        # (intentional_drops = {"users.legacy_hash"})
        "seed": "INSERT INTO users (name, legacy_hash) VALUES ('Alice', 'xyz123');",
    },
}


def setup():
    os.makedirs(BENCHMARKS_DIR, exist_ok=True)
    for case_name, data in CASES.items():
        case_dir = os.path.join(BENCHMARKS_DIR, case_name)
        os.makedirs(case_dir, exist_ok=True)

        db_path = os.path.join(case_dir, "physical.db")
        target_path = os.path.join(case_dir, "target_schema.sql")

        with open(target_path, "w") as f:
            f.write(data["target"])

        if os.path.exists(db_path):
            os.remove(db_path)

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(data["physical"])
        conn.executescript(data["seed"])
        conn.commit()
        conn.close()

    print(f"Successfully generated {len(CASES)} benchmark scenarios: {list(CASES.keys())}")


if __name__ == "__main__":
    setup()
