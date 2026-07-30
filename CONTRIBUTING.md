# Contributing to tai42-channel-twilio

`tai42-channel-twilio` is a Twilio SMS/WhatsApp **channel** plugin for the TAI
ecosystem: `ask_user(..., channel="twilio")` delivers the question to a human's
phone and bridges the reply back to the interaction's public callback door. It
implements the `tai42_contract.channels.Channel` protocol. The hard rule (the
plugin rule): **it depends on `tai42-contract` + `tai42-kit` only and never imports
the skeleton.** The skeleton loads it through the manifest's `channel_modules`
field; `tai42_channel_twilio.register` registers the `"twilio"` channel and its
inbound route as a side-effect — there is no import edge to the skeleton in
either direction.

## Ground rules

- **No skeleton import — ever.** The package is contract-facing; the ban is
  enforced by ruff (`flake8-tidy-imports`), so a stray import fails lint:
  ```bash
  grep -rn "tai42_skeleton" src/   # must be empty
  ```
- **Credentials are operator-bound, never LLM-visible.** The Twilio auth token
  and sender/recipient configuration come from the environment, never from a
  tool parameter.
- **Fail closed.** A delivery or inbound event against an unconfigured channel
  raises loudly, naming the missing env var; the inbound door validates the
  request signature before accepting a reply.
- **Typed package** (`py.typed`). Pyright runs clean.

## Layout

- `register.py` — registers the `"twilio"` channel and the inbound route as an
  import side-effect.
- `channel.py` — the outbound `Channel` implementation (SMS/WhatsApp).
- `inbound.py` — the inbound door that bridges the reply back to the callback.
- `client.py`, `correlation.py`, `settings.py` — the HTTP client, the Redis
  correlation store, and the `CHANNEL_TWILIO_` settings.

## Naming

PyPI is a flat namespace with no owner in the path, so distributions carry the
`tai42-` prefix. GitHub repositories keep their `tai-` names, because the
`tai42ai` organisation already namespaces them. Import packages follow the
distribution.

| Surface | Form |
| --- | --- |
| Distribution — PyPI, `pip install`, dependency pins | `tai42-<name>` |
| Import package | `tai42_<name>` |
| GitHub repository | `tai-<name>` |

So a dependency is declared as `tai42-<name>` while its repository is named
`tai-<name>`, and both spellings are correct in their own context.

Some surfaces are deliberately neither, and must not be renamed: the `tai` CLI
command (`tai42` is an alias), the Prometheus metric namespace (`tai_tool_*`),
`TAI_*` environment variables, and the `tai-plugin.yml` descriptor filename.

## Dev

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

`make dev` installs the sibling `tai-contract` and `tai-kit` repos as editable installs for local cross-repo development.

Before any commit, run a secret scan over `src/` and `tests/` (e.g.
`detect-secrets scan`).

## Dependency resolution

`uv.lock` pins the `tai42-*` siblings to their released index versions while `[tool.uv.sources]` points them at local `../tai-*` checkouts. The two disagree deliberately: CI sets `UV_NO_SOURCES=1` and asserts the lock with `uv sync --locked`, so it resolves the artifacts a user installs. A bare `uv lock` beside sibling checkouts re-couples the lock to editable path entries, which then fails that `--locked` check — run `uv lock --no-sources` instead. See [How dependencies resolve](https://tai42.ai/contributing#how-dependencies-resolve).

## License

By contributing you agree your contributions are licensed under Apache-2.0.
