# Spot pricing and offers

Two tools help you choose a machine shape before you provision anything. Neither tool creates a VM,
reserves capacity, or changes a cloud or GitHub setting. They can write local files: `spot_prices.py --csv`
saves its rows, and `spot_finder.py` writes a quota cooldown marker in the temporary directory after a quota
error.

| Tool | Use it to | Data source |
| --- | --- | --- |
| `tools/spot_prices.py` | List every machine type that meets a minimum CPU and RAM and costs at most a daily cap. | Official Billing Catalog and the machine-type inventory. |
| `src/spot_finder.py` | Rank the cheapest Spot offers across all active regions and show per-runner costs. | Billing Catalog rates when a Billing Catalog API key is set and `--list-price` isn't. Otherwise, Compute Engine Spot price lookups. |

Both tools use the active `gcloud` account and project, or the project that you pass with `--project`. See
[Getting started](getting-started.md) for the prerequisites.

## List prices with a daily cap

Use `tools/spot_prices.py` when you know the CPU, RAM, and budget:

```bash
python3 tools/spot_prices.py --min-cpu 4 --min-ram 8 \
  --price-per-day 5 --list cheapest --results all
```

- `--min-ram` is in GiB, and `--price-per-day` is the maximum dollars (USD) for 24 hours of Spot **compute only**.
  Disks, GPUs, network traffic, and taxes are excluded.
- `--list` is `cheapest` (the default) or `expensive`. `--results` is `all` (the default) or a positive count.
- The command prints CSV to standard output with the columns `region`, `machine_type`, `vCPU`, `RAM_GiB`,
  `USD_hour`, `USD_day`, and `zones`. Progress and diagnostics go to standard error. `--csv PATH` also saves the
  displayed rows to a file.
- Accelerator and TPU families, bare-metal shapes, and obsolete or deleted machine types are skipped without a
  diagnostic. Shared-core shapes and machine types without a supported catalog price are omitted and counted in
  the diagnostics. A catalog component with conflicting prices is also omitted and reported as unsupported.
- An authentication failure or an incomplete machine inventory fails the command instead of printing partial
  results.

The tool prices M1 and M2 shapes, and the legacy N1 memory shapes, with the memory-optimized catalog rates.
M2 on-demand and Spot prices apply only to an evaluation period, and long-running M2 use requires a
resource-based commitment. Check the
[M2 machine series requirements](https://docs.cloud.google.com/compute/docs/memory-optimized-machines#the_m2_machine_series)
before you launch one.

## Find candidate offers

Use `src/spot_finder.py` to compare offers with deployment constraints:

```bash
python3 src/spot_finder.py \
  --project YOUR_PROJECT \
  --min-vcpu 16 \
  --min-ram 32 \
  --top 10
```

- The defaults are `--min-vcpu 32`, `--min-ram 128`, and `--top 10`. `--min-ram` accepts a number with an
  optional `GB` or `GiB` suffix.
- Each row shows the hourly, daily, and monthly price (730 hours), the runner count, the vCPUs per runner, and
  the cost per runner. By default a host gets one runner for every 4 whole vCPUs, so a 32 vCPU host has 8 runners. A host below 4 vCPUs or with at most 8 GiB of RAM gets one runner, as the manager assigns. Set
  `--runners` to change that count. The cost per runner is the host's total price divided by the runner count,
  so the daily figure for a 32 vCPU host is one eighth of its daily price by default.
- The finder prints a discount alert for each offer that is at least 95 percent below the on-demand price. It
  compares against `--list-price` (an hourly on-demand baseline) or, when a Billing Catalog API key is set
  with `--billing-api-key` or `GOOGLE_CLOUD_API_KEY`, against the public on-demand rates. Without either, the
  alerts are turned off and the tool says so.
- `--concurrency` limits parallel API requests (default 600). Repeat `-v` for more diagnostic detail.
- The exit status is 2 when no matching Spot price data exists, and 1 for an authentication or API error. An unreachable zone in the machine inventory
  only logs a warning.
  A partial scan exits 0 and prints a warning on stderr. In a partial scan, some regional price requests fail but
  offers remain. If every request fails, the status is 2.

## Treat results as candidates

A result is not a reservation. Before you provision, confirm:

- **Zone availability and quota**: a listed shape can still be out of capacity or over a regional quota.
- **Compatible storage**: newer families use Hyperdisk Balanced with NVMe, and older families use different
  Persistent Disk types. See [Fleet and autoscaler](fleet-and-autoscaler.md).
- **Total cost**: attached boot and data disks cost money, so an hourly compute limit alone isn't a daily
  budget. The deployment manager's `--max-total-daily-usd` option caps compute plus storage.
- **Price changes**: the price of a running Spot VM can change after launch. The autoscaler's price guard re-checks running
  runner hosts against its `--max-daily-usd` cap.

When you are ready to preview a deployment, use `src/spot_runner_manager.py` without `--apply`. See the
[command reference](commands.md#spot-runner-manager).
