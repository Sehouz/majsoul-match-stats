#!/usr/bin/env python3
"""
Majsoul Paipu Downloader

A standalone tool to download game records from Majsoul.
Based on amae-koromo-scripts implementation.

Usage:
    python paipu_dl.py --capture              # Capture credentials via browser
    python paipu_dl.py --list [N]             # List recent N records (default 10)
    python paipu_dl.py --id <UUID>            # Download specific record
    python paipu_dl.py --id <UUID1> <UUID2>   # Download multiple records
"""

import asyncio
import json
import struct
import time
import argparse
import base64
import uuid
from pathlib import Path
from typing import Optional, List
from enum import IntEnum

import aiohttp
import websockets

from proto import liqi_pb2 as pb
from google.protobuf.json_format import MessageToDict


# ============== Configuration ==============

CONFIG_DIR = Path(__file__).parent / ".config"
OUTPUT_DIR = Path(__file__).parent / "output"
CONFIG_FILE = CONFIG_DIR / "credentials.json"

# Server configurations
SERVERS = {
    "cn": {
        "name": "China",
        "base_url": "https://game.maj-soul.com/1/",
    },
    "jp": {
        "name": "Japan", 
        "base_url": "https://game.mahjongsoul.com/",
    },
    "en": {
        "name": "International",
        "base_url": "https://mahjongsoul.game.yo-star.com/",
    },
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# ============== Protobuf Codec ==============

class MsgType(IntEnum):
    NOTIFY = 1
    REQUEST = 2
    RESPONSE = 3


class MajsoulCodec:
    """Encode/decode Majsoul protobuf messages using dynamic proto definition"""
    
    def __init__(self, proto_def: dict):
        self.proto_def = proto_def
        self.msg_index = 0
        self.pending_requests = {}  # msg_id -> (method_name, response_type)
        
        # Build type registry from proto definition
        self.types = {}
        self.services = {}
        self._parse_proto_def(proto_def)
    
    def _parse_proto_def(self, proto_def: dict, prefix: str = ""):
        """Parse proto definition to build type and service registry"""
        nested = proto_def.get("nested", {})
        for name, value in nested.items():
            full_name = f"{prefix}.{name}" if prefix else name
            
            if "fields" in value:
                # This is a message type
                self.types[full_name] = value
            elif "methods" in value:
                # This is a service
                self.services[full_name] = value
            
            # Recurse into nested definitions
            if "nested" in value:
                self._parse_proto_def(value, full_name)
    
    def _encode_varint(self, value: int) -> bytes:
        """Encode integer as varint"""
        result = []
        while value > 127:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value)
        return bytes(result)
    
    def _decode_varint(self, data: bytes, pos: int) -> tuple:
        """Decode varint from bytes, return (value, new_pos)"""
        result = 0
        shift = 0
        while True:
            byte = data[pos]
            result |= (byte & 0x7F) << shift
            pos += 1
            if not (byte & 0x80):
                break
            shift += 7
        return result, pos
    
    def _encode_field(self, field_num: int, wire_type: int, value: bytes) -> bytes:
        """Encode a protobuf field"""
        tag = (field_num << 3) | wire_type
        return self._encode_varint(tag) + value
    
    def _encode_string(self, field_num: int, value: bytes) -> bytes:
        """Encode a length-delimited field (string/bytes/message)"""
        return self._encode_field(field_num, 2, self._encode_varint(len(value)) + value)
    
    def _encode_message(self, type_name: str, data: dict) -> bytes:
        """Encode a message to protobuf bytes"""
        type_def = self.types.get(type_name)
        if not type_def:
            # Try with lq prefix
            type_def = self.types.get(f"lq.{type_name}")
        if not type_def:
            raise ValueError(f"Unknown type: {type_name}")
        
        result = b""
        fields = type_def.get("fields", {})
        
        for field_name, field_def in fields.items():
            if field_name not in data:
                continue
            
            value = data[field_name]
            field_id = field_def["id"]
            field_type = field_def["type"]
            is_repeated = field_def.get("rule") == "repeated"
            
            # Handle repeated fields
            if is_repeated and isinstance(value, list):
                for item in value:
                    result += self._encode_single_field(field_id, field_type, item)
            else:
                result += self._encode_single_field(field_id, field_type, value)
        
        return result
    
    def _encode_single_field(self, field_id: int, field_type: str, value) -> bytes:
        """Encode a single field value"""
        if value is None:
            return b""
        
        if field_type == "string":
            if isinstance(value, str):
                value = value.encode("utf-8")
            return self._encode_string(field_id, value)
        elif field_type == "bytes":
            return self._encode_string(field_id, value)
        elif field_type in ("int32", "int64", "uint32", "uint64"):
            return self._encode_field(field_id, 0, self._encode_varint(int(value)))
        elif field_type == "bool":
            return self._encode_field(field_id, 0, self._encode_varint(1 if value else 0))
        elif field_type in self.types or f"lq.{field_type}" in self.types:
            # Nested message
            if isinstance(value, dict):
                nested_bytes = self._encode_message(field_type, value)
                return self._encode_string(field_id, nested_bytes)
        
        return b""
    
    def _decode_message(self, type_name: str, data: bytes) -> dict:
        """Decode protobuf bytes to dict"""
        type_def = self.types.get(type_name)
        if not type_def:
            type_def = self.types.get(f"lq.{type_name}")
        if not type_def:
            return {"_raw": base64.b64encode(data).decode()}
        
        result = {}
        fields = type_def.get("fields", {})
        field_by_id = {f["id"]: (name, f) for name, f in fields.items()}
        
        pos = 0
        while pos < len(data):
            tag, pos = self._decode_varint(data, pos)
            field_id = tag >> 3
            wire_type = tag & 0x7
            
            if field_id not in field_by_id:
                # Skip unknown field
                if wire_type == 0:
                    _, pos = self._decode_varint(data, pos)
                elif wire_type == 2:
                    length, pos = self._decode_varint(data, pos)
                    pos += length
                continue
            
            field_name, field_def = field_by_id[field_id]
            field_type = field_def["type"]
            
            if wire_type == 0:  # Varint
                value, pos = self._decode_varint(data, pos)
                if field_type == "bool":
                    value = bool(value)
                result[field_name] = value
            elif wire_type == 2:  # Length-delimited
                length, pos = self._decode_varint(data, pos)
                value = data[pos:pos + length]
                pos += length
                
                if field_type == "string":
                    result[field_name] = value.decode("utf-8")
                elif field_type == "bytes":
                    result[field_name] = base64.b64encode(value).decode()
                elif field_type in self.types or f"lq.{field_type}" in self.types:
                    result[field_name] = self._decode_message(field_type, value)
                else:
                    result[field_name] = base64.b64encode(value).decode()
        
        return result
    
    def encode_request(self, method_name: str, payload: dict) -> bytes:
        """Encode a request message"""
        self.msg_index += 1
        msg_id = self.msg_index
        
        # Parse method name: .lq.Lobby.fetchGameRecord
        parts = method_name.lstrip(".").split(".")
        if len(parts) >= 3:
            service_name = ".".join(parts[:-1])
            method = parts[-1]
        else:
            raise ValueError(f"Invalid method name: {method_name}")
        
        # Get request/response types from service definition
        service = self.services.get(service_name)
        if not service:
            raise ValueError(f"Unknown service: {service_name}")
        
        method_def = service.get("methods", {}).get(method)
        if not method_def:
            raise ValueError(f"Unknown method: {method}")
        
        request_type = method_def["requestType"]
        response_type = method_def["responseType"]
        
        # Store for response decoding
        self.pending_requests[msg_id] = (method_name, response_type)
        
        # Encode the request payload
        request_data = self._encode_message(request_type, payload)
        
        # Wrap in Wrapper message
        wrapper_data = (
            self._encode_string(1, method_name.encode()) +
            self._encode_string(2, request_data)
        )
        
        # Build final message: type(1) + msg_id(2) + wrapper
        return bytes([MsgType.REQUEST]) + struct.pack("<H", msg_id) + wrapper_data
    
    def decode_response(self, data: bytes) -> Optional[dict]:
        """Decode a response message"""
        if len(data) < 3:
            return None
        
        msg_type = data[0]
        
        if msg_type == MsgType.RESPONSE:
            msg_id = struct.unpack("<H", data[1:3])[0]
            
            if msg_id not in self.pending_requests:
                return None
            
            method_name, response_type = self.pending_requests.pop(msg_id)
            
            # Parse wrapper
            wrapper_data = data[3:]
            pos = 0
            method = None
            payload_data = None
            
            while pos < len(wrapper_data):
                tag, pos = self._decode_varint(wrapper_data, pos)
                field_id = tag >> 3
                wire_type = tag & 0x7
                
                if wire_type == 2:
                    length, pos = self._decode_varint(wrapper_data, pos)
                    value = wrapper_data[pos:pos + length]
                    pos += length
                    
                    if field_id == 1:
                        method = value.decode()
                    elif field_id == 2:
                        payload_data = value
            
            if payload_data:
                payload = self._decode_message(response_type, payload_data)
                return {
                    "id": msg_id,
                    "method": method_name,
                    "data": payload,
                }
        
        return None


