"""Synthetic-only concurrency experiment. No Flask, ORM, network or files as inputs."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import sqlite3
import statistics
import threading
import time


@dataclass(frozen=True)
class Shipment:
    id: int
    number: str
    account: str
    manual: tuple


def fixtures():
    db = sqlite3.connect(':memory:')
    db.executescript("""
        CREATE TABLE agents (id INTEGER PRIMARY KEY, account TEXT);
        CREATE TABLE shipments (id INTEGER PRIMARY KEY, number TEXT, agent INTEGER, customer INTEGER);
        CREATE TABLE manual (shipment INTEGER, description TEXT);
    """)
    db.executemany('INSERT INTO agents VALUES (?,?)', [(i, f'FAKE-ACCOUNT-{i}') for i in range(1, 4)])
    db.executemany('INSERT INTO shipments VALUES (?,?,?,?)',
                   [(i, f'DEMO-{i:03}', i % 3 + 1, 1) for i in range(1, 201)]
                   + [(201, 'AMBIGUOUS', 1, 1), (202, 'AMBIGUOUS', 2, 1),
                      (203, 'DEMO-001', 3, 2)])
    db.executemany('INSERT INTO manual VALUES (?,?)', [(i, f'MANUAL-{i}') for i in range(1, 204)])
    db.commit()
    return db


def select(db, numbers, customer=1):
    numbers = list(dict.fromkeys(numbers))
    if len(numbers) > 200:
        raise ValueError('maximum 200 numbers')
    if not numbers:
        return []
    placeholders = ','.join('?' for _ in numbers)
    # Read only the unique matches within the authorized fixture scope.
    rows = db.execute(f"""
        WITH matches AS (
            SELECT number, count(*) AS count, min(id) AS id FROM shipments
            WHERE customer = ? AND number IN ({placeholders}) GROUP BY number
        )
        SELECT m.number, m.count, s.id, a.account
        FROM matches m LEFT JOIN shipments s ON m.count = 1 AND s.id = m.id
        LEFT JOIN agents a ON a.id = s.agent
    """, (customer, *numbers)).fetchall()
    matches = {number: (count, sid, account) for number, count, sid, account in rows}
    ids = [sid for count, sid, _ in matches.values() if count == 1]
    manual = {}
    if ids:
        marks = ','.join('?' for _ in ids)
        for sid, description in db.execute(
            f'SELECT shipment, description FROM manual WHERE shipment IN ({marks}) ORDER BY rowid', ids
        ):
            manual.setdefault(sid, []).append(description)
    selections = []
    for number in numbers:
        count, sid, account = matches.get(number, (0, None, None))
        if count == 1:
            selections.append((number, Shipment(sid, number, account, tuple(manual.get(sid, ())))))
        else:
            selections.append((number, 'ambiguous' if count else 'missing'))
    return selections


class FakeProvider:
    def __init__(self, delay, faults=False):
        self.delay = delay
        self.faults = faults
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.calls = Counter()

    def fetch(self, shipment):
        with self.lock:
            self.calls[(shipment.id, shipment.number, shipment.account)] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            delay = self.delay
            if self.faults and shipment.id == 1:
                delay = .06
            # The simulator cooperatively enforces a per-call deadline.
            timeout = .55 if not self.faults else .02
            time.sleep(min(delay, timeout))
            if delay > timeout:
                raise TimeoutError()
            if self.faults and shipment.id == 2:
                raise ValueError('synthetic failure')
            if self.faults and shipment.id == 3:
                return 'WRONG-NUMBER', shipment.account
            return shipment.number, shipment.account
        finally:
            with self.lock:
                self.active -= 1


def query(db, numbers, workers, delay, faults=False):
    if workers not in (1, 2, 4, 8):
        raise ValueError('unsupported concurrency')
    statements = []
    db.set_trace_callback(statements.append)
    start = time.perf_counter()
    try:
        selections = select(db, numbers)
    finally:
        db.set_trace_callback(None)
    provider = FakeProvider(delay, faults)

    def run(selection):
        number, shipment = selection
        if isinstance(shipment, str):
            return number, shipment, ()
        try:
            identity = provider.fetch(shipment)
            status = 'ok' if identity == (number, shipment.account) else 'identity_mismatch'
        except TimeoutError:
            status = 'timeout'
        except ValueError:
            status = 'failed'
        return number, status, shipment.manual

    if workers == 1:
        results = list(map(run, selections))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(run, selections))
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert [number for number, _, _ in results] == list(dict.fromkeys(numbers))
    expected = Counter((int(n[5:]), n, f'FAKE-ACCOUNT-{int(n[5:]) % 3 + 1}')
                       for n in dict.fromkeys(numbers) if n.startswith('DEMO-'))
    assert provider.calls == expected and 0 <= provider.peak <= workers
    for number, _, manual in results:
        if number.startswith('DEMO-'):
            assert manual == (f'MANUAL-{int(number[5:])}',)
    statuses = Counter(status for _, status, _ in results)
    return dict(ms=elapsed_ms, sql=len(statements), peak=provider.peak,
                calls=sum(provider.calls.values()), statuses=dict(statuses))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, choices=(2, 3, 5), default=3)
    args = parser.parse_args()
    db = fixtures()
    for workers in (1, 2, 4, 8):
        check = query(db, ['DEMO-004', 'DEMO-001', 'DEMO-002', 'DEMO-003',
                           'DEMO-004', 'MISSING', 'AMBIGUOUS'], workers, .005, faults=True)
        assert check['statuses'] == dict(ok=1, timeout=1, failed=1, identity_mismatch=1, missing=1, ambiguous=1)
        print(json.dumps(dict(kind='safety', workers=workers, **check)), flush=True)
    for latency in (10, 100, 500):
        for size in (10, 50, 200):
            numbers = [f'DEMO-{i:03}' for i in range(size, 0, -1)]
            for workers in (1, 2, 4, 8):
                query(db, numbers[:1], workers, latency / 1000)
                samples = [query(db, numbers, workers, latency / 1000) for _ in range(args.repeats)]
                assert all(s['statuses'] == {'ok': size} for s in samples)
                times = [s['ms'] for s in samples]
                print(json.dumps(dict(kind='timing', latency=latency, size=size, workers=workers,
                                      median=statistics.median(times), p95=max(times), samples=samples)), flush=True)
    db.close()


if __name__ == '__main__':
    main()
