#!/usr/bin/env python3
"""Minimal DNS correctness probe (standard library only).

The load generators count NOERROR replies, so a server that answers fast but
wrong (an empty answer, a truncated or stale record, a broken patch) would
score well. This probe sends real queries and normalizes every response to a
comparable form:

    {"rcode": "NOERROR", "aa": true, "tc": false,
     "answer": ["owner ttl TYPE rdata", ...],      sorted
     "authority": [...]}                           sorted

Names inside rdata are decompressed, so message-compression settings do not
change the result. validity.py compares responses with a reference captured
from the stock server; the comparison ignores the additional section and only
looks at the authority section when the answer is empty (referrals, NODATA,
NXDOMAIN), so legitimate options such as minimal-responses do not count as
wrong answers.

It needs nothing but python3, so it is also copied to the name server and run
there against 127.0.0.1 while a trial is loading the server:

    python3 dns_probe.py query --server 10.10.1.1 --names sample.txt --out obs.json
    python3 dns_probe.py query --server 127.0.0.1 --names sample.txt \
        --rate 50 --duration 30 --out obs.json
    python3 dns_probe.py compare --reference ref.json --observed obs.json
"""
import argparse
import ipaddress
import json
import random
import select
import socket
import struct
import sys
import time

TYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15, "TXT": 16,
         "AAAA": 28, "SRV": 33, "OPT": 41, "SVCB": 64, "HTTPS": 65, "ANY": 255}
TYPE_NAMES = {v: k for k, v in TYPES.items()}
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP",
          5: "REFUSED"}
EDNS_UDP_SIZE = 1232
# Stop a probe early when the server has clearly stopped answering.
MAX_CONSECUTIVE_TIMEOUTS = 25


class ParseError(Exception):
    pass


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        if label:
            raw = label.encode()
            out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(name, qtype, qid):
    """A query with RD clear and an EDNS0 OPT record (UDP size 1232)."""
    header = struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 1)
    question = encode_name(name) + struct.pack("!HH", TYPES[qtype.upper()], 1)
    opt = b"\x00" + struct.pack("!HHIH", TYPES["OPT"], EDNS_UDP_SIZE, 0, 0)
    return header + question + opt


def read_name(data, offset):
    """(name, offset after the name), following compression pointers."""
    labels, jumped, end, hops = [], False, offset, 0
    while True:
        if offset >= len(data):
            raise ParseError("name runs past the end of the message")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ParseError("truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            jumped, offset, hops = True, pointer, hops + 1
            if hops > 64:
                raise ParseError("compression loop")
            continue
        if length == 0:
            if not jumped:
                end = offset + 1
            break
        labels.append(data[offset + 1:offset + 1 + length].decode("ascii", "replace"))
        offset += 1 + length
    return (".".join(labels) + ".").lower(), end


def render_rdata(data, offset, rtype, rdlength):
    rdata = data[offset:offset + rdlength]
    if rtype == TYPES["A"] and rdlength == 4:
        return str(ipaddress.IPv4Address(rdata))
    if rtype == TYPES["AAAA"] and rdlength == 16:
        return str(ipaddress.IPv6Address(rdata))
    if rtype in (TYPES["NS"], TYPES["CNAME"], TYPES["PTR"]):
        return read_name(data, offset)[0]
    if rtype == TYPES["MX"]:
        return f"{struct.unpack('!H', rdata[:2])[0]} {read_name(data, offset + 2)[0]}"
    if rtype == TYPES["SOA"]:
        mname, pos = read_name(data, offset)
        rname, pos = read_name(data, pos)
        numbers = struct.unpack("!IIIII", data[pos:pos + 20])
        return " ".join([mname, rname] + [str(n) for n in numbers])
    if rtype == TYPES["TXT"]:
        parts, pos = [], 0
        while pos < len(rdata):
            n = rdata[pos]
            parts.append(json.dumps(rdata[pos + 1:pos + 1 + n].decode("utf-8", "replace")))
            pos += 1 + n
        return " ".join(parts)
    return rdata.hex()


def parse_response(data):
    """(query id, normalized response)."""
    if len(data) < 12:
        raise ParseError("message shorter than a header")
    qid, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qdcount):
        _, offset = read_name(data, offset)
        offset += 4
    sections = {"answer": [], "authority": []}
    for section, count in (("answer", ancount), ("authority", nscount)):
        for _ in range(count):
            owner, offset = read_name(data, offset)
            rtype, _, ttl, rdlength = struct.unpack("!HHIH", data[offset:offset + 10])
            offset += 10
            rdata = render_rdata(data, offset, rtype, rdlength)
            offset += rdlength
            name = TYPE_NAMES.get(rtype, f"TYPE{rtype}")
            sections[section].append(f"{owner} {ttl} {name} {rdata}")
    response = {"rcode": RCODES.get(flags & 0xF, str(flags & 0xF)),
                "aa": bool(flags & 0x0400), "tc": bool(flags & 0x0200),
                "answer": sorted(sections["answer"]),
                "authority": sorted(sections["authority"])}
    return qid, response


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def read_queries(path, limit=None):
    """[(name, type)] from a dnsperf-format file, sampled evenly to ``limit``."""
    queries = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and not line.startswith(("#", ";")) \
                    and parts[1].upper() in TYPES:
                queries.append((parts[0], parts[1].upper()))
    if limit and len(queries) > limit:
        step = len(queries) / limit
        queries = [queries[int(i * step)] for i in range(limit)]
    return queries


