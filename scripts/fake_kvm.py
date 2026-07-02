#!/usr/bin/env python3
"""Development helper: emulate a TESmart HKS1601-EB23 on TCP.

Usage: python scripts/fake_kvm.py [port]   (default 5999)

Implements the observed behaviour: 6-byte command frames, zero-indexed
status responses with the 0x16+idx checksum, response only while the
connection stays open.
"""

import asyncio
import sys


class FakeKVM:
    def __init__(self) -> None:
        self.active_input = 1  # one-based

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            data = await reader.read(64)
            if not data:
                return
            print(f"[fake-kvm] {peer} -> {data.hex(' ')}")
            if data[:4] == bytes([0xAA, 0xBB, 0x03, 0x10]):
                idx = self.active_input - 1
                frame = bytes([0xAA, 0xBB, 0x03, 0x11, idx, (0x16 + idx) & 0xFF])
                writer.write(frame)
                await writer.drain()
                print(f"[fake-kvm] {peer} <- {frame.hex(' ')}")
            elif data[:4] == bytes([0xAA, 0xBB, 0x03, 0x01]) and 1 <= data[4] <= 16:
                self.active_input = data[4]
                print(f"[fake-kvm] switched to PC{self.active_input}")
        finally:
            writer.close()


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5999
    kvm = FakeKVM()
    server = await asyncio.start_server(kvm.handle, "127.0.0.1", port)
    print(f"[fake-kvm] listening on 127.0.0.1:{port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
