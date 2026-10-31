"""Load probie.toml. Unknown keys are errors, so a typo can't silently change a run."""

from __future__ import annotations

import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field, fields
from pathlib import Path

PROVIDERS = ("openai-compatible", "offline")
INVOICE_INPUTS = ("fields", "document")
VOICEMAIL_INPUTS = ("script", "transcript", "audio")
# Which free-credit pool a model draws on, from its host. credit_pool in the config overrides this.
POOL_HOSTS = (("meta.ai", "meta"), ("baseten.co", "baseten"), ("fireworks.ai", "fireworks"), ("modal.run", "modal"))
MODES = ("agent", "prompt")
OFFLINE_PROFILES = ("careful", "quick", "sloppy")


class ConfigError(ValueError):
    pass


@dataclass
class ModelSpec:
    name: str
    model: str
    provider: str = "openai-compatible"
    base_url: str | None = None
    api_key_env: str | None = None
    mode: str = "agent"
    note: str = ""
    max_tokens: int = 4096
    max_tokens_param: str = "max_tokens"
    price_input: float | None = None
    price_cached_input: float | None = None
    price_output: float | None = None
    price_per_hour: float | None = None
    rpm: float | None = None
    concurrency: int | None = None
    timeout: float = 180.0
    json_mode: bool = False
    include_in_report: bool = True
    offline_profile: str | None = None
    invoice_input: str = "fields"
    voicemail_input: str = "script"
    credit_pool: str | None = None
    params: dict = field(default_factory=dict)

    @property
    def offline(self) -> bool:
        return self.provider == "offline"

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def pool(self) -> str | None:
        if self.credit_pool:
            return self.credit_pool
        if self.offline or not self.base_url:
            return None
        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower()
        for suffix, name in POOL_HOSTS:
            if host == suffix or host.endswith("." + suffix):
                return name
        return None

    def priced(self) -> bool:
        return self.price_per_hour is not None or (self.price_input is not None and self.price_output is not None)


@dataclass
class RunSettings:
    cases: Path = Path("data/cases.jsonl")
    runs_dir: Path = Path("runs")
    cache: Path = Path("runs/cache.sqlite")
    documents: Path = Path("data/documents")
    voicemails: Path = Path("data/voicemails")
    concurrency: int = 4
    budget_usd: float = 5.0
    max_steps: int = 8


@dataclass
class VerdictSettings:
    min_agreement: float = 0.90
    max_unsafe_rate: float = 0.05


@dataclass
class Config:
    run: RunSettings
    verdict: VerdictSettings
    models: list[ModelSpec]
    path: Path | None = None
    credits: dict[str, float] = field(default_factory=dict)
    credit_margin: float = 0.05
    credit_total: float | None = None
    game: dict = field(default_factory=dict)

    def model(self, name: str) -> ModelSpec:
        for spec in self.models:
            if spec.name == name:
                return spec
        names = ", ".join(spec.name for spec in self.models) or "none"
        raise ConfigError(f"No model called {name!r} in the config. Models: {names}")


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NUMBER = (int, float)


def _check_keys(section: str, data: dict, allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"[{section}] has unknown key(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}")


def _number(section: str, key: str, value: object, *, minimum: float = 0.0, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, _NUMBER):
        raise ConfigError(f"[{section}] {key} must be a number")
    if value < minimum or (not allow_zero and value == 0):
        raise ConfigError(f"[{section}] {key} must be {'more than' if not allow_zero else 'at least'} {minimum}")
    return float(value)


def _integer(section: str, key: str, value: object, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"[{section}] {key} must be a whole number of at least {minimum}")
    return value


def _model(index: int, data: dict) -> ModelSpec:
    section = f"model {index + 1}"
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] must be a table")
    allowed = {f.name for f in fields(ModelSpec)}
    _check_keys(section, data, allowed)
    for key in ("name", "model"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ConfigError(f"[{section}] needs a {key}")
    name = data["name"]
    if not _NAME.match(name):
        raise ConfigError(f"[{section}] name {name!r} may only use letters, digits, '.', '_' and '-'")
    section = f"model {name}"
    spec = ModelSpec(name=name, model=data["model"])
    for key in ("provider", "base_url", "api_key_env", "mode", "note", "max_tokens_param", "offline_profile",
                "invoice_input", "voicemail_input", "credit_pool"):
        if key in data:
            if not isinstance(data[key], str):
                raise ConfigError(f"[{section}] {key} must be text")
            setattr(spec, key, data[key])
    for key in ("price_input", "price_cached_input", "price_output", "price_per_hour"):
        if key in data:
            setattr(spec, key, _number(section, key, data[key]))
    if "rpm" in data:
        spec.rpm = _number(section, "rpm", data["rpm"], allow_zero=False)
    if "timeout" in data:
        spec.timeout = _number(section, "timeout", data["timeout"], minimum=1.0)
    if "max_tokens" in data:
        spec.max_tokens = _integer(section, "max_tokens", data["max_tokens"], 16)
    if "concurrency" in data:
        spec.concurrency = _integer(section, "concurrency", data["concurrency"])
    for key in ("json_mode", "include_in_report"):
        if key in data:
            if not isinstance(data[key], bool):
                raise ConfigError(f"[{section}] {key} must be true or false")
            setattr(spec, key, data[key])
    if "params" in data:
        if not isinstance(data["params"], dict):
            raise ConfigError(f"[{section}] params must be a table")
        reserved = {"model", "messages", "tools", "tool_choice"} & set(data["params"])
        if reserved:
            raise ConfigError(f"[{section}] params can't set {', '.join(sorted(reserved))}")
        spec.params = dict(data["params"])

    if spec.provider not in PROVIDERS:
        raise ConfigError(f"[{section}] provider must be one of: {', '.join(PROVIDERS)}")
    if spec.mode not in MODES:
        raise ConfigError(f"[{section}] mode must be agent or prompt")
    if spec.invoice_input not in INVOICE_INPUTS:
        raise ConfigError(f"[{section}] invoice_input must be fields or document")
    if spec.voicemail_input not in VOICEMAIL_INPUTS:
        raise ConfigError(f"[{section}] voicemail_input must be script, transcript or audio")
    if spec.provider == "openai-compatible":
        if not spec.base_url or not spec.base_url.startswith(("https://", "http://")):
            raise ConfigError(f"[{section}] needs a base_url starting with https://")
    else:
        if spec.offline_profile not in OFFLINE_PROFILES:
            raise ConfigError(f"[{section}] offline models need offline_profile: {', '.join(OFFLINE_PROFILES)}")
    if spec.price_per_hour is not None and (spec.price_input is not None or spec.price_output is not None):
        raise ConfigError(f"[{section}] use either per-token prices or price_per_hour, not both")
    return spec


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"No config at {path}.")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} isn't valid TOML: {exc}") from None
    return config_from_dict(data, base_dir=path.parent, path=path)


