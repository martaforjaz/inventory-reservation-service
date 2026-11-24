# Inventory Reservation Service

![Two requests compete for one inventory unit](figures/reservation.svg)

An ASP.NET Core API backed by PostgreSQL that reserves stock without overselling. When 64 requests competed for the last unit, one succeeded. Replaying a request did not consume extra stock, and concurrent cancellations returned the unit exactly once.

## Run and verify

Requires Docker Compose and Python 3. Start the API, run the integration checks, and optionally reproduce the local benchmark.

```sh
docker compose up -d --build
python verify.py
python verify.py --load
docker compose down
```

The API listens at `http://localhost:18081`. Read available inventory with `GET /stock/LAST`. Send `POST /reservations/{uuid}` with JSON `{"sku":"LAST","quantity":1}`. Repeat the same UUID and payload to replay a request. Use `DELETE /reservations/{uuid}` to cancel. The Compose database seeds synthetic stock automatically.

## Design

A client-generated UUID identifies each reservation. The reservation insert and a conditional stock decrement commit in one PostgreSQL transaction. If the decrement finds insufficient stock, the reservation insert rolls back. Cancellation changes the reservation state and restores inventory in one statement. Concurrent cancellations observe the same state transition and only the winning update returns stock. The GitHub Actions workflow runs integration checks against a real PostgreSQL container.

## Measured load

Single-SKU workload with 450 reservation requests per configuration, after a warm-up. Client-side p95 includes HTTP and database time. Cancellation runs outside the timed interval and restores stock after every run.

| Concurrent clients | Requests/s | p95 latency |
| ---: | ---: | ---: |
| 1 | 89.8 | 30.4 ms |
| 8 | 455.7 | 40.1 ms |
| 32 | 435.7 | 221.1 ms |

At 32 clients, tail latency rose without a throughput gain. The workload intentionally targets one stock row, making database contention a candidate for follow-up profiling. The benchmark does not isolate the cause or measure cloud capacity. See [raw results](results.json).

## Investigating PostgreSQL lock contention

Run `python profile_contention.py` with Docker Compose running. Each trial sends 450 reservations with 32 concurrent clients. The script alternates between sending all requests to one stock row and distributing them across 32 independent SKUs. It samples active PostgreSQL sessions to count stock updates waiting on locks and verifies inventory before and after cancellation.

Across three trials per configuration, median p95 latency was **64.6 ms** for one SKU and **18.1 ms** for 32 SKUs. PostgreSQL showed stock updates waiting on locks in **82 of 83** samples in the one-SKU trials, with up to **28 updates waiting at once**. In the 32-SKU trials no such waits appeared in 11 samples. The shorter runs yielded fewer samples. See the [recorded trials](contention-results.json).

The results identify contention on a shared stock row as a contributor to tail latency. Distributing requests across distinct SKUs is an experimental control. Requests for the same physical stock still use the atomic update to prevent overselling.

## Operations

The API provides separate liveness and database readiness endpoints at `/health/live` and `/health/ready`. The `Inventory.Reservations` .NET meter records request counts and durations by HTTP status for collection by a compatible listener. The Docker image accepts a PostgreSQL connection string via `ConnectionStrings__Inventory`. Initialize an external database with [schema.sql](schema.sql) before starting the container. The current benchmark was run locally.
