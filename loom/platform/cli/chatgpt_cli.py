"""ChatGPT account onboarding, model discovery, and explicit live verification."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import click

from loom.core.cognition.chatgpt_auth import ChatGPTAuth, ChatGPTAuthError


@click.group("chatgpt", invoke_without_command=True)
@click.pass_context
def chatgpt(ctx: click.Context) -> None:
    """Official Sign in with ChatGPT; use --help for models and verification."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(login)


@chatgpt.command()
@click.option("--account", default=None, help="Saved account ID or label to reauthorize.")
@click.option("--new-account", is_flag=True, help="Register a different ChatGPT account/workspace.")
@click.option("--label", default=None, help="Local label for this account registration.")
@click.option("--no-browser", is_flag=True, help="Print the login URL without opening a browser.")
@click.option("--timeout", default=180.0, type=click.FloatRange(min=1), show_default=True)
def login(account: str | None, new_account: bool, label: str | None, no_browser: bool, timeout: float) -> None:
    """Continue with ChatGPT and authorize Loom to use your plan."""
    try:
        auth = ChatGPTAuth()
        result = auth.login(account=account, new_account=new_account, label=label,
                            open_browser=not no_browser, timeout=timeout, on_url=click.echo)
        click.echo(f"Connected account: {result['email'] or result['account']}")
        if not result["plan_usage"]:
            click.echo("Signed in, but ChatGPT plan usage was not granted. Inference is disabled.")
            return
        models = auth.catalog(refresh=True)
        click.echo(f"ChatGPT plan usage enabled. {len(models)} models listed for this account.")
        click.echo("Run `loom auth chatgpt models`, then `loom auth chatgpt verify --model SLUG`.")
    except ChatGPTAuthError as exc:
        raise click.ClickException(str(exc)) from exc


@chatgpt.command("accounts")
def accounts() -> None:
    """Show saved accounts without exposing credentials."""
    try:
        click.echo(json.dumps(ChatGPTAuth().accounts(), indent=2, ensure_ascii=False))
    except ChatGPTAuthError as exc:
        raise click.ClickException(str(exc)) from exc


@chatgpt.command("use")
@click.argument("account")
def use_account(account: str) -> None:
    """Select a saved account for subsequent ChatGPT requests."""
    try:
        ChatGPTAuth().select(account)
        click.echo("ChatGPT account selected. Model discovery follows the selected account.")
    except ChatGPTAuthError as exc:
        raise click.ClickException(str(exc)) from exc


@chatgpt.command()
@click.option("--account", default=None)
def logout(account: str | None) -> None:
    """Revoke the session and clear local tokens; retain the client registration."""
    try:
        confirmed = ChatGPTAuth().logout(account)
        click.echo("Signed out locally. " + ("Remote session revoked." if confirmed else
                   "Remote revocation unconfirmed; disconnect Loom in ChatGPT Settings."))
    except ChatGPTAuthError as exc:
        raise click.ClickException(str(exc)) from exc


@chatgpt.command()
@click.option("--account", default=None)
def models(account: str | None) -> None:
    """Fetch the selected account's live model catalog and advertised modalities."""
    from loom.core.cognition.chatgpt_capabilities import describe_model
    try:
        entries = ChatGPTAuth().catalog(account, refresh=True)
        click.echo(json.dumps([describe_model(m) for m in entries], indent=2, ensure_ascii=False))
    except ChatGPTAuthError as exc:
        raise click.ClickException(str(exc)) from exc


@chatgpt.command()
@click.option("--model", "selected_models", multiple=True, help="Model slug to test; repeat for several models.")
@click.option("--all-models", is_flag=True, help="Test every listed model (consumes shared plan usage).")
@click.option("--account", default=None)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
def verify(selected_models: tuple[str, ...], all_models: bool, account: str | None, output: Path | None) -> None:
    """Use plan allowance to test text, image understanding and a harmless tool loop.

    Audio is recorded as unsupported by this route; it is not probed against
    another billing path. Stops on an error instead of fanning out quota failures.
    """
    from loom.core.cognition.chatgpt_capabilities import ModelVerificationError, verify_model
    from loom.core.cognition.chatgpt_provider import ChatGPTResponsesProvider
    from loom.core.cognition.responses_sse import ResponsesProviderError
    auth = ChatGPTAuth()
    try:
        account = account or auth.active_account()
        entries = auth.catalog(account, refresh=True)
        slugs = [m["slug"] for m in entries]
        selected = slugs if all_models else [m.removeprefix("chatgpt/") for m in selected_models]
        if not selected:
            raise click.ClickException("Choose --model SLUG or --all-models after reviewing `models`.")
        if any(m not in slugs for m in selected):
            raise click.ClickException("A selected model is absent from this account's current catalog.")
        report = {"account": account, "checked_at": datetime.now(timezone.utc).isoformat(),
                  "requested_models": selected, "models": []}
        failure = None
        for model in selected:
            click.echo(f"Verifying {model}: text, image, tool call and replay…", err=True)
            try:
                provider = ChatGPTResponsesProvider(model=f"chatgpt/{model}", auth=auth, account=account)
                result = asyncio.run(verify_model(provider))
            except (ChatGPTAuthError, ResponsesProviderError, ModelVerificationError) as exc:
                result = exc.result if isinstance(exc, ModelVerificationError) else {
                          "model": model, "verification": "failed", "error": str(exc),
                          "audio_input": "unsupported_by_route", "audio_output": "unsupported_by_route"}
                failure = exc
            report["models"].append(result)
            if output:
                output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if failure:
                break
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if output:
            output.write_text(rendered + "\n", encoding="utf-8")
        click.echo(rendered)
        if failure:
            raise click.ClickException("Verification stopped on a provider error; see the report.")
        if any(any(m.get(k) not in ("verified", "unsupported") for k in ("text", "image_input", "tools"))
               for m in report["models"]):
            raise click.ClickException("Some checks were inconclusive; see the report.")
    except ChatGPTAuthError as exc:
        raise click.ClickException(str(exc)) from exc
