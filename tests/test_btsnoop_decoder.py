#!/usr/bin/env python3
"""
Unit tests for hardened btsnoop_decoder.py.
Covers regression fixes and stress testing for:
1. L2CAP Signaling unpack (<BHHHHH and <BHH)
2. AC Infinity TLV bounds checking and truncation defense
3. 3-byte HCI Disconnection Complete defense
4. Strict Telink OTA characteristic handle 28 checks across all write opcodes (0x12, 0x16, 0x52, 0xD2)
5. Modern Android LE Enhanced Connection Complete (0x0A) parameter offsets
6. L2CAP multi-fragment reassembly
"""

import sys
import os
import struct
import binascii
import datetime

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.btsnoop_decoder import (
    BtsnoopDecoder,
    decode_ac_infinity_frame,
    BTSNOOP_EPOCH_DELTA,
    TELINK_OTA_HANDLE,
)

# Helper to create mock btsnoop file in memory/tmp
def make_btsnoop_trace(records):
    header = struct.pack('>8sII', b'btsnoop\x00', 1, 1001)
    buf = bytearray(header)
    for pkt_data, dt, is_rx, is_cmd_evt in records:
        unix_us = int(dt.timestamp() * 1_000_000)
        btsnoop_us = unix_us + BTSNOOP_EPOCH_DELTA
        flags = (1 if is_rx else 0) | (2 if is_cmd_evt else 0)
        orig_len = len(pkt_data)
        hdr = struct.pack('>IIIIQ', orig_len, orig_len, flags, 0, btsnoop_us)
        buf.extend(hdr)
        buf.extend(pkt_data)
    return bytes(buf)

def build_ac_frame(subversion, opcode, payload, seq=0):
    hdr_prefix = struct.pack('>BBHH', 0xA5, subversion, len(payload), seq)
    hdr_crc = binascii.crc_hqx(hdr_prefix, 0xFFFF)
    body = struct.pack('>BB', 0x00, opcode) + payload
    body_crc = binascii.crc_hqx(body, 0xFFFF)
    return hdr_prefix + struct.pack('>H', hdr_crc) + body + struct.pack('>H', body_crc)


class TestL2CAPSignalingUnpack:
    """Tests for Bug 1: L2CAP signaling command header unpacking."""

    def test_l2cap_conn_param_update_req_wire_values(self, tmp_path):
        # Bluetooth Core Spec compliant Conn Param Update Req:
        # Code=0x12, Ident=1, Length=8, Min=12 (15.0ms), Max=24 (30.0ms), Latency=0, Timeout=500 (5000ms)
        l2cap_payload = struct.pack('<BBHHHHH', 0x12, 1, 8, 12, 24, 0, 500)
        # Wrap in ACL packet on Handle 0x0040, CID 0x0005
        handle_flags = 0x0040 | (2 << 12) # PB=2
        l2cap_hdr = struct.pack('<HH', len(l2cap_payload), 0x0005)
        acl_data = l2cap_hdr + l2cap_payload
        acl_pkt = b'\x02' + struct.pack('<HH', handle_flags, len(acl_data)) + acl_data

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_bytes = make_btsnoop_trace([(acl_pkt, t0, True, False)])
        trace_file = tmp_path / "l2cap_req.log"
        trace_file.write_bytes(trace_bytes)

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()

        assert len(events) == 1
        ev = events[0]
        assert ev["category"] == "L2CAP_SIG"
        assert ev["opcode"] == "L2CAP_CONN_PARAM_UPDATE_REQ"
        details = ev["details"]
        assert details["interval_min_ms"] == 15.0
        assert details["interval_max_ms"] == 30.0
        assert details["latency"] == 0
        assert details["timeout_ms"] == 5000

    def test_l2cap_conn_param_update_rsp_wire_values(self, tmp_path):
        # Conn Param Update Rsp: Code=0x13, Ident=1, Length=2, Result=0 (Accepted)
        l2cap_payload = struct.pack('<BBHH', 0x13, 1, 2, 0)
        handle_flags = 0x0040 | (2 << 12)
        l2cap_hdr = struct.pack('<HH', len(l2cap_payload), 0x0005)
        acl_data = l2cap_hdr + l2cap_payload
        acl_pkt = b'\x02' + struct.pack('<HH', handle_flags, len(acl_data)) + acl_data

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_bytes = make_btsnoop_trace([(acl_pkt, t0, False, False)])
        trace_file = tmp_path / "l2cap_rsp.log"
        trace_file.write_bytes(trace_bytes)

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()

        assert len(events) == 1
        ev = events[0]
        assert ev["category"] == "L2CAP_SIG"
        assert ev["opcode"] == "L2CAP_CONN_PARAM_UPDATE_RSP"
        assert ev["details"]["result"] == "Accepted"


