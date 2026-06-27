"""Specialized request routes (weather, anime, news) for the research service.

These were extracted from :class:`magpie.service.ResearchService` for file
organization. They are standalone functions that receive the service as their
first argument and call back into its private helpers exactly as the inlined
methods did. This is a pure relocation; no behavior changed.
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING

from .errors import AnimeError, NewsError, ResearchCancelled, WeatherError
from .models import (
    AnimeReport,
    AnimeRequestKind,
    NewsRequestKind,
    ResearchRequest,
    ResearchResult,
    RequestRoute,
    StopReason,
    WeatherKind,
)

if TYPE_CHECKING:
    from .service import ResearchService


def try_specialized_route(
    service: ResearchService,
    run_id: str,
    request: ResearchRequest,
    timings: dict[str, list[float]],
    warnings: list[str],
) -> ResearchResult | None:
    if service.weather_client is None and service.anime_client is None and service.news_client is None:
        return None
    try:
        service._set_stage(run_id, "route")
        decision, elapsed = service._call_resolver("route_request", request.question)
        service._record_timing(timings, "resolver.route_request", elapsed)
        service._trace(run_id, "REQUEST ROUTED", [
            f"route: {decision.route.value}",
            f"weather_kind: {decision.weather_kind.value if decision.weather_kind else ''}",
            f"zip_code: {decision.zip_code or ''}",
            f"elapsed_ms: {elapsed}",
        ])
    except Exception as exc:  # noqa: BLE001
        service._record_operation_error(run_id, "resolver", "route_request", exc)
        warnings.append(f"Request routing failed; used web research instead: {exc}")
        service._trace(run_id, "REQUEST ROUTING FALLBACK", [f"error: {exc}"])
        service._select_route(run_id, RequestRoute.WEB_RESEARCH.value, str(exc))
        return None
    service._select_route(run_id, decision.route.value)
    if decision.route == RequestRoute.ANIME and service.anime_client is not None:
        return try_anime_route(service, run_id, request, timings, warnings)
    if decision.route == RequestRoute.NEWS and service.news_client is not None:
        return try_news_route(service, run_id, request, timings, warnings)
    if decision.route != RequestRoute.WEATHER or service.weather_client is None:
        return None
    if not decision.zip_code:
        warnings.append("Weather route could not determine a US ZIP code; used web research instead.")
        service._select_route(
            run_id, RequestRoute.WEB_RESEARCH.value, "weather_zip_code_unavailable"
        )
        return None

    started = perf_counter()
    try:
        service._set_stage(run_id, "weather")
        report = service.weather_client.get_weather(
            decision.zip_code, decision.weather_kind or WeatherKind.CONDITIONS
        )
    except WeatherError as exc:
        service._record_operation_error(run_id, "weather", "get_weather", exc)
        warnings.append(f"Specialized weather lookup failed; used web research instead: {exc}")
        service._trace(run_id, "WEATHER ROUTE FALLBACK", [f"error: {exc}"])
        service._select_route(run_id, RequestRoute.WEB_RESEARCH.value, str(exc))
        return None
    elapsed = round((perf_counter() - started) * 1000, 2)
    service._record_timing(timings, "weather", elapsed)
    references = [report.reference][: max(0, request.max_references)]
    service._finalize_storage(
        run_id, report.summary, report.answer, references, "completed",
        StopReason.SPECIALIZED_ROUTE,
    )
    service._record_specialized_source(run_id, report.reference, "neonhail", elapsed)
    service._record_run_finished(
        run_id,
        "research.run.completed",
        "ok",
        StopReason.SPECIALIZED_ROUTE,
        timings,
        reference_ids=[item.source_id for item in references],
    )
    service._trace(run_id, "COMPLETED", ["status: ok", "route: weather"])
    return ResearchResult(
        status="ok",
        run_id=run_id,
        summary=report.summary,
        answer=report.answer,
        references=references,
        warnings=warnings,
        stop_reason=StopReason.SPECIALIZED_ROUTE,
        debug=service._build_debug(run_id, request, timings),
    )


def try_anime_route(
    service: ResearchService,
    run_id: str,
    request: ResearchRequest,
    timings: dict[str, list[float]],
    warnings: list[str],
) -> ResearchResult | None:
    try:
        service._set_stage(run_id, "anime")
        anime_request, elapsed = service._call_resolver("classify_anime_request", request.question)
        service._record_timing(timings, "resolver.classify_anime_request", elapsed)
        service._trace(run_id, "ANIME REQUEST CLASSIFIED", [
            f"kind: {anime_request.kind.value}",
            f"title_query: {anime_request.title_query or ''}",
            f"character_query: {anime_request.character_query or ''}",
            f"requested_fields: {', '.join(item.value for item in anime_request.requested_fields)}",
            f"elapsed_ms: {elapsed}",
        ])
        started = perf_counter()
        if anime_request.kind == AnimeRequestKind.SCHEDULE:
            report = service.anime_client.get_daily_schedule()
        else:
            if not anime_request.title_query:
                raise AnimeError("Anime title could not be determined.")
            candidates = service.anime_client.search_anime(anime_request.title_query)
            if not candidates:
                refined_queries, elapsed = service._call_resolver(
                    "refine_anime_title_queries", request.question, anime_request.title_query
                )
                service._record_timing(timings, "resolver.refine_anime_title_queries", elapsed)
                for refined_query in refined_queries:
                    if refined_query == anime_request.title_query:
                        continue
                    candidates = service.anime_client.search_anime(refined_query)
                    if candidates:
                        break
            if len(candidates) == 1:
                selected_id = candidates[0].anime_id
            else:
                selected_id, elapsed = service._call_resolver(
                    "select_anime_candidate", request.question, candidates
                )
                service._record_timing(timings, "resolver.select_anime_candidate", elapsed)
            if selected_id is None:
                raise AnimeError("No AniList title candidate matched the request.")
            if anime_request.kind == AnimeRequestKind.LOOKUP:
                report = service.anime_client.get_anime_info(selected_id, anime_request.requested_fields)
            else:
                title, credits, reference = service.anime_client.get_credits(selected_id)
                if anime_request.character_query:
                    character_name, elapsed = service._call_resolver(
                        "select_character", anime_request.character_query, credits
                    )
                    service._record_timing(timings, "resolver.select_anime_character", elapsed)
                    credit = next(
                        (item for item in credits if item.character_name == character_name), None
                    )
                    if credit is None:
                        raise AnimeError("No character matched the requested name.")
                    answer = (
                        f"{credit.character_name} in {title} is voiced in Japanese by "
                        f"{', '.join(credit.voice_actor_names)}."
                    )
                else:
                    answer = f"Japanese voice cast for {title}:\n" + "\n".join(
                        f"{item.character_name} - {', '.join(item.voice_actor_names)}"
                        for item in credits[:15]
                    )
                report = AnimeReport(
                    f"Japanese voice cast information for {title}.", answer, reference
                )
        service._record_timing(timings, "anime", round((perf_counter() - started) * 1000, 2))
    except ResearchCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        service._record_operation_error(run_id, "anime", "specialized_lookup", exc)
        warnings.append(f"Specialized anime lookup failed; used web research instead: {exc}")
        service._trace(run_id, "ANIME ROUTE FALLBACK", [f"error: {exc}"])
        service._select_route(run_id, RequestRoute.WEB_RESEARCH.value, str(exc))
        return None
    references = [report.reference][: max(0, request.max_references)]
    service._finalize_storage(
        run_id, report.summary, report.answer, references, "completed",
        StopReason.SPECIALIZED_ROUTE,
    )
    service._record_specialized_source(
        run_id, report.reference, "anilist", timings.get("anime", [0.0])[-1]
    )
    service._record_run_finished(
        run_id,
        "research.run.completed",
        "ok",
        StopReason.SPECIALIZED_ROUTE,
        timings,
        reference_ids=[item.source_id for item in references],
    )
    service._trace(run_id, "COMPLETED", ["status: ok", "route: anime"])
    return ResearchResult(
        "ok", run_id, report.summary, report.answer, references, warnings=warnings,
        stop_reason=StopReason.SPECIALIZED_ROUTE,
        debug=service._build_debug(run_id, request, timings),
    )


def try_news_route(
    service: ResearchService,
    run_id: str,
    request: ResearchRequest,
    timings: dict[str, list[float]],
    warnings: list[str],
) -> ResearchResult | None:
    try:
        service._set_stage(run_id, "news")
        news_request, elapsed = service._call_resolver("classify_news_request", request.question)
        service._record_timing(timings, "resolver.classify_news_request", elapsed)
        service._trace(run_id, "NEWS REQUEST CLASSIFIED", [
            f"kind: {news_request.kind.value}",
            f"category: {news_request.category.value if news_request.category else ''}",
            f"time_scope: {news_request.time_scope.value}",
            f"elapsed_ms: {elapsed}",
        ])
        if news_request.kind == NewsRequestKind.UNSUPPORTED_TOPIC:
            service._trace(run_id, "NEWS ROUTE FALLBACK", ["reason: unsupported_topic"])
            service._select_route(run_id, RequestRoute.WEB_RESEARCH.value, "unsupported_news_topic")
            return None
        started = perf_counter()
        report = service.news_client.get_news(news_request, service.settings.news_digest_size)
        service._record_timing(timings, "news", round((perf_counter() - started) * 1000, 2))
    except NewsError as exc:
        service._record_operation_error(run_id, "news", "get_news", exc)
        warnings.append(f"Specialized news lookup failed; used web research instead: {exc}")
        service._trace(run_id, "NEWS ROUTE FALLBACK", [f"error: {exc}"])
        service._select_route(run_id, RequestRoute.WEB_RESEARCH.value, str(exc))
        return None
    warnings.extend(report.warnings)
    references = report.references[: max(0, request.max_references)]
    service._finalize_storage(
        run_id, report.summary, report.answer, references, "completed",
        StopReason.SPECIALIZED_ROUTE,
    )
    # News references come from one batched RSS fetch; amortize the
    # batch duration across references rather than overstating each.
    news_elapsed = timings.get("news", [0.0])[-1]
    per_source_elapsed = round(news_elapsed / len(references), 2) if references else 0.0
    for reference in references:
        service._record_specialized_source(run_id, reference, "rss", per_source_elapsed)
    service._record_run_finished(
        run_id,
        "research.run.completed",
        "ok",
        StopReason.SPECIALIZED_ROUTE,
        timings,
        reference_ids=[item.source_id for item in references],
    )
    service._trace(run_id, "COMPLETED", [
        "status: ok",
        "route: news",
        f"reference_count: {len(references)}",
    ])
    return ResearchResult(
        "ok",
        run_id,
        report.summary,
        report.answer,
        references,
        warnings=warnings,
        stop_reason=StopReason.SPECIALIZED_ROUTE,
        debug=service._build_debug(run_id, request, timings),
    )
