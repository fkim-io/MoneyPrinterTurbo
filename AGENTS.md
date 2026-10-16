# Slapz Studio fork

Read `README-SLAPZ.md` for the fork's supported workflow. Preserve upstream behavior and MIT notices. Slapz-specific composition, provider and handoff code lives under `app/studio/`; its page is `webui/pages/1_Slapz_Studio.py`.

- Work from `develop` on a `codex/` feature branch; preserve upstream `main`.
- Verify new behavior with `test/studio/`, Ruff and actual local renders or browser inspection as appropriate. Paid predictions are separate actions and need explicit approval of their inputs and budget. Mock provider requests in tests.
- This fork is public. Do not commit private app captures, customer content, internal HQ documents, prompts containing private data, credentials or generated job records. Local assets and generation state belong in ignored `storage/studio/`.
- Rendering and preparing handoffs are local draft operations. Publishing, customer email, uploads, spending or deploying require the founder's explicit approval for that action. Do not infer those approvals from a request to implement a feature.
- HQ owns vendor accounts, publishing plans and cross-app reports. Link to its integration contract when available rather than copying private configuration into this repository.