# ============== XOR Decryption ==============

DECODE_KEYS = [0x84, 0x5e, 0x4e, 0x42, 0x39, 0xa2, 0x1f, 0x60, 0x1c]

def decode_record_data(data: bytes) -> bytes:
    """Decode XOR encrypted game record data"""
    data = bytearray(data)
    for i in range(len(data)):
        u = (23 ^ len(data)) + 5 * i + DECODE_KEYS[i % len(DECODE_KEYS)] & 255
        data[i] ^= u
    return bytes(data)


# ============== Majsoul API Client ==============

class MajsoulClient:
    """Majsoul API client"""
    
    def __init__(self, server: str = "cn"):
        self.server = server
        self.server_config = SERVERS.get(server, SERVERS["cn"])
        self.base_url = self.server_config["base_url"]
        self.ws = None
        self.codec = None
        self.version = None
        self.client_version_string = None
        
    async def _fetch_json(self, url: str, bust_cache: bool = False) -> dict:
        """Fetch JSON from URL"""
        if bust_cache:
            url += ("&" if "?" in url else "?") + f"randv={int(time.time() * 1000)}"
        
        headers = {"User-Agent": USER_AGENT}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                return await resp.json()
    
    async def _get_server_list(self) -> List[str]:
        """Get available WebSocket servers"""
        # Get version info
        version_info = await self._fetch_json(f"{self.base_url}version.json", bust_cache=True)
        self.version = version_info["version"]
        self.client_version_string = "web-" + self.version.replace(".w", "")
        
        # Get resource version info
        res_info = await self._fetch_json(f"{self.base_url}resversion{self.version}.json")
        
        # Get proto definition
        pb_prefix = res_info["res"]["res/proto/liqi.json"]["prefix"]
        proto_def = await self._fetch_json(f"{self.base_url}{pb_prefix}/res/proto/liqi.json")
        self.codec = MajsoulCodec(proto_def)
        
        # Get config with server list
        config_prefix = res_info["res"]["config.json"]["prefix"]
        config = await self._fetch_json(f"{self.base_url}{config_prefix}/config.json")
        
        # Extract server list
        ip_def = next((x for x in config.get("ip", []) if x.get("name") == "player"), None)
        if not ip_def:
            ip_def = config.get("ip", [{}])[0]
        
        servers = []
        
        # Try gateways first
        if ip_def.get("gateways"):
            for gw in ip_def["gateways"]:
                url = gw.get("url", "")
                if url:
                    # Remove http(s):// prefix
                    url = url.replace("https://", "").replace("http://", "")
                    servers.append(url)
        
        # Try region_urls
        if not servers and ip_def.get("region_urls"):
            region_urls = ip_def["region_urls"]
            if isinstance(region_urls, dict):
                region_urls = list(region_urls.values())
            for region in region_urls:
                url = region.get("url", region) if isinstance(region, dict) else region
                if url:
                    servers.append(url)
        
        return servers
    
    async def connect(self):
        """Connect to Majsoul server"""
        servers = await self._get_server_list()
        if not servers:
            raise Exception("No servers available")
        
        # Try each server
        last_error = None
        for server in servers:
            try:
                # Get route info
                route_url = f"https://{server}/api/clientgate/routes?platform=Web&version={self.version}"
                try:
                    route_info = await self._fetch_json(route_url, bust_cache=True)
                    if route_info.get("data", {}).get("maintenance"):
                        print(f"Server {server} is under maintenance")
                        continue
                except Exception:
                    pass
                
                ws_url = f"wss://{server}/gateway"
                print(f"Connecting to {ws_url}...")
                
                self.ws = await websockets.connect(
                    ws_url,
                    additional_headers={"User-Agent": USER_AGENT},
                )
                print("Connected!")
                return
            except Exception as e:
                last_error = e
                print(f"Failed to connect to {server}: {e}")
                continue
        
        raise Exception(f"Failed to connect to any server: {last_error}")
    
    async def close(self):
        """Close connection"""
        if self.ws:
            await self.ws.close()
    
    async def call(self, method: str, payload: dict = None) -> dict:
        """Call an API method"""
        if payload is None:
            payload = {}
        
        request = self.codec.encode_request(method, payload)
        await self.ws.send(request)
        
        while True:
            response = await self.ws.recv()
            result = self.codec.decode_response(response)
            if result:
                return result["data"]
    
    async def login(self, access_token: str):
        """Login with access token"""
        # Send heartbeat first
        await self.call(".lq.Lobby.heatbeat", {"no_operation_counter": 0})
        await asyncio.sleep(0.1)
        
        # Check token
        check_result = await self.call(".lq.Lobby.oauth2Check", {
            "type": 0,
            "access_token": access_token,
        })
        
        if not check_result.get("has_account"):
            await asyncio.sleep(2)
            check_result = await self.call(".lq.Lobby.oauth2Check", {
                "type": 0,
                "access_token": access_token,
            })
        
        if not check_result.get("has_account"):
            raise Exception("Token invalid or no account associated")
        
        # Login
        result = await self.call(".lq.Lobby.oauth2Login", {
            "type": 0,
            "access_token": access_token,
            "reconnect": False,
            "device": {
                "platform": "pc",
                "hardware": "pc",
                "os": "windows",
                "os_version": "win10",
                "is_browser": True,
                "software": "Chrome",
                "sale_platform": "web",
            },
            "random_key": str(uuid.uuid4()),
            "client_version": {"resource": self.version},
            "currency_platforms": [],
            "client_version_string": self.client_version_string,
        })
        
        if not result.get("account_id"):
            raise Exception(f"Login failed: {result}")
        
        print(f"Logged in as account: {result['account_id']}")
        return result
    
    async def fetch_record_list(self, start: int = 0, count: int = 10) -> dict:
        """Fetch list of game records"""
        return await self.call(".lq.Lobby.fetchGameRecordList", {
            "start": start,
            "count": count,
            "type": 0,
        })
    
    async def fetch_record(self, game_uuid: str) -> dict:
        """Fetch a specific game record"""
        return await self.call(".lq.Lobby.fetchGameRecord", {
            "game_uuid": game_uuid,
            "client_version_string": self.client_version_string,
        })


