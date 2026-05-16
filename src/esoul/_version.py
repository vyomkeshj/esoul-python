"""Single source of truth for the package version.

`pyproject.toml` reads this via `tool.hatch.version.path`, so bumping here
is the only step required to cut a release (combined with a `CHANGELOG.md`
entry + git tag).
"""

__version__ = "0.2.0"
