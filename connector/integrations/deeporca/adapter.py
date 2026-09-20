"""Platform-facing adapter declaration for the embedded DeepOrca runtime."""
from .probe import probe


def create_adapter():
    # Keep direct imports of this module independent of registry initialization.
    from ...runtimes import RuntimeAdapter

    return RuntimeAdapter(
        id="deeporca", label="DeepOrca", base_argv=(),
        family="deeporca", surface="structured", default_surface=True,
        structured=True, backend="python-library", renderer="deeporca-chat-v1",
        version_argv=(), allow_custom_models=True, capability_probe=probe,
    )
