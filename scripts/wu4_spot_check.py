"""WU-4 spot-check: validate pilot GeoParquet schema, provenance, and CRS."""

from pathlib import Path

import geopandas as gpd

from wildfire_exposure_eo.schemas import BurnPerimeter, BurnPerimeterProvenance

parquets = sorted(
    p for p in Path("outputs/parquet").glob("icnf_burns_202*.parquet") if "smoke" not in p.name
)
path = parquets[-1]
gdf = gpd.read_parquet(path)

print(f"File: {path}")
print(f"Total rows: {len(gdf)}")
print(f"Vintages: {sorted(int(v) for v in gdf['vintage_year'].unique())}")
assert gdf.crs is not None
print(f"CRS EPSG: {gdf.crs.to_epsg()}")

for year in (2017, 2020, 2024):
    n = len(gdf[gdf["vintage_year"] == year])
    total_ha = float(gdf[gdf["vintage_year"] == year]["area_ha"].sum())
    print(f"  year={year}: {n} features, {total_ha:,.1f} ha")

row = gdf.iloc[0]
prov = BurnPerimeterProvenance.model_validate(row["provenance"])
BurnPerimeter.model_validate(
    {
        "row_id": str(row["row_id"]),
        "vintage_year": int(row["vintage_year"]),
        "icnf_feature_id": int(row["feature_id"]),
        "geometry_wkb": bytes(row["geometry_wkb"]),
        "area_ha": float(row["area_ha"]),
        "provenance": prov,
    }
)
print(f"\nRow 0 row_id: {row['row_id']}")
print(f"Row 0 vintage_year (column): {int(row['vintage_year'])}")
print(f"Row 0 attribution: {prov.attribution!r}")
print("BurnPerimeter.model_validate: OK")

area_stored = float(row["area_ha"])
area_computed = float(gdf.iloc[[0]].to_crs("EPSG:3763").geometry.area.iloc[0] / 10000)
pct_diff = abs(area_stored - area_computed) / max(area_stored, 1) * 100
print(
    f"\nArea sanity: stored={area_stored:.2f} ha,"
    f" 3763-computed={area_computed:.2f} ha, diff={pct_diff:.1f}%"
)
