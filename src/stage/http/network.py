import socket
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait

PROBE_HOSTS = ("raw.githubusercontent.com", "boards-api.greenhouse.io", "api.lever.co")
PROBE_TIMEOUT_S = 3.0


def _resolves(host: str) -> bool:
    try:
        return bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    except OSError:
        return False


def online(hosts: Sequence[str] = PROBE_HOSTS, timeout: float = PROBE_TIMEOUT_S) -> bool:
    pool = ThreadPoolExecutor(max_workers=max(1, len(hosts)))
    try:
        futures = [pool.submit(_resolves, host) for host in hosts]
        done, _ = wait(futures, timeout=timeout)
        return any(future.result() for future in done)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def wait_until_online(
    patience_s: float,
    *,
    check: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    step_s: float = 10.0,
) -> bool:
    probe = check or online
    deadline = clock() + max(0.0, patience_s)
    while True:
        if probe():
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(step_s, remaining))
