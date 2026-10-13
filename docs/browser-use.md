# Browser Use engine

`/browse` tasks can run on [Browser Use](https://github.com/browser-use/browser-use) instead of the built-in
step loop. Browser Use reads the page's interactive elements (including iframes and shadow DOM), sends the
model a screenshot with numbered element highlights, and can chain several actions per step. It is usually
much better at navigating real websites.

## How it fits

```
browse workflow ──► browser.agent_task (one action) ──► worker ──► Browser Use subprocess (own virtualenv)
                                                                      ├─ opens the authorized Chrome profile
                                                                      └─ model calls ──► coordinator /v1 gateway
                                                                                          (routes, budget, metering)
◄── final URL, screenshot, page text, agent summary ──┘
review model checks the final page and screenshot ──► result to the owner
```

* Browser Use pins exact dependency versions that conflict with OMCA's, so it lives in its own virtualenv
  and runs as a subprocess (`src/omca/worker/browser_use_runner.py`).
* Its model calls go through the coordinator's model gateway with the `vision` role, so they use the
  configured routes and count against the shared monthly budget.
* The whole run is **one** policy action. The policy gate does not see individual clicks, typing or form
  submissions inside it. With `browser.agent.require_approval: true` the owner approves each run once;
  with `false` runs start without asking.
* Before a run, the worker closes its own Playwright browser for that profile; a profile can only be open
  in one Chrome at a time, so also quit any Chrome window you opened on the profile yourself.

## Install

```bash
uv venv --python 3.12 .venv-browser-use
VIRTUAL_ENV=.venv-browser-use uv pip install "browser-use==0.13.10"
```

Then in `config/application.yaml`:

```yaml
browser:
  engine: browser_use
  agent:
    python: ../.venv-browser-use/bin/python   # relative to the config file
    model: vision
    max_steps: 40
    max_minutes: 15
    require_approval: true                     # false: no approval before each run
```

Restart the coordinator. Set `engine: builtin` to go back to the step loop with per-action approvals.

Standalone workers (`omca worker`) need a `browser_agent:` section in `worker.yaml` with the same fields
plus `gateway_url` (e.g. `https://coordinator:8765/v1`) and `gateway_token_ref`.
