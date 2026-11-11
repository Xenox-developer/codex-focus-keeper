# Codex Focus Keeper

macOS helper that restores your working window when another app takes focus during Codex Computer Use. Starts and stops through Codex hooks.

## Requirements

- macOS, Codex with hooks and built-in Computer Use (`mcp__cua_repl__js`).
- Apple Command Line Tools (`xcode-select --install`) or Xcode, including Swift and `/usr/bin/python3`.
- Tested on macOS 15.6.1 with Codex 26.1106.51428.

## Install

1. Download and extract the repository, or clone it:
   ```sh
   git clone https://github.com/Xenox-developer/codex-focus-keeper.git
   cd codex-focus-keeper
   /bin/zsh ./install.command
   ```
   If you downloaded the ZIP, open Terminal in the extracted folder and run the last command.
2. In **Codex → Settings → Hooks**, review and allow the six entries calling `focus-keeper/lifecycle.py`. See [OpenAI's hook review documentation](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).
3. For window-level recovery, manually add `~/.codex/focus-keeper/FocusKeeper` in **System Settings → Privacy & Security → Accessibility**. Without this permission, recovery is limited to Codex taking focus.
4. Start a new chat, run a Computer Use task, and switch to your working app. Normal clicks and ⌘Tab can change the protected app.

Files go into `${CODEX_HOME:-$HOME/.codex}/focus-keeper`; hooks go into the same configuration directory's `hooks.json`. Existing unrelated hooks are preserved. Use the corresponding path in step 3 if `CODEX_HOME` is set.

## Install through an agent

Copy the prompt from [AGENT_INSTALL.md](AGENT_INSTALL.md).

## Check / remove

```sh
/usr/bin/python3 -I "${CODEX_HOME:-$HOME/.codex}/focus-keeper/lifecycle.py" --status
/bin/zsh ./uninstall.command
```

`running` should be `true` during Computer Use and `false` after it ends. Removal deletes this helper and its hooks.

## Limits

Recovery happens after focus changes: a brief flash or misdirected keystroke is possible. Detection of manual switching is approximate. System prompts, unavailable windows, and Space changes are skipped. The helper does not record typed text, window contents, or the clipboard, and does not change how Computer Use sends input. Abandoned sessions expire after five minutes without tool events.

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests -p 'test_lifecycle.py'
/usr/bin/python3 tests/test_setup_e2e.py
```

The second test requires the `codex` CLI. Both use temporary state; the second compiles the helper and checks install, hook discovery, reinstall, and removal without changing your active installation.
