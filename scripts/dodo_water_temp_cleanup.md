# Dodo Water Temperature Cleanup

This note documents the one-off cleanup workflow for the historical Dodo
cooling-water artifacts on Prometheus metrics:

- `cpatempwi_celsius{job="sensor_data",instance="fridge-dodo"}`
- `cpatempwo_celsius{job="sensor_data",instance="fridge-dodo"}`

## What Happened

Dodo sometimes logged raw cooling-water values as `0`. In the older uploader
path, those were converted as if they were Fahrenheit readings, which created
artifact points near `-17.77777777777778 C`. After later cleanup passes, some
remaining artifacts also appeared as raw `0` values.

The cleanup helper is:

```bash
python3 scripts/scrub_dodo_water_temp_artifacts.py
```

The script now:

- supports explicit `--start/--end` or relative `--past-hours`
- detects windows from raw samples, not carried-forward query results
- can print each suspicious sample with `--show`
- can snapshot, delete, clean tombstones, and verify that the raw samples are gone

## Common Commands

Inspect suspicious `-17.78 C` windows for the last 26 hours:

```bash
python3 scripts/scrub_dodo_water_temp_artifacts.py \
  --past-hours 26 \
  --metric cpatempwo_celsius \
  --show
```

Delete suspicious `-17.78 C` windows and clean tombstones:

```bash
python3 scripts/scrub_dodo_water_temp_artifacts.py \
  --past-hours 26 \
  --metric cpatempwo_celsius \
  --delete \
  --clean-tombstones
```

Inspect remaining zero-valued artifacts:

```bash
python3 scripts/scrub_dodo_water_temp_artifacts.py \
  --past-hours 26 \
  --metric cpatempwo_celsius \
  --low -0.001 \
  --high 0.001 \
  --show
```

Delete remaining zero-valued artifacts:

```bash
python3 scripts/scrub_dodo_water_temp_artifacts.py \
  --past-hours 26 \
  --metric cpatempwo_celsius \
  --low -0.001 \
  --high 0.001 \
  --show \
  --delete \
  --clean-tombstones
```

Check for short suspicious windows in the last day:

```bash
bash scripts/find_recent_dodo_water_temp_windows.sh
```

## Operational Notes

- Prometheus must be started with `--web.enable-admin-api` for deletion.
- On this repo, that flag is enabled in `docker-compose.yml` for the Prometheus service.
- `--past-hours` uses the current UTC time. If you need exact 15-second-aligned
  boundaries for a specific incident, prefer explicit `--start/--end` values.
- The script prints `Deleted ...` after a successful delete request, and then
  performs a verification pass against raw samples in the deleted interval.
