# LeafBridge

A small command-line tool for exchanging files between local Git repositories and Overleaf. Login, project listing, cloning, associating existing repositories, inspecting file differences, and manual push/pull synchronization are implemented. Git manages local history; LeafBridge exchanges working-tree files with Overleaf.

## Install with Conda

Run these commands from the project root:

~~~powershell
conda create -n leafbridge python=3.11 pip -y
conda activate leafbridge
python -m pip install -e .
~~~

The named environment is managed by Conda outside the project directory. Editable installation makes source changes available immediately during development.

## Log in

~~~powershell
leaf login
~~~

Each login opens a new Chrome window with a temporary, separate browser profile. Complete password entry, CAPTCHA, or SSO on the Overleaf website. The tool saves session cookies and the CSRF token, not your password. It does not read your everyday browser profile.

Select another browser or extend the login timeout:

~~~powershell
leaf login --browser edge
leaf login --browser chrome --timeout 600
~~~

Chrome is the default. The auto option tries Chrome, Edge, then Chromium. To use Playwright's Chromium:

~~~powershell
python -m playwright install chromium
leaf login --browser chromium
~~~

Close the login window or press Ctrl+C to cancel. Failed or cancelled logins preserve the previous saved session. Website restrictions on automated browsers may require further compatibility work.

## List projects

~~~powershell
leaf list
~~~

This is a terminal command, similar to conda env list. It uses the saved login session and prints a table without opening a browser:

~~~text
ID                        STATUS    NAME
0123456789abcdef01234567  active    My Paper
123456789abcdef012345678  active    Another Paper
Projects: 2
~~~

The list shows active owned and shared projects only. Archived and trashed projects are excluded, and the displayed count refers to active projects. Project names retain their original language; tool messages and help text are in English.

The client calls the dashboard's POST /api/project JSON endpoint with the saved cookies and CSRF token. Pagination uses page.size and page.lastId; retrieval stops once the number of unique projects matches totalSize, then excludes archived and trashed entries. Unexpected responses, incomplete pagination, rate limits, and expired sessions produce an error instead of a partial success.

