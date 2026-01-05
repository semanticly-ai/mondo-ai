"""CLI interface for mondo-ai."""

import typer
from typing_extensions import Annotated

app = typer.Typer(help="mondo-ai: AI-powered tools for Mondo disease ontology curation and quality assurance.")


@app.command()
def run(
    name: Annotated[str, typer.Option(help="Name of the person to greet")],
):
    typer.echo(f"Hello, {name}!")

def main():
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
