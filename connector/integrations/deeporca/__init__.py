"""DeepOrca library integration: local bindings, isolated workers and sessions.

Importing this package never imports the optional native SDK. Worker entrypoints
remain module-level functions in ``worker`` for multiprocessing spawn.
"""
