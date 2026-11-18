"""Configuration and secrets handling.

Secrets are stored **only** as environment-variable references in committed
config; their values live in the environment (or a local git-ignored ``.env``)
and are never written to committed files. See :mod:`papercli.config.loader`.
"""