# ============== Credential Capture ==============

def capture_credentials():
    """Capture credentials via Playwright browser automation"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Installing...")
        import subprocess
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        from playwright.sync_api import sync_playwright
    
    print("=" * 60)
    print("Select server:")
    for i, (key, cfg) in enumerate(SERVERS.items(), 1):
        print(f"  {i}. {cfg['name']} ({key})")
    
    choice = input("Enter choice (1/2/3): ").strip()
    server_keys = list(SERVERS.keys())
    server = server_keys[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= 3 else "cn"
    
    url = SERVERS[server]["base_url"]
    captured = {"server": server, "access_token": None, "account_id": None}
    
    def handle_ws_message(msg):
        """Parse WebSocket message to extract credentials"""
        if not isinstance(msg, bytes) or len(msg) < 4:
            return
        
        import re
        
        try:
            msg_str = msg.decode("utf-8", errors="ignore")
            
            # Debug: show messages containing login-related keywords
            if "login" in msg_str.lower() or "account" in msg_str.lower() or "token" in msg_str.lower():
                print(f"[DEBUG] Found relevant message, length={len(msg)}")
            
            # Look for accessToken - try multiple patterns
            if "accessToken" in msg_str or "access_token" in msg_str:
                # Pattern 1: UUID format (most common)
                match = re.search(r'["\x12]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', msg_str)
                if match and not captured["access_token"]:
                    captured["access_token"] = match.group(1)
                    print(f"[*] Captured access_token: {captured['access_token']}")
            
            # Look for accountId - it's usually encoded as varint after field tag
            if "accountId" in msg_str or "account_id" in msg_str or b'\x08' in msg:
                # Try to find account ID (usually a 7-8 digit number)
                match = re.search(r'accountId[^0-9]*(\d{6,12})', msg_str)
                if match and not captured["account_id"]:
                    captured["account_id"] = int(match.group(1))
                    print(f"[*] Captured account_id: {captured['account_id']}")
                
                # Also try to extract from binary - look for the pattern after "Lobby.login" response
                if not captured["account_id"] and b'Lobby' in msg and msg[0] == 3:
                    # Try to find 8-digit number in the message
                    nums = re.findall(r'(\d{7,10})', msg_str)
                    for num in nums:
                        n = int(num)
                        if 10000000 < n < 100000000:  # Reasonable account ID range
                            captured["account_id"] = n
                            print(f"[*] Captured account_id (from binary): {n}")
                            break
            
            if captured["access_token"] and captured["account_id"]:
                save_credentials(captured)
                print("\n[+] Credentials saved! You can close the browser.")
        except Exception as e:
            print(f"[DEBUG] Parse error: {e}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        
        def on_ws(ws):
            ws.on("framereceived", lambda data: handle_ws_message(data))
            ws.on("framesent", lambda data: handle_ws_message(data))
        
        page.on("websocket", on_ws)
        page.goto(url)
        
        print("\n" + "=" * 60)
        print("Please login to Majsoul.")
        print("Credentials will be captured automatically.")
        print("Press Enter after login to close browser...")
        print("=" * 60)
        
        input()
        browser.close()
    
    return captured if captured["access_token"] else None


def save_credentials(data: dict):
    """Save credentials to config file"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def load_credentials() -> Optional[dict]:
    """Load credentials from config file"""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return None