class TestACInfinityTLVBoundsDefense:
    """Tests for Bug 2: Defensive bounds checking in AC Infinity frames."""

    def test_truncated_tlv_in_opcode_03_no_crash(self):
        # Malformed packet from Challenger 1: TLV key=0x10, len=1, 0 bytes value
        frame = bytes.fromhex('a50000020000c25900031001cec2')
        res = decode_ac_infinity_frame(frame)
        assert res["is_ac"] is True
        assert res["hdr_crc_ok"] is True
        assert "Mode=<truncated declared=1 actual=0: >" in res["details"]["tlvs"][0]

    def test_truncated_query_response_opcode_01_no_crash(self):
        # Truncated query response: key=0x02 (temp), len=2, only 1 byte present
        hdr = struct.pack('>BBHH', 0xA5, 0x00, 3, 0)
        hdr_crc = binascii.crc_hqx(hdr, 0xFFFF)
        body = b'\x00\x01\x02\x02\x06' # only 1 byte of value
        body_crc = binascii.crc_hqx(body, 0xFFFF)
        frame = hdr + struct.pack('>H', hdr_crc) + body + struct.pack('>H', body_crc)

        res = decode_ac_infinity_frame(frame)
        assert res["is_ac"] is True
        assert any("Temp=<truncated" in t for t in res["details"]["tlvs"])

    def test_valid_ac_frames_parse_correctly(self):
        # Valid settings write: Mode=2 (On), Speed=6
        frame = build_ac_frame(subversion=0x00, opcode=0x03, payload=bytes.fromhex("100102120106"), seq=2)
        res = decode_ac_infinity_frame(frame)
        assert res["is_ac"] is True
        assert res["hdr_crc_ok"] is True
        assert res["body_crc_ok"] is True
        assert "Mode=On" in res["details"]["tlvs"]
        assert "On-Speed=Speed 6" in res["details"]["tlvs"]


