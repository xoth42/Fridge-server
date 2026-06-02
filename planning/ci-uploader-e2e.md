# Plan: CI Uploader E2E Test (Tier 4)

## Context

The existing three-tier CI (boot / integration / e2e) validates the server stack in isolation. It does not test whether real fridge data — as produced by the Fridge-data-uploader client — actually makes it through the pipeline. This plan adds a fourth CI workflow that checks out both repos, generates mock Bluefors log files, runs `push_metrics.py` against the local stack, and verifies the metrics appear in Prometheus.

Only fake things: the log files (synthetic CSV) and SMTP routing (Mailpit). The push path — `push_metrics.py` → Pushgateway → Prometheus scrape — is fully real.

---

## Architecture

Two repos checked out in the same GitHub Actions runner workspace:

```
./          ← Fridge-server (current repo)
./uploader/ ← Fridge-data-uploader (xoth42/Fridge-data-uploader, public, no token needed)
```

---

## Workflow: `ci-uploader.yml`

**Trigger:** Same as other CI workflows — push to main, PR targeting main.

**No Python version matrix** — the uploader's `requirements.txt` is unpinned; one version is sufficient. Use `3.12`.

### Steps

1. Checkout Fridge-server (default path `.`)
2. Checkout Fridge-data-uploader:
   ```yaml
   - uses: actions/checkout@v4
     with:
       repository: xoth42/Fridge-data-uploader
       path: uploader
   ```
3. Set up Python 3.12 + install system deps (`gettext`, `jq`)
4. Set up CI environment: `cp test/.env.ci .env` + `envsubst` alertmanager template
5. Start stack: `docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d --build`
6. Wait for Grafana + run `scripts/ci_bootstrap.sh`
7. Install server test deps: `pip install -e ".[test]"`
8. Install client deps: `pip install -r uploader/requirements.txt`
9. Run pytest:
   ```yaml
   - run: python -m pytest test/testuploader/ -v
     env:
       UPLOADER_DIR: ${{ github.workspace }}/uploader
       PUSHGATEWAY_URL: http://localhost:9091
       PROMETHEUS_URL: http://localhost:9090
       MACHINE_NAME: fridge-manny
   ```
10. Tear down (`if: always()`)

---

## New Files

### `test/testuploader/mock_logs.py`

Importable module + standalone script. Creates a Bluefors-format date folder for today under a given root directory.

```python
def create_mock_logs(root: Path, machine: str = "fridge-manny") -> Path:
    """Create mock Bluefors log files under root/YY-MM-DD/.
    Returns the root path (logs_dir for push_metrics.py).
    """
```

**Date formats** (verified from actual Bluefors sample data):
- **Folder name**: `YY-MM-DD` — `date.today().strftime("%y-%m-%d")` e.g. `26-05-30`
- **Line content date**: `DD-MM-YY` — the parsers ignore this column entirely
- **Line content time**: fixed `10:00:00` — stale-data guard reads HH:MM; fresh run has `_last_pushed_time=None` so first call always pushes