Interface references: [Overleaf routes](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/router.mjs), [project list controller](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Project/ProjectListController.mjs), and [dashboard API types](https://github.com/overleaf/overleaf/blob/main/services/web/types/project/dashboard/api.d.ts). These are website interfaces, so compatibility depends on Overleaf's deployed version.

## Clone a project

Always wrap the project name in double quotes, even when it contains no spaces:

~~~powershell
leaf clone "My Paper"
~~~

The command finds an active project by its exact name, downloads its current source ZIP, and creates a same-named directory under the current working directory. It uses the saved login session without opening a browser.

Specify a different local directory when needed:

~~~powershell
leaf clone "My Paper" my-paper
~~~

The destination must not already exist, and its parent directory must exist. Duplicate project names are rejected; rename one of the projects in Overleaf before cloning. Names unsuitable for a local folder require an explicit destination.

The clone creates a fresh local Git repository and preserves the downloaded files, including nested folders and binary files. It saves the project ID and source-file hashes under the ignored `.leafbridge/` directory for future synchronization:

~~~text
My Paper/
  .git/
  .gitignore
  .leafbridge/
    config.json
    state.json
  main.tex
  figures/
~~~

Existing `.gitignore` rules are preserved and `/.leafbridge/` is appended. The tool does not create Git commits; review and commit the source files yourself:

~~~powershell
cd "My Paper"
git add .
git commit -m "Import Overleaf source"
~~~

Clone failures clean up newly created temporary files. Archive paths are checked to prevent extraction outside the destination or replacement of Git and LeafBridge metadata. The command only reads the remote project.

## Associate an existing repository

Run this command inside your existing Git working tree or any of its subdirectories:

~~~powershell
cd my-paper
leaf remote add "My Paper"
~~~

The name must exactly match an active project in your saved Overleaf account session. The command does not open a browser. For duplicate names, use the project ID shown by `leaf list`:

~~~powershell
leaf remote add --id 0123456789abcdef01234567
~~~

Specify either a name or `--id`. Each local repository can be associated with one Overleaf project. This stores a LeafBridge association, leaving Git remotes such as `origin` unchanged; it does not require Overleaf's paid Git integration. Non-repositories and bare repositories are rejected.

The command saves `base_url`, `project_id`, and `name` in `.leafbridge/config.json` at the working tree root and initializes `.leafbridge/state.json` with an empty file baseline. It preserves existing `.gitignore` rules and adds `/.leafbridge/` if absent. Local source files are preserved; no files are uploaded or downloaded, and no Git commit is created.

Adding the same project again preserves the existing configuration and synchronization state. Adding a different project fails without replacing the association. Invalid or incomplete existing metadata must be checked before retrying. A failed project lookup does not create an association.

The empty baseline represents a repository that has not yet synchronized. Future synchronization must treat differing local and remote versions of the same file as a first-sync conflict. `leaf remote add` provides the association workflow previously planned as `leaf init`; there is no separate `leaf init` command.

## Inspect file differences

Run inside an associated or cloned Git working tree, including its subdirectories:

~~~powershell
leaf status
~~~

The command uses the saved login session to download the current source ZIP into memory. It hashes remote files without extracting them and compares them with local SHA-256 fingerprints and the last successful synchronization baseline. It does not open a browser, change source files, update the baseline, or upload anything.

Only differences appear in the file table; matching files are counted in the summary. Paths are quoted and control characters are escaped:

~~~text
STATUS            FILE
local only        "notes/local.txt"
remote modified   "sections/introduction.tex"
conflict          "main.tex"
~~~

| Status | Meaning |
| --- | --- |
| `local only` / `remote only` | A file without a baseline exists on only one side. |
| `local modified` / `remote modified` | Both sides contain the file; only the indicated side differs from the baseline. |
| `conflict` | Both sides changed differently, or their contents differ on first sync. Also includes a missing file on one side when the other side changed. |
| `missing local` / `missing remote` | A previously synchronized file is absent on the indicated side; the other side still matches the baseline. |
| `missing both` | A baseline file is now absent on both sides. |

Equal content is counted as `same`, even when it differs from the saved baseline. Inspection leaves that baseline unchanged. Missing files are reported for review; deletion is not propagated.

Selection starts with system Git's tracked and untracked file list, then applies Git's ignore rules to every candidate, including already tracked files, remote-only files, and baseline-only paths. Matching files are excluded. Any path with a dot-prefixed file or directory component is also excluded: `.gitignore`, `.env`, `.notes/main.tex`, and `sections/.draft.tex` are examples. Hidden and ignored files are not counted, transferred, or treated as conflicts. These rules apply to status, push, pull, and force. Reserved remote ZIP paths, unsafe local paths, unsupported source directories, and invalid metadata produce errors.

A completed inspection returns exit code 0, including when conflicts are found. Failed inspections return 1; cancellation returns 130. A request failure never produces a partial success table.

## Push and pull

Run these commands inside your associated Git working tree or any subdirectory:

~~~powershell
leaf push
leaf pull
~~~

`leaf push` uploads local additions and modifications. `leaf pull` downloads remote additions and modifications. They use the saved login session without opening a browser. Files do not need to be committed; Git remains responsible for your local history, and LeafBridge never stages files or creates commits.

Each command first compares all selected files against the synchronization baseline. If any conflicts exist, the entire ordinary synchronization stops before changing files or state, lists the conflicting paths, and returns exit code 1:

~~~text
Push failed: Conflicts detected. Synchronization paused; no files or state were changed.
  "main.tex"
Resolve conflicts or use leaf push --force to prefer the source side.
~~~

Choose the source explicitly when you want to overwrite differing destination contents:

~~~powershell
leaf push --force
leaf pull --force
~~~

`push --force` prefers local files; `pull --force` prefers remote files. Force transfers every selected source file whose content differs from the destination, including conflicts and changes made only at the destination. Identical files are not transferred. Git exclusions still apply.

Ordinary commands transfer only source additions or modifications. Changes made only on the opposite side are preserved and listed as skipped; use the opposite command to synchronize them. If the destination deleted a file while its source still matches the baseline, ordinary synchronization preserves that difference; force can restore the file from the source.

No command propagates deletions, even with force. Destination-only files are preserved and listed as skipped. A source file missing during a deletion-versus-edit conflict is also preserved at the destination by force. File-to-folder or folder-to-file replacement requires deletion and is rejected before transfer. Resolve such paths manually.

Nested folders and binary files are supported. Every confirmed file transfer is followed by an atomic baseline save; equal content also updates the baseline without transfer. A failed transfer stops the operation, identifies the file, and retains already completed files and their saved fingerprints. Failed files retain their previous fingerprints. If a file transfers but its state cannot be saved, the command reports that explicitly; run `leaf status` before retrying. Synchronization is not an all-or-nothing transaction after transmission begins.

Pull replaces each local file atomically and refuses to overwrite local changes made since its initial snapshot. `.gitignore` is a local configuration file and is neither uploaded nor downloaded. Selection uses the local Git ignore rules at the start of the command, including nested `.gitignore` files and other standard Git exclusions. Add LaTeX build outputs such as `*.aux`, `*.log`, and `main.pdf` to your repository's `.gitignore` to exclude them; LeafBridge does not guess which ordinary files are build outputs. Already tracked ignored files are also excluded, and force never bypasses exclusions.

Successful commands return 0 and summarize transfers and skipped differences. Failed commands return 1; cancellation returns 130. Review any skipped paths, since completion does not mean that every file is identical.

Uploads use the website's lowercase `POST /project/:Project_id/upload` multipart endpoint with saved cookies and CSRF, the `qqfile` file field, `relativePath` for nested paths, and the project root folder ID as the `folder_id` query parameter. Without `folder_id`, the relative-path folder lookup can return 404 even on the correct route. Only HTTP 404 triggers a fallback to `/Project/:Project_id/upload` for older deployments; other errors and network failures are not retried. The server creates missing folders and replaces same-named entities. HTTP errors, login redirects, malformed responses, and `success: false` are failures. Interface references: [Overleaf upload routes](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Uploads/UploadsRouter.mjs), [upload controller](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Uploads/ProjectUploadController.mjs), and [file import implementation](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Uploads/FileSystemImportManager.mjs). Public Overleaf's deployed route casing was checked against its editor frontend; it can differ from the open-source router.

Before the first upload, the client reads the root folder ID from the website's automatic `joinProjectResponse` through a short-lived Socket.IO HTTP polling connection. It closes the connection and caches only the root folder ID for that command. This uses existing authenticated HTTP requests, adds no browser window or dependency, verifies that the metadata belongs to the requested project, and refuses uploads if the folder lookup fails. Protocol references: [Overleaf real-time router](https://github.com/overleaf/overleaf/blob/main/services/real-time/app/js/Router.js) and [project folder locator](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Project/ProjectLocator.mjs).

The comparison uses a downloaded snapshot, and the upload endpoint does not provide conditional writes. Avoid simultaneous edits in Overleaf while pushing. Overleaf can normalize editable text encodings and line endings during import; fingerprints compare exact bytes, so these conversions can appear as subsequent remote differences. UTF-8 files with LF line endings avoid the common newline conversion.

## Session storage

- Windows: AppData/Roaming/leafbridge/session.json under the user profile, respecting APPDATA.
- macOS: Library/Application Support/leafbridge/session.json under the user profile.
- Linux: .config/leafbridge/session.json under the user profile, respecting XDG_CONFIG_HOME.

The session is stored as JSON using atomic writes. New files use mode 0600 on POSIX systems; Windows uses the configuration directory's access permissions. Treat this file as a login credential and do not share it or commit it to Git. Run leaf login again when the session expires or is revoked.

## Tests

~~~powershell
conda activate leafbridge
python -m unittest discover -s tests -v
~~~

Tests simulate the browser and HTTP responses. They do not read real credentials or connect to Overleaf. Actual CAPTCHA and SSO behavior requires interactive verification.

Browser and request behavior follows the [Playwright browser API](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context) and [request API](https://playwright.dev/python/docs/api/class-apirequestcontext).
