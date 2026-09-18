"""Leakage scanning for agent-visible payloads and exports.

The scanner knows a pack's private vocabulary (canonical asset/pool keys, addresses,
transaction ids, wallet ids, pack id, title, real dates, file paths) and rejects any
payload that contains one of them. It is applied to every agent-plane envelope and
to role-redacted exports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..datasets.pack import Pack

_DATE_RE = re.compile(r"\b(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_PATH_RE = re.compile(r"(?:/home/|/Users/|/tmp/|/data/|[A-Za-z]:\\)[^\s\"']*")
_EPOCH_MS_RE = re.compile(r"\b1[6-9]\d{11}\b")  # 13-digit epoch milliseconds in 2020..2033


@dataclass(slots=True)
class LeakFinding:
    kind: str
    value: str
    path: str


@dataclass(slots=True)
class LeakScanner:
    private_terms: set[str] = field(default_factory=set)
    allow_dates: bool = False

    @classmethod
    def for_pack(cls, pack: Pack, allow_dates: bool = False) -> LeakScanner:
        terms: set[str] = set()
        m = pack.manifest
        terms.add(m.pack_id)
        terms.add(m.title_private)
        terms.add(str(pack.path))
        for a in pack.assets.values():
            terms.add(a.key)
            if not a.is_numeraire:
                terms.add(a.address)
                if a.name:
                    terms.add(a.name)
        for p in pack.pools.values():
            terms.add(p.key)
            terms.add(p.address)
        for r in pack.tape[:20000]:
            if r.get("tx"):
                terms.add(str(r["tx"]))
            if r.get("wallet"):
                terms.add(str(r["wallet"]))
        terms.discard("")
        return cls(private_terms=terms, allow_dates=allow_dates)

    def scan(self, payload: Any) -> list[LeakFinding]:
        findings: list[LeakFinding] = []
        self._walk(payload, "$", findings)
        return findings

    def _walk(self, node: Any, path: str, out: list[LeakFinding], key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                self._check_str(str(k), f"{path}.{k}", out)
                self._walk(v, f"{path}.{k}", out, key=str(k))
        elif isinstance(node, list | tuple):
            for i, v in enumerate(node):
                self._walk(v, f"{path}[{i}]", out, key=key)
        elif isinstance(node, str):
            self._check_str(node, path, out)
            if self._time_key(key) and _EPOCH_MS_RE.fullmatch(node) and not self.allow_dates:
                out.append(LeakFinding("absolute_epoch_ms", node, path))
        elif isinstance(node, int) and not isinstance(node, bool):
            # Only time-like keys are checked so large raw token quantities are never mistaken for timestamps.
            if self._time_key(key) and _EPOCH_MS_RE.fullmatch(str(node)) and not self.allow_dates:
                out.append(LeakFinding("absolute_epoch_ms", str(node), path))

    @staticmethod
    def _time_key(key: str) -> bool:
        k = key.lower()
        return any(s in k for s in ("utc", "date", "timestamp", "time_ms", "_at", "_ms"))

    def _check_str(self, s: str, path: str, out: list[LeakFinding]) -> None:
        for term in self.private_terms:
            if len(term) >= 4 and term in s:
                out.append(LeakFinding("private_term", term, path))
        if _ADDR_RE.search(s):
            out.append(LeakFinding("address", _ADDR_RE.search(s).group(0), path))  # type: ignore[union-attr]
        if _HASH_RE.search(s):
            out.append(LeakFinding("tx_hash", _HASH_RE.search(s).group(0), path))  # type: ignore[union-attr]
        if _PATH_RE.search(s):
            out.append(LeakFinding("filesystem_path", _PATH_RE.search(s).group(0), path))  # type: ignore[union-attr]
        if not self.allow_dates and _DATE_RE.search(s):
            out.append(LeakFinding("calendar_date", _DATE_RE.search(s).group(0), path))  # type: ignore[union-attr]

    def assert_clean(self, payload: Any) -> None:
        f = self.scan(payload)
        if f:
            raise LeakDetected(f)


class LeakDetected(RuntimeError):
    def __init__(self, findings: list[LeakFinding]) -> None:
        self.findings = findings
        super().__init__(f"{len(findings)} leakage finding(s): " + "; ".join(f"{x.kind}@{x.path}" for x in findings[:5]))


_PATH_TOKEN_RE = re.compile(r"\.([^.\[\]]+)|\[(\d+)\]")


def _redact_at(data: Any, finding: LeakFinding) -> bool:
    """Replace the leaking value at the finding's path in place. Returns False when the path cannot be
    resolved (a key containing a dot, for instance), so the caller can fall back."""
    tokens: list[str | int] = [int(i) if i else k for k, i in _PATH_TOKEN_RE.findall(finding.path)]
    if not tokens:
        return False
    node = data
    for t in tokens[:-1]:
        try:
            node = node[t]
        except (KeyError, IndexError, TypeError):
            return False
    last = tokens[-1]
    try:
        if isinstance(node, dict) and isinstance(last, str):
            if last not in node:
                return False
            if finding.value in last and not isinstance(node[last], str | int):
                node["[redacted]"] = node.pop(last)  # the key itself leaked
                return True
            cur = node[last]
            node[last] = cur.replace(finding.value, "[redacted]") if isinstance(cur, str) else "[redacted]"
            if finding.value in last:
                node["[redacted]"] = node.pop(last)
            return True
        if isinstance(node, list) and isinstance(last, int) and last < len(node):
            cur = node[last]
            node[last] = cur.replace(finding.value, "[redacted]") if isinstance(cur, str) else "[redacted]"
            return True
    except (AttributeError, TypeError):
        return False
    return False


def redact_for_role(obj: Any, role: str, scanner: LeakScanner | None) -> Any:
    """Role-based redaction for exports: 'admin' sees everything; 'participant' gets the scanned public view.

    Findings are replaced at their path in the parsed object, never by text substitution: a bare
    number (an epoch timestamp) replaced inside the JSON text would leave the document unparsable."""
    if role == "admin":
        return obj
    data = json.loads(json.dumps(obj, sort_keys=True, default=str))
    if scanner is None:
        return data
    for _ in range(3):  # a replacement can expose nothing new, but a key rename changes paths: re-scan
        findings = scanner.scan(data)
        if not findings:
            break
        for f in findings:
            if not _redact_at(data, f):
                # Path not resolvable: fall back to replacing the value wherever it appears, as a string.
                text = json.dumps(data, sort_keys=True)
                quoted = json.dumps(f.value)
                text = text.replace(quoted, json.dumps("[redacted]")).replace(quoted[1:-1], "[redacted]")
                data = json.loads(text)
    return data
