import struct
import zlib
import hashlib
from typing import List, Tuple, Dict, Any, Optional, Set
from forge.store import Store

# Pkt-line utilities
def pkt_line(data: bytes) -> bytes:
    length = len(data) + 4
    return f"{length:04x}".encode("ascii") + data

def pkt_flush() -> bytes:
    return b"0000"

def pkt_delim() -> bytes:
    return b"0001"

def pkt_sideband(band: int, data: bytes) -> bytes:
    return pkt_line(bytes([band]) + data)

def pkt_sideband_stream(band: int, data: bytes) -> bytes:
    chunk_size = 65516 - 5
    out = bytearray()
    i = 0
    while i < len(data):
        chunk = data[i:i+chunk_size]
        out.extend(pkt_line(bytes([band]) + chunk))
        i += len(chunk)
    return bytes(out)

def parse_pkt_lines(stream_data: bytes) -> List[bytes]:
    packets = []
    i = 0
    n = len(stream_data)
    while i < n:
        if i + 4 > n:
            break
        hex_len = stream_data[i:i+4].decode("ascii", errors="ignore")
        if hex_len == "0000":
            packets.append(b"0000")
            i += 4
            continue
        if hex_len == "0001":
            packets.append(b"0001")
            i += 4
            continue
        try:
            length = int(hex_len, 16)
        except ValueError:
            break
        if length == 0:
            packets.append(b"0000")
            i += 4
            continue
        if i + length > n:
            # incomplete packet, take rest
            packets.append(stream_data[i+4:])
            break
        pkt_data = stream_data[i+4:i+length]
        packets.append(pkt_data)
        i += length
    return packets

