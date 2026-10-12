# Known-limitations register

Honest status as of the first handoff. "Verified" means exercised by the deterministic suite in this
repository; everything else needs the owner-side validation listed in the operator guide.

## Not yet validated against real services

| Item | What was verified | What remains |
|---|---|---|
| Open-model perception and planning quality | Probe machinery, routing, metering and every workflow path with a scripted fixture model that works from real pixels | Run `omca models probe` and the `live` suite against the selected route; capture AT03/AT06/AT07 scorecards (perception, grounding, completion, evidence quality) on the chosen hardware |
| OpenClaw runtime | Plugin type-checks against `openclaw@2026.10.2` SDK types; config translation; bridge endpoints | Run the gateway (needs Node ≥ 24.16) with the plugin; perform the image-path spike (docs/openclaw.md) |
| Telegram | Direct-mode poller and outbound API against a mock Bot API; bridge against recorded requests | Live bot: pairing, buttons, document delivery, 20 MB attachment limit |
| Firecrawl / SearXNG | Normalised contracts against recorded response shapes | Live keys and rate limits; Firecrawl's spending limit is separate from the model budget |
| Whisper transcription | Captions path end to end; Whisper path code (VAD, word timestamps) | Model download is blocked in the build environment: pre-download `faster-whisper` weights (or set `models.speech.model` to a local path) and test a narrated clip |
| Windows specifics | Browser/office logic on Linux Chromium; Windows paths in config | Edge channel, interactive-session Task Scheduler start, RDP/console behaviour, `desktop.raw_input` via pywinauto |
| Dedicated endpoints | Reservation, settlement, pause/resume calls against a mock API | Confirm the Hugging Face endpoint management API paths for your account before enabling time billing |
| SMTP | Send/invite against a local SMTP sink, approval gating, recipient allowlist | A real mail account (only when the owner selects one) |
| macOS | LaunchAgent generation, macOS configuration defaults, `dotenv:` secrets, key-chord mapping (all platform-neutral logic) | A run on a real Mac: Chrome channel, Keychain prompts, Accessibility permission for raw input, Local Network prompt, `caffeinate` behaviour |

## Design limitations

1. **Classification is heuristic.** Disclosure buttons (`aria-expanded`) outside forms and links with real
   URLs are treated as navigation. A hostile page could disguise a mutating control as one of these, and a
   GET URL with side effects outside the recognised path patterns is treated as navigation. Everything else
   that mutates needs approval; use standing rules sparingly.
2. **Page structure coverage.** Element references are collected from the top document; shadow DOM and
   cross-origin iframes are not enumerated. Those controls fall back to vision-guided coordinates, which are
   `UNKNOWN_MUTATION` (approval or a standing rule).
3. **While a task waits for approval it releases the desktop lease.** Revalidation catches changed
   elements, but the owner sees the action as it was when requested; if the page changed, the action is
   refused and must be proposed again.
4. **Prompt-injection detection in reports is pattern-based.** The protection that matters is structural
   (routes, budgets and recipients are not reachable from model output; every action passes the gate).
5. **"Supported" claims** passed verbatim-quote, numeric and open-model entailment checks; semantic misreads
   remain possible. Reports mark unsupported, uncertain and disputed statements; owner review is still
   expected for consequential use.
6. **Video:** sampling is bounded and disclosed; events shorter than the sampling interval may be missed
   unless scene detection or the model flags them. Browser playback capture is visual-only (no audio).
   Live streams are observed only for a finite interval. Native video input to the model is not used.
7. **Long video limits:** `video.max_duration_seconds` (default 60 min) caps one task; longer media needs an
   explicit interval.
8. **Budget enforcement is conservative, not exact.** Image-token estimates are heuristic and providers may
   invoice differently; calls without usage keep their reservation until reconciled
   (`omca budget reconcile`). Prices and FX rates are maintained by the operator.
9. **Single coordinator.** SQLite with one writer suits the single-host prototype; it is not highly
   available. Back up `data/` and `workspace/` (operator guide §12).
10. **OpenClaw does not see coordinator messages.** Progress, approvals and results are sent through the Bot
    API directly, so they are not in OpenClaw's conversation history; tasks are linked by ID.
11. **Office adapters are deliberately narrow (on Windows and macOS alike):** `.xlsx` via openpyxl (no macros, formulas are not
    recalculated), email via SMTP with `.eml` drafts, calendar invitations as `.ics` attachments. No
    Outlook/Graph/IMAP integration.
12. **PDF companion reports** are not generated (Word is the mandatory format).
13. **The generated approval signing key** is a file under `data/secrets/` when no keyring reference is
    configured; prefer `server.approval_signing_key_ref` pointing to the credential store.
14. **Owner steering by reply** works for messages the coordinator delivered (mapped through the outbox);
    otherwise use `/steer <id> <text>`.
15. **No automatic redaction of personal information in screenshots.** Browser screenshots stay temporary
    observations (7-day retention) and are never put into reports or sent anywhere automatically; reports
    only embed images fetched from the cited sources. If the owner asks for a screenshot (`task_image`), it
    is delivered to the owner's own DM as captured.

## Passing fixtures does not prove universal coverage

The acceptance fixtures are deterministic local pages, generated media and a scripted model. They prove
the controls and the plumbing; they do not prove that any given website, video platform or model behaves
the same way. Report live-site results separately (§21).
