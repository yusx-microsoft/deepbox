"""Explicit runtime extension selection; not a dynamic plugin loader."""
from agentbridge.integrations.deeporca.contract import RUNTIME_ID

from .policy import InputRejected, RuntimePolicy
from .deeporca import DeepOrcaPolicy

_DEFAULT = RuntimePolicy()
_DEEPORCA = DeepOrcaPolicy()


def runtime_policy(runtime: str | None) -> RuntimePolicy:
    return _DEEPORCA if runtime == RUNTIME_ID else _DEFAULT


__all__ = ["InputRejected", "RuntimePolicy", "runtime_policy"]
