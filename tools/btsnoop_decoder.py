#!/usr/bin/env python3
"""
proposed_btsnoop_decoder.py - Hardened Bluetooth HCI Snoop Log Decoder & Analyzer
Specialized for AC Infinity AirTap T6 (A4:C1:38:F6:48:F6) Reverse Engineering.

Hardened Features:
- Exact L2CAP Signaling unpack using <BHHHHH (Conn Param Update Req) and <BHH (Conn Param Update Rsp)
- Defensive TLV parsing in AC Infinity frames preventing IndexError and struct.error on truncated frames
- Robust 4-byte length check on HCI Disconnection Complete events
- Strict Telink OTA characteristic handle 28 checks across all write opcodes (0x12, 0x16, 0x52, 0xD2) with CLI warnings
- Correct LE Enhanced Connection Complete (0x0A) parameter offsets (24:30) vs LE Connection Complete (0x01, 12:18)
- Transparent L2CAP multi-fragment reassembly preserving individual fragment tracking and reassembled PDU verification
- Dual settle delay measurement: from CCCD write request (TX) and from CCCD write response ACK (RX)
"""

import sys
import os
import argparse
import struct
import binascii
import datetime
import json
import csv
from typing import Dict, List, Optional, Tuple, Any

BTSNOOP_EPOCH_DELTA = 0x00DCDDB30F2F8000
TELINK_OTA_HANDLE = 28
TELINK_OTA_UUID = "00010203-0405-0607-0809-0a0b0c0d2b12"

# ATT Opcodes
ATT_OPCODES = {
    0x01: "ATT_ERROR_RSP",
    0x02: "ATT_EXCHANGE_MTU_REQ",
    0x03: "ATT_EXCHANGE_MTU_RSP",
    0x04: "ATT_FIND_INFO_REQ",
    0x05: "ATT_FIND_INFO_RSP",
    0x06: "ATT_FIND_BY_TYPE_VALUE_REQ",
    0x07: "ATT_FIND_BY_TYPE_VALUE_RSP",
    0x08: "ATT_READ_BY_TYPE_REQ",
    0x09: "ATT_READ_BY_TYPE_RSP",
    0x0A: "ATT_READ_REQ",
    0x0B: "ATT_READ_RSP",
    0x0C: "ATT_READ_BLOB_REQ",
    0x0D: "ATT_READ_BLOB_RSP",
    0x0E: "ATT_READ_MULTIPLE_REQ",
    0x0F: "ATT_READ_MULTIPLE_RSP",
    0x10: "ATT_READ_BY_GROUP_TYPE_REQ",
    0x11: "ATT_READ_BY_GROUP_TYPE_RSP",
    0x12: "ATT_WRITE_REQ",
    0x13: "ATT_WRITE_RSP",
    0x16: "ATT_PREPARE_WRITE_REQ",
    0x17: "ATT_PREPARE_WRITE_RSP",
    0x18: "ATT_EXECUTE_WRITE_REQ",
    0x19: "ATT_EXECUTE_WRITE_RSP",
    0x1B: "ATT_HANDLE_VALUE_NTF",
    0x1D: "ATT_HANDLE_VALUE_IND",
    0x1E: "ATT_HANDLE_VALUE_CFM",
    0x52: "ATT_WRITE_CMD",
    0xD2: "ATT_SIGNED_WRITE_CMD",
}

# SMP Opcodes
SMP_OPCODES = {
    0x01: "SMP_PAIRING_REQ",
    0x02: "SMP_PAIRING_RSP",
    0x03: "SMP_PAIRING_CONFIRM",
    0x04: "SMP_PAIRING_RANDOM",
    0x05: "SMP_PAIRING_FAILED",
    0x06: "SMP_ENCRYPTION_INFO",
    0x07: "SMP_MASTER_IDENTIFICATION",
    0x08: "SMP_IDENTITY_INFO",
    0x09: "SMP_IDENTITY_ADDR_INFO",
    0x0A: "SMP_SIGNING_INFO",
    0x0B: "SMP_SECURITY_REQ",
    0x0C: "SMP_PAIRING_PUBLIC_KEY",
    0x0D: "SMP_PAIRING_DHKEY_CHECK",
    0x0E: "SMP_PAIRING_KEYPRESS_NTF",
}

# L2CAP Signaling Opcodes
L2CAP_SIGNALING_OPCODES = {
    0x01: "L2CAP_CMD_REJ",
    0x12: "L2CAP_CONN_PARAM_UPDATE_REQ",
    0x13: "L2CAP_CONN_PARAM_UPDATE_RSP",
}

