---
name: ipyagent-help
allowed-tools: is_ipyagentskill_loaded
description: Explains ipyagent usage, commands, configuration, and features. Use when the user asks how to use ipyagent, what commands are available, or needs help with ipyagent configuration.
---

# ipyagent Help

ipyagent is an IPython extension that turns any input starting with `.` into an AI prompt.

## Quick Start

Load the extension:

```python
%load_ext ipyagent
```

Then type a period followed by your prompt:

```python
.write a haiku about sqlite
```

## Commands

- `%ipyagent` — show current settings (model, think level, search level, code theme, log flag, config paths)
- `%ipyagent model <name>` — change model for this session (e.g. `%ipyagent model claude-sonnet-4-6`)
- `%ipyagent think <level>` — set thinking level: `l`, `m`, or `h`
- `%ipyagent search <level>` — set search level: `l`, `m`, or `h`
- `%ipyagent code_theme <name>` — set Rich code theme (e.g. `monokai`, `github-dark`)
- `%ipyagent log_exact <bool>` — enable/disable exact prompt/response logging
- `%ipyagent save` — save current session (code + AI history) to startup file
- `%ipyagent reset` — clear AI prompt history for current session

## Tools

Expose a function as a tool by referencing it with `&`name`` in your prompt:

```python
def weather(city): return f"Sunny in {city}"

. use `use &`weather` to check Brisbane`weather` to check Brisbane
```

With `#|eval: true` as the first line, the code is run when the skill is loaded. For example, after loading this, you can run `is_ipyagentskill_loaded()`:

```python
#|eval: true
def is_ipyagentskill_loaded():
    "A test tool for the ipyagent skill"
    return True
```

## Notes

Any IPython cell containing only a string literal is treated as a "note". Notes appear in AI context as `<note>` blocks rather than `<code>` blocks, and are saved as markdown cells in the startup notebook.

```python
"This is a note that provides context to the AI"
```

## Configuration Files

- `~/.config/ipyagent/config.json` — model, think, search, code_theme, log_exact
- `~/.config/ipyagent/sysp.txt` — system prompt
- `~/.config/ipyagent/startup.ipynb` — saved session snapshot
- `~/.config/ipyagent/exact-log.jsonl` — raw prompt/response log (when enabled)

## Multiline Prompts

Paste multiple lines or use backslash continuation:

```python
. summarize this module:
focus on state management
and persistence behavior
```

```python
. draft a migration plan \
with risks and rollback steps
```
