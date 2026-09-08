# Security Policy

## Supported versions

AIFictionForge is a **self-hosted** project maintained by a small team. Security fixes are applied to the latest release and the `main` branch only. If you run an older version, please upgrade before reporting an issue that is already fixed on `main`.

| Version | Supported |
|---------|-----------|
| latest release / `main` | Yes |
| older releases | No |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately by email to:

**[INSERT SECURITY CONTACT EMAIL]**

Please include as much of the following as possible:

- A clear description of the vulnerability and its impact.
- Step-by-step instructions or a proof of concept to reproduce it.
- The affected version (and commit SHA if known), deployment mode (Docker / source), and your environment.
- Any suggested mitigation or fix, if you have one.

### Response expectations

This is a self-hosted, community project without a dedicated security team. We aim to acknowledge reports within **72 hours** and to provide a fix or a mitigation plan on a best-effort basis. We will keep you informed of progress and credit you in the fix notes if you wish.

### Responsible disclosure

Please give us a reasonable amount of time to fix the issue before any public disclosure, and do not test vulnerabilities against instances you do not own.

## Scope notes

- This project is designed to be **self-hosted**. You are responsible for the security of your own deployment: set strong passwords, keep your `.env` and API keys private, and do not expose your instance to the public internet without proper protection.
- Reports about the upstream project [MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) should be directed upstream unless the issue also affects this codebase.
- When reporting, please **do not include original text from user-imported books, chapter excerpts, character names, or other user data** from your instance. Describe the issue in generic terms — see the data desensitization policy in [CONTRIBUTING.md](CONTRIBUTING.md).
