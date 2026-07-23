"""Message routing based on @mentions with per-channel loop guard."""

import re


class Router:
    def __init__(self, agent_names: list[str], default_mention: str = "both",
                 max_hops: int = 4, online_checker=None,
                 mention_aliases: dict[str, str | list[str]] | None = None,
                 guard_enabled: bool = True):
        self.agent_names = set(n.lower() for n in agent_names)
        self.default_mention = default_mention
        self.max_hops = max_hops
        self.guard_enabled = guard_enabled
        self._online_checker = online_checker  # callable() -> set of online agent names
        self._mention_aliases: dict[str, set[str]] = {}
        self._set_mention_aliases(mention_aliases or {})
        # Per-channel state: { channel: { hop_count, paused, guard_emitted } }
        self._channels: dict[str, dict] = {}
        self._build_pattern()

    def _get_ch(self, channel: str) -> dict:
        if channel not in self._channels:
            self._channels[channel] = {
                "hop_count": 0,
                "paused": False,
                "guard_emitted": False,
            }
        return self._channels[channel]

    def _build_pattern(self):
        # Sort longest-first so "gemini-2" is tried before "gemini"
        mention_names = self.agent_names | set(self._mention_aliases)
        names = [re.escape(n) for n in sorted(mention_names, key=len, reverse=True)]
        alternatives = "|".join(names + ["both", "all"])
        self._mention_re = re.compile(
            rf"@({alternatives})(?![\w-])", re.IGNORECASE
        )

    def _set_mention_aliases(self, aliases: dict[str, str | list[str]]):
        """Normalize display-name aliases to canonical routing names."""
        normalized: dict[str, set[str]] = {}
        for alias, raw_targets in aliases.items():
            key = alias.strip().lower()
            if not key or key in ("all", "both"):
                continue
            targets = [raw_targets] if isinstance(raw_targets, str) else raw_targets
            valid = {
                target.strip().lower()
                for target in targets
                if isinstance(target, str) and target.strip().lower() in self.agent_names
            }
            if valid:
                normalized[key] = valid
        self._mention_aliases = normalized

    def parse_mentions(self, text: str) -> list[str]:
        mentions = set()
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name in ("both", "all"):
                # Only tag online agents when using @all
                if self._online_checker:
                    online = self._online_checker()
                    mentions.update(n for n in self.agent_names if n in online)
                else:
                    mentions.update(self.agent_names)
            elif name in self.agent_names:
                # Canonical names always win if a display label collides.
                mentions.add(name)
            else:
                mentions.update(self._mention_aliases.get(name, ()))
        return list(mentions)

    def _is_agent(self, sender: str) -> bool:
        return sender.lower() in self.agent_names

    def get_targets(self, sender: str, text: str, channel: str = "general") -> list[str]:
        """Determine which agents should receive this message."""
        ch = self._get_ch(channel)
        mentions = self.parse_mentions(text)

        if not self._is_agent(sender):
            # Human message resets hop counter and unpauses
            ch["hop_count"] = 0
            ch["paused"] = False
            ch["guard_emitted"] = False
            if not mentions:
                if self.default_mention in ("both", "all"):
                    return list(self.agent_names)
                elif self.default_mention == "none":
                    return []
                return [self.default_mention]
            return mentions
        else:
            # Agent message: blocked while loop guard is active
            if ch["paused"]:
                return []
            # Only route if explicit @mention
            if not mentions:
                return []
            if self._register_hop(ch):
                return []
            # Don't route back to self
            return [m for m in mentions if m != sender]

    def _register_hop(self, ch: dict) -> bool:
        """Count an agent-to-agent hop and pause the channel if the loop
        guard trips. Returns True when routing should be blocked. When the
        guard is disabled, hops are never counted so chatter can't auto-pause.
        """
        if not self.guard_enabled:
            return False
        ch["hop_count"] += 1
        if ch["hop_count"] > self.max_hops:
            ch["paused"] = True
            return True
        return False

    def continue_routing(self, channel: str = "general"):
        """Resume after loop guard pause."""
        ch = self._get_ch(channel)
        ch["hop_count"] = 0
        ch["paused"] = False
        ch["guard_emitted"] = False

    def is_paused(self, channel: str = "general") -> bool:
        return self._get_ch(channel)["paused"]

    def is_guard_emitted(self, channel: str = "general") -> bool:
        return self._get_ch(channel)["guard_emitted"]

    def set_guard_emitted(self, channel: str = "general"):
        self._get_ch(channel)["guard_emitted"] = True

    def update_agents(self, names: list[str],
                      mention_aliases: dict[str, str | list[str]] | None = None):
        """Replace routable names/aliases and rebuild the mention regex."""
        self.agent_names = set(n.lower() for n in names)
        self._set_mention_aliases(mention_aliases or {})
        self._build_pattern()
