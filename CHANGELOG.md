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
