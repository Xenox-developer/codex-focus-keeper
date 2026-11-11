# Agent installation prompt

```text
Install Codex Focus Keeper on this Mac from:
https://github.com/Xenox-developer/codex-focus-keeper

Clone the repository into the current workspace. Read README.md, setup.py,
lifecycle.py, and FocusKeeper.swift. Check macOS, the Apple developer tools,
Swift, and /usr/bin/python3. If a prerequisite is missing, report it.

Run /bin/zsh ./install.command from the repository. Preserve unrelated Codex
configuration and respect CODEX_HOME. Verify the installed files and the six
hook entries calling focus-keeper/lifecycle.py.

Leave hook trust and macOS Accessibility permissions for me to review and
approve manually. Do not bypass either. Report the installed helper's exact
path and the remaining manual steps from README.md.

After those approvals, use a new chat for a short Computer Use task with a
reversible Calculator toggle. Confirm lifecycle.py --status reports running
true during the task and false after completion. Do not change calculations
or run the helper manually. Report the result and the uninstall command.
```
