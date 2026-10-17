"""WSGI entry point."""

import os

from flask import Flask

from netadmin import create_app


def build_app() -> Flask:
    """Return the configured app, or the add-on setup wizard when unconfigured."""
    try:
        return create_app()
    except RuntimeError:
        if os.getenv("NETADMIN_ADDON") == "1":
            from netadmin.setup import create_setup_app  # pylint: disable=import-outside-toplevel

            return create_setup_app()
        raise


app = build_app()


def main() -> None:
    """Run the local development server."""
    # The server must listen on every interface inside its container.
    app.run(host="0.0.0.0", port=8099)  # noqa
