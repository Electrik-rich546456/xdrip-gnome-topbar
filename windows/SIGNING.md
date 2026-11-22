# Setting up free code signing (SignPath Foundation)

This is for the repository owner. The workflow is already prepared, so once SignPath has accepted the project, signing switches itself on.

## 1. Before applying
- [ ] Turn on two-factor authentication for your GitHub account. SignPath requires it.
- [ ] Check that the repository is public with an OSI-approved licence. Both are already true (GPL-3.0).
- [ ] Check there is at least one release. `windows-v1.0.0` already exists.
- [ ] Make sure `CODE_SIGNING_POLICY.md` is linked from the README. It is already.

## 2. Apply
1. Apply at <https://signpath.org/apply> with these details:
   - **Repository:** `https://github.com/Electrik-rich546456/xdrip-gnome-topbar`
   - **Build system:** GitHub Actions, workflow `.github/workflows/windows.yml`, branch `windows`
   - **Files to sign:** `xDripTray.exe`, a PyInstaller one-file Windows program
   - **Code signing policy:** `CODE_SIGNING_POLICY.md` in the repository
2. Wait for SignPath to accept the project. They will set up an organisation and project for you in SignPath.io.

## 3. Configure SignPath.io (after acceptance)
1. **Trusted build system:** link the GitHub.com trusted build system to the project.
2. **Artifact configuration:** GitHub delivers the file as a zip with the .exe inside. Use:
   ```xml
   <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
     <zip-file>
       <pe-file path="xDripTray.exe">
         <authenticode-sign />
       </pe-file>
     </zip-file>
   </artifact-configuration>
   ```
3. **Signing policy:** name it `release-signing`, and turn on manual approval.
4. **API token:** create a CI user with submitter rights on the project, and generate an API token for it.

## 4. Add the settings to GitHub
In the repository, go to **Settings › Secrets and variables › Actions**.

| Kind | Name | Value |
|---|---|---|
| Secret | `SIGNPATH_API_TOKEN` | the API token from step 3.4 |
| Variable | `SIGNPATH_ORGANIZATION_ID` | your SignPath organisation ID, the GUID in the SignPath URL |
| Variable | `SIGNPATH_PROJECT_SLUG` | only needed if it isn't `xdrip-gnome-topbar` |
| Variable | `SIGNPATH_SIGNING_POLICY_SLUG` | only needed if it isn't `release-signing` |

## 5. Release
1. Increase `VERSION` in `windows/xdrip_tray.py` and push to the `windows` branch.
2. The workflow builds the .exe and sends it to SignPath. Approve the request in SignPath.
3. The workflow checks the signature, runs the self-test on the signed .exe, creates the build attestation, and publishes the release.

**Actions artifacts:** SignPath can only collect the file as an Actions artifact. The workflow uploads it with 1-day retention and deletes it as soon as the signed file is back, so it hardly uses any storage.

**Once signing is active,** update `CODE_SIGNING_POLICY.md` (change "Status" to say releases are signed) and the "Windows protected your PC" section of `windows/README.md`.