def decode_ac_infinity_frame(raw_bytes: bytes) -> Dict[str, Any]:
    """Decodes AC Infinity application protocol envelope and TLVs with defensive bounds checks."""
    if len(raw_bytes) < 12:
        return {"is_ac": False, "raw_hex": raw_bytes.hex()}
    
    if raw_bytes[0] != 0xA5:
        if raw_bytes[:2] == b'\x1E\xFF':
            return {
                "is_ac": True,
                "type": "telemetry_push",
                "prefix": "1E FF",
                "raw_hex": raw_bytes.hex()
            }
        return {"is_ac": False, "raw_hex": raw_bytes.hex()}

    subversion = raw_bytes[1]
    payload_len, seq = struct.unpack('>HH', raw_bytes[2:6])
    hdr_crc = struct.unpack('>H', raw_bytes[6:8])[0]
    expected_hdr_crc = binascii.crc_hqx(raw_bytes[:6], 0xFFFF)
    hdr_crc_ok = (hdr_crc == expected_hdr_crc)

    if len(raw_bytes) < 12 + payload_len:
        return {
            "is_ac": True,
            "type": "truncated",
            "subversion": f"0x{subversion:02X}",
            "seq": seq,
            "hdr_crc_ok": hdr_crc_ok,
            "payload_len_declared": payload_len,
            "actual_len": len(raw_bytes),
            "raw_hex": raw_bytes.hex()
        }

    body = raw_bytes[8:10 + payload_len]
    body_crc = struct.unpack('>H', raw_bytes[10 + payload_len : 12 + payload_len])[0]
    expected_body_crc = binascii.crc_hqx(body, 0xFFFF)
    body_crc_ok = (body_crc == expected_body_crc)

    opcode = body[1] if len(body) >= 2 else None
    opcode_name = {0x01: "GET", 0x03: "SET", 0x66: "STATUS/ACK"}.get(opcode, f"0x{opcode:02X}" if opcode is not None else "NONE")

    details: Dict[str, Any] = {}
    payload_bytes = body[2:]

    if opcode == 0x03:
        if payload_bytes.startswith(b'\x01\x07') and len(payload_bytes) >= 9:
            yy, mm, dd, wday, hh, mi, ss = struct.unpack('>BBBBBBB', payload_bytes[2:9])
            details["feature"] = "Clock Sync (Calendar)"
            details["date"] = f"20{yy:02d}-{mm:02d}-{dd:02d} (ISO wday {wday}) {hh:02d}:{mi:02d}:{ss:02d}"
        elif payload_bytes == b'\x01\x00':
            details["feature"] = "Clock Sync ACK"
            details["status"] = "Success (Result 00)"
        elif payload_bytes == b'\x01\x01':
            details["feature"] = "Clock Sync NACK"
            details["status"] = "Rejected / Error (Result 01)"
        else:
            details["feature"] = "Control Settings Write"
            tlvs = []
            idx = 0
            n = len(payload_bytes)
            while idx < n:
                if idx + 2 > n:
                    tlvs.append(f"<trailing: {payload_bytes[idx:].hex()}>")
                    break
                k = payload_bytes[idx]
                l = payload_bytes[idx+1]
                if idx + 2 + l > n:
                    v_trunc = payload_bytes[idx+2:]
                    k_name = {0x10: "Mode", 0x11: "Off-Speed", 0x12: "On-Speed", 0x20: "Temp-Unit", 0x21: "Display"}.get(k, f"Key 0x{k:02X}")
                    tlvs.append(f"{k_name}=<truncated declared={l} actual={len(v_trunc)}: {v_trunc.hex()}>")
                    break
                v = payload_bytes[idx+2 : idx+2+l]
                idx += 2 + l
                k_name = {0x10: "Mode", 0x11: "Off-Speed", 0x12: "On-Speed", 0x20: "Temp-Unit", 0x21: "Display"}.get(k, f"Key 0x{k:02X}")
                val_repr = v.hex()
                if k == 0x10 and l == 1 and len(v) >= 1:
                    val_repr = {1: "Off", 2: "On", 3: "Auto"}.get(v[0], str(v[0]))
                elif k in (0x11, 0x12) and l == 1 and len(v) >= 1:
                    val_repr = f"Speed {v[0]}"
                tlvs.append(f"{k_name}={val_repr}")
            details["tlvs"] = tlvs
    elif opcode == 0x01:
        if len(payload_bytes) in (2, 4) and all(b in (0x10, 0x02, 0x03, 0x11, 0x12, 0x20) for b in payload_bytes):
            key_names = [{0x10: "Mode", 0x02: "Temp", 0x03: "Speed", 0x11: "OffSpd", 0x12: "OnSpd", 0x20: "Unit"}.get(k, f"0x{k:02X}") for k in payload_bytes]
            details["feature"] = "Parameter Query (GET)"
            details["keys_queried"] = key_names
        else:
            details["feature"] = "Query Response"
            tlvs = []
            idx = 0
            n = len(payload_bytes)
            while idx < n:
                if idx + 2 > n:
                    tlvs.append(f"<trailing: {payload_bytes[idx:].hex()}>")
                    break
                k = payload_bytes[idx]
                l = payload_bytes[idx+1]
                if idx + 2 + l > n:
                    v_trunc = payload_bytes[idx+2:]
                    k_name = {0x10: "Mode", 0x02: "Temp", 0x03: "Speed", 0x11: "OffSpd", 0x12: "OnSpd", 0x20: "Unit"}.get(k, f"0x{k:02X}")
                    tlvs.append(f"{k_name}=<truncated declared={l} actual={len(v_trunc)}: {v_trunc.hex()}>")
                    break
                v = payload_bytes[idx+2 : idx+2+l]
                idx += 2 + l
                k_name = {0x10: "Mode", 0x02: "Temp", 0x03: "Speed", 0x11: "OffSpd", 0x12: "OnSpd", 0x20: "Unit"}.get(k, f"0x{k:02X}")
                val_str = v.hex()
                if k == 0x10 and l == 1 and len(v) >= 1:
                    val_str = {1: "Off", 2: "On", 3: "Auto"}.get(v[0], str(v[0]))
                elif k in (0x03, 0x11, 0x12) and l == 1 and len(v) >= 1:
                    val_str = f"Speed {v[0]}"
                elif k == 0x02 and l == 2 and len(v) >= 2:
                    raw_t = struct.unpack('>h', v)[0]
                    val_str = f"{raw_t / 100.0:.2f} C"
                tlvs.append(f"{k_name}={val_str}")
            details["tlvs"] = tlvs

    return {
        "is_ac": True,
        "type": "command_envelope",
        "subversion": f"0x{subversion:02X}",
        "seq": seq,
        "hdr_crc_ok": hdr_crc_ok,
        "body_crc_ok": body_crc_ok,
        "opcode": opcode_name,
        "details": details,
        "raw_hex": raw_bytes.hex()
    }

