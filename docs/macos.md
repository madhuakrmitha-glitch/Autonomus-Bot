# Running on macOS

The specification's reference platform is Windows, but every component also runs on macOS: the
coordinator, the worker (browser, media, office adapters), OpenClaw and a local Ollama model. Apple
silicon is a good host for the local model (unified memory, Metal acceleration). This guide covers what
differs from the [Windows operator guide](operator-guide.md); everything not mentioned here (Telegram owner,
model probes, release checks, budget, schedules, backups) is the same.

## Supported setups

| Setup | Notes |
|---|---|
| **One Mac** (your Mac or a dedicated Mac mini): coordinator + worker + OpenClaw + Ollama | Simplest. Use a separate macOS user account for OMCA if you also use the Mac yourself, so the assistant's browser and desktop never collide with yours |
| **Mac coordinator + Windows VM workers** (or the reverse) | Workers talk to the coordinator over HTTPS and are platform-independent; mix freely |
| **macOS VMs as workers (P3)** | macOS guests run only on Apple hardware (UTM, Parallels, Tart, VMware Fusion), and Apple's macOS license limits how many guests you may run - check the current license before planning several |

## 1. Install

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"   # if needed
brew install uv ffmpeg git
brew install --cask google-chrome            # or Microsoft Edge (then use channel: msedge)
git clone https://github.com/madhuakrmitha-glitch/Autonomus-Bot ~/OMCA/app
cd ~/OMCA/app
uv sync --extra all                          # pinned versions from uv.lock; includes pyautogui on macOS
uv run omca init                             # detects macOS: Chrome channel, ~/Library/Application Support profile paths
```

`uv run omca init --platform macos` forces the macOS defaults (useful when preparing a config elsewhere).

## 2. Secrets

* **OMCA-only secrets** go into the macOS Keychain: `uv run omca secrets set keyring:omca/deepinfra-api-key`.
  Keychain may ask once to allow access; choose *Always Allow*.
* **Secrets shared with OpenClaw** go into `~/.openclaw/.env` (OpenClaw reads it). LaunchAgents do not inherit
  your shell's environment variables, so reference that file from `application.yaml` with `dotenv:` refs:

```bash
mkdir -p ~/.openclaw && touch ~/.openclaw/.env && chmod 600 ~/.openclaw/.env
for name in OMCA_OWNER_TOKEN OMCA_BRIDGE_TOKEN OMCA_MODEL_GATEWAY_TOKEN OPENCLAW_GATEWAY_TOKEN; do
  echo "$name=$(openssl rand -hex 24)" >> ~/.openclaw/.env
done
echo "TELEGRAM_BOT_TOKEN=<token from @BotFather>" >> ~/.openclaw/.env
```

```yaml
telegram:
  bot_token_ref: dotenv:~/.openclaw/.env#TELEGRAM_BOT_TOKEN
  openclaw: {bridge_token_ref: dotenv:~/.openclaw/.env#OMCA_BRIDGE_TOKEN}
server:
  owner_api_token_ref: dotenv:~/.openclaw/.env#OMCA_OWNER_TOKEN
  model_gateway_token_ref: dotenv:~/.openclaw/.env#OMCA_MODEL_GATEWAY_TOKEN
```

## 3. Local model

```bash
brew install ollama        # or the Ollama app
ollama serve &             # the app starts it automatically
ollama pull qwen3.5:9b
```

Use `base_url: http://127.0.0.1:11434/v1` for `local-qwen35-9b`, enable it and run
`uv run omca models probe local-qwen35-9b`. Measure memory with the browser open: a quantised 9B model plus
Chrome is comfortable with 24 GB of unified memory and tight with 16 GB. faster-whisper runs on the CPU on
macOS (no Metal backend); `small` with `int8` is a reasonable default.

## 4. Browser profile

Create the dedicated profile and log in to the sites the assistant may use:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$HOME/Library/Application Support/OMCA/profiles/vm1-chrome"
```

Quit Chrome, then set `authorized: true` for `vm1-chrome` in both `config/application.yaml` and
`config/worker.yaml`. To attach to a Chrome you started yourself, add `--remote-debugging-port=9222`
(loopback only, dedicated profile) and use `mode: cdp`.

## 5. OpenClaw

```bash
curl -fsSL https://openclaw.ai/install.sh | bash          # Node 24.16+ / 26; or npm install -g openclaw@2026.10.2
openclaw onboard --install-daemon                         # gateway as a LaunchAgent
uv run omca openclaw config > ~/.openclaw/openclaw.generated.json5   # review, then merge into ~/.openclaw/openclaw.json
openclaw plugins install --link ~/OMCA/app/integrations/openclaw/omca-plugin
openclaw plugins enable omca
openclaw gateway restart
```

See [openclaw.md](openclaw.md) for what the integration enforces.

## 6. Start OMCA at login

Browser and desktop automation need your logged-in graphical (Aqua) session, so OMCA runs as a per-user
LaunchAgent, not a system daemon:

```bash
uv run omca service install --app-dir ~/OMCA/app --mode all-in-one --worker-id vm1
# logs: ~/Library/Logs/OMCA/ ; preview the definition: uv run omca service show --app-dir ~/OMCA/app
# remove: uv run omca service uninstall
```

The agent runs under `caffeinate -i` so the Mac does not idle-sleep while OMCA runs, restarts after a crash,
and puts Homebrew on `PATH` for ffmpeg. Also set *System Settings > Energy > Start up automatically after a
power failure* on a dedicated machine, and keep the OMCA user logged in (FileVault prevents automatic login
after a reboot, so plan for a manual login).

## 7. macOS permissions

| Permission | Needed for | Notes |
|---|---|---|
| None | Browser automation (Playwright drives Chrome over its debugging protocol; screenshots come from the browser) | Works with the display asleep |
| Accessibility | Only `desktop.raw_input` (raw mouse/keyboard, always approval-gated) | Grant it to the Python interpreter of `.venv` (or to the program named in the macOS prompt). Without it the action fails with `AUTH_REQUIRED`. Raw input also needs an awake, unlocked screen: install the agent with `--keep-display-awake` and do not lock the session |
| Local Network | Coordinator ↔ model server or remote workers on your LAN (macOS 15+) | Approve the prompt when it appears |
| Keychain access | `keyring:` secrets | *Always Allow* for the interpreter |

## 8. Differences from Windows at a glance

| Topic | Windows | macOS |
|---|---|---|
| Autostart in the desktop session | `scripts/windows/register-tasks.ps1` (Task Scheduler at log on) | `omca service install` (LaunchAgent, Aqua) |
| Default browser channel / profile | `msedge`, `C:/OMCA/profiles/...` | `chrome`, `~/Library/Application Support/OMCA/profiles/...` |
| Credential store | Windows Credential Manager | Keychain (`dotenv:` for secrets shared with OpenClaw) |
| Keep awake | Power plan *never sleep*; avoid locking RDP sessions | `caffeinate` (built into the LaunchAgent) |
| Raw desktop input | pywinauto | pyautogui + Accessibility permission |
| Keyboard shortcuts in policy | `Control+…` | `Meta+…` (Cmd) is classified the same way (e.g. `Meta+Enter`, `Meta+s` need approval) |