def config_from_dict(data: dict, *, base_dir: Path = Path("."), path: Path | None = None) -> Config:
    _check_keys("top level", data, {"run", "verdict", "model", "credits", "game"})
    run_data = data.get("run", {})
    _check_keys("run", run_data, {"cases", "runs_dir", "cache", "documents", "voicemails", "concurrency", "budget_usd",
                                  "max_steps"})
    run = RunSettings()
    for key in ("cases", "runs_dir", "cache", "documents", "voicemails"):
        if key in run_data:
            if not isinstance(run_data[key], str) or not run_data[key]:
                raise ConfigError(f"[run] {key} must be a path")
            setattr(run, key, Path(run_data[key]))
    if "concurrency" in run_data:
        run.concurrency = _integer("run", "concurrency", run_data["concurrency"])
    if "budget_usd" in run_data:
        run.budget_usd = _number("run", "budget_usd", run_data["budget_usd"], allow_zero=False)
    if "max_steps" in run_data:
        run.max_steps = _integer("run", "max_steps", run_data["max_steps"], 2)
    for key in ("cases", "runs_dir", "cache", "documents", "voicemails"):
        value = getattr(run, key)
        if not value.is_absolute():
            setattr(run, key, base_dir / value)

    verdict_data = data.get("verdict", {})
    _check_keys("verdict", verdict_data, {"min_agreement", "max_unsafe_rate"})
    verdict = VerdictSettings()
    for key in ("min_agreement", "max_unsafe_rate"):
        if key in verdict_data:
            value = _number("verdict", key, verdict_data[key])
            if value > 1:
                raise ConfigError(f"[verdict] {key} is a share between 0 and 1")
            setattr(verdict, key, value)

    credits_data = data.get("credits", {})
    if not isinstance(credits_data, dict):
        raise ConfigError("[credits] must be a table of pool = USD")
    credits: dict[str, float] = {}
    margin = 0.05
    total = None
    for key, value in credits_data.items():
        if key == "total":
            total = _number("credits", "total", value, allow_zero=False)
            continue
        if key == "margin":
            margin = _number("credits", "margin", value)
            if margin >= 0.5:
                raise ConfigError("[credits] margin is the share held back, below 0.5")
            continue
        if not _NAME.match(key):
            raise ConfigError(f"[credits] {key!r} isn't a valid pool name")
        credits[key] = _number("credits", key, value)

    raw_models = data.get("model", [])
    if not isinstance(raw_models, list):
        raise ConfigError("Models go in [[model]] tables")
    models = [_model(i, m) for i, m in enumerate(raw_models)]
    names = [m.name for m in models]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"Model names must be unique: {', '.join(duplicates)}")
    game_data = data.get("game", {})
    if not isinstance(game_data, dict):
        raise ConfigError("[game] must be a table")
    _check_keys("game", game_data, {"models", "cap_usd", "per_hour", "per_day", "boss", "boss_cap_usd"})
    game: dict = {}
    if "models" in game_data:
        names = game_data["models"]
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ConfigError("[game] models must be a list of model names")
        unknown = [n for n in names if n not in {m.name for m in models}]
        if unknown:
            raise ConfigError(f"[game] models names models that aren't in the config: {', '.join(unknown)}")
        game["models"] = list(names)
    if "boss" in game_data:
        if not isinstance(game_data["boss"], str) or game_data["boss"] not in {m.name for m in models}:
            raise ConfigError("[game] boss must name a model in the config")
        game["boss"] = game_data["boss"]
    for key in ("cap_usd", "boss_cap_usd"):
        if key in game_data:
            game[key] = _number("game", key, game_data[key], allow_zero=False)
    for key in ("per_hour", "per_day"):
        if key in game_data:
            game[key] = _integer("game", key, game_data[key])
    return Config(run=run, verdict=verdict, models=models, path=path, credits=credits, credit_margin=margin,
                  credit_total=total, game=game)


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Load KEY=VALUE lines into the environment without overriding what's already set.

    Returns the names that were set.
    """
    path = Path(path)
    if not path.exists():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