class BtsnoopDecoder:
    def __init__(self, filename: str, target_mac: str = "A4:C1:38:F6:48:F6", filter_handle: Optional[int] = None, decode_all: bool = False):
        self.filename = filename
        self.target_mac = target_mac.upper().strip()
        self.filter_handle = filter_handle
        self.decode_all = decode_all
        
        self.handle_to_mac: Dict[int, str] = {}
        self.reassembly: Dict[Tuple[int, bool], Dict[str, Any]] = {}
        
        self.t0: Optional[float] = None
        self.prev_ts: Optional[float] = None
        self.events: List[Dict[str, Any]] = []
        
        # Protocol Metrics
        self.client_mtu: Optional[int] = None
        self.server_mtu: Optional[int] = None
        self.negotiated_mtu: Optional[int] = None
        
        self.smp_pairing_requested: bool = False
        self.smp_pairing_responded: bool = False
        self.smp_pairing_success: bool = False
        
        self.conn_params: Optional[Dict[str, Any]] = None
        self.cccd_subscribed: bool = False
        self.cccd_sub_req_ts: Optional[float] = None
        self.cccd_sub_ack_ts: Optional[float] = None
        
        self.first_control_write_ts: Optional[float] = None
        self.settle_delay_ms: Optional[float] = None
        self.settle_delay_from_ack_ms: Optional[float] = None
        
        self.att_writes: List[Dict[str, Any]] = []
        self.att_notifications: List[Dict[str, Any]] = []
        self.ota_violations: List[Dict[str, Any]] = []

    def run(self) -> List[Dict[str, Any]]:
        with open(self.filename, 'rb') as f:
            magic, ver, link = struct.unpack('>8sII', f.read(16))
            if magic != b'btsnoop\x00':
                raise ValueError(f"Invalid file format: magic={magic}")

            pkt_idx = 0
            while True:
                rec_hdr = f.read(24)
                if len(rec_hdr) < 24:
                    break
                pkt_idx += 1
                orig_len, inc_len, flags, drops, btsnoop_us = struct.unpack('>IIIIQ', rec_hdr)
                pkt = f.read(inc_len)

                unix_us = btsnoop_us - BTSNOOP_EPOCH_DELTA
                ts = unix_us / 1_000_000.0
                if self.t0 is None:
                    self.t0 = ts
                
                elapsed_ms = (ts - self.t0) * 1000.0
                delta_ms = (ts - self.prev_ts) * 1000.0 if self.prev_ts is not None else 0.0
                self.prev_ts = ts
                
                dt_utc = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
                is_rx = bool(flags & 1)
                direction = "RX (Phone <- Vent)" if is_rx else "TX (Phone -> Vent)"

                if len(pkt) < 1:
                    continue
                h4_type = pkt[0]

                # 1. HCI Event
                if h4_type == 0x04 and len(pkt) >= 3:
                    evt_code, plen = struct.unpack('<BB', pkt[1:3])
                    params = pkt[3:3+plen]
                    if evt_code == 0x3E and len(params) >= 1: # LE Meta Event
                        subevt = params[0]
                        if subevt in (0x01, 0x0A): # LE Connection Complete / Enhanced Connection Complete
                            if len(params) >= 12:
                                status, ch, role, pat = struct.unpack('<BHBB', params[1:6])
                                mac_bytes = params[6:12]
                                mac_str = ":".join(f"{b:02X}" for b in reversed(mac_bytes))
                                self.handle_to_mac[ch] = mac_str
                                
                                # Sub-event 0x01 has Conn_Interval at offset 12..18
                                # Sub-event 0x0A (Enhanced) has Local RPA (6B) + Peer RPA (6B) at 12..24, Conn_Interval at 24..30
                                if subevt == 0x01 and len(params) >= 18:
                                    interval, latency, timeout = struct.unpack('<HHH', params[12:18])
                                    self.conn_params = {
                                        "interval_ms": interval * 1.25,
                                        "latency": latency,
                                        "timeout_ms": timeout * 10
                                    }
                                elif subevt == 0x0A and len(params) >= 30:
                                    interval, latency, timeout = struct.unpack('<HHH', params[24:30])
                                    self.conn_params = {
                                        "interval_ms": interval * 1.25,
                                        "latency": latency,
                                        "timeout_ms": timeout * 10
                                    }
                                
                                ev = {
                                    "pkt_idx": pkt_idx, "ts": ts, "utc": dt_utc,
                                    "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                                    "category": "HCI_EVENT",
                                    "event": "LE_ENHANCED_CONNECTION_COMPLETE" if subevt == 0x0A else "LE_CONNECTION_COMPLETE",
                                    "handle": ch, "bd_addr": mac_str,
                                    "details": f"Role={'Central' if role==0 else 'Peripheral'}, Address={mac_str}, Handle=0x{ch:04X}"
                                }
                                self.events.append(ev)
                        elif subevt == 0x03: # LE Connection Update Complete
                            if len(params) >= 10:
                                status, ch, interval, latency, to = struct.unpack('<BHHHH', params[1:10])
                                self.conn_params = {
                                    "interval_ms": interval * 1.25,
                                    "latency": latency,
                                    "timeout_ms": to * 10
                                }
                                ev = {
                                    "pkt_idx": pkt_idx, "ts": ts, "utc": dt_utc,
                                    "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                                    "category": "HCI_EVENT", "event": "LE_CONN_UPDATE_COMPLETE",
                                    "handle": ch,
                                    "details": f"Interval={interval*1.25}ms, Latency={latency}, Timeout={to*10}ms"
                                }
                                self.events.append(ev)
                    elif evt_code == 0x05: # Disconnection Complete
                        if len(params) >= 4:
                            status, ch, reason = struct.unpack('<BHB', params[:4])
                            details_str = f"Handle=0x{ch:04X}, Status=0x{status:02X}, Reason=0x{reason:02X}"
                        else:
                            ch = None
                            details_str = f"Truncated Disconnection Complete ({len(params)} bytes: {params.hex()})"
                        ev = {
                            "pkt_idx": pkt_idx, "ts": ts, "utc": dt_utc,
                            "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                            "category": "HCI_EVENT", "event": "DISCONNECTION_COMPLETE",
                            "handle": ch, "details": details_str
                        }
                        self.events.append(ev)

                # 2. HCI ACL Data
                elif h4_type == 0x02 and len(pkt) >= 5:
                    hflags, acl_len = struct.unpack('<HH', pkt[1:5])
                    conn_h = hflags & 0x0FFF
                    pb = (hflags >> 12) & 0x03
                    payload = pkt[5:5+acl_len]
                    key = (conn_h, is_rx)

                    # Check MAC filtering
                    peer_mac = self.handle_to_mac.get(conn_h)
                    if not self.decode_all:
                        if self.filter_handle is not None and conn_h != self.filter_handle:
                            continue
                        if self.filter_handle is None and peer_mac is not None and peer_mac != self.target_mac:
                            continue

                    # L2CAP Reassembly
                    if pb in (0, 2): # First fragment
                        if len(payload) < 4:
                            continue
                        l2cap_len, cid = struct.unpack('<HH', payload[:4])
                        data_part = payload[4:]
                        self.reassembly[key] = {
                            'cid': cid,
                            'total': l2cap_len,
                            'bytes': bytearray(data_part),
                            'first_ts': ts,
                            'first_elapsed_ms': elapsed_ms,
                            'first_delta_ms': delta_ms,
                            'first_pkt_idx': pkt_idx,
                            'fragments': [pkt_idx]
                        }
                        # If first fragment does not complete PDU, record fragment event
                        if len(data_part) < l2cap_len:
                            ev = {
                                "pkt_idx": pkt_idx, "ts": ts, "utc": dt_utc,
                                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                                "category": "L2CAP_FRAG", "direction": direction,
                                "handle": conn_h, "cid": cid,
                                "summary": f"L2CAP First Fragment: {len(data_part)}/{l2cap_len} bytes (CID 0x{cid:04X})"
                            }
                            self.events.append(ev)
                    elif pb == 1: # Continuation fragment
                        if key in self.reassembly:
                            self.reassembly[key]['bytes'].extend(payload)
                            self.reassembly[key]['fragments'].append(pkt_idx)
                            # If continuation does not finish PDU, record fragment event
                            if len(self.reassembly[key]['bytes']) < self.reassembly[key]['total']:
                                ev = {
                                    "pkt_idx": pkt_idx, "ts": ts, "utc": dt_utc,
                                    "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                                    "category": "L2CAP_FRAG", "direction": direction,
                                    "handle": conn_h, "cid": self.reassembly[key]['cid'],
                                    "summary": f"L2CAP Continuation Fragment: {len(payload)} bytes ({len(self.reassembly[key]['bytes'])}/{self.reassembly[key]['total']} assembled)"
                                }
                                self.events.append(ev)

                    if key in self.reassembly:
                        ctx = self.reassembly[key]
                        if len(ctx['bytes']) >= ctx['total']:
                            l2cap_data = bytes(ctx['bytes'][:ctx['total']])
                            cid = ctx['cid']
                            c_elapsed_ms = ctx['first_elapsed_ms']
                            c_delta_ms = ctx['first_delta_ms']
                            frag_list = list(ctx['fragments'])
                            del self.reassembly[key]

                            self._dissect_l2cap(
                                pkt_idx, ts, dt_utc, c_elapsed_ms, c_delta_ms, conn_h, is_rx, direction, cid, l2cap_data,
                                reassembled_from=frag_list if len(frag_list) > 1 else None
                            )

        return self.events

    def _dissect_l2cap(self, pkt_idx: int, ts: float, utc: str, elapsed_ms: float, delta_ms: float,
                       conn_h: int, is_rx: bool, direction: str, cid: int, data: bytes,
                       reassembled_from: Optional[List[int]] = None):
        if cid == 0x0004: # ATT Protocol
            self._dissect_att(pkt_idx, ts, utc, elapsed_ms, delta_ms, conn_h, is_rx, direction, data, reassembled_from=reassembled_from)
        elif cid == 0x0005: # L2CAP LE Signaling
            self._dissect_l2cap_signaling(pkt_idx, ts, utc, elapsed_ms, delta_ms, conn_h, is_rx, direction, data)
        elif cid == 0x0006: # SMP Protocol
            self._dissect_smp(pkt_idx, ts, utc, elapsed_ms, delta_ms, conn_h, is_rx, direction, data)

    def _dissect_att(self, pkt_idx: int, ts: float, utc: str, elapsed_ms: float, delta_ms: float,
                     conn_h: int, is_rx: bool, direction: str, data: bytes,
                     reassembled_from: Optional[List[int]] = None):
        if not data:
            return
        opcode = data[0]
        op_name = ATT_OPCODES.get(opcode, f"ATT 0x{opcode:02X}")

        # 1. Exchange MTU
        if opcode == 0x02 and len(data) >= 3: # MTU Req
            self.client_mtu = struct.unpack('<H', data[1:3])[0]
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT", "opcode": op_name, "direction": direction,
                "client_rx_mtu": self.client_mtu,
                "summary": f"Exchange MTU Request: Client Rx MTU = {self.client_mtu}"
            }
            self.events.append(ev)
        elif opcode == 0x03 and len(data) >= 3: # MTU Rsp
            self.server_mtu = struct.unpack('<H', data[1:3])[0]
            if self.client_mtu is not None:
                self.negotiated_mtu = min(self.client_mtu, self.server_mtu)
            else:
                self.negotiated_mtu = self.server_mtu
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT", "opcode": op_name, "direction": direction,
                "server_rx_mtu": self.server_mtu,
                "negotiated_mtu": self.negotiated_mtu,
                "summary": f"Exchange MTU Response: Server Rx MTU = {self.server_mtu} -> Negotiated MTU = {self.negotiated_mtu}"
            }
            self.events.append(ev)

        # 2. Write Request (0x12) / Write Command (0x52) / Signed Write (0xD2) / Prepare Write (0x16)
        elif opcode in (0x12, 0x52, 0xD2) and len(data) >= 3:
            handle = struct.unpack('<H', data[1:3])[0]
            val = data[3:]
            w_type = {
                0x12: "Write Request (Acked)",
                0x52: "Write Command (Without Response)",
                0xD2: "Signed Write Command"
            }.get(opcode, "Write")
            
            # Check CCCD subscription (Handle 18)
            if handle == 18:
                if val == b'\x01\x00':
                    self.cccd_subscribed = True
                    self.cccd_sub_req_ts = ts
            
            # Check Handle 20 writes (Application commands)
            if handle == 20:
                if self.first_control_write_ts is None:
                    self.first_control_write_ts = ts
                    if self.cccd_sub_req_ts is not None:
                        self.settle_delay_ms = (ts - self.cccd_sub_req_ts) * 1000.0
                    if self.cccd_sub_ack_ts is not None:
                        self.settle_delay_from_ack_ms = (ts - self.cccd_sub_ack_ts) * 1000.0

            # Strict Telink OTA check
            ota_violation = (handle == TELINK_OTA_HANDLE)

            ac_info = decode_ac_infinity_frame(val)
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_WRITE", "opcode": op_name, "direction": direction,
                "handle": handle, "write_type": w_type, "length": len(val),
                "payload_hex": val.hex(), "ac_info": ac_info,
                "ota_violation": ota_violation,
                "summary": f"{w_type} on Handle {handle} ({len(val)} bytes)"
            }
            if ota_violation:
                self.ota_violations.append(ev)
            self.att_writes.append(ev)
            self.events.append(ev)

        # 2b. Prepare Write Request (0x16) - explicitly covered for OTA safety
        elif opcode == 0x16 and len(data) >= 5:
            handle, offset = struct.unpack('<HH', data[1:5])
            val = data[5:]
            ota_violation = (handle == TELINK_OTA_HANDLE)
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_WRITE", "opcode": op_name, "direction": direction,
                "handle": handle, "offset": offset, "write_type": "Prepare Write Request",
                "length": len(val), "payload_hex": val.hex(),
                "ota_violation": ota_violation,
                "summary": f"Prepare Write Request on Handle {handle} (offset {offset}, {len(val)} bytes)"
            }
            if ota_violation:
                self.ota_violations.append(ev)
            self.att_writes.append(ev)
            self.events.append(ev)

        # 3. Write Response (ACK)
        elif opcode == 0x13:
            if self.cccd_sub_req_ts is not None and self.cccd_sub_ack_ts is None:
                self.cccd_sub_ack_ts = ts
                if self.first_control_write_ts is not None:
                    self.settle_delay_from_ack_ms = (self.first_control_write_ts - self.cccd_sub_ack_ts) * 1000.0

            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_ACK", "opcode": op_name, "direction": direction,
                "summary": "ATT Write Response (GATT Layer ACK)"
            }
            self.events.append(ev)

        # 4. Handle Value Notification / Indication
        elif opcode in (0x1B, 0x1D) and len(data) >= 3:
            handle = struct.unpack('<H', data[1:3])[0]
            val = data[3:]
            n_type = "Notification" if opcode == 0x1B else "Indication"
            ac_info = decode_ac_infinity_frame(val)
            reassembly_note = f" [Reassembled from Pkts {reassembled_from}]" if reassembled_from else ""
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_NOTIF", "opcode": op_name, "direction": direction,
                "handle": handle, "notif_type": n_type, "length": len(val),
                "payload_hex": val.hex(), "ac_info": ac_info,
                "reassembled_from": reassembled_from,
                "summary": f"{n_type} on Handle {handle} ({len(val)} bytes){reassembly_note}"
            }
            self.att_notifications.append(ev)
            self.events.append(ev)

        # 5. Read Request / Response
        elif opcode == 0x0A and len(data) >= 3:
            handle = struct.unpack('<H', data[1:3])[0]
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_READ", "opcode": op_name, "direction": direction,
                "handle": handle, "summary": f"Read Request on Handle {handle}"
            }
            self.events.append(ev)
        elif opcode == 0x0B:
            val = data[1:]
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_READ", "opcode": op_name, "direction": direction,
                "value_hex": val.hex(), "value_ascii": val.decode('ascii', errors='replace'),
                "summary": f"Read Response ({len(val)} bytes): {val.decode('ascii', errors='replace')}"
            }
            self.events.append(ev)

        # 6. Error Response
        elif opcode == 0x01 and len(data) >= 5:
            req_op, req_h, err_c = struct.unpack('<BBH', data[1:5])
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_ERROR", "opcode": op_name, "direction": direction,
                "req_opcode": req_op, "handle": req_h, "error_code": err_c,
                "summary": f"ATT Error Response: Op 0x{req_op:02X}, Handle {req_h}, Error 0x{err_c:02X}"
            }
            self.events.append(ev)
        else:
            ev = {
                "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
                "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
                "category": "ATT_OTHER", "opcode": op_name, "direction": direction,
                "raw_hex": data[1:].hex(), "summary": f"{op_name}: {data[1:].hex()}"
            }
            self.events.append(ev)

    def _dissect_smp(self, pkt_idx: int, ts: float, utc: str, elapsed_ms: float, delta_ms: float,
                     conn_h: int, is_rx: bool, direction: str, data: bytes):
        if not data:
            return
        code = data[0]
        code_name = SMP_OPCODES.get(code, f"SMP 0x{code:02X}")
        details: Dict[str, Any] = {}

        if code == 0x01 and len(data) >= 7: # Pairing Request
            self.smp_pairing_requested = True
            io_cap, oob, auth_req, max_k, init_k, resp_k = struct.unpack('<BBBBBB', data[1:7])
            details = {"io_cap": io_cap, "oob": oob, "auth_req": auth_req, "max_key_size": max_k}
        elif code == 0x02 and len(data) >= 7: # Pairing Response
            self.smp_pairing_responded = True
            self.smp_pairing_success = True
            io_cap, oob, auth_req, max_k, init_k, resp_k = struct.unpack('<BBBBBB', data[1:7])
            details = {"io_cap": io_cap, "oob": oob, "auth_req": auth_req, "max_key_size": max_k}
        elif code == 0x05 and len(data) >= 2: # Pairing Failed
            self.smp_pairing_success = False
            details = {"reason": data[1]}

        ev = {
            "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
            "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
            "category": "SMP", "opcode": code_name, "direction": direction,
            "details": details, "raw_hex": data[1:].hex(),
            "summary": f"{code_name}: {details if details else data[1:].hex()}"
        }
        self.events.append(ev)

    def _dissect_l2cap_signaling(self, pkt_idx: int, ts: float, utc: str, elapsed_ms: float, delta_ms: float,
                                 conn_h: int, is_rx: bool, direction: str, data: bytes):
        if not data:
            return
        code = data[0]
        code_name = L2CAP_SIGNALING_OPCODES.get(code, f"L2CAP Sig 0x{code:02X}")
        details: Dict[str, Any] = {}

        # Bluetooth Core Specification Vol 3, Part A, Sec 4.1:
        # Signaling header: Code(1), Ident(1), Length(2)
        if code == 0x12 and len(data) >= 12: # Conn Param Update Req
            ident, length, min_i, max_i, lat, to = struct.unpack('<BHHHHH', data[1:12])
            details = {
                "interval_min_ms": min_i * 1.25,
                "interval_max_ms": max_i * 1.25,
                "latency": lat,
                "timeout_ms": to * 10
            }
        elif code == 0x13 and len(data) >= 6: # Conn Param Update Rsp
            ident, length, res = struct.unpack('<BHH', data[1:6])
            details = {"result": "Accepted" if res == 0 else f"Rejected ({res})"}

        ev = {
            "pkt_idx": pkt_idx, "ts": ts, "utc": utc,
            "elapsed_ms": elapsed_ms, "delta_ms": delta_ms,
            "category": "L2CAP_SIG", "opcode": code_name, "direction": direction,
            "details": details, "raw_hex": data[1:].hex(),
            "summary": f"{code_name}: {details if details else data[1:].hex()}"
        }
        self.events.append(ev)

    def print_timeline(self):
        print("=" * 110)
        print(f"  AC INFINITY AIRTAP T6 (MAC: {self.target_mac}) BLE SNOOP DECODER")
        print("=" * 110)
        print(f"{'Time (+Delta)':<22} | {'Direction':<18} | {'Layer / Transaction':<30} | {'Details / Payload'}")
        print("-" * 110)
        
        for ev in self.events:
            t_str = f"+{ev['elapsed_ms']:7.1f}ms (+{ev['delta_ms']:5.1f}ms)"
            dir_str = ev.get('direction', 'LOCAL')
            cat = ev.get('category', '')
            summary = ev.get('summary', '')

            # Check and alert for OTA violations prominently
            if ev.get('ota_violation'):
                print(f"*** [CRITICAL ALERT: FORBIDDEN WRITE TO TELINK OTA HANDLE {TELINK_OTA_HANDLE} DETECTED] ***")

            print(f"{t_str:<22} | {dir_str:<18} | {ev.get('opcode', cat):<30} | {summary}")
            
            # Print AC Infinity details if present
            ac_info = ev.get('ac_info')
            if ac_info and ac_info.get('is_ac'):
                if ac_info['type'] == 'command_envelope':
                    det = ac_info.get('details', {})
                    crc_status = f"Hdr={'OK' if ac_info['hdr_crc_ok'] else 'FAIL'}, Body={'OK' if ac_info['body_crc_ok'] else 'FAIL'}"
                    print(f"{'':<22} | {'':<18} |   └── [AC Protocol]          | Seq={ac_info['seq']} Op={ac_info['opcode']} CRC({crc_status})")
                    if det:
                        feat = det.get('feature', '')
                        tlvs = det.get('tlvs', [])
                        q_keys = det.get('keys_queried', [])
                        date_str = det.get('date', '')
                        stat = det.get('status', '')
                        desc = f"{feat}: " + (f"Date={date_str} " if date_str else "") + (f"Status={stat} " if stat else "") + (f"Keys={q_keys} " if q_keys else "") + (f"TLVs={tlvs} " if tlvs else "")
                        print(f"{'':<22} | {'':<18} |                              |   └── {desc}")
                elif ac_info['type'] == 'telemetry_push':
                    print(f"{'':<22} | {'':<18} |   └── [Telemetry Push]       | Raw={ac_info['raw_hex']}")

        print("-" * 110)
        print("PROTOCOL METRICS & EXECUTIVE SUMMARY:")
        print(f"  • Final Negotiated MTU:        {self.negotiated_mtu} (Client requested: {self.client_mtu}, Server reported: {self.server_mtu})")
        print(f"  • SMP Pairing / Security:      {'Exchanged & Successful' if self.smp_pairing_success else ('Requested' if self.smp_pairing_requested else 'None (Unbonded / No SMP)')}")
        print(f"  • Connection Parameters:       {self.conn_params if self.conn_params else 'Default'}")
        print(f"  • CCCD Subscribed (Handle 18): {'YES (0x0100)' if self.cccd_subscribed else 'NO'}")
        settle_str = f"{self.settle_delay_ms:.1f} ms" if self.settle_delay_ms else "N/A"
        settle_ack_str = f" ({self.settle_delay_from_ack_ms:.1f} ms post-ACK)" if self.settle_delay_from_ack_ms else ""
        print(f"  • Telink Post-Subscribe Settle:{settle_str}{settle_ack_str}")
        print(f"  • Total ATT Writes:            {len(self.att_writes)}")
        print(f"  • Total ATT Notifications:     {len(self.att_notifications)}")
        if len(self.ota_violations) == 0:
            print(f"  • Telink OTA Handle 28 Safety:  PASSED (0 writes detected to OTA handle {TELINK_OTA_HANDLE})")
        else:
            print(f"  • Telink OTA Handle 28 Safety:  CRITICAL VIOLATION ({len(self.ota_violations)} writes detected to handle {TELINK_OTA_HANDLE}!)")
        print("=" * 110)

