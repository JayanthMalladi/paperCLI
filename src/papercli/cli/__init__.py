"""Command-line interface: the ``papers`` console entry point.

The CLI is a thin machine-readable layer. Every command emits exactly one
JSON envelope on stdout (except the human-readable ``--help``/``--version``
exceptions); diagnostics go to stderr. See :mod:`papercli.cli.main`.
"""
