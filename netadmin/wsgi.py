"""WSGI entry point."""

from netadmin import create_app

app = create_app()


def main() -> None:
    """Run the local development server."""
    app.run(host="0.0.0.0", port=8099)  # noqa: S104
