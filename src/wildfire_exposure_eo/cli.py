"""Command-line entry point for wildfire-exposure-eo."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from wildfire_exposure_eo import audit as audit_mod

app = typer.Typer(
    name="wildfire-exposure-eo",
    help="STAC-native wildfire exposure scoring for OSM critical infrastructure (Portugal pilot).",
    no_args_is_help=True,
)

_STATUS_STYLE = {"GREEN": "bold green", "YELLOW": "bold yellow", "RED": "bold red"}


@app.callback()
def _root() -> None:
    """Force Typer to treat subcommands as subcommands even when only one is registered."""


@app.command()
def audit(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"),
        "--aoi",
        exists=True,
        readable=True,
        dir_okay=False,
        help="Path to the AOI GeoJSON. Defaults to the frozen pilot AOI.",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit results as machine-readable JSON instead of a table."
    ),
    report_dir: Path = typer.Option(
        Path("outputs/audit"),
        "--report-dir",
        help="Directory to write the machine-readable JSON report into.",
    ),
) -> None:
    """Run the nine data-source health checks against the AOI; write a JSON report."""
    console = Console()
    console.print(f"[dim]AOI:[/dim] {aoi}")
    bbox = audit_mod.load_aoi_bbox(aoi)
    console.print(f"[dim]bbox (WGS84):[/dim] {bbox}\n")

    results = audit_mod.run_all(aoi)
    checked_at = datetime.now(UTC)
    run_id = checked_at.strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "run_id": run_id,
        "aoi_path": str(aoi),
        "aoi_bbox_wgs84": list(bbox),
        "checked_at_utc": checked_at.isoformat(),
        "results": [
            {"name": r.name, "status": r.status, "message": r.message, "details": r.details}
            for r in results
        ],
    }

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{run_id}.json"
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    if json_out:
        console.print_json(json.dumps(payload))
    else:
        table = Table(title="Data-source audit", show_lines=False)
        table.add_column("Source", style="bold")
        table.add_column("Status")
        table.add_column("Message")
        for r in results:
            table.add_row(r.name, f"[{_STATUS_STYLE[r.status]}]{r.status}[/]", r.message)
        console.print(table)
        console.print(f"\n[dim]report:[/dim] {report_path}")

    has_red = any(r.status == "RED" for r in results)
    has_yellow = any(r.status == "YELLOW" for r in results)
    if has_red:
        raise typer.Exit(code=1)
    if has_yellow:
        raise typer.Exit(code=2)


def _aoi_label(aoi_path: Path) -> str:
    """Short label from the first feature's `name` property, falling back to filename stem."""
    try:
        payload = json.loads(aoi_path.read_text())
        feats = payload.get("features", [])
        if feats:
            name = feats[0].get("properties", {}).get("name")
            if name:
                return str(name)
    except Exception:
        pass
    return aoi_path.stem


@app.command("audit-all")
def audit_all(
    aoi_dir: Path = typer.Option(
        Path("data/aoi"),
        "--dir",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Directory containing AOI GeoJSON candidates.",
    ),
    include_smoke: bool = typer.Option(
        False, "--include-smoke", help="Also audit smoke*.geojson tiles (default: skip)."
    ),
) -> None:
    """Run the full audit against every candidate AOI in a directory and emit a comparison matrix.

    Skips `smoke*.geojson` by default, and dedupes AOIs by bbox so duplicates
    (e.g. a frozen `pilot.geojson` plus its archived alternative) are audited once.
    """
    console = Console()

    candidates = sorted(p for p in aoi_dir.glob("*.geojson") if p.is_file())
    if not include_smoke:
        candidates = [p for p in candidates if not p.name.startswith("smoke")]

    seen: dict[tuple[float, float, float, float], Path] = {}
    work: list[tuple[Path, tuple[float, float, float, float]]] = []
    duplicates: list[tuple[Path, Path]] = []
    for path in candidates:
        try:
            bbox = audit_mod.load_aoi_bbox(path)
        except Exception as exc:
            console.print(f"[yellow]skip[/] {path.name}: {exc}")
            continue
        if bbox in seen:
            duplicates.append((path, seen[bbox]))
            continue
        seen[bbox] = path
        work.append((path, bbox))

    if not work:
        console.print("[red]no AOIs found[/]")
        raise typer.Exit(code=1)

    console.print(f"[bold]Auditing {len(work)} AOI(s)[/] from {aoi_dir}/\n")
    for path, _ in duplicates:
        target = seen[audit_mod.load_aoi_bbox(path)]
        console.print(f"  [dim]dedup:[/] {path.name} (same bbox as {target.name})")
    if duplicates:
        console.print()

    all_results: dict[str, list[audit_mod.CheckResult]] = {}
    for path, bbox in work:
        label = _aoi_label(path)
        console.rule(f"[bold]{label}[/]   [dim]{path.name}   bbox={bbox}[/]")
        results = audit_mod.run_all(path)
        all_results[path.name] = results
        for r in results:
            console.print(
                f"  [{_STATUS_STYLE[r.status]}]{r.status:6}[/]  {r.name:<22}  {r.message}"
            )
        console.print()

    # Summary matrix
    matrix = Table(title="Audit summary — status by AOI x source", show_lines=False)
    matrix.add_column("AOI", style="bold")
    for name in audit_mod.CHECKS:
        matrix.add_column(name, justify="center")
    for path, _ in work:
        results = all_results[path.name]
        by_name = {r.name: r for r in results}
        row = [path.stem]
        for check_name in audit_mod.CHECKS:
            r = by_name.get(check_name)
            if r is None:
                row.append("[red]?[/]")
            else:
                row.append(f"[{_STATUS_STYLE[r.status]}]{r.status[0]}[/]")
        matrix.add_row(*row)
    console.print(matrix)

    has_red = any(r.status == "RED" for rs in all_results.values() for r in rs)
    has_yellow = any(r.status == "YELLOW" for rs in all_results.values() for r in rs)
    if has_red:
        raise typer.Exit(code=1)
    if has_yellow:
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
