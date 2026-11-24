"""Compare one hot stock row with 32 stock rows under the same local load."""
import json
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from statistics import quantiles
from uuid import uuid4

from verify import cancel, reserve, stock


def database(sql, **kwargs):
    return subprocess.Popen(
        ['docker', 'compose', 'exec', '-T', 'db', 'psql', '-U', 'postgres',
         '-v', 'ON_ERROR_STOP=1', '-Atc', sql], **kwargs
    )


def trial(rows):
    skus = [f'PROFILE-{i}' for i in range(rows)]
    before = {sku: stock(sku) for sku in skus}
    requests = [(str(uuid4()), skus[i % rows]) for i in range(450)]

    def send(task):
        started = time.perf_counter()
        status = reserve(*task)
        return status, (time.perf_counter() - started) * 1000

    # One persistent PostgreSQL connection, sampling every 10 ms.
    sql = """DO $$ DECLARE waiting int; BEGIN
    FOR i IN 1..220 LOOP
      PERFORM pg_stat_clear_snapshot();
      SELECT count(*) INTO waiting FROM pg_stat_activity
        WHERE pid <> pg_backend_pid() AND wait_event_type = 'Lock'
          AND query LIKE 'UPDATE inventory%';
      RAISE NOTICE '%|%', extract(epoch from clock_timestamp()), waiting;
      PERFORM pg_sleep(0.01);
    END LOOP; END $$;"""
    with database(sql, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True) as sampler:
        time.sleep(0.3)
        start = time.time()
        with ThreadPoolExecutor(max_workers=32) as pool:
            outcomes = list(pool.map(send, requests))
        end = time.time()
        _, notices = sampler.communicate()
        assert sampler.returncode == 0, notices
    waits = []
    for line in notices.splitlines():
        if line.startswith('NOTICE:'):
            stamp, count = line.split(':', 1)[1].split('|')
            if start <= float(stamp) <= end:
                waits.append(int(count))
    assert waits, notices
    assert Counter(status for status, _ in outcomes) == {201: len(requests)}
    used = Counter(sku for _, sku in requests)
    assert all(stock(sku) == before[sku] - used[sku] for sku in skus)
    with ThreadPoolExecutor(max_workers=32) as pool:
        assert list(pool.map(cancel, (identifier for identifier, _ in requests))) == [204] * len(requests)
    assert all(stock(sku) == before[sku] for sku in skus)
    return dict(rows=rows, clients=32, requests=len(requests),
                requests_per_second=round(len(requests)/(end-start), 1),
                p95_ms=round(quantiles([ms for _, ms in outcomes], n=100)[94], 1),
                samples=len(waits), samples_with_update_locks=sum(wait > 0 for wait in waits),
                peak_waiting_updates=max(waits))


if __name__ == '__main__':
    with database("INSERT INTO inventory SELECT 'PROFILE-' || n, 5000 FROM generate_series(0,31) n ON CONFLICT DO NOTHING;", stdout=subprocess.DEVNULL) as seed:
        assert seed.wait() == 0
    results = [trial(rows) for rows in (1, 32, 32, 1, 1, 32)]
    print(json.dumps(results, indent=2))
