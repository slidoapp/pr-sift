"""User configuration from ~/.config/pr-sift/config.toml.

```toml
[agent]
command = "claude"            # or a path, or another agent CLI
web_fetch = true              # false: no WebFetch at all
web_fetch_domains = []        # e.g. ["docs.python.org", "pypi.org"]
max_parallel = 3
timeout_seconds = 300
```
"""

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class AgentConfig:
    command: list[str] = field(default_factory=lambda: ["claude"])
    web_fetch: bool = True
    web_fetch_domains: list[str] = field(default_factory=list)
    max_parallel: int = 3
    timeout_seconds: int = 300


def config_path() -> Path:
    return Path(os.environ.get("PR_SIFT_CONFIG_DIR", "~/.config/pr-sift")).expanduser() / "config.toml"


def load_agent_config(path: Path | None = None) -> AgentConfig:
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return AgentConfig()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    agent = data.get("agent", {})
    config = AgentConfig()
    if "command" in agent:
        command = agent["command"]
        config.command = shlex.split(command) if isinstance(command, str) else list(command)
        if not config.command:
            raise ConfigError(f"{path}: agent.command is empty")
    for key, kind in (("web_fetch", bool), ("max_parallel", int), ("timeout_seconds", int)):
        if key in agent:
            if not isinstance(agent[key], kind):
                raise ConfigError(f"{path}: agent.{key} must be a {kind.__name__}")
            setattr(config, key, agent[key])
    if "web_fetch_domains" in agent:
        domains = agent["web_fetch_domains"]
        if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
            raise ConfigError(f"{path}: agent.web_fetch_domains must be a list of strings")
        config.web_fetch_domains = domains
    config.max_parallel = max(1, config.max_parallel)
    return config