**Files created** (all file types enabled in fridge-manny's config):

| File | Sample content |
|------|---------------|
| `Status_{date}.log` | `DD-MM-YY,10:00:00,nxdsf,3.0e+01,nxdspt,3.0e+02,nxdsct,3.2e+02,nxdstrs,7.1e+06,ctrl_pres_ok,1.0,ctrl_pres,1.0,cpastate,3.0,cparun,1.0,cpawarn,0.0,cpaerr,0.0,cpatempwi,1.8e+01,cpatempwo,2.8e+01,cpatempo,3.3e+01,cpatemph,6.7e+01,cpalp,8.2e+01,cpalpa,8.0e+01,cpahp,3.0e+02,cpahpa,3.0e+02,cpadp,2.2e+02,cpacurrent,1.5e+01,cpahours,2.3e+04,cpascale,0.0,cpasn,1.1e+04,tc400actualspd,8.2e+02,tc400drvpower,2.0e+02,tc400ovtempelec,0.0,tc400ovtemppum,0.0,tc400heating,0.0,tc400pumpaccel,0.0,tc400pumpstatn,1.0,tc400remoteprio,0.0,tc400spdswptatt,0.0,tc400setspdatt,0.0,tc400standby,0.0` |
| `CH1 T {date}.log` | `DD-MM-YY,10:00:00,4.030615e+01` |
| `CH1 R {date}.log` | `DD-MM-YY,10:00:00,5.737000e+00` |
| `CH2 T / R`, `CH5 T / R`, `CH6 T / R`, `CH9 T / R` | Same single-value format with realistic values |
| `Flowmeter {date}.log` | `DD-MM-YY,10:00:00,0.465553` |
| `Heaters {date}.log` | `DD-MM-YY,10:00:00,0,0.00e+00,1,8.00e-03` |
| `Channels {date}.log` | `DD-MM-YY,10:00:00,0,v1,1,v2,0,v3,0,v4,1,v5,0,v6,0,v7,1,v8,0,v9,1,v10,1,v11,0,v12,0,v13,1,v14,0,v15,0,v16,0,v17,0,v18,0,v19,0,v20,0,v21,0,v22,0,v23,0,turbo1,1,turbo2,0,scroll1,1,scroll2,0,compressor,0,pulsetube,1,hs-still,0,hs-mc,0,ext,1` |
| `maxigauge {date}.log` | `DD-MM-YY,10:00:00,CH1,        ,1,2.27e-06,0,1,CH2,        ,1,4.49e-02,0,1,CH3,        ,1,4.01e+02,0,1,CH4,        ,1,4.23e+02,0,1,CH5,        ,1,1.30e+00,0,1,CH6,        ,1,1.98e-01,0,1,` |

Status line uses keys from actual sample data — all recognized by `metric_metadata.py`.

---

### `test/testuploader/test_uploader_e2e.py`

Module-scoped fixtures so the push runs exactly once.

**Fixtures:**

- `mock_logs(tmp_path_factory)` — calls `create_mock_logs()`, returns the root log dir
- `uploader_configured(mock_logs)` — writes `.env` and `server.env` into `UPLOADER_DIR`; teardown deletes them

```
# .env written to uploader dir:
FRIGE_LOGS_DIR=<tmp path>
MACHINE_NAME=fridge-manny

# server.env written to uploader dir:
PUSHGATEWAY_URL=localhost:9091
```

**Tests:**

1. `test_push_exits_zero(uploader_configured)` — `subprocess.run(["python", "push_metrics.py"], cwd=UPLOADER_DIR)`, assert `returncode == 0`, assert no `ERROR` in stdout
2. `test_metrics_reach_prometheus(uploader_configured)` — poll `GET /api/v1/query?query={job="sensor_data",instance="fridge-manny"}` every 5s up to 90s; assert at least one result
3. `test_expected_metrics_present(uploader_configured)` — after same poll, check metric names include `ch1_t_kelvin`, `ch1_r_ohms`, `flowmeter_mmol_per_s`

Tests 2 and 3 share the same module-scoped fixture as test 1, so the subprocess push only runs once. If test 1 fails, 2 and 3 fail fast.

---

### `test/conftest.py` — add `prometheus_url` fixture

```python
@pytest.fixture(scope="session")
def prometheus_url() -> str:
    return os.getenv("PROMETHEUS_URL", "http://localhost:9090")
```

---

## Key Implementation Notes

- **`ch1_r` metric name is `ch1_r_ohms`** (not `ch1_r_ohm`) — verified in `metric_metadata.py` line 218
- **`flowmeter` metric name is `flowmeter_mmol_per_s`** — verified in `metric_metadata.py` line 305
- **Channels file parse**: `parts[2]` is a leading zero, name/state pairs start at `parts[3]`; hyphens in names become underscores
- **Maxigauge parse**: each channel block is 6 fields wide starting at index 2; pressure is at `block_start + 3`
- **Uploader env files** must NOT be committed — only written at test time and removed in teardown
- **`permissions: contents: read`** is sufficient for public repo checkout — no PAT needed

---

## Verification

- `ci-uploader` workflow triggers on push to main and PRs
- `test_push_exits_zero` passes: push_metrics.py exits 0 with no ERROR lines
- `test_metrics_reach_prometheus` passes: sensor_data metrics visible for fridge-manny in Prometheus
- `test_expected_metrics_present` passes: ch1_t_kelvin, ch1_r_ohms, flowmeter_mmol_per_s confirmed
