# LeafBridge

A small command-line tool for exchanging files between local Git repositories and Overleaf. Login, project listing, and cloning are implemented. Push and pull synchronization is still under development.

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
