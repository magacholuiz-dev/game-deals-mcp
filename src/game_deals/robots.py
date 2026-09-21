"""Minimal robots.txt matcher following RFC 9309.

Python's urllib.robotparser does not understand the `*` and `$` wildcards, so a
rule like `Disallow: /buscar*` would silently be treated as a literal path and
never block anything. That is exactly the kind of rule sites use, so we match
the RFC ourselves: longest matching pattern wins, and Allow wins a tie.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote


@dataclass
class Robots:
    # agent token (lowercase) -> list of (is_allow, pattern)
    groups: dict[str, list[tuple[bool, str]]] = field(default_factory=dict)
    allow_all: bool = False       # robots.txt missing or 4xx: RFC says allow
    deny_all: bool = False        # robots.txt unreachable (5xx): RFC says deny

    @classmethod
    def parse(cls, text: str) -> "Robots":
        groups: dict[str, list[tuple[bool, str]]] = {}
        agents: list[str] = []
        last_was_rule = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if last_was_rule:
                    agents = []
                agents.append(value.lower())
                last_was_rule = False
                groups.setdefault(value.lower(), [])
            elif key in ("allow", "disallow"):
                last_was_rule = True
                # An empty Disallow means "allow everything" and adds no rule.
                if value == "":
                    continue
                for a in agents:
                    groups[a].append((key == "allow", value))
        return cls(groups=groups)

    @classmethod
    def unavailable(cls, status: int | None) -> "Robots":
        """4xx (including 404 and 403): no restrictions. 5xx or a network
        failure: assume everything is disallowed until it can be read."""
        if status is not None and 400 <= status < 500:
            return cls(allow_all=True)
        return cls(deny_all=True)

    def _rules_for(self, agent: str) -> list[tuple[bool, str]]:
        token = agent.split("/")[0].strip().lower()
        if token in self.groups:
            return self.groups[token]
        return self.groups.get("*", [])

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        anchored = pattern.endswith("$")
        core = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if c == "*" else re.escape(c) for c in core)
        return re.match(regex + ("$" if anchored else ""), path) is not None

    def allowed(self, agent: str, path: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        path = unquote(path) or "/"
        best_len, best_allow = -1, True
        for is_allow, pattern in self._rules_for(agent):
            if self._matches(pattern, path):
                length = len(pattern)
                if length > best_len or (length == best_len and is_allow):
                    best_len, best_allow = length, is_allow
        return best_allow
