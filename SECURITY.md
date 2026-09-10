# Security Policy

## Reporting

If you find a security issue in Michelle.ai, email the repository owner privately. Do not open a public issue for undisclosed vulnerabilities.

## Secrets

- Never commit `.env`, API keys, tokens, or `michelle.db`.
- `.env` is gitignored. Use `.env.example` patterns documented in README when sharing setup steps.
- Do not paste `COMPOSIO_API_KEY`, `GEMINI_*`, or other credentials into chat logs or issues.

## Local data

- Chat history, long-term facts, and action audit rows live in `michelle.db` on the user's machine.
- Run `./scripts/reset_michelle.sh` before handing the repo to someone else.

## Action surface

- Only whitelisted actions run (`open_app`, `send_email`). High-risk sends require Confirm/Cancel.
- Email composer Send is opaque — it must not route through `/chat` or intent classification.

## Dependencies

- Python: `requirements.txt` — review with Dependabot (`.github/dependabot.yml`).
- Node/Electron: `package.json` — dev-only for the desktop shell.

## Agent / harness guardrails

- ECC hooks in `.cursor/hooks.json` scan prompts for secret patterns before submit.
- CI runs pytest with mock LLM and rules intent — no live external APIs in the test gate.
