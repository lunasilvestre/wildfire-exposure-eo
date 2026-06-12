# HIL — WU-9: create the `geodata-v1` GitHub Release (one command)

**2026-06-12, WU-9 build session.** `gh release create` **and** `gh api -X POST
…/releases` are permission-blocked in the unattended session, so the Release
could not be created. Everything else in WU-9 is complete, gated, and
committed; the burn-scar STAC asset href and the site's two release-hosted
layers already point at the deterministic download URLs below and go live the
moment the release exists. No rebuild needed.

## What to run (from the repo root, on this machine)

```bash
gh release create geodata-v1 --target main \
  --title "geodata-v1 — published geodata (pilot AOI)" \
  --notes-file outputs/logs/wu9-release-notes.md \
  outputs/cogs/burn_scar_20260610T072820Z.tif \
  outputs/geobrowser/burn_scar_3857_20260610T072820Z.tif \
  outputs/geobrowser/icnf_burns_20260610T164453Z.geojson
```

All three files and the notes file exist on disk now (generated this session).
Expected sha256s (also in the notes file):

| file | sha256 |
|---|---|
| `burn_scar_20260610T072820Z.tif` (authoritative, EPSG:4326, STAC href) | `72075ac478de68e2a72a29bfe90e3f6f0d1201b9545036f6cbbdbbfe05dcacdd` |
| `burn_scar_3857_20260610T072820Z.tif` (EPSG:3857 display copy for the site) | `e839ad46eacb6190a9af397606c8bf2f8c8cb191430b1e3bba2c9b44915860c7` |
| `icnf_burns_20260610T164453Z.geojson` (EPSG:4326 display copy for the site) | `7295f85a21834b6be0bb59bd4c066270c4e21279ec74f47e04f64e1ca68ad886` |

## After the release exists (orchestrator/next session can do these)

1. Verify the three download URLs resolve and serve CORS + byte ranges
   (needed by the static page for the COG layer):
   `curl -sIL -H "Origin: https://lunasilvestre.github.io" -H "Range: bytes=0-99" https://github.com/lunasilvestre/wildfire-exposure-eo/releases/download/geodata-v1/burn_scar_3857_20260610T072820Z.tif`
   — expect `206` + `access-control-allow-origin: *` on the final hop. If CORS
   or ranges fail, the prompt's fallback applies (PMTiles pyramid or documented
   constraint) — that would need a small follow-up session.
2. Independent review of `43bc62c..HEAD`, then enable Pages
   (`prompts/_HANDOVER_WU9.md` step 6) and run the human visual-check list in
   the WU-9 session-log entry.

## Why this is the only open item

The repo-side TODO(provenance) is exactly this: the release URLs are
deterministic from the tag name but **unverifiable until the release exists**.
Session log entry 2026-06-12 (WU-9) records the decision to complete all
release-independent deliverables rather than strand the WU (a rebuild costs
~35 % of a block; this costs one command). OB1 capture was also
permission-blocked this session — the session log is the decision record.

Delete this file once the release exists and the URLs verify.
