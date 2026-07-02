"""Integration-style tests for TESmartKVM against a fake in-process KVM."""

import asyncio

import pytest

from app.tesmart import TESmartConnectionError, TESmartKVM


class FakeKVM:
    """Minimal TCP server emulating the switch's observed behaviour."""

    def __init__(self, active_input: int = 1) -> None:
        self.active_input = active_input  # one-based
        self.received: list[bytes] = []
        self._got_frame = asyncio.Event()
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None

    async def wait_received(self, count: int, timeout: float = 2.0) -> None:
        """Wait until at least `count` frames arrived (client writes are
        fire-and-forget, so the server may not have read them yet)."""
        async with asyncio.timeout(timeout):
            while len(self.received) < count:
                self._got_frame.clear()
                await self._got_frame.wait()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.read(64)
            if not data:
                return
            self.received.append(data)
            self._got_frame.set()
            if data[:4] == bytes([0xAA, 0xBB, 0x03, 0x10]):  # status query
                zero_based = self.active_input - 1
                writer.write(bytes([0xAA, 0xBB, 0x03, 0x11, zero_based, 0x16 + zero_based]))
                await writer.drain()
            elif data[:4] == bytes([0xAA, 0xBB, 0x03, 0x01]):  # select input
                self.active_input = data[4]
            # other commands: accept silently, like the real device
        finally:
            writer.close()


@pytest.fixture
async def fake_kvm():
    kvm = FakeKVM(active_input=1)
    await kvm.start()
    yield kvm
    await kvm.stop()


async def test_get_current_input(fake_kvm):
    client = TESmartKVM("127.0.0.1", fake_kvm.port, timeout=1.0)
    assert await client.get_current_input() == 1
    fake_kvm.active_input = 7
    assert await client.get_current_input() == 7


async def test_select_input_sends_correct_frame(fake_kvm):
    client = TESmartKVM("127.0.0.1", fake_kvm.port, timeout=1.0)
    await client.select_input(5)
    await fake_kvm.wait_received(1)
    assert fake_kvm.received[-1] == bytes([0xAA, 0xBB, 0x03, 0x01, 0x05, 0xEE])
    assert await client.get_current_input() == 5


async def test_buzzer_and_led_frames(fake_kvm):
    client = TESmartKVM("127.0.0.1", fake_kvm.port, timeout=1.0)
    await client.mute_buzzer()
    await fake_kvm.wait_received(1)
    assert fake_kvm.received[-1] == bytes([0xAA, 0xBB, 0x03, 0x02, 0x00, 0xEE])
    await client.unmute_buzzer()
    await fake_kvm.wait_received(2)
    assert fake_kvm.received[-1] == bytes([0xAA, 0xBB, 0x03, 0x02, 0x01, 0xEE])
    await client.set_led_timeout("never")
    await fake_kvm.wait_received(3)
    assert fake_kvm.received[-1] == bytes([0xAA, 0xBB, 0x03, 0x03, 0x00, 0xEE])


async def test_offline_device_raises_connection_error():
    # Nothing listens on this port; retries should end in a clear error.
    client = TESmartKVM("127.0.0.1", 1, timeout=0.3, retries=1)
    with pytest.raises(TESmartConnectionError):
        await client.get_current_input()


async def test_select_input_validates_range(fake_kvm):
    client = TESmartKVM("127.0.0.1", fake_kvm.port, timeout=1.0)
    with pytest.raises(ValueError):
        await client.select_input(0)
    with pytest.raises(ValueError):
        await client.select_input(17)
    assert fake_kvm.received == []
