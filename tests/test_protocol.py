"""Tests for frame generation and response parsing."""

import pytest

from app.tesmart import protocol


class TestBuildSelectInput:
    def test_pc1(self):
        assert protocol.build_select_input(1) == bytes([0xAA, 0xBB, 0x03, 0x01, 0x01, 0xEE])

    def test_pc2(self):
        assert protocol.build_select_input(2) == bytes([0xAA, 0xBB, 0x03, 0x01, 0x02, 0xEE])

    def test_pc16(self):
        assert protocol.build_select_input(16) == bytes([0xAA, 0xBB, 0x03, 0x01, 0x10, 0xEE])

    @pytest.mark.parametrize("bad", [0, 17, -1, 100])
    def test_out_of_range(self, bad):
        with pytest.raises(ValueError):
            protocol.build_select_input(bad)

    @pytest.mark.parametrize("bad", ["1", 1.5, None, True])
    def test_non_int(self, bad):
        with pytest.raises(ValueError):
            protocol.build_select_input(bad)


class TestOtherFrames:
    def test_query_input(self):
        assert protocol.build_query_input() == bytes([0xAA, 0xBB, 0x03, 0x10, 0x00, 0xEE])

    def test_mute(self):
        assert protocol.build_buzzer(mute=True) == bytes([0xAA, 0xBB, 0x03, 0x02, 0x00, 0xEE])

    def test_unmute(self):
        assert protocol.build_buzzer(mute=False) == bytes([0xAA, 0xBB, 0x03, 0x02, 0x01, 0xEE])

    def test_led_timeout_10(self):
        assert protocol.build_led_timeout("10") == bytes([0xAA, 0xBB, 0x03, 0x03, 0x0A, 0xEE])

    def test_led_timeout_30(self):
        assert protocol.build_led_timeout("30") == bytes([0xAA, 0xBB, 0x03, 0x03, 0x1E, 0xEE])

    def test_led_timeout_never(self):
        assert protocol.build_led_timeout("never") == bytes([0xAA, 0xBB, 0x03, 0x03, 0x00, 0xEE])

    def test_led_timeout_invalid(self):
        with pytest.raises(ValueError):
            protocol.build_led_timeout("60")


class TestParseCurrentInput:
    def test_pc1_observed_response(self):
        # Real capture: AA BB 03 11 00 16
        assert protocol.parse_current_input(bytes([0xAA, 0xBB, 0x03, 0x11, 0x00, 0x16])) == 1

    def test_pc2_observed_response(self):
        # Real capture: AA BB 03 11 01 17
        assert protocol.parse_current_input(bytes([0xAA, 0xBB, 0x03, 0x11, 0x01, 0x17])) == 2

    def test_pc16(self):
        assert protocol.parse_current_input(bytes([0xAA, 0xBB, 0x03, 0x11, 0x0F, 0x25])) == 16

    def test_last_byte_is_not_ee(self):
        # The final byte is a checksum, never assume 0xEE.
        frame = bytes([0xAA, 0xBB, 0x03, 0x11, 0x04, 0x1A])
        assert protocol.parse_current_input(frame) == 5

    def test_checksum_mismatch_is_tolerated(self, caplog):
        # Checksum scheme is inferred; a mismatch warns but still parses.
        frame = bytes([0xAA, 0xBB, 0x03, 0x11, 0x02, 0xFF])
        with caplog.at_level("WARNING"):
            assert protocol.parse_current_input(frame) == 3
        assert "checksum mismatch" in caplog.text

    def test_leading_garbage_is_skipped(self):
        noisy = bytes([0x00, 0xFF]) + bytes([0xAA, 0xBB, 0x03, 0x11, 0x05, 0x1B])
        assert protocol.parse_current_input(noisy) == 6

    def test_empty_response(self):
        with pytest.raises(protocol.TESmartProtocolError):
            protocol.parse_current_input(b"")

    def test_wrong_command_byte(self):
        with pytest.raises(protocol.TESmartProtocolError):
            protocol.parse_current_input(bytes([0xAA, 0xBB, 0x03, 0x01, 0x01, 0xEE]))

    def test_truncated_frame(self):
        with pytest.raises(protocol.TESmartProtocolError):
            protocol.parse_current_input(bytes([0xAA, 0xBB, 0x03, 0x11, 0x00]))

    def test_input_byte_out_of_range(self):
        with pytest.raises(protocol.TESmartProtocolError):
            protocol.parse_current_input(bytes([0xAA, 0xBB, 0x03, 0x11, 0x20, 0x36]))


class TestNetworkConfig:
    def test_full_payload(self):
        payload = protocol.build_network_config(
            ip="10.0.4.50", port=5000, gateway="10.0.4.1", netmask="255.255.255.0"
        )
        assert payload == b"IP:10.0.4.50;PT:5000;GW:10.0.4.1;MA:255.255.255.0;"

    def test_single_field(self):
        assert protocol.build_network_config(ip="192.168.1.10") == b"IP:192.168.1.10;"

    def test_requires_at_least_one_field(self):
        with pytest.raises(ValueError):
            protocol.build_network_config()
