from collections.abc import Callable, Sequence
from typing import Any

from textual.app import App
from textual.screen import Screen

from stage.tui.context import database_of
from stage.tui.safe import counted, told


async def tell_if_gone(
    app: App[object], job_ids: Sequence[str], after: Callable[[], object] | None = None
) -> None:
    database = database_of(app)
    if database is None or not job_ids:
        return
    from stage.services.liveness import check_opened, gone_notice

    result = await check_opened(database, job_ids)
    if result.gone:
        told(app, gone_notice(result.gone), "warning")
        if after is not None:
            after()


def check_in_background(
    app: App[object], job_ids: Sequence[str], after: Callable[[], object] | None = None
) -> None:
    if job_ids:
        app.run_worker(tell_if_gone(app, tuple(job_ids), after), group="liveness")


def open_postings(
    screen: Screen[Any],
    links: Sequence[tuple[str, str, str]],
    after: Callable[[], object] | None = None,
) -> None:
    import webbrowser

    from stage.domain import web_url

    launched: list[str] = []
    refused = 0
    for job_id, raw, _ in links:
        url = web_url(raw)
        if url is None:
            refused += 1
            continue
        if not webbrowser.open(url):
            told(screen, "No browser available on this machine.", "warning")
            check_in_background(screen.app, launched, after)
            return
        launched.append(job_id)
    check_in_background(screen.app, launched, after)
    if refused:
        they = "it was" if refused == 1 else "they were"
        told(
            screen,
            f"{counted(refused, 'posting')} had an apply link that is not a plain "
            f"http or https address, so {they} not opened.",
            "error",
        )
    if launched:
        named = links[0][2] if len(links) == 1 else counted(len(launched), "posting")
        told(screen, f"Opened {named}", subject=tuple(launched))


__all__ = ["check_in_background", "open_postings", "tell_if_gone"]
