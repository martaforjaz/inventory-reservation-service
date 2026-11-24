"""Run concurrency checks and a local load benchmark against Docker Compose."""
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import quantiles
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = 'http://127.0.0.1:18081'


def request(path, method='GET', payload=None):
    data = json.dumps(payload).encode() if payload else None
    req = Request(BASE + path, data, {'Content-Type': 'application/json'}, method=method)
    try:
        with urlopen(req, timeout=10) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def reserve(identifier, sku='LAST', quantity=1):
    return request(f'/reservations/{identifier}', 'POST', {'sku': sku, 'quantity': quantity})[0]


def cancel(identifier):
    return request(f'/reservations/{identifier}', 'DELETE')[0]


def stock(sku):
    status, body = request(f'/stock/{sku}')
    assert status == 200, (status, body)
    return json.loads(body)['remaining']


def verify(pool):
    ids = [str(uuid4()) for _ in range(64)]
    assert stock('LAST') == 1
    outcomes = list(pool.map(reserve, ids))
    assert Counter(outcomes) == {201: 1, 409: 63}, Counter(outcomes)
    winner = ids[outcomes.index(201)]
    assert stock('LAST') == 0
    assert list(pool.map(reserve, [winner] * 24)) == [200] * 24
    assert reserve(winner, 'LAST', 2) == 409
    assert stock('LAST') == 0
    assert list(pool.map(cancel, [winner] * 24)) == [204] * 24
    assert stock('LAST') == 1
    assert reserve(winner) == 200
    assert stock('LAST') == 1
    retry = str(uuid4())
    assert reserve(retry) == 201 and cancel(retry) == 204
    assert cancel(str(uuid4())) == 404
    assert stock('LAST') == 1
    print('Verified 64 competing requests, 24 replays and 24 concurrent cancellations.')


def load(workers, size):
    def trial(identifier):
        start = time.perf_counter()
        status = reserve(identifier, 'LOAD')
        return status, (time.perf_counter() - start) * 1000

    before = stock('LOAD')
    ids = [str(uuid4()) for _ in range(size)]
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(trial, ids))
    elapsed = time.perf_counter() - start
    assert Counter(status for status, _ in outcomes) == {201: size}
    assert stock('LOAD') == before - size
    with ThreadPoolExecutor(max_workers=workers) as pool:
        assert list(pool.map(cancel, ids)) == [204] * size
    assert stock('LOAD') == before
    return {'clients': workers, 'requests': size, 'requests_per_second': round(size/elapsed, 1),
            'p95_ms': round(quantiles([ms for _, ms in outcomes], n=100)[94], 1)}


if __name__ == '__main__':
    for _ in range(30):
        try:
            if request('/health/ready')[0] == 200:
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('Inventory API did not become ready')
    with ThreadPoolExecutor(max_workers=32) as pool:
        verify(pool)
    if '--load' in sys.argv:
        load(8, 100)
        results = [load(clients, 450) for clients in (1, 8, 32)]
        Path('results.json').write_text(json.dumps(results, indent=2)+'\n', encoding='utf-8')
        print(results)
