# Requirement traceability

Machine-readable acceptance inventory: [`tests/acceptance_cases.json`](../tests/acceptance_cases.json)
(each case's tests carry `@pytest.mark.acceptance("ATxx")`; `tests/test_traceability.py` fails if a case or
an FR is left uncovered).

## Functional requirements

| ID | Requirement (short) | Implementation | Tests |
|---|---|---|---|
| FR01 | Owner tasks, attachments, steering; durable task ID | `channels/commands.py`, `coordinator.submit_task`, `tasks.py` | AT01, API bridge tests |
| FR02 | Persist state; queued/running/blocked/approval/completed/failed status | `tasks.py` (transition table), `engine.py`, `channels/notifier.py` | AT12, lifecycle unit checks |
| FR03 | Configured search API, replaceable | `research/search.py` (Firecrawl, SearXNG, HTTP) | AT08 |
| FR04 | Authorized browser session, cookies across tasks | `worker/browser.py` (`cdp`, `persistent_profile`), attachment authorization | AT04 |
| FR05 | Page text, accessibility structure, current element refs | `OBSERVE_JS`, `RESOLVE_JS`, fingerprints | AT05 |
| FR06 | Screenshots and original images/crops as model inputs | observations, `browser.fetch_image`, `web.fetch_image`, `ArtifactStore.crop`, router image parts | AT03, AT06, AT07 |
| FR07 | Vision-guided coordinates as fallback | `workflows/browse.py::model_point_to_css`, `browser.click_point` + hit test | AT06 |
| FR08 | Reobserve and verify expected state | `_after()` reobservation, `verify_expected`, final page verification | AT05, AT06 |
| FR09 | Claim-level evidence with URL, time, locator/region, limitations | `evidence.py`, research/video workflows | AT07, AT09 |
| FR10 | Word report with citations, saved, reopened, delivered | `reports.py`, `workflows/common.py::finalize_report` | AT09 |
| FR11 | Approval before consequential operations on every path | `policy/*`, `engine.TaskContext.act` | AT10, AT11, AT15, AT24 |
| FR12 | Cancellation and pause; interrupted never complete | `coordinator.pause_task/cancel_task`, engine interrupts | AT13 |
| FR13 | Preferences, procedures, freshness rules apart from observations | `memory.py`, `ObservationStore` | AT12 |
| FR14 | Resume without repeating consequential effects | journal, `_recover`, `_resolve_uncertain`, worker receipt cache | AT11, AT12, AT23 |
| FR15 | Open-model allowlist and bounded spend | `models/registry.py`, `budget.py`, `models/gateway.py` | AT01, AT02, AT03, AT14, AT15 |
| FR16 | Honest reporting of gaps and uncertainty | evidence gaps, limitations, `PARTIAL`/`FAILED` outcomes | AT09, AT19 |
| FR17 | Accessible video or authorized playback | `worker/media.py` (yt-dlp), `browser.video_frames` | AT16, AT19 |
| FR18 | Ordered frames, transcript, timestamped findings | `media/video.py`, `workflows/video.py` | AT16, AT17, AT20 |
| FR19 | Coarse then dense sampling | scene detection, frame differences, model flags | AT17, AT18, AT20 |
| FR20 | Schedules in owner timezone, no overlapping duplicates | `scheduler.py` | AT21 |
| FR21 | Explicit standing goals; owner notifications | schedules → tasks → outbox | AT21 |
| FR22 | Multiple workers, capability routing, separate sessions | `workers.py::route`, per-worker attachments | AT22, AT23, routing test |
| FR23 | Serialised desktop interaction via worker lease | UI leases + fencing tokens | AT22, AT23 |
| FR24 | Bounded desktop/office adapters | `worker/office.py`, `workflows/desktop.py` | AT24 |
| FR25 | Per-worker credentials; only declared outputs transfer | worker auth, `validate_upload`, `validate_input_download`, hash checks | AT25 |

## Non-functional targets

| ID | Target | Mechanism | Evidence |
|---|---|---|---|
| NFR01 | Ack within 5 s | Ack is enqueued synchronously on task creation, before any model call; outbox flushes continuously | Measure on the deployed bot |
| NFR02 | Progress on state changes and ≥ every 60 s | `on_status_change`; maintenance loop edits a per-task status message every `progress_interval_seconds` | Notification tests |
| NFR03 | State before/after tool execution; complete audit | `actions` intent/receipt rows, `task_events`, `task_steps` | AT12 |
| NFR04 | One active task per VM, one UI lease holder | UI lease per VM, capability routing prefers idle workers | AT22 |
| NFR05 | Three consecutive passes of P1 fixtures | `omca fixtures --repeat 3` | `docs/release/` |
| NFR06 | Approval, cancellation, model restriction, budget tests pass | AT01, AT02, AT10, AT11, AT13, AT14, AT15 | Fixture results |
| NFR07 | Citations for external claims; unsupported marked | `review_claims`, report markers, validation | AT09 |
| NFR08 | Video findings linked to source + timestamp; missed events disclosed | `video_interval` evidence, coverage limitation | AT16, AT18 |
| NFR09 | Understandable without chat history | `README.md`, `docs/`, example configs, generated contracts | This documentation |
