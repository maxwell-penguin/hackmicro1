"""
src/cli.py

Command-line entry point for MigraLoop. Wraps the baseline runner and
the advanced orchestrator loop into the two commands the micro1 rubric
requires -- the Makefile's `make baseline` and `make advanced` targets
call this as `python -m src.cli run-baseline --all` /
`run-advanced --all`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

from src.baseline import run_baseline_case  # noqa: E402  (must follow load_dotenv())
from src.orchestrator import run_case  # noqa: E402

app = typer.Typer(add_completion=False)
console = Console()

BENCHMARKS_DIR = Path("benchmarks")
RESULTS_DIR = Path("results")


def _case_dirs() -> list:
    return sorted(
        p
        for p in BENCHMARKS_DIR.iterdir()
        if p.is_dir() and (p / "physical.db").exists() and (p / "target_schema.sql").exists()
    )


def _select_cases(all_cases: bool, case: Optional[str]) -> list:
    if case:
        case_dir = BENCHMARKS_DIR / case
        if not case_dir.exists():
            console.print(f"[red]No such case: {case}[/red]")
            raise typer.Exit(code=1)
        return [case_dir]
    if all_cases:
        return _case_dirs()
    console.print("[red]Specify --all or --case NAME[/red]")
    raise typer.Exit(code=1)


def _require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/red] Copy .env.example to .env and add your "
            "key, or `export ANTHROPIC_API_KEY=...` in your shell."
        )
        raise typer.Exit(code=1)


@app.command("run-baseline")
def run_baseline(
    all_cases: bool = typer.Option(False, "--all", help="Run every benchmark case"),
    case: Optional[str] = typer.Option(None, "--case", help="Run a single case by directory name"),
):
    """Run the naive zero-shot baseline against benchmark case(s)."""
    _require_api_key()
    cases = _select_cases(all_cases, case)

    results = []
    table = Table(title="Baseline Results")
    table.add_column("Case")
    table.add_column("SQL OK")
    table.add_column("Data Loss")

    for case_dir in cases:
        console.print(f"[bold]Running baseline: {case_dir.name}[/bold]")
        result = run_baseline_case(case_dir)
        results.append(
            {
                "case_name": result.case_name,
                "sql_succeeded": result.sql_succeeded,
                "sql_error": result.sql_error,
                "data_loss_detected": result.data_loss_detected,
                "data_loss_details": result.data_loss_details,
                "trajectory_path": result.trajectory_path,
            }
        )
        table.add_row(
            case_dir.name,
            "[green]yes[/green]" if result.sql_succeeded else "[red]no[/red]",
            "[red]LOST DATA[/red]" if result.data_loss_detected else "[green]ok[/green]",
        )

    console.print(table)
    _write_results("baseline_results.json", results)


@app.command("run-advanced")
def run_advanced(
    all_cases: bool = typer.Option(False, "--all", help="Run every benchmark case"),
    case: Optional[str] = typer.Option(None, "--case", help="Run a single case by directory name"),
):
    """Run the full MigraLoop self-correction loop against benchmark case(s)."""
    _require_api_key()
    cases = _select_cases(all_cases, case)

    results = []
    table = Table(title="MigraLoop (Advanced) Results")
    table.add_column("Case")
    table.add_column("Outcome")
    table.add_column("Attempts")

    for case_dir in cases:
        console.print(f"[bold]Running advanced: {case_dir.name}[/bold]")
        result = run_case(case_dir)
        results.append(
            {
                "case_name": result.case_name,
                "outcome": result.outcome,
                "attempts": result.attempts,
                "attempt_errors": result.attempt_errors,
                "trajectory_path": result.trajectory_path,
            }
        )
        color = "green" if result.outcome == "success" else "red"
        table.add_row(case_dir.name, f"[{color}]{result.outcome}[/{color}]", str(result.attempts))

    console.print(table)
    _write_results("advanced_results.json", results)


def _write_results(filename: str, results: list) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / filename
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    console.print(f"Wrote {path}")


if __name__ == "__main__":
    app()