# ============== Main Functions ==============

async def list_records(config: dict, count: int = 10):
    """List recent game records"""
    client = MajsoulClient(config.get("server", "cn"))
    
    try:
        await client.connect()
        await client.login(config["access_token"])
        
        print(f"\nFetching recent {count} records...")
        result = await client.fetch_record_list(count=count)
        
        records = result.get("record_list", [])
        if not records:
            print("No records found.")
            return
        
        print(f"\n{'#':<3} {'UUID':<40} {'Time':<20}")
        print("-" * 65)
        for i, rec in enumerate(records, 1):
            uuid = rec.get("uuid", "N/A")
            start_time = rec.get("start_time", 0)
            time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(start_time))
            print(f"{i:<3} {uuid:<40} {time_str:<20}")
        
    finally:
        await client.close()


async def download_record(config: dict, game_uuid: str):
    """Download a specific game record and parse to readable JSON"""
    client = MajsoulClient(config.get("server", "cn"))
    
    try:
        await client.connect()
        await client.login(config["access_token"])
        
        print(f"\nFetching record: {game_uuid}")
        record = await client.fetch_record(game_uuid)
        
        # Parse the record into readable format
        result = parse_game_record(record)
        
        # Save to file
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_file = OUTPUT_DIR / f"{game_uuid}.json"
        output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"Saved to: {output_file}")
        print(f"Actions: {len(result.get('actions', []))}")
        
        return result
        
    finally:
        await client.close()


