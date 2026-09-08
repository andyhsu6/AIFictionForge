<!--
  English is the default language for PR titles and bodies.
  标题默认英文；正文可用英文或中英双语（English first, Chinese supplement below）。
  Title format: `<type>: <summary>` — e.g. `fix: streaming timeout in chapter generation`
-->

## Summary

<!-- One or two sentences: what does this PR do, and why? -->

## Changes

<!-- Bullet list of the main changes -->

-

## Related issues

<!-- e.g. closes #123, refs #45 -->

## Test plan

<!-- How was this tested? Commands run, scenarios covered, screenshots if UI changed. -->

-

## Checklist

- [ ] Branch name follows `<type>/<short-description>`; commit messages are conventional and reference the issue (`closes #N` / `refs #N`).
- [ ] `pytest` passes locally (`cd backend && .venv/bin/python -m pytest tests/ -v`) for backend changes.
- [ ] i18n: new UI strings have been added to **both** `frontend/src/locales/zh/` and `frontend/src/locales/en/` locale files (no hard-coded strings in components).
- [ ] No source-text or user data in the diff: no imported book/document original text, chapter excerpts, character names, or world-building proper nouns; no secrets or `.env` contents.
- [ ] Docs and `CHANGELOG.md` updated if this change affects users.
- [ ] This is not a major change bypassing review (major = core architecture, DB schema, auth/security, cross-module interfaces, user data/UX).

---

## 中文补充（可选）

<!-- 如需中文说明，可在此补充：改动背景、影响范围、测试情况。 -->