# Packfile parser (for receive-pack unpack)
def parse_packfile(pack_data: bytes, store: Store, owner: str, repo: str) -> Tuple[bool, str, List[str]]:
    if not pack_data.startswith(b"PACK"):
        return False, "Invalid pack header", []
    
    if len(pack_data) < 12:
        return False, "Packfile too short", []
    
    num_objects = struct.unpack("!I", pack_data[8:12])[0]
    pos = 12
    objects_read = 0
    
    # We store raw object entries: list of (obj_type_int, decompressed_data, raw_sha)
    # Types: 1=COMMIT, 2=TREE, 3=BLOB, 4=TAG, 6=OFS_DELTA, 7=REF_DELTA
    entries = []
    
    while pos < len(pack_data) - 20 and objects_read < num_objects:
        byte = pack_data[pos]
        pos += 1
        obj_type = (byte >> 4) & 7
        size = byte & 15
        shift = 4
        while byte & 0x80:
            if pos >= len(pack_data):
                return False, "Unexpected end of packfile in size encoding", []
            byte = pack_data[pos]
            pos += 1
            size += (byte & 0x7f) << shift
            shift += 7
            
        ref_data = None
        if obj_type == 7: # REF_DELTA
            if pos + 20 > len(pack_data):
                return False, "Unexpected end of packfile for REF_DELTA base", []
            ref_data = pack_data[pos:pos+20].hex()
            pos += 20
        elif obj_type == 6: # OFS_DELTA
            # read variable length negative offset
            c = pack_data[pos]
            pos += 1
            offset = c & 0x7f
            while c & 0x80:
                if pos >= len(pack_data):
                    return False, "Unexpected end of packfile for OFS_DELTA offset", []
                c = pack_data[pos]
                pos += 1
                offset = ((offset + 1) << 7) + (c & 0x7f)
            ref_data = offset

        try:
            decomp = zlib.decompressobj()
            uncompressed = decomp.decompress(pack_data[pos:])
            consumed = decomp.unused_data
            # advance pos by total pack data consumed
            pos = len(pack_data) - len(consumed)
        except Exception as e:
            return False, f"Zlib decompression error: {e}", []

        entries.append((obj_type, uncompressed, ref_data))
        objects_read += 1

    # Resolve deltas and store objects
    # First pass: store non-deltas and build map by index / sha
    resolved_objects = [] # list of (type_str, bytes)
    
    # Simple resolution loop (handle up to 3 passes for nested deltas if needed)
    pending = list(enumerate(entries))
    index_to_obj: Dict[int, Tuple[str, bytes]] = {}
    sha_to_obj: Dict[str, Tuple[str, bytes]] = {}

    type_map = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}

    # Pass non-deltas first
    remaining = []
    for idx, (ot, data, ref) in pending:
        if ot in type_map:
            t_str = type_map[ot]
            index_to_obj[idx] = (t_str, data)
            sha = store.store_object(owner, repo, t_str, data)
            sha_to_obj[sha] = (t_str, data)
            resolved_objects.append((t_str, data, sha))
        else:
            remaining.append((idx, ot, data, ref))

    # Resolve deltas (OFS_DELTA / REF_DELTA)
    def apply_delta(base_data: bytes, delta_data: bytes) -> bytes:
        # standard git delta application
        pos = 0
        def decode_size(d, p):
            s = 0
            shift = 0
            while p < len(d):
                b = d[p]
                p += 1
                s |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            return s, p

        base_sz, pos = decode_size(delta_data, pos)
        target_sz, pos = decode_size(delta_data, pos)
        
        out = bytearray()
        while pos < len(delta_data):
            cmd = delta_data[pos]
            pos += 1
            if cmd & 0x80:
                # copy from base
                cp_off = 0
                cp_sz = 0
                if cmd & 0x01: cp_off |= delta_data[pos]; pos += 1
                if cmd & 0x02: cp_off |= delta_data[pos] << 8; pos += 1
                if cmd & 0x04: cp_off |= delta_data[pos] << 16; pos += 1
                if cmd & 0x08: cp_off |= delta_data[pos] << 24; pos += 1
                if cmd & 0x10: cp_sz |= delta_data[pos]; pos += 1
                if cmd & 0x20: cp_sz |= delta_data[pos] << 8; pos += 1
                if cmd & 0x40: cp_sz |= delta_data[pos] << 16; pos += 1
                if cp_sz == 0: cp_sz = 0x10000
                out.extend(base_data[cp_off:cp_off+cp_sz])
            elif cmd > 0:
                # add data
                out.extend(delta_data[pos:pos+cmd])
                pos += cmd
        return bytes(out)

    max_iter = 10
    while remaining and max_iter > 0:
        next_remaining = []
        for idx, ot, data, ref in remaining:
            base_data = None
            if ot == 7: # REF_DELTA (ref is hex sha)
                if ref in sha_to_obj:
                    base_data = sha_to_obj[ref][1]
            elif ot == 6: # OFS_DELTA (ref is negative offset)
                base_idx = idx - ref
                if base_idx in index_to_obj:
                    base_data = index_to_obj[base_idx][1]
            
            if base_data is not None:
                # figure out object type of base? In git packfile delta, type is inherited from base object type!
                # Wait, delta objects in git packfile inherit the base object type.
                base_type = sha_to_obj[ref][0] if ot == 7 else index_to_obj[idx - ref][0]
                target_data = apply_delta(base_data, data)
                index_to_obj[idx] = (base_type, target_data)
                sha = store.store_object(owner, repo, base_type, target_data)
                sha_to_obj[sha] = (base_type, target_data)
                resolved_objects.append((base_type, target_data, sha))
            else:
                next_remaining.append((idx, ot, data, ref))
        remaining = next_remaining
        max_iter -= 1

    return True, "unpack ok", [sha for _, _, sha in resolved_objects]

# Packfile generator (for upload-pack fetch)
def generate_packfile(store: Store, owner: str, repo: str, object_shas: Set[str]) -> bytes:
    key = (owner.lower(), name.lower() if 'name' in locals() else repo.lower())
    # Correct key:
    repo_key = (owner.lower(), repo.lower())
    repo_objs = store.git_objects.get(repo_key, {})
    
    # Collect all reachable objects
    to_pack = []
    for sha in object_shas:
        if sha in repo_objs:
            to_pack.append((sha, repo_objs[sha]["type"], repo_objs[sha]["data"]))

    # Build packfile bytes
    pack = bytearray()
    pack.extend(b"PACK")
    pack.extend(struct.pack("!II", 2, len(to_pack)))

    for sha, obj_type, data in to_pack:
        t_code = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}.get(obj_type, 3)
        size = len(data)
        
        # Encode type and size
        byte = ((t_code & 7) << 4) | (size & 15)
        size >>= 4
        if size > 0:
            byte |= 0x80
        pack.append(byte)
        while size > 0:
            b = size & 0x7f
            size >>= 7
            if size > 0:
                b |= 0x80
            pack.append(b)

        compressed = zlib.compress(data)
        pack.extend(compressed)

    # Calculate checksum over pack
    hasher = hashlib.sha1(bytes(pack))
    pack.extend(hasher.digest())
    return bytes(pack)
