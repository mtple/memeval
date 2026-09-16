from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sdk" / "python"))

from market_replay.datasets.generator import dev_short_config, generate_pack  # noqa: E402
from market_replay.datasets.pack import Pack  # noqa: E402


class _NoNetwork:
    """Guard: tests must never open sockets to non-loopback hosts."""

    def __init__(self) -> None:
        self._orig_connect = socket.socket.connect

    def install(self) -> None:
        orig = self._orig_connect

        def guarded(sock, address):  # type: ignore[no-untyped-def]
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(host, str) and host not in ("127.0.0.1", "localhost", "::1") and not host.startswith("/"):
                raise RuntimeError(f"network access to {host} is blocked in tests")
            return orig(sock, address)

        socket.socket.connect = guarded  # type: ignore[method-assign]

    def uninstall(self) -> None:
        socket.socket.connect = self._orig_connect  # type: ignore[method-assign]


@pytest.fixture(scope="session", autouse=True)
def _block_network():
    g = _NoNetwork()
    g.install()
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.setdefault("MARKET_REPLAY_INFERENCE_PROVIDER", "mock")
    yield
    g.uninstall()


@pytest.fixture(scope="session")
def dev_pack_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("packs") / "gen_dev_short"
    generate_pack(dev_short_config(), out)
    return out


@pytest.fixture(scope="session")
def dev_pack(dev_pack_dir: Path) -> Pack:
    return Pack.load(dev_pack_dir)


@pytest.fixture()
def fresh_pack(dev_pack_dir: Path) -> Pack:
    return Pack.load(dev_pack_dir)
