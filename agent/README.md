# tire-photo-agent

Runs on the server (since 2026-10-23: `tick --stdin-secrets` from the `tires-photo-agent` timer, see `deploy/README.md`, «Агент фото на сервере») or on the owner's Mac. Takes generation jobs from the panel, calls OpenAI image edits with the supplier picture as reference, returns the result (or the failure) with its cost.

## On the server
`python -m tires_sync photo-agent-tick` starts `tire-photo-agent tick --stdin-secrets` only when the panel has a request. Stdin carries one JSON object `{"token", "openai_key", "proxy"}`: a one-time agent token (revoked after the run), the OpenAI key and the proxy from «Подключения → OpenAI (фото)». The proxy (`http://` or `socks5://`, optionally `user:password@`) is used only for `api.openai.com`; the panel is reached directly at `TIRES_PANEL_URL` (= `public_base`). `TIRES_AGENT_DIR` (`/var/lib/tires-sync/agent`) holds `pending/` and `backgrounds/`. Size cards need a font: Arial on the Mac, Liberation Sans (`fonts-liberation`, the metrics of Arial) on Linux; with neither the card is not made. Build: `agent/build-server.sh` (static `x86_64-unknown-linux-musl` via `cargo-zigbuild`); the binary is not kept in git.

## Files and env (the Mac)
- `~/.config/tires/agent-token` — panel agent token (one line)
- `~/.config/openai/key` — OpenAI API key (one line)
- `TIRES_PANEL_URL` — panel base URL (required)
- `TIRES_AGENT_DIR` — the agent's folder (`pending/`, `backgrounds/`), default `~/.config/tires`
- `TIRES_IMAGE_MODEL` — default `gpt-image-2.5-flare`
- `TIRES_PRICE_IN` / `TIRES_PRICE_OUT` — USD per 1M tokens, default 10.0 / 40.0

Keys and the token are never logged; the token is sent only to URLs with the same scheme, host and port as `TIRES_PANEL_URL`. Jobs are claimed at once (`--limit` up to 10) and processed in order; jobs not finished within 2 hours time out on the panel.

A paid result is first saved to `~/.config/tires/pending/<job_id>.png/.json`, uploaded with 3 retries, and deleted on success; if upload finally fails the job is failed with its cost. Pending files are retried at the start of each run (a 409 keeps the files).

## Run
    export PATH=/opt/homebrew/opt/rustup/bin:$PATH
    TIRES_PANEL_URL=https://panel.example cargo run --release --manifest-path agent/Cargo.toml -- generate --limit 1

## Run from the panel (`tick`)
`tire-photo-agent tick` asks the panel `/api/image-jobs/work` whether the owner pressed «Сгенерировать» (on a model: `scope=rush`, only its jobs; «Сгенерировать очередь»: `scope=all`) or approved a frame (copies due). Nothing asked: it exits at once, so it is meant to be started every minute. Asked: jobs one by one until the panel has none left (`only=rush` for a model), then copies, crops and cards, then `/api/image-jobs/run-done` with the counts; the panel shows the result in «Генерация». A bad key, a closed region or no quota at OpenAI (HTTP 401/403/429) stops the run at the first job instead of using up the attempts of the whole queue.

Tests (no network): `cargo test --manifest-path agent/Cargo.toml`