def key(name, qtype):
    return f"{name.lower().rstrip('.')}. {qtype.upper()}"


def query_many(server, queries, timeout=1.0, rate=None, duration=None, port=53):
    """{key: normalized response, or None if it went unanswered}.

    Sequential, one outstanding query at a time. With ``rate`` the queries
    are spread at that many per second; with ``duration`` the list is cycled
    until that many seconds have passed (results keep the last response per
    key, and ``counts`` tallies every attempt)."""
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    results, counts = {}, {"sent": 0, "answered": 0, "unanswered": 0, "parse_errors": 0}
    started = time.time()
    consecutive = 0
    i = 0
    try:
        while queries:
            if duration is None and i >= len(queries):
                break
            if duration is not None and time.time() - started >= duration:
                break
            name, qtype = queries[i % len(queries)]
            i += 1
            if rate:
                wait = started + i / rate - time.time()
                if wait > 0:
                    time.sleep(wait)
            qid = random.randint(0, 0xFFFF)
            sock.sendto(build_query(name, qtype, qid), (server, port))
            counts["sent"] += 1
            response = None
            deadline = time.time() + timeout
            while time.time() < deadline:
                ready, _, _ = select.select([sock], [], [], max(0, deadline - time.time()))
                if not ready:
                    break
                data, _ = sock.recvfrom(65535)
                try:
                    got_id, parsed = parse_response(data)
                except (ParseError, struct.error, ValueError):
                    counts["parse_errors"] += 1
                    continue
                if got_id == qid:
                    response = parsed
                    break
            k = key(name, qtype)
            if response is None:
                counts["unanswered"] += 1
                results.setdefault(k, None)
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_TIMEOUTS and duration is None:
                    for n, t in queries[i:]:
                        results.setdefault(key(n, t), None)
                    counts["aborted"] = True
                    break
            else:
                counts["answered"] += 1
                results[k] = response
                consecutive = 0
    finally:
        sock.close()
    return results, counts


def same(expected, observed):
    if (expected["rcode"], expected["aa"], expected["tc"], expected["answer"]) != \
            (observed["rcode"], observed["aa"], observed["tc"], observed["answer"]):
        return False
    if not expected["answer"]:
        return expected["authority"] == observed["authority"]
    return True


def compare(reference, observed, max_examples=5):
    """Summary of ``observed`` responses against ``reference`` (both
    {key: response or None}). Unanswered queries are counted separately from
    wrong ones: losing a query under load is not a wrong answer."""
    summary = {"checked": 0, "answered": 0, "unanswered": 0, "mismatched": 0,
               "examples": []}
    for k, response in observed.items():
        expected = reference.get(k)
        if expected is None:
            continue
        summary["checked"] += 1
        if response is None:
            summary["unanswered"] += 1
            continue
        summary["answered"] += 1
        if not same(expected, response):
            summary["mismatched"] += 1
            if len(summary["examples"]) < max_examples:
                summary["examples"].append({"query": k, "expected": expected,
                                            "observed": response})
    return summary


def zone_expectations(zone_text):
    """{key: sorted rdata} for the plain A and AAAA records of a zone file:
    an independent check that a captured reference matches the zone."""
    expected = {}
    for line in zone_text.splitlines():
        parts = line.split(";")[0].split()
        if len(parts) == 4 and parts[1] == "IN" and parts[2] in ("A", "AAAA") \
                and parts[0].endswith("."):
            try:
                value = str(ipaddress.ip_address(parts[3]))
            except ValueError:
                continue
            expected.setdefault(key(parts[0], parts[2]), []).append(value)
    return {k: sorted(v) for k, v in expected.items()}


def check_against_zone(responses, expectations):
    """Keys whose answer does not hold exactly the zone's A/AAAA data."""
    wrong = []
    for k, values in expectations.items():
        response = responses.get(k)
        if response is None:
            continue
        rdata = sorted(r.split(" ", 3)[3] for r in response["answer"])
        if response["rcode"] != "NOERROR" or rdata != values:
            wrong.append(k)
    return wrong


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    q = sub.add_parser("query")
    q.add_argument("--server", required=True)
    q.add_argument("--names", required=True, help="dnsperf-format query file")
    q.add_argument("--limit", type=int, help="sample this many queries evenly")
    q.add_argument("--timeout", type=float, default=1.0)
    q.add_argument("--rate", type=float, help="queries per second (default: as fast as answered)")
    q.add_argument("--duration", type=float, help="cycle the queries for this many seconds")
    q.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--reference", required=True)
    c.add_argument("--observed", required=True)
    args = parser.parse_args()

    if args.action == "query":
        queries = read_queries(args.names, args.limit)
        results, counts = query_many(args.server, queries, args.timeout,
                                     args.rate, args.duration)
        with open(args.out, "w") as f:
            json.dump({"server": args.server, "counts": counts, "responses": results}, f)
        print(json.dumps(counts))
        return 0
    with open(args.reference) as f:
        reference = json.load(f)["responses"]
    with open(args.observed) as f:
        observed = json.load(f)["responses"]
    summary = compare(reference, observed)
    print(json.dumps(summary, indent=1))
    return 1 if summary["mismatched"] else 0


if __name__ == "__main__":
    sys.exit(main())
