# Code signing policy

This policy covers the Windows release files (`xDripTray.exe`) published on this repository's [Releases](https://github.com/Electrik-rich546456/xdrip-gnome-topbar/releases) page.

**Status:** this project is applying to the [SignPath Foundation](https://signpath.org) open-source programme. Once it's accepted, Windows releases will carry:

> Free code signing provided by [SignPath.io](https://signpath.io), certificate by [SignPath Foundation](https://signpath.org).

Until then, release files are unsigned. Every release is still built only by GitHub Actions from this public repository, with a GitHub build attestation and a SHA-256 checksum (see "Verifying a download" below).

## What gets signed
- Only `xDripTray.exe`, which is built by `.github/workflows/windows.yml` on GitHub-hosted runners from the `windows` branch of this repository.
- No files built anywhere else, and no third-party programs, are signed with this project's certificate.

## Team roles
| Role | Who |
|---|---|
| Committers and reviewers | [Electrik-rich546456](https://github.com/Electrik-rich546456) |
| Approvers (approve each signing request) | [Electrik-rich546456](https://github.com/Electrik-rich546456) |

Every signing request is approved by hand. All team members use multi-factor authentication for GitHub and SignPath.

## Privacy policy
This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.

Specifically:
- **The only network connection** is the one you configure: to the xDrip+ Cloud Sync server, to receive your own glucose data from your own xDrip+ sync group, using the sync key you enter.
- **In passive mode (the default),** the app sends nothing to the group apart from the connection handshake and the acknowledgements the protocol needs.
- **In active mode (which you have to switch on),** it also announces itself in your group's followers list.
- **Your data stays on your computer.** Readings are kept in your data folder, and the sync key is kept in Windows Credential Manager. No analytics, no telemetry, no update checks.

## Verifying a download
```sh
gh attestation verify xDripTray.exe --repo Electrik-rich546456/xdrip-gnome-topbar
```
This confirms the file was built by this repository's GitHub Actions workflow. You can also compare the file's SHA-256 with `xDripTray.exe.sha256` from the same release.
