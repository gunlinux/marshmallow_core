"""Opt-in Rust acceleration for stock marshmallow.

``marshmallow_core`` installs alongside an unmodified ``marshmallow`` and, when
activated, replaces marshmallow's per-object ``_serialize``/``_deserialize``
loops with a Rust core. It is strictly a speedup: with the core installed,
``dump``/``load`` produce results identical to pure-Python marshmallow, falling
back to the pure-Python path for anything the core does not model (and for
*every* error/edge case on load).

Activate it explicitly::

    import marshmallow_core

    marshmallow_core.install()      # patch marshmallow.Schema in this process
    # ... use marshmallow as usual ...
    marshmallow_core.uninstall()    # restore the stock methods

Set ``MARSHMALLOW_NO_ACCEL=1`` (or a protocol-version mismatch) to make the core
a no-op even when installed.
"""

from __future__ import annotations

from marshmallow_core._compiler import is_available
from marshmallow_core._patch import install, is_installed, uninstall

__all__ = ["install", "is_available", "is_installed", "uninstall"]
__version__ = "0.1.7"