def convert_btsnoop_to_pcap(btsnoop_path: str, pcap_path: str) -> int:
    with open(btsnoop_path, "rb") as fin, open(pcap_path, "wb") as fout:
        magic, ver, link = struct.unpack(">8sII", fin.read(16))
        # DLT 201: LINKTYPE_BLUETOOTH_HCI_H4_WITH_PHDR
        fout.write(struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 201))
        
        count = 0
        while True:
            rec_hdr = fin.read(24)
            if len(rec_hdr) < 24:
                break
            orig_len, inc_len, flags, drops, btsnoop_us = struct.unpack(">IIIIQ", rec_hdr)
            pkt_data = fin.read(inc_len)
            
            unix_us = btsnoop_us - BTSNOOP_EPOCH_DELTA
            ts_sec = int(unix_us // 1_000_000)
            ts_usec = int(unix_us % 1_000_000)
            
            is_rx = 1 if (flags & 1) else 0
            phdr = struct.pack(">I", is_rx)
            
            fout.write(struct.pack("<IIII", ts_sec, ts_usec, len(pkt_data) + 4, orig_len + 4))
            fout.write(phdr)
            fout.write(pkt_data)
            count += 1
        return count

def main():
    parser = argparse.ArgumentParser(description="AC Infinity AirTap T6 Hardened BTSnoop Decoder")
    parser.add_argument("snoop_log", help="Path to btsnoop_hci.log")
    parser.add_argument("--mac", default="A4:C1:38:F6:48:F6", help="Target Bluetooth BD_ADDR (default: A4:C1:38:F6:48:F6)")
    parser.add_argument("--handle", type=lambda x: int(x, 0), default=None, help="Filter by specific HCI connection handle (e.g. 0x0040)")
    parser.add_argument("--all", action="store_true", help="Decode all packets regardless of MAC")
    parser.add_argument("--json", help="Path to export structured event timeline in JSON")
    parser.add_argument("--csv", help="Path to export ATT writes and notifications in CSV")
    parser.add_argument("--pcap", help="Convert input btsnoop to standard PCAP format (DLT 201)")
    
    args = parser.parse_args()

    if not os.path.exists(args.snoop_log):
        print(f"Error: file not found: {args.snoop_log}", file=sys.stderr)
        sys.exit(1)

    if args.pcap:
        count = convert_btsnoop_to_pcap(args.snoop_log, args.pcap)
        print(f"Successfully converted {count} packets to PCAP: {args.pcap}")

    decoder = BtsnoopDecoder(args.snoop_log, target_mac=args.mac, filter_handle=args.handle, decode_all=args.all)
    events = decoder.run()
    decoder.print_timeline()

    if args.json:
        with open(args.json, 'w') as f:
            json.dump({
                "target_mac": decoder.target_mac,
                "negotiated_mtu": decoder.negotiated_mtu,
                "client_mtu": decoder.client_mtu,
                "server_mtu": decoder.server_mtu,
                "smp_pairing_success": decoder.smp_pairing_success,
                "conn_params": decoder.conn_params,
                "settle_delay_ms": decoder.settle_delay_ms,
                "settle_delay_from_ack_ms": decoder.settle_delay_from_ack_ms,
                "ota_violations_detected": len(decoder.ota_violations) > 0,
                "ota_violations_count": len(decoder.ota_violations),
                "ota_violations": decoder.ota_violations,
                "events_count": len(events),
                "events": events
            }, f, indent=2)
        print(f"Exported JSON events to: {args.json}")

    if args.csv:
        with open(args.csv, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Index", "Elapsed_ms", "Delta_ms", "Direction", "Category", "Opcode", "Handle", "Write_or_Notif_Type", "Length", "Payload_Hex", "AC_Seq", "AC_Op", "AC_Details"])
            for ev in events:
                if ev.get("category") in ("ATT_WRITE", "ATT_NOTIF"):
                    ac = ev.get("ac_info", {})
                    writer.writerow([
                        ev.get("pkt_idx"),
                        f"{ev.get('elapsed_ms'):.2f}",
                        f"{ev.get('delta_ms'):.2f}",
                        ev.get("direction"),
                        ev.get("category"),
                        ev.get("opcode"),
                        ev.get("handle"),
                        ev.get("write_type") or ev.get("notif_type"),
                        ev.get("length"),
                        ev.get("payload_hex"),
                        ac.get("seq", ""),
                        ac.get("opcode", ""),
                        json.dumps(ac.get("details", {}))
                    ])
        print(f"Exported CSV transactions to: {args.csv}")

if __name__ == '__main__':
    main()