def parse_single_pb(data: bytes, msg_class) -> dict:
    """Parse a single protobuf message"""
    try:
        msg = msg_class()
        msg.ParseFromString(data)
        return MessageToDict(msg, preserving_proto_field_name=True)
    except Exception:
        return None


def parse_repeated_pb(data: bytes, msg_class) -> list:
    """Parse repeated protobuf messages (length-delimited)"""
    results = []
    pos = 0
    while pos < len(data):
        # Read field tag
        if pos >= len(data):
            break
        tag = data[pos]
        pos += 1
        wire_type = tag & 0x7
        
        if wire_type == 2:  # Length-delimited
            # Read length (varint)
            length = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                pos += 1
                length |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            
            # Parse message
            if pos + length <= len(data):
                msg = msg_class()
                msg.ParseFromString(data[pos:pos + length])
                results.append(MessageToDict(msg, preserving_proto_field_name=True))
                pos += length
        else:
            # Skip other wire types
            break
    return results


def parse_game_record(record: dict) -> dict:
    """Parse raw game record into readable JSON using protobuf definitions"""
    result = {}
    
    # Parse head info with nested protobuf fields
    if "head" in record:
        head = record["head"].copy()
        
        # Decode accounts field (PlayerGameView)
        if "accounts" in head and head["accounts"]:
            parsed = parse_single_pb(base64.b64decode(head["accounts"]), pb.PlayerGameView)
            if parsed:
                head["accounts"] = parsed
        
        # Decode result.players field (RecordPlayerResult)
        if "result" in head and head["result"]:
            result_data = head["result"]
            if "players" in result_data and result_data["players"]:
                parsed = parse_single_pb(base64.b64decode(result_data["players"]), pb.RecordPlayerResult)
                if parsed:
                    head["result"]["players"] = parsed
        
        result["head"] = head
        result["uuid"] = head.get("uuid")
    
    # Parse game data
    if "data" not in record or not record["data"]:
        result["error"] = "No game data"
        return result
    
    try:
        data_bytes = base64.b64decode(record["data"])
        
        # Parse outer Wrapper
        wrapper = pb.Wrapper()
        wrapper.ParseFromString(data_bytes)
        
        # Parse GameDetailRecords
        detail = pb.GameDetailRecords()
        detail.ParseFromString(wrapper.data)
        
        result["version"] = detail.version
        result["actions"] = []
        
        # Parse each action
        for action in detail.actions:
            if not action.result:
                continue
            
            # Parse action Wrapper (no XOR decoding needed for new format)
            action_wrapper = pb.Wrapper()
            action_wrapper.ParseFromString(action.result)
            
            # Get message type and parse
            type_name = action_wrapper.name.split(".")[-1]
            msg_class = getattr(pb, type_name, None)
            
            if msg_class:
                msg = msg_class()
                msg.ParseFromString(action_wrapper.data)
                result["actions"].append({
                    "type": type_name,
                    "data": MessageToDict(msg, preserving_proto_field_name=True)
                })
            else:
                result["actions"].append({
                    "type": type_name,
                    "raw": base64.b64encode(action_wrapper.data).decode()
                })
                
    except Exception as e:
        result["parse_error"] = str(e)
        # Save raw data as fallback
        result["raw_data"] = record.get("data")
    
    return result


def main():
    parser = argparse.ArgumentParser(description="Majsoul Paipu Downloader")
    parser.add_argument("--capture", action="store_true", help="Capture credentials via browser")
    parser.add_argument("--list", type=int, nargs="?", const=10, metavar="N", help="List recent N records")
    parser.add_argument("--id", type=str, nargs="+", metavar="UUID", help="Download record(s) by UUID")
    parser.add_argument("--server", type=str, choices=["cn", "jp", "en"], help="Override server")
    
    args = parser.parse_args()
    
    if args.capture:
        capture_credentials()
        return
    
    config = load_credentials()
    if not config:
        print("No credentials found. Run with --capture first.")
        return
    
    if args.server:
        config["server"] = args.server
    
    if args.list is not None:
        asyncio.run(list_records(config, args.list))
    elif args.id:
        for uuid in args.id:
            asyncio.run(download_record(config, uuid))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
