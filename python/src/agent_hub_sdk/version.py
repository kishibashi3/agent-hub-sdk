"""SDK version.

Held in a tiny module so `pyproject.toml`'s `[tool.hatch.version]` can read it
without importing the rest of the package. Bump on each release tag.
"""

__version__ = "0.9.0"
