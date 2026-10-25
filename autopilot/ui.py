"""Terminal rendering (rich). Everything the demo recording shows comes through here."""
from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

console = Console(highlight=False)


def banner(text: str, style: str = "bold cyan") -> None:
    console.rule(f"[{style}]{text}[/{style}]", style=style)


def chaos_banner(name: str, description: str) -> None:
    console.print(Panel(f"[bold]Injecting fault:[/bold] [yellow]{name}[/yellow]\n{description}",
                        title="[bold red]CHAOS[/bold red] - simulated upstream fault (labelled, agents cannot see this)",
                        border_style="red"))


def info(msg: str) -> None:
    console.print(f"[dim]-[/dim] {msg}")


def ok(msg: str) -> None:
    console.print(f"[green]OK[/green] {msg}")


def warn(msg: str) -> None:
    console.print(f"[yellow]!![/yellow] {msg}")


def fail(msg: str) -> None:
    console.print(f"[bold red]FAILED[/bold red] {msg}")


def check_table(results: list[dict[str, Any]], title: str = "Quality checks") -> None:
    t = Table(title=title, show_lines=False, header_style="bold")
    t.add_column("check"); t.add_column("status"); t.add_column("failed/total", justify="right"); t.add_column("detail")
    for r in results:
        st = r["status"]
        color = {"passed": "green", "failed": "red", "error": "magenta"}.get(st, "white")
        det = r.get("details") or {}
        if isinstance(det, str):
            det = json.loads(det)
        detail = det.get("reason") or det.get("stats") or det.get("error") or ""
        t.add_row(r["check_name"], f"[{color}]{st}[/{color}]",
                  f"{r.get('failed_rows')}/{r.get('total_rows')}", str(detail)[:70])
    console.print(t)


def agent_header(agent: str, model: str) -> None:
    console.print(Panel(f"[bold]{agent}[/bold]  [dim]model={model}[/dim]", border_style="blue"))


def tool_call(name: str, args: dict[str, Any]) -> None:
    shown = json.dumps(args, default=str)
    if len(shown) > 160:
        shown = shown[:157] + "..."
    console.print(f"  [bold blue]->[/bold blue] [bold]{name}[/bold][dim]({shown})[/dim]")


def tool_result(text: str, max_lines: int = 6) -> None:
    lines = text.rstrip().splitlines()
    for ln in lines[:max_lines]:
        console.print(f"     [dim]{ln[:140]}[/dim]")
    if len(lines) > max_lines:
        console.print(f"     [dim]... {len(lines) - max_lines} more lines[/dim]")


def assistant_text(text: str) -> None:
    text = text.strip()
    if text:
        console.print(f"  [italic]{text[:600]}[/italic]")


def sql(text: str, title: str | None = None) -> None:
    console.print(Panel(Syntax(text, "sql", theme="ansi_dark", word_wrap=True), title=title, border_style="dim"))


def diff(text: str, title: str = "Proposed change") -> None:
    console.print(Panel(Syntax(text, "diff", theme="ansi_dark", word_wrap=True), title=title, border_style="yellow"))


def kv(title: str, data: dict[str, Any]) -> None:
    t = Table(title=title, show_header=False, box=None)
    t.add_column(style="bold"); t.add_column()
    for k, v in data.items():
        t.add_row(str(k), str(v) if not isinstance(v, (list, dict)) else json.dumps(v, default=str)[:300])
    console.print(t)
