#!/usr/bin/env python3
"""Read-only AST inventory; recommendations are triage hints, never review evidence."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOTS = ("backend/app", "packages/product/src", "runtime/workmate_runtime")
SQL_START = re.compile(r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|SET|SHOW|TRUNCATE)\b", re.I)
DRIVER_METHODS = {"execute", "executemany", "fetch", "fetchrow", "fetchval", "exec_driver_sql"}
UNKNOWN = "manual review required"
EXCLUDED_PATHS = (
    "packages/product/src/workmate_product/skills/builtin_skills/data-analysis/scripts",
)
# Common SQL wrappers; additional local signatures are collected from definitions.
HELPERS = {name: (1, "sql") for name in ("row", "_row", "rows", "_rows")}
SQL_PARAMETERS = {"sql", "query", "statement"}


def sql_value(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{dynamic}" for v in node.values), True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, ld = sql_value(node.left, constants)
        right, rd = sql_value(node.right, constants)
        return left + right, ld or rd
    return "", True


def classify(sql, file):
    upper = sql.upper()
    locks = [word for word in ("FOR UPDATE", "FOR KEY SHARE", "SKIP LOCKED", "PG_ADVISORY_XACT_LOCK") if word in upper]
    specific = [word for word in ("JSONB", "RETURNING", "ON CONFLICT", "CLOCK_TIMESTAMP", "SKIP LOCKED", "PG_ADVISORY_XACT_LOCK") if word in upper]
    match = SQL_START.match(sql)
    verb = match.group(1).upper() if match else "UNKNOWN"
    operation = {"SELECT": "read", "WITH": "cte"}.get(verb, verb.lower())
    protocol = "/transport/" in file or "/projections/" in file or any(
        token in file for token in ("control_commands", "node_recovery", "full_access", "delivery", "/commands.py")
    )
    if protocol or locks or any(token in upper for token in ("EXECUTION_EPOCH", "LEASE_UNTIL", "OWNER_ID", "STATE_VERSION")):
        recommendation = "Protocol review"
        reason = "Protocol/locking candidate: preserve connection, outer transaction, lock order, WHERE fences and atomicity; review complete operation before extraction."
    elif "/persistence/" in file:
        recommendation = "Boundary review"
        reason = "Already within a persistence boundary; verify dependencies and caller-owned transaction. No SQL syntax conversion is required."
    elif operation == "read" and not specific:
        recommendation = "Query boundary review"
        reason = "Review query placement, authorization, fresh reads and autoflush; preserve SQL when extracting the operation."
    else:
        recommendation = "Atomic operation review" if sql else UNKNOWN
        reason = "Review the complete write/batch operation and transaction ownership; preserve statement shape and RETURNING semantics."
    return operation, locks, specific, recommendation, reason


class Inventory(ast.NodeVisitor):
    def __init__(self, file, helpers):
        self.file = file
        self.helpers = helpers
        self.names = set()
        self.modules = set()
        self.scope = []
        self.constants = [{}]
        self.rows = []

    def visit_ImportFrom(self, node):
        if node.module in {"sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression"}:
            self.names.update(alias.asname or alias.name for alias in node.names if alias.name == "text")

    def visit_Import(self, node):
        self.modules.update(alias.asname or alias.name for alias in node.names if alias.name == "sqlalchemy")

    def visit_Assign(self, node):
        known = {key: value for scope in self.constants for key, value in scope.items()}
        value = sql_value(node.value, known)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.constants[-1][target.id] = value
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.constants.append({})
        self.generic_visit(node)
        self.constants.pop()
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def is_text_function(self, fn):
        return (isinstance(fn, ast.Name) and fn.id in self.names) or (
            isinstance(fn, ast.Attribute) and fn.attr == "text"
            and isinstance(fn.value, ast.Name) and fn.value.id in self.modules
        )

    def visit_Call(self, node):
        fn = node.func
        is_text = self.is_text_function(fn)
        is_driver = isinstance(fn, ast.Attribute) and fn.attr in DRIVER_METHODS
        argument = node.args[0] if (is_text or is_driver) and node.args else None
        is_helper = isinstance(fn, ast.Name) and fn.id in self.helpers
        if is_helper and not (is_text or is_driver):
            position, parameter = self.helpers[fn.id]
            argument = node.args[position] if len(node.args) > position else next(
                (item.value for item in node.keywords if item.arg == parameter), None
            )
        if argument is not None:
            constants = {key: value for scope in self.constants for key, value in scope.items()}
            sql, dynamic = sql_value(argument, constants)
            # A nested text(...) is inventoried by its own visit, not again here.
            nested_text = isinstance(argument, ast.Call) and self.is_text_function(argument.func)
            unresolved_driver = is_driver and fn.attr in {"fetch", "fetchrow", "fetchval", "exec_driver_sql"} and not sql and not nested_text
            unresolved_helper = is_helper and not sql and not nested_text
            if is_text or SQL_START.match(sql) or unresolved_driver or unresolved_helper:
                operation, locks, specific, recommendation, reason = classify(sql, self.file)
                if is_text:
                    style = "text SQL"
                elif is_helper:
                    style = "unresolved helper call" if unresolved_helper else "helper SQL"
                else:
                    style = "unresolved driver call" if unresolved_driver else "driver SQL"
                self.rows.append({
                    "file": self.file, "function": ".".join(self.scope) or "<module>",
                    "line": node.lineno, "style": style,
                    "operation": operation, "dynamic": dynamic,
                    "sql_preview": " ".join(sql.split())[:240],
                    "transaction_owner": UNKNOWN, "lock_semantics": locks,
                    "lock_order": UNKNOWN, "postgresql_specific": specific,
                    "recommendation": recommendation, "reason": reason,
                    "review_status": UNKNOWN,
                    "test_evidence": UNKNOWN, "performance_constraint": UNKNOWN,
                })
        self.generic_visit(node)


def inventory_source(source, file):
    tree = ast.parse(source, filename=file)
    helpers = dict(HELPERS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.posonlyargs, *node.args.args]
            for position, argument in enumerate(arguments):
                if argument.arg in SQL_PARAMETERS:
                    helpers[node.name] = (position, argument.arg)
                    break
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in HELPERS:
                    helpers[alias.asname or alias.name] = HELPERS[alias.name]
    visitor = Inventory(file, helpers)
    visitor.visit(tree)
    return sorted(visitor.rows, key=lambda row: row["line"])


def build_report(root, baseline_commit):
    rows = []
    digest = hashlib.sha256()
    for directory in ROOTS:
        for path in sorted((root / directory).rglob("*.py")):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            relative = path.relative_to(root).as_posix()
            if any(relative.startswith(prefix + "/") for prefix in EXCLUDED_PATHS):
                continue
            source = path.read_text(encoding="utf-8")
            digest.update(relative.encode() + b"\0" + source.encode() + b"\0")
            rows.extend(inventory_source(source, relative))
    return {
        "baseline_commit": baseline_commit,
        "source_tree_sha256": digest.hexdigest(),
        "reproduce": "python scripts/sql_persistence_inventory.py --output docs/sql-persistence-inventory.json",
        "scope": list(ROOTS),
        "excluded_paths": list(EXCLUDED_PATHS),
        "exclusion_reason": "Embedded data-analysis Skill scripts query user data, not the Product database.",
        "limitations": "AST candidates, not executed SQL or ORM coverage. Detects SQLAlchemy text aliases, SQL literal/constant driver calls, common row helpers and local functions with sql/query/statement parameters. Helper matches are candidates, not verified database access. Dynamic execute/helper call expressions, imported helpers beyond known aliases, shadowing and control flow need manual review. Recommendations concern responsibility boundaries, not ORM/Core conversion. Transaction ownership, lock order and test evidence are deliberately unverified. Snapshot describes working-tree source; baseline commit alone does not reproduce uncommitted changes.",
        "total_calls": len(rows), "files": len({row["file"] for row in rows}),
        "styles": dict(sorted(Counter(row["style"] for row in rows).items())),
        "calls": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    baseline = subprocess.check_output(["git", "-C", str(args.root), "rev-parse", "HEAD"], text=True).strip()
    report = json.dumps(build_report(args.root, baseline), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
