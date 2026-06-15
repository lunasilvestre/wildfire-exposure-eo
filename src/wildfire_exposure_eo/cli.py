"""Command-line entry point for wildfire-exposure-eo."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from wildfire_exposure_eo import audit as audit_mod
from wildfire_exposure_eo import burn_scar as burn_scar_mod
from wildfire_exposure_eo import burns as burns_mod
from wildfire_exposure_eo import fuel as fuel_mod
from wildfire_exposure_eo import osm as osm_mod
from wildfire_exposure_eo import stac as stac_mod
from wildfire_exposure_eo import static_rasters as sr_mod

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
    """Run the ten data-source health checks against the AOI; write a JSON report."""
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


def _parse_iso_date(value: str, *, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{flag}: expected ISO date YYYY-MM-DD, got {value!r}") from exc


def _configure_module_logging(module: str) -> None:
    """Route a `wildfire_exposure_eo.*` module's INFO logs to stderr.

    Honors the CLAUDE.md verify-then-act protocol: every candidate item ID is
    logged before any raster is read or manifest written.
    """
    log = logging.getLogger(module)
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _configure_stac_logging() -> None:
    _configure_module_logging("wildfire_exposure_eo.stac")


@app.command("resolve-stac")
def resolve_stac(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"),
        "--aoi",
        readable=True,
        dir_okay=False,
        help="Path to the AOI GeoJSON. Ignored when --smoke is set.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help=(
            "Output manifest path. Supports {run_id} templating. Defaults to "
            "outputs/manifests/stac_{run_id}.json (or stac_smoke_{run_id}.json with --smoke)."
        ),
    ),
    spring_start: str = typer.Option(
        "2025-03-01", "--spring-start", help="Spring window start (ISO date)."
    ),
    spring_end: str = typer.Option(
        "2025-06-15", "--spring-end", help="Spring window end (ISO date)."
    ),
    spring_cloud: int = typer.Option(
        30, "--spring-cloud", min=0, max=100, help="Max eo:cloud_cover for spring S2."
    ),
    summer_start: str = typer.Option(
        "2025-07-01", "--summer-start", help="Summer window start (ISO date)."
    ),
    summer_end: str = typer.Option(
        "2025-10-31", "--summer-end", help="Summer window end (ISO date)."
    ),
    summer_cloud: int = typer.Option(
        60, "--summer-cloud", min=0, max=100, help="Max eo:cloud_cover for summer S2 (relaxed)."
    ),
    worldcover_vintage: int = typer.Option(
        2021, "--worldcover-vintage", help="ESA WorldCover vintage year."
    ),
    catalog: str = typer.Option(stac_mod.PC_STAC_URL, "--catalog", help="STAC catalog root URL."),
    smoke: bool = typer.Option(
        False, "--smoke", help="Use data/aoi/smoke.geojson and the smoke output path."
    ),
) -> None:
    """Resolve a deterministic STAC manifest for the AOI; no rasters are read.

    On success, prints the manifest path and per-collection totals. The
    manifest validates as `wildfire_exposure_eo.schemas.StacManifest`.
    """
    console = Console()
    _configure_stac_logging()

    if smoke:
        aoi = Path("data/aoi/smoke.geojson")
        default_out_template = "outputs/manifests/stac_smoke_{run_id}.json"
    else:
        default_out_template = "outputs/manifests/stac_{run_id}.json"
    if not aoi.exists():
        raise typer.BadParameter(f"--aoi: {aoi} does not exist")

    spring_s = _parse_iso_date(spring_start, flag="--spring-start")
    spring_e = _parse_iso_date(spring_end, flag="--spring-end")
    summer_s = _parse_iso_date(summer_start, flag="--summer-start")
    summer_e = _parse_iso_date(summer_end, flag="--summer-end")
    if spring_s > spring_e:
        raise typer.BadParameter("--spring-start must be on or before --spring-end")
    if summer_s > summer_e:
        raise typer.BadParameter("--summer-start must be on or before --summer-end")

    resolved_at = datetime.now(UTC)
    run_id = resolved_at.strftime("%Y%m%dT%H%M%SZ")

    console.print(f"[dim]AOI:[/dim] {aoi}")
    console.print(f"[dim]catalog:[/dim] {catalog}")
    console.print(f"[dim]spring:[/dim] {spring_s}..{spring_e}  cloud<={spring_cloud}%")
    console.print(f"[dim]summer:[/dim] {summer_s}..{summer_e}  cloud<={summer_cloud}%")
    console.print(f"[dim]worldcover vintage:[/dim] {worldcover_vintage}")
    console.print(f"[dim]run_id:[/dim] {run_id}\n")

    manifest = stac_mod.build_manifest(
        aoi,
        spring_start=spring_s,
        spring_end=spring_e,
        spring_cloud=spring_cloud,
        summer_start=summer_s,
        summer_end=summer_e,
        summer_cloud=summer_cloud,
        worldcover_vintage=worldcover_vintage,
        catalog_url=catalog,
        run_id=run_id,
        resolved_at_utc=resolved_at,
    )

    out_path = out if out is not None else Path(default_out_template)
    out_str = str(out_path).replace("{run_id}", run_id)
    final_path = stac_mod.write_manifest(manifest, Path(out_str))

    table = Table(title="STAC manifest — totals by collection", show_lines=False)
    table.add_column("Collection", style="bold")
    table.add_column("Items", justify="right")
    for coll, n in manifest.totals.items():
        style = "green" if n > 0 else "yellow"
        table.add_row(coll, f"[{style}]{n}[/]")
    console.print(table)
    console.print(f"\n[dim]manifest:[/dim] {final_path}")


@app.command("infer-burn-scar")
def infer_burn_scar(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"),
        "--aoi",
        readable=True,
        dir_okay=False,
        help="Path to the AOI GeoJSON. Ignored when --smoke is set.",
    ),
    config: Path = typer.Option(
        burn_scar_mod.DEFAULT_CONFIG_PATH,
        "--config",
        exists=True,
        readable=True,
        dir_okay=False,
        help="Burn-scar config YAML (validated as BurnScarConfig).",
    ),
    window_months: int | None = typer.Option(
        None,
        "--window-months",
        min=1,
        max=24,
        help="Trailing S2 window length. Defaults to the config value.",
    ),
    window_end: str | None = typer.Option(
        None,
        "--window-end",
        help="Window end (ISO date). Defaults to today UTC; pass explicitly to reproduce a run.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help=(
            "Output COG path. Supports {run_id} templating. Defaults to "
            "outputs/cogs/burn_scar_{run_id}.tif (burn_scar_smoke_{run_id}.tif with --smoke)."
        ),
    ),
    device: str | None = typer.Option(
        None,
        "--device",
        help="torch device (e.g. cuda, cpu). Defaults to cuda when available.",
    ),
    smoke: bool = typer.Option(
        False, "--smoke", help="Use data/aoi/smoke.geojson and the smoke output path."
    ),
) -> None:
    """Run pretrained Prithvi burn-scar inference over the AOI; write a provenance-tagged COG.

    Inference only — frozen weights, no fine-tuning. The output value is a
    burn-scar inference probability (relative model score), never a calibrated
    probability and never a fire forecast.
    """
    console = Console()
    _configure_module_logging("wildfire_exposure_eo.burn_scar")

    if smoke:
        aoi = Path("data/aoi/smoke.geojson")
        default_out_template = "outputs/cogs/burn_scar_smoke_{run_id}.tif"
    else:
        default_out_template = "outputs/cogs/burn_scar_{run_id}.tif"
    if not aoi.exists():
        raise typer.BadParameter(f"--aoi: {aoi} does not exist")

    bs_config = burn_scar_mod.load_burn_scar_config(config)
    months = window_months if window_months is not None else bs_config.inference.window_months
    end = (
        _parse_iso_date(window_end, flag="--window-end")
        if window_end is not None
        else datetime.now(UTC).date()
    )
    start = burn_scar_mod.months_back(end, months)

    created_at = datetime.now(UTC)
    run_id = created_at.strftime("%Y%m%dT%H%M%SZ")
    out_path = out if out is not None else Path(default_out_template)
    final_out = Path(str(out_path).replace("{run_id}", run_id))

    console.print(f"[dim]AOI:[/dim] {aoi}")
    console.print(f"[dim]config:[/dim] {config}")
    console.print(f"[dim]model:[/dim] {bs_config.model.hf_model_id}")
    console.print(
        f"[dim]window:[/dim] {start}..{end} ({months} mo)  "
        f"cloud<={bs_config.inference.s2_max_cloud_cover}%"
    )
    console.print(f"[dim]run_id:[/dim] {run_id}\n")

    geometry, aoi_sha = stac_mod.load_aoi_geometry(aoi)
    handle = burn_scar_mod.resolve_prithvi_burn_scar_model(bs_config, device=device)
    items = burn_scar_mod.query_recent_s2(
        geometry,
        months,
        max_cloud_cover=bs_config.inference.s2_max_cloud_cover,
        window_end=end,
    )
    items = burn_scar_mod.filter_to_season(
        items,
        season_start_month=bs_config.inference.season_start_month,
        season_end_month=bs_config.inference.season_end_month,
    )
    if not items:
        console.print("[red]no S2 items in the trailing window — nothing to infer[/]")
        raise typer.Exit(code=1)

    da = burn_scar_mod.infer_burn_probability(
        items,
        handle,
        geometry,
        s2_assets=bs_config.inference.s2_assets,
        scl_mask_classes=bs_config.inference.scl_mask_classes,
        reducer=bs_config.inference.reducer,
        tile_size=bs_config.inference.tile_size,
        tile_stride=bs_config.inference.tile_stride,
    )

    from importlib.metadata import version as pkg_version

    provenance = burn_scar_mod.BurnScarRun(
        run_id=run_id,
        code_commit_sha=stac_mod.code_commit_sha(cwd=Path.cwd()),
        created_at_utc=created_at,
        model_id=handle.hf_model_id,
        model_version=handle.model_version,
        hf_revision_sha=handle.hf_revision_sha,
        terratorch_version=pkg_version("terratorch"),
        torch_version=pkg_version("torch"),
        device=handle.device,
        aoi_path=str(aoi),
        aoi_geometry_sha=aoi_sha,
        stac_catalog_url=stac_mod.PC_STAC_URL,
        window_start=start,
        window_end=end,
        s2_max_cloud_cover=bs_config.inference.s2_max_cloud_cover,
        s2_item_ids=tuple(it.id for it in items),
        scl_mask_classes=bs_config.inference.scl_mask_classes,
        reducer=bs_config.inference.reducer,
        season_start_month=bs_config.inference.season_start_month,
        season_end_month=bs_config.inference.season_end_month,
        binarisation_threshold=bs_config.inference.binarisation_threshold,
        output_crs=burn_scar_mod.OUTPUT_CRS,
        resampling=burn_scar_mod.RESAMPLING,
        nodata=burn_scar_mod.NODATA,
        output_path=str(final_out),
    )
    cog_path = burn_scar_mod.write_burn_scar_cog(da, final_out, provenance)

    table = Table(title="Burn-scar inference run", show_lines=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("scenes", str(len(items)))
    table.add_row("device", handle.device)
    table.add_row("COG", str(cog_path))
    table.add_row("sidecar", str(cog_path.with_suffix(".json")))
    if not smoke:
        item_path = burn_scar_mod.write_stac_item(provenance, cog_path)
        table.add_row("STAC item", str(item_path))
    console.print(table)


def _configure_osm_logging() -> None:
    _configure_module_logging("wildfire_exposure_eo.osm")


@app.command("fetch-osm")
def fetch_osm(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"),
        "--aoi",
        readable=True,
        dir_okay=False,
        help="Path to the AOI GeoJSON. Ignored when --smoke is set.",
    ),
    taxonomy: Path = typer.Option(
        Path("data/taxonomy/critical_infrastructure.yaml"),
        "--taxonomy",
        exists=True,
        readable=True,
        dir_okay=False,
        help="Path to the critical_infrastructure.yaml taxonomy.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help=(
            "Output GeoParquet path. Supports {run_id} templating. Defaults to "
            "outputs/parquet/osm_assets_{run_id}.parquet "
            "(osm_assets_smoke_{run_id}.parquet with --smoke)."
        ),
    ),
    endpoint: str = typer.Option(
        osm_mod._DEFAULT_ENDPOINT,
        "--endpoint",
        help="Primary Overpass API endpoint URL.",
    ),
    fallback_endpoint: str | None = typer.Option(
        osm_mod._FALLBACK_ENDPOINT,
        "--fallback-endpoint",
        help="Fallback Overpass endpoint used after primary exhaustion.",
    ),
    smoke: bool = typer.Option(
        False, "--smoke", help="Use data/aoi/smoke.geojson and the smoke output path."
    ),
) -> None:
    """Query Overpass for every infrastructure class; write a provenance-tagged GeoParquet.

    All OSM IDs are real values from Overpass — none are invented. The output
    validates as wildfire_exposure_eo.schemas.OsmAsset per row.
    """
    console = Console()
    _configure_osm_logging()

    if smoke:
        aoi = Path("data/aoi/smoke.geojson")
        default_out_template = "outputs/parquet/osm_assets_smoke_{run_id}.parquet"
    else:
        default_out_template = "outputs/parquet/osm_assets_{run_id}.parquet"
    if not aoi.exists():
        raise typer.BadParameter(f"--aoi: {aoi} does not exist")

    created_at = datetime.now(UTC)
    run_id = created_at.strftime("%Y%m%dT%H%M%SZ")
    out_path = out if out is not None else Path(default_out_template)
    final_out = Path(str(out_path).replace("{run_id}", run_id))

    _, aoi_sha = stac_mod.load_aoi_geometry(aoi)
    commit_sha = stac_mod.code_commit_sha(cwd=Path.cwd())

    console.print(f"[dim]AOI:[/dim] {aoi}")
    console.print(f"[dim]taxonomy:[/dim] {taxonomy}")
    console.print(f"[dim]endpoint:[/dim] {endpoint}")
    console.print(f"[dim]run_id:[/dim] {run_id}\n")

    result_path = osm_mod.fetch_osm(
        aoi,
        taxonomy,
        final_out,
        endpoint=endpoint,
        fallback_endpoint=fallback_endpoint,
        run_id=run_id,
        code_commit_sha=commit_sha,
        aoi_geometry_sha=aoi_sha,
    )

    import geopandas as gpd

    gdf = gpd.read_parquet(result_path)
    counts: dict[str, int] = gdf.groupby("asset_class").size().to_dict()
    # Iterate the taxonomy, not the groupby result, so zero-count classes
    # still get their YELLOW row in the table.
    taxonomy_classes = sorted(k.class_id for k in osm_mod.load_taxonomy(taxonomy).classes)

    from rich.table import Table as RichTable

    table = RichTable(title="OSM asset counts by class", show_lines=False)
    table.add_column("Asset class", style="bold")
    table.add_column("Count", justify="right")
    for cls in taxonomy_classes:
        cnt = int(counts.get(cls, 0))
        style = "green" if cnt > 0 else "yellow"
        table.add_row(cls, f"[{style}]{cnt}[/]")
    console.print(table)
    console.print(f"\n[dim]rows:[/dim] {len(gdf)}  [dim]parquet:[/dim] {result_path}")


_ALL_SOURCES = {"eth-gch", "effis", "cosc", "cos"}


def _parse_only(value: str | None) -> set[str] | None:
    if value is None:
        return None
    parts = {s.strip() for s in value.split(",") if s.strip()}
    unknown = parts - _ALL_SOURCES
    if unknown:
        raise typer.BadParameter(
            f"--only: unknown source(s) {sorted(unknown)}; valid: {sorted(_ALL_SOURCES)}"
        )
    return parts


def _configure_sr_logging() -> None:
    _configure_module_logging("wildfire_exposure_eo.static_rasters")


@app.command("fetch-rasters")
def fetch_rasters(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"),
        "--aoi",
        readable=True,
        dir_okay=False,
        help="Path to the AOI GeoJSON. Ignored when --smoke is set.",
    ),
    cache_dir: Path = typer.Option(
        Path("data/cache"),
        "--cache-dir",
        help="Directory for cached raster files.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help=(
            "Output manifest path. Supports {run_id} templating. Defaults to "
            "outputs/manifests/static_rasters_{run_id}.json "
            "(static_rasters_smoke_{run_id}.json with --smoke)."
        ),
    ),
    cosc_vintage: str = typer.Option(
        "2024_pre_verao",
        "--cosc-vintage",
        help="DGT COSc vintage to fetch ('2023' or '2024_pre_verao').",
    ),
    cos_vintage: str = typer.Option(
        "2023_v1",
        "--cos-vintage",
        help="DGT COS vintage to fetch ('2018_v3' or '2023_v1').",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated subset of sources to fetch: eth-gch,effis,cosc,cos.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-download even if a valid cache entry exists."
    ),
    smoke: bool = typer.Option(
        False, "--smoke", help="Use data/aoi/smoke.geojson and the smoke output path."
    ),
) -> None:
    """Fetch static rasters (ETH GCH, EFFIS, DGT COSc); write a provenance manifest.

    DGT COS (species-level GeoPackage) is opt-in via ``--only cos`` only — it is
    future work and its DGT download URL currently 404s, so the default fetch
    excludes it.

    Downloads are idempotent: re-running without --force skips files whose
    SHA-256 matches the sidecar written on first download.
    """
    console = Console()
    _configure_sr_logging()

    if smoke:
        aoi = Path("data/aoi/smoke.geojson")
        default_out_template = "outputs/manifests/static_rasters_smoke_{run_id}.json"
    else:
        default_out_template = "outputs/manifests/static_rasters_{run_id}.json"
    if not aoi.exists():
        raise typer.BadParameter(f"--aoi: {aoi} does not exist")

    try:
        source_filter = _parse_only(only)
    except typer.BadParameter as exc:
        raise typer.BadParameter(str(exc)) from exc

    resolved_at = datetime.now(UTC)
    run_id = resolved_at.strftime("%Y%m%dT%H%M%SZ")
    out_path = out if out is not None else Path(default_out_template)
    final_out = Path(str(out_path).replace("{run_id}", run_id))

    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union as _union

    aoi_geojson = json.loads(aoi.read_text())
    features = aoi_geojson.get("features", [aoi_geojson])
    aoi_geom = _union([_shape(f["geometry"]) for f in features])
    aoi_sha = stac_mod.load_aoi_geometry(aoi)[1]
    commit_sha = stac_mod.code_commit_sha(cwd=Path.cwd())

    console.print(f"[dim]AOI:[/dim] {aoi}")
    console.print(f"[dim]cache-dir:[/dim] {cache_dir}")
    console.print(f"[dim]sources:[/dim] {sorted(source_filter) if source_filter else 'all'}")
    console.print(f"[dim]run_id:[/dim] {run_id}\n")

    records: list[sr_mod.FetchRecord] = []

    if source_filter is None or "eth-gch" in source_filter:
        tile_ids = sr_mod.compute_eth_gch_tile_ids(aoi_geom)
        console.print(f"[dim]ETH GCH tiles:[/dim] {tile_ids}")
        for tile_id in tile_ids:
            rec = sr_mod.fetch_eth_gch_tile(tile_id, cache_dir=cache_dir, force=force)
            records.append(rec)
            status = "[dim]cache hit[/dim]" if rec.cache_hit else "[green]downloaded[/green]"
            console.print(f"  eth-gch {tile_id}: {rec.bytes_downloaded:,} bytes  {status}")

    if source_filter is None or "effis" in source_filter:
        rec = sr_mod.fetch_effis_fuel_map(cache_dir=cache_dir, force=force)
        records.append(rec)
        status = "[dim]cache hit[/dim]" if rec.cache_hit else "[green]downloaded[/green]"
        console.print(f"  effis: {rec.bytes_downloaded:,} bytes  {status}")

    if source_filter is None or "cosc" in source_filter:
        rec = sr_mod.fetch_dgt_cosc(cosc_vintage, cache_dir=cache_dir, force=force)
        records.append(rec)
        status = "[dim]cache hit[/dim]" if rec.cache_hit else "[green]downloaded[/green]"
        console.print(f"  dgt-cosc ({cosc_vintage}): {rec.bytes_downloaded:,} bytes  {status}")

    # DGT COS (species-level GeoPackage) is OPT-IN ONLY — request it explicitly
    # with `--only cos`. It is future work (unused by the fuel/score path; see
    # fuel.py) and its DGT download URL currently 404s, so it is excluded from
    # the default fetch to keep `fetch-rasters` (and the CPU demo) green.
    if source_filter is not None and "cos" in source_filter:
        rec = sr_mod.fetch_dgt_cos(cos_vintage, cache_dir=cache_dir, force=force)
        records.append(rec)
        status = "[dim]cache hit[/dim]" if rec.cache_hit else "[green]downloaded[/green]"
        console.print(f"  dgt-cos ({cos_vintage}): {rec.bytes_downloaded:,} bytes  {status}")

    if not records:
        console.print("[yellow]no sources selected — nothing fetched[/yellow]")
        raise typer.Exit(code=0)

    manifest = sr_mod.build_fetch_manifest(
        records,
        aoi_path=str(aoi),
        run_id=run_id,
        code_commit_sha=commit_sha,
        aoi_geometry_sha=aoi_sha,
        resolved_at_utc=resolved_at,
    )
    manifest_path = sr_mod.write_manifest(manifest, final_out)

    n_records = len(records)
    total_b = manifest.totals_bytes
    console.print(f"\n[dim]total:[/dim] {total_b:,} bytes across {n_records} record(s)")
    console.print(f"[dim]manifest:[/dim] {manifest_path}")


def _configure_burns_logging() -> None:
    _configure_module_logging("wildfire_exposure_eo.burns")


@app.command("fetch-burns")
def fetch_burns(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"),
        "--aoi",
        readable=True,
        dir_okay=False,
        help="Path to the AOI GeoJSON. Ignored when --smoke is set.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help=(
            "Output GeoParquet path. Supports {run_id} templating. Defaults to "
            "outputs/parquet/icnf_burns_{run_id}.parquet "
            "(icnf_burns_smoke_{run_id}.parquet with --smoke)."
        ),
    ),
    start_year: int = typer.Option(
        1975, "--start-year", min=1975, max=2100, help="First vintage year to include."
    ),
    end_year: int = typer.Option(
        2025, "--end-year", min=1975, max=2100, help="Last vintage year to include."
    ),
    mapserver_url: str = typer.Option(
        burns_mod.ICNF_MAPSERVER_URL,
        "--mapserver-url",
        help="ICNF ArcGIS REST MapServer root URL.",
    ),
    smoke: bool = typer.Option(
        False, "--smoke", help="Use data/aoi/smoke.geojson and the smoke output path."
    ),
) -> None:
    """Query ICNF Áreas Ardidas (1975–latest) and write a provenance-tagged GeoParquet.

    All feature IDs and vintage years come from the live MapServer — none are
    invented.  The output validates as wildfire_exposure_eo.schemas.BurnPerimeter
    per row.
    """
    console = Console()
    _configure_burns_logging()

    if smoke:
        aoi = Path("data/aoi/smoke.geojson")
        default_out_template = "outputs/parquet/icnf_burns_smoke_{run_id}.parquet"
    else:
        default_out_template = "outputs/parquet/icnf_burns_{run_id}.parquet"
    if not aoi.exists():
        raise typer.BadParameter(f"--aoi: {aoi} does not exist")
    if start_year > end_year:
        raise typer.BadParameter("--start-year must be ≤ --end-year")

    created_at = datetime.now(UTC)
    run_id = created_at.strftime("%Y%m%dT%H%M%SZ")
    out_path = out if out is not None else Path(default_out_template)
    final_out = Path(str(out_path).replace("{run_id}", run_id))

    _, aoi_sha = stac_mod.load_aoi_geometry(aoi)
    commit_sha = stac_mod.code_commit_sha(cwd=Path.cwd())

    console.print(f"[dim]AOI:[/dim] {aoi}")
    console.print(f"[dim]mapserver:[/dim] {mapserver_url}")
    console.print(f"[dim]years:[/dim] {start_year}–{end_year}")
    console.print(f"[dim]run_id:[/dim] {run_id}\n")

    result_path = burns_mod.fetch_burns(
        aoi,
        final_out,
        start_year=start_year,
        end_year=end_year,
        mapserver_url=mapserver_url,
        run_id=run_id,
        code_commit_sha=commit_sha,
        aoi_geometry_sha=aoi_sha,
    )

    import geopandas as gpd

    gdf = gpd.read_parquet(result_path)
    counts: dict[int, int] = {}
    if not gdf.empty:
        counts = gdf.groupby("vintage_year").size().to_dict()

    from rich.table import Table as RichTable

    table = RichTable(title="ICNF burn perimeters by vintage year", show_lines=False)
    table.add_column("Vintage year", style="bold")
    table.add_column("Features", justify="right")
    table.add_column("Area (ha)", justify="right")
    for year in sorted(counts):
        year_gdf = gdf[gdf["vintage_year"] == year]
        total_ha = float(year_gdf["area_ha"].sum())
        table.add_row(str(year), str(int(counts[year])), f"{total_ha:,.1f}")
    console.print(table)
    console.print(
        f"\n[dim]total rows:[/dim] {len(gdf)}  "
        f"[dim]vintages:[/dim] {gdf['vintage_year'].nunique() if not gdf.empty else 0}  "
        f"[dim]parquet:[/dim] {result_path}"
    )


def _configure_fuel_logging() -> None:
    _configure_module_logging("wildfire_exposure_eo.fuel")


@app.command("fuel-layer")
def fuel_layer(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"),
        "--aoi",
        help="Path to the AOI GeoJSON. Ignored when --smoke is set.",
    ),
    crosswalk: Path = typer.Option(
        Path("config/fuel_crosswalk.yaml"),
        "--crosswalk",
        help="Path to the fuel crosswalk YAML.",
    ),
    cache_dir: Path = typer.Option(
        Path("data/cache"),
        "--cache-dir",
        help="Root directory of the WU-3 raster cache.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help=(
            "Output COG path (default: "
            "outputs/cogs/fuel_class_{run_id}.tif or "
            "outputs/cogs/fuel_class_smoke_{run_id}.tif with --smoke)."
        ),
    ),
    stac_root: Path = typer.Option(
        Path("stac"),
        "--stac-root",
        help="Root of the STAC catalog tree.",
    ),
    smoke: bool = typer.Option(
        False, "--smoke", help="Use data/aoi/smoke.geojson and the smoke output path."
    ),
) -> None:
    """Derive a fuel-class COG (EFFIS + COSc crosswalk) on the pilot or smoke grid.

    Reads from the WU-3 raster cache; no network calls.
    Outputs a 2-band COG:  band 1 = EFFIS NFFL fuel class (uint8),
                           band 2 = severity × 100 (uint8).
    Appends a STAC item under stac/fuel-layer/.
    """
    _configure_fuel_logging()
    console = Console()

    if smoke:
        aoi = Path("data/aoi/smoke.geojson")
        default_out_template = "outputs/cogs/fuel_class_smoke_{run_id}.tif"
    else:
        default_out_template = "outputs/cogs/fuel_class_{run_id}.tif"

    if not aoi.exists():
        raise typer.BadParameter(f"--aoi: {aoi} does not exist")
    if not crosswalk.exists():
        raise typer.BadParameter(f"--crosswalk: {crosswalk} does not exist")

    effis_path = cache_dir / "effis" / "effis_european_fuel_map.tif"
    cosc_path = cache_dir / "dgt-cosc" / "cosc_2024_pre_verao.tif"

    for p, label in [(effis_path, "EFFIS"), (cosc_path, "COSc")]:
        if not p.exists():
            console.print(f"[red]ERROR:[/red] {label} cache not found: {p}")
            console.print(
                "Run [bold]uv run wildfire-exposure-eo fetch-rasters[/bold] first (WU-3)."
            )
            raise typer.Exit(code=1)

    from datetime import UTC, datetime

    run_ts = datetime.now(UTC)
    run_id = run_ts.strftime("%Y%m%dT%H%M%SZ")

    out_path = out if out is not None else Path(default_out_template)
    final_out = Path(str(out_path).replace("{run_id}", run_id))
    final_out.parent.mkdir(parents=True, exist_ok=True)

    commit_sha = stac_mod.code_commit_sha(cwd=Path.cwd())
    aoi_sha = fuel_mod._sha256_bytes(aoi.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))

    import rasterio

    with rasterio.open(effis_path) as ds:
        effis_res_m = float(abs(ds.res[0]))
        effis_sha = fuel_mod._sha256_file(effis_path)
    with rasterio.open(cosc_path) as ds:
        cosc_res_m = float(abs(ds.res[0]))
        cosc_sha = fuel_mod._sha256_file(cosc_path)

    console.print(f"[dim]AOI:[/dim] {aoi}")
    console.print(f"[dim]crosswalk:[/dim] {crosswalk}")
    console.print(f"[dim]EFFIS:[/dim] {effis_path} ({effis_res_m:.0f} m native res)")
    console.print(f"[dim]COSc:[/dim] {cosc_path} ({cosc_res_m:.0f} m native res)")
    console.print(f"[dim]run_id:[/dim] {run_id}")

    # 1. Load crosswalk
    cw = fuel_mod.load_crosswalk(crosswalk)
    console.print(f"[dim]crosswalk version:[/dim] {cw.version} ({len(cw.entries)} entries)")

    # 2. Compute explicit grid
    grid = fuel_mod.pilot_grid(aoi)
    console.print(
        f"[dim]grid:[/dim] {grid.width} × {grid.height} px @ {grid.resolution_m} m  CRS {grid.crs}"
    )

    # 3. Reproject EFFIS + apply crosswalk
    console.print("Reprojecting EFFIS and applying crosswalk …")
    klass, severity_x100 = fuel_mod.reclass_effis(effis_path, grid, cw)

    # 4. Refine with COSc
    console.print("Refining with COSc …")
    klass, severity_x100 = fuel_mod.refine_with_cosc(klass, severity_x100, cosc_path, grid, cw)

    # 5. Build provenance
    from wildfire_exposure_eo.schemas.fuel_layer import FuelLayerProvenance

    provenance = FuelLayerProvenance(
        run_id=run_id,
        code_commit_sha=commit_sha,
        aoi_path=str(aoi),
        aoi_geometry_sha=aoi_sha,
        effis_cache_path=str(effis_path),
        effis_sha256=effis_sha,
        effis_vintage="2023",
        effis_native_res_m=effis_res_m,
        cosc_cache_path=str(cosc_path),
        cosc_sha256=cosc_sha,
        cosc_vintage="2024_pre_verao",
        cosc_native_res_m=cosc_res_m,
        crosswalk_sha=cw.crosswalk_sha,
        crosswalk_version=cw.version,
        grid=grid,
    )

    # 6. Write COG
    console.print(f"Writing COG → {final_out} …")
    fuel_mod.write_fuel_cog(klass, severity_x100, grid, final_out, provenance=provenance)

    # 7. Append STAC item
    console.print("Appending STAC item …")
    item_path = fuel_mod.write_stac_item(final_out, provenance, stac_root=stac_root)

    import numpy as np

    fuel_pixels = int(np.count_nonzero((klass > 0) & (klass != 255)))
    nonfuel_pixels = int(np.count_nonzero(klass == 0))
    nodata_pixels = int(np.count_nonzero(klass == 255))
    console.print(
        f"\n[green]Done.[/green] "
        f"fuel={fuel_pixels:,} px  non-fuel={nonfuel_pixels:,} px  nodata={nodata_pixels:,} px"
    )
    console.print(f"[dim]COG:[/dim] {final_out}")
    console.print(f"[dim]STAC item:[/dim] {item_path}")


def _configure_features_logging() -> None:
    _configure_module_logging("wildfire_exposure_eo.features")
    _configure_module_logging("wildfire_exposure_eo.burn_scar")


def _latest_artifact(folder: Path, prefix: str, suffix: str, *, smoke: bool) -> Path:
    """Newest ``{prefix}[_smoke]_*{suffix}`` in ``folder`` (timestamps sort lexically)."""
    pattern = f"{prefix}_smoke_*{suffix}" if smoke else f"{prefix}_*{suffix}"
    candidates = sorted(folder.glob(pattern))
    if not smoke:
        candidates = [c for c in candidates if "_smoke_" not in c.name]
    if not candidates:
        raise typer.BadParameter(f"no {prefix}*{suffix} artefact found in {folder}")
    return candidates[-1]


@app.command("score")
def score(
    aoi: Path = typer.Option(
        Path("data/aoi/pilot.geojson"), "--aoi", help="AOI GeoJSON. Ignored when --smoke is set."
    ),
    window_end: str = typer.Option(
        ...,
        "--window-end",
        help="Score-input window end (YYYY-MM-DD). Required; WU-7's leakage rule depends on it.",
    ),
    osm_parquet: Path | None = typer.Option(None, "--osm", help="WU-2 OSM asset GeoParquet."),
    burns_parquet: Path | None = typer.Option(None, "--burns", help="WU-4 ICNF burns GeoParquet."),
    fuel_cog: Path | None = typer.Option(None, "--fuel-cog", help="WU-5 fuel-class COG."),
    burn_scar_cog: Path | None = typer.Option(None, "--burn-scar-cog", help="WU-1 burn-scar COG."),
    cache_dir: Path = typer.Option(Path("data/cache"), "--cache-dir", help="WU-3 raster cache."),
    taxonomy: Path = typer.Option(Path("data/taxonomy/critical_infrastructure.yaml"), "--taxonomy"),
    exposure_config: Path = typer.Option(Path("config/exposure_score.yaml"), "--exposure-config"),
    features_out: Path | None = typer.Option(
        None, "--features-out", help="Default outputs/parquet/features[_smoke]_{run_id}.parquet."
    ),
    exposure_out: Path | None = typer.Option(
        None, "--exposure-out", help="Default outputs/parquet/exposure[_smoke]_{run_id}.parquet."
    ),
    smoke: bool = typer.Option(False, "--smoke", help="Use data/aoi/smoke.geojson + smoke inputs."),
) -> None:
    """Compute per-asset features and the composite exposure rank (WU-6).

    Writes two GeoParquet artefacts: raw per-asset features and the scored rows
    (one ``ScoredAsset`` per row, full provenance). The exposure value is a
    relative, AOI-normalised screening rank — never a probability of fire.
    """
    _configure_features_logging()
    console = Console()
    from wildfire_exposure_eo import features as features_mod

    tag = "_smoke" if smoke else ""
    if smoke:
        aoi = Path("data/aoi/smoke.geojson")
    if not aoi.exists():
        raise typer.BadParameter(f"--aoi: {aoi} does not exist")
    win_end = _parse_iso_date(window_end, flag="--window-end")

    parquet_dir = Path("outputs/parquet")
    cogs_dir = Path("outputs/cogs")
    osm_path = osm_parquet or _latest_artifact(parquet_dir, "osm_assets", ".parquet", smoke=smoke)
    burns_path = burns_parquet or _latest_artifact(
        parquet_dir, "icnf_burns", ".parquet", smoke=smoke
    )
    fuel_path = fuel_cog or _latest_artifact(cogs_dir, "fuel_class", ".tif", smoke=smoke)
    burn_scar_path = burn_scar_cog or _latest_artifact(cogs_dir, "burn_scar", ".tif", smoke=smoke)
    gch_candidates = sorted((cache_dir / "eth-gch-2020").glob("*.tif"))
    if not gch_candidates:
        raise typer.BadParameter(f"no ETH GCH tile in {cache_dir / 'eth-gch-2020'} (run WU-3)")
    gch_path = gch_candidates[0]

    crosswalk_sha = str(json.loads(fuel_path.with_suffix(".json").read_text())["crosswalk_sha"])

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    feats_out = Path(
        str(features_out or Path(f"outputs/parquet/features{tag}_{{run_id}}.parquet")).replace(
            "{run_id}", run_id
        )
    )
    exp_out = Path(
        str(exposure_out or Path(f"outputs/parquet/exposure{tag}_{{run_id}}.parquet")).replace(
            "{run_id}", run_id
        )
    )

    for label, p in [
        ("OSM", osm_path),
        ("burns", burns_path),
        ("fuel COG", fuel_path),
        ("burn-scar COG", burn_scar_path),
        ("ETH GCH", gch_path),
    ]:
        console.print(f"[dim]{label}:[/dim] {p}")
    console.print(f"[dim]window-end:[/dim] {win_end}   [dim]run_id:[/dim] {run_id}")
    console.print("Computing features (Cop-DEM slope + S2 NBR delta are resolved from STAC) …")

    result = features_mod.run_scoring(
        aoi_path=aoi,
        taxonomy_path=taxonomy,
        exposure_config_path=exposure_config,
        crosswalk_sha=crosswalk_sha,
        osm_parquet=osm_path,
        burns_parquet=burns_path,
        fuel_cog=fuel_path,
        gch_cog=gch_path,
        burn_scar_cog=burn_scar_path,
        window_end=win_end,
        run_id=run_id,
        code_commit_sha=stac_mod.code_commit_sha(cwd=Path.cwd()),
        features_out=feats_out,
        exposure_out=exp_out,
    )

    console.print(
        f"\n[green]Done.[/green] {result.n_assets:,} assets  "
        f"[dim]zonal {result.ms_per_asset:.2f} ms/asset; "
        f"raster build {result.build_seconds:.1f}s (one-time)[/dim]"
    )
    if result.ms_per_asset > 10.0:
        console.print(
            f"[yellow]NOTE:[/yellow] zonal {result.ms_per_asset:.1f} ms/asset exceeds the "
            "10 ms target."
        )
    console.print(f"[dim]features present:[/dim] {', '.join(result.features_present_global)}")
    console.print(f"[dim]features:[/dim] {result.features_path}")
    console.print(f"[dim]exposure:[/dim] {result.exposure_path}")
    console.print(
        f"[dim]top-ranked:[/dim] {result.sample_row['asset_id']} "
        f"({result.sample_row['asset_class']})  "
        f"score={result.sample_row['exposure_score']:.4f}  rank=1"
    )


@app.command("validate-schema")
def validate_schema(
    parquet: Path = typer.Argument(..., help="Exposure GeoParquet to validate row-by-row."),
    limit: int = typer.Option(0, "--limit", help="Validate only the first N rows (0 = all)."),
) -> None:
    """Validate every row of an exposure GeoParquet against the ScoredAsset schema."""
    import geopandas as gpd

    from wildfire_exposure_eo.schemas import ScoredAsset

    console = Console()
    gdf = gpd.read_parquet(parquet)
    rows = gdf if limit <= 0 else gdf.head(limit)
    drop = {"geometry"}
    n = 0
    for _, row in rows.iterrows():
        ScoredAsset.model_validate({k: v for k, v in row.items() if k not in drop})
        n += 1
    console.print(f"[green]OK:[/green] {n} ScoredAsset row(s) validated in {parquet}")


if __name__ == "__main__":
    app()