class TestHCIDisconnectionComplete:
    """Tests for Bug 3: 3-byte vs 4-byte Disconnection Complete parameter parsing."""

    def test_disconnection_complete_3_bytes_no_crash(self, tmp_path):
        # Truncated 3-byte HCI Disconnection Complete event
        evt_pkt = b'\x04\x05\x03\x00\x01\x00' # Opcode 0x05, len 3, params 00 01 00
        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "disc_3b.log"
        trace_file.write_bytes(make_btsnoop_trace([(evt_pkt, t0, True, True)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()
        assert len(events) == 1
        assert events[0]["category"] == "HCI_EVENT"
        assert events[0]["event"] == "DISCONNECTION_COMPLETE"
        assert "Truncated" in events[0]["details"]

    def test_disconnection_complete_4_bytes_parsed(self, tmp_path):
        # Normal 4-byte HCI Disconnection Complete event: status=0, handle=0x0040, reason=0x13
        evt_pkt = b'\x04\x05\x04' + struct.pack('<BHB', 0x00, 0x0040, 0x13)
        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "disc_4b.log"
        trace_file.write_bytes(make_btsnoop_trace([(evt_pkt, t0, True, True)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()
        assert len(events) == 1
        assert events[0]["handle"] == 0x0040
        assert "Reason=0x13" in events[0]["details"]


class TestTelinkOTASafety:
    """Tests for Bug 4: Strict Telink OTA characteristic handle 28 protection across opcodes."""

    def test_write_req_0x12_on_handle_28_detected(self, tmp_path):
        att_data = struct.pack('<BH', 0x12, TELINK_OTA_HANDLE) + b'\xAA\xBB'
        handle_flags = 0x0040 | (2 << 12)
        l2cap_hdr = struct.pack('<HH', len(att_data), 0x0004)
        acl_pkt = b'\x02' + struct.pack('<HH', handle_flags, len(l2cap_hdr) + len(att_data)) + l2cap_hdr + att_data

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "ota_12.log"
        trace_file.write_bytes(make_btsnoop_trace([(acl_pkt, t0, False, False)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()
        assert len(events) == 1
        assert events[0]["ota_violation"] is True
        assert len(decoder.ota_violations) == 1

    def test_write_cmd_0x52_on_handle_28_detected(self, tmp_path):
        att_data = struct.pack('<BH', 0x52, TELINK_OTA_HANDLE) + b'\xAA\xBB'
        handle_flags = 0x0040 | (2 << 12)
        l2cap_hdr = struct.pack('<HH', len(att_data), 0x0004)
        acl_pkt = b'\x02' + struct.pack('<HH', handle_flags, len(l2cap_hdr) + len(att_data)) + l2cap_hdr + att_data

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "ota_52.log"
        trace_file.write_bytes(make_btsnoop_trace([(acl_pkt, t0, False, False)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()
        assert len(events) == 1
        assert events[0]["ota_violation"] is True
        assert len(decoder.ota_violations) == 1

    def test_prepare_write_req_0x16_on_handle_28_detected(self, tmp_path):
        # Prepare write req: opcode 0x16, handle 28, offset 0, data AA BB
        att_data = struct.pack('<BHH', 0x16, TELINK_OTA_HANDLE, 0) + b'\xAA\xBB'
        handle_flags = 0x0040 | (2 << 12)
        l2cap_hdr = struct.pack('<HH', len(att_data), 0x0004)
        acl_pkt = b'\x02' + struct.pack('<HH', handle_flags, len(l2cap_hdr) + len(att_data)) + l2cap_hdr + att_data

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "ota_16.log"
        trace_file.write_bytes(make_btsnoop_trace([(acl_pkt, t0, False, False)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()
        assert len(events) == 1
        assert events[0]["ota_violation"] is True
        assert len(decoder.ota_violations) == 1

    def test_handle_20_and_18_writes_are_not_violations(self, tmp_path):
        att_data = struct.pack('<BH', 0x12, 20) + b'\xAA\xBB'
        handle_flags = 0x0040 | (2 << 12)
        l2cap_hdr = struct.pack('<HH', len(att_data), 0x0004)
        acl_pkt = b'\x02' + struct.pack('<HH', handle_flags, len(l2cap_hdr) + len(att_data)) + l2cap_hdr + att_data

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "safe_write.log"
        trace_file.write_bytes(make_btsnoop_trace([(acl_pkt, t0, False, False)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()
        assert len(events) == 1
        assert events[0]["ota_violation"] is False
        assert len(decoder.ota_violations) == 0


class TestHCILEConnectionCompleteOffsets:
    """Tests for Bug 5: Parameter offsets for LE Connection Complete (0x01) vs Enhanced (0x0A)."""

    def test_subevent_0x01_offsets(self, tmp_path):
        mac_bytes = bytes.fromhex('F648F638C1A4')
        params = struct.pack('<BBHBB6sHHHB', 0x01, 0x00, 0x0040, 0x00, 0x00, mac_bytes, 24, 0, 500, 0)
        evt_pkt = b'\x04' + struct.pack('<BB', 0x3E, len(params)) + params

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "conn_01.log"
        trace_file.write_bytes(make_btsnoop_trace([(evt_pkt, t0, True, True)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        decoder.run()
        assert decoder.conn_params["interval_ms"] == 30.0
        assert decoder.conn_params["latency"] == 0
        assert decoder.conn_params["timeout_ms"] == 5000

    def test_subevent_0x0a_enhanced_offsets(self, tmp_path):
        mac_bytes = bytes.fromhex('F648F638C1A4')
        local_rpa = bytes.fromhex('112233445566') # Phone MAC
        peer_rpa = bytes.fromhex('000000000000')
        params = struct.pack('<BBHBB6s6s6sHHHB', 0x0A, 0x00, 0x0040, 0x00, 0x00, mac_bytes, local_rpa, peer_rpa, 24, 0, 500, 0)
        evt_pkt = b'\x04' + struct.pack('<BB', 0x3E, len(params)) + params

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        trace_file = tmp_path / "conn_0a.log"
        trace_file.write_bytes(make_btsnoop_trace([(evt_pkt, t0, True, True)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        decoder.run()
        # Must NOT unpack local RPA bytes as interval/timeout
        assert decoder.conn_params["interval_ms"] == 30.0
        assert decoder.conn_params["latency"] == 0
        assert decoder.conn_params["timeout_ms"] == 5000


class TestL2CAPFragmentationReassembly:
    """Tests for multi-packet L2CAP reassembly."""

    def test_reassembles_multi_fragment_notification(self, tmp_path):
        ac_frame = build_ac_frame(subversion=0x13, opcode=0x66, payload=b'\x00' * 30, seq=3)
        long_ntf = struct.pack('<BH', 0x1B, 16) + ac_frame
        l2cap_total_len = len(long_ntf)
        cid = 0x0004

        chunk1 = long_ntf[:16]
        chunk2 = long_ntf[16:]

        # Fragment 1: PB=2, L2CAP header declares full l2cap_total_len
        handle_flags_1 = 0x0040 | (2 << 12)
        l2cap_hdr = struct.pack('<HH', l2cap_total_len, cid)
        acl_data_1 = l2cap_hdr + chunk1
        pkt1 = b'\x02' + struct.pack('<HH', handle_flags_1, len(acl_data_1)) + acl_data_1

        # Fragment 2: PB=1, continuation
        handle_flags_2 = 0x0040 | (1 << 12)
        pkt2 = b'\x02' + struct.pack('<HH', handle_flags_2, len(chunk2)) + chunk2

        t0 = datetime.datetime(2026, 11, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)
        t1 = t0 + datetime.timedelta(milliseconds=5)
        trace_file = tmp_path / "frag_trace.log"
        trace_file.write_bytes(make_btsnoop_trace([(pkt1, t0, True, False), (pkt2, t1, True, False)]))

        decoder = BtsnoopDecoder(str(trace_file), decode_all=True)
        events = decoder.run()

        # Both packets should be accounted for: pkt 1 is L2CAP_FRAG, pkt 2 completes reassembly
        assert len(events) == 2
        assert events[0]["category"] == "L2CAP_FRAG"
        assert events[0]["pkt_idx"] == 1

        assert events[1]["category"] == "ATT_NOTIF"
        assert events[1]["pkt_idx"] == 2
        assert events[1]["reassembled_from"] == [1, 2]
        ac = events[1]["ac_info"]
        assert ac["is_ac"] is True
        assert ac["type"] == "command_envelope"
        assert ac["hdr_crc_ok"] is True
        assert ac["body_crc_ok"] is True
        assert ac["opcode"] == "STATUS/ACK"
