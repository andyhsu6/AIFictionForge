# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The frontend reads this file and shows it in the in-app changelog dialog.

## [Unreleased]

### Added

- Internationalization of the whole app: all user-facing UI strings moved into `zh`/`en` locale files, with an in-app language switcher (`zh` remains the product default). (#27)
- User setting for content language: AI generation (outlines, characters, chapters, world settings) can produce output in a user-chosen language. (#27)
- Content-language injection into generation prompts and the skill loader, plus a per-generation language override that takes precedence over the account default. (#27)
- Backend error codes mapped to localized, human-readable messages through a registry and locale files. (#27)
- Community files: `CONTRIBUTING.md` (+ `CONTRIBUTING.zh-CN.md`), `CODE_OF_CONDUCT.md`, `SECURITY.md`, PR template, and bilingual issue templates. (#27)

### Changed

- **Breaking: the backing model must now have a context window of at least 1M tokens.** The window is measured against your own endpoint (a metadata read, then a server-side `max_tokens` bound probe) whenever the model a request is about to dispatch has no verdict on file, and re-checked in the background once per UTC day. A model measured below 1M is rejected both on save and on dispatch with no override checkbox; a model the probe cannot judge is rejected until you declare its window as ≥1M in the settings form. (#55)
- **Breaking: the tiered context degradation is removed.** Chapter generation no longer silently swaps whole-book injection for recent-chapter summaries plus retrieval, and book deconstruction no longer falls back to truncated per-chapter excerpts just because a model name was unrecognized. Over-budget splitting (a book that is simply too large) is unaffected and stays. (#55)
- **Breaking: there is no implicit default model any more**, and the `DEFAULT_MODEL` environment variable is no longer read by the backend. An account with no configured model gets an explicit `validation.ai_model_not_configured` error instead of a system-chosen default; configure the model per account under Settings. (#55)
- Accounts that already had a smaller model configured are not migrated silently: their next AI request is refused with `validation.ai_model_below_minimum` and the app guides them to Settings, where the model form shows the measured window, the declared window and the budget actually adopted side by side from the cached verdict. (#55)
- README (both languages) gained a "Model Requirements" section and a top-of-file breaking-change notice, including the two blind spots the probe still has: a gateway that truncates instead of erroring can pass the wired probes, and a gateway that swaps the model between daily re-checks is admitted under the stale verdict for up to one day. (#55)

## [1.5.4] - 2026-08-29

### Added

- Project renamed to AIFictionForge (灵创).

### Changed

- Removed upstream sponsorship links, QQ/WeChat groups, and the cloud announcement entry.
- The in-app changelog now reads a local `CHANGELOG.md` file.

### Fixed

- Embedding model download source switched to the official Hugging Face repository.

<details>
<summary>历史条目（中文原文）</summary>

> 以下为 2026-08-29 版本的原始中文更新日志，保留备查。

## 2026-08-29

- 新增：项目更名为 AIFictionForge / 灵创。
- 变更：移除上游赞助、QQ/WX 群和云端公告入口。
- 变更：更新日志改为本地文件。
- 修复：Embedding 下载源改为 Hugging Face 官方仓库。

</details>
