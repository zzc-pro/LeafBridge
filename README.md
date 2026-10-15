# LeafBridge

A command-line tool for syncing working-tree files between local Git repositories and Overleaf, without Overleaf's paid Git integration. Git manages local history; LeafBridge handles manual file transfers.

## Install with Conda

Requires Python 3.10+ and Git. Install directly from GitHub without manually cloning the source repository:

~~~powershell
conda create -n leafbridge python=3.11 pip -y
conda activate leafbridge
python -m pip install git+https://github.com/zzc-pro/LeafBridge.git
~~~

Run these commands from any directory. Pip downloads the source temporarily and installs LeafBridge and its dependencies into the Conda environment. If you already have a suitable Python environment, only the pip command is needed.

## Log in

~~~powershell
leaf login
~~~

Complete login, CAPTCHA, or SSO in a separate Chrome window. LeafBridge saves cookies and a CSRF token, not your password, and does not access your usual browser profile.

Choose another browser or increase the timeout:

~~~powershell
leaf login --browser edge
leaf login --browser chrome --timeout 600
~~~

Chrome is the default; `--browser auto` tries Chrome, Edge, then Chromium. To use Chromium:

~~~powershell
python -m playwright install chromium
leaf login --browser chromium
~~~

Close the window or press Ctrl+C to cancel. Failed or cancelled logins preserve the previous session.

## List projects

~~~powershell
leaf list
~~~

Lists active owned and shared projects using the saved session, without opening a browser. Archived and trashed projects are excluded.

~~~text
ID                        STATUS    NAME
0123456789abcdef01234567  active    My Paper
123456789abcdef012345678  active    Another Paper
Projects: 2
~~~

## Clone a project

Always put project names in double quotes:

~~~powershell
leaf clone "My Paper"
leaf clone "My Paper" my-paper
~~~

Downloads the active project matching the exact name into a new directory, using the saved session. The default directory matches the project name; specify one if the name is unsuitable. The destination must not exist and its parent must exist. Rename duplicate projects before cloning.

Clone initializes Git and stores the project association and synchronization baseline in the ignored `.leafbridge/` directory. Existing `.gitignore` rules are preserved. Commit the downloaded files yourself:

~~~powershell
cd "My Paper"
git add .
git commit -m "Import Overleaf source"
~~~

## Associate an existing repository

Run inside a Git working tree or any subdirectory:

~~~powershell
leaf remote add "My Paper"
~~~

Use an exact active project name, or its ID from `leaf list` when names are duplicated:

~~~powershell
leaf remote add --id 0123456789abcdef01234567
~~~

Each repository can link to one project. The command saves the association and an empty baseline in `.leafbridge/`, adds that directory to `.gitignore`, and leaves Git remotes unchanged. It transfers no source files and creates no commits.

Linking the same project again preserves synchronization state; linking a different project fails. With an empty baseline, differing local and remote contents are first-sync conflicts.

To unlink this repository, run from the working tree or any subdirectory:

~~~powershell
leaf remote remove
~~~

This offline command deletes only `.leafbridge/config.json` and `.leafbridge/state.json`, then removes the empty metadata directory. It preserves paper files, Git history and remotes, `.gitignore`, the saved login session, and the Overleaf project. Running it again succeeds without changes. Incomplete or invalid JSON metadata can also be removed; unexpected files or unsafe paths are rejected before deletion.

You can then use `leaf remote add "Another Paper"` to link another project with an empty synchronization baseline.

## Inspect file differences

Run inside a linked or cloned working tree:

~~~powershell
leaf status
~~~

Compares a remote ZIP snapshot, local files, and the last synchronization baseline without changing files or state. Only differences appear in the table; matching files are counted in the summary.

| Status | Meaning |
| --- | --- |
| `local only` / `remote only` | Exists on one side without a baseline. |
| `local modified` / `remote modified` | Only that side changed from the baseline. |
| `conflict` | Different changes on both sides, different first-sync contents, or deletion versus modification. |
| `missing local` / `missing remote` | Missing on that side; the other matches the baseline. |
| `missing both` | A baseline file is absent on both sides. |

Equal contents count as `same`, regardless of the baseline. Status only reports missing files; it never changes or deletes files.

Git-ignored files, including tracked files, and paths containing any dot-prefixed component are excluded from status, push, and pull, even with force. This includes `.gitignore` and `.env`. See [Recommended .gitignore for paper repositories](#recommended-gitignore-for-paper-repositories) for LaTeX build outputs and local files.

Inspection returns 0 even if conflicts exist, 1 on failure, or 130 on cancellation.

## Push and pull

Run inside a linked working tree or any subdirectory:

~~~powershell
leaf push
leaf pull
~~~

Push uploads local additions and changes; pull downloads remote additions and changes. Both use the saved session and support nested folders and binary files. Files need not be committed; LeafBridge never stages or commits them.

Ordinary synchronization checks all files first. Any conflict stops the operation before files or state change. Changes only at the destination are preserved and reported as skipped; use the opposite command to synchronize them.

Choose the command according to which side should be the source:

| Command | Source | Effect on the destination |
| --- | --- | --- |
| `leaf push` | Local | Upload local additions and changes; preserve remote-only files and report conflicts. |
| `leaf pull` | Overleaf | Download remote additions and changes; preserve local-only files and report conflicts. |
| `leaf push --force` | Local | Overwrite differing remote files and delete remote-only files. |
| `leaf pull --force` | Overleaf | Overwrite differing local files and delete local-only files, including uncommitted and untracked files. |

**`--force` includes deletion.** It mirrors the selected source files even on the first sync, when there is no baseline. Both forced commands print the deletion list before changing files and proceed without another confirmation prompt. Identical files are not transferred.

For example, if `old.tex` exists only on Overleaf, `leaf push` preserves it and reports it as skipped; `leaf push --force` deletes it. If `draft.tex` exists only locally, `leaf pull` preserves it; `leaf pull --force` deletes it. Run `leaf status` to inspect differences before choosing a direction.

Git-ignored files and hidden paths remain excluded from overwrites and deletions. Empty directories may remain; file/folder replacement and paths differing only in case are rejected and must be resolved manually.

Transfers finish before planned deletions start. Each successful transfer or deletion saves its baseline; equal contents also update the baseline. Force removes obsolete baseline entries for files missing on both sides. Failure stops the operation but keeps completed changes. If saving state fails, run `leaf status` before retrying. Pull replaces files atomically and checks local contents again before overwriting or deleting them.

Avoid editing Overleaf during push: uploads cannot check for concurrent remote changes. Overleaf may normalize text encoding or line endings, causing later byte-level differences; use UTF-8 with LF line endings.

Commands return 0 on success, 1 on failure, or 130 on cancellation. Check skipped paths: success does not mean every file matches.

## Session storage

- Windows: `%APPDATA%/leafbridge/session.json`
- macOS: `~/Library/Application Support/leafbridge/session.json`
- Linux: `$XDG_CONFIG_HOME/leafbridge/session.json`, defaulting to `~/.config/leafbridge/session.json`.

Session files are written atomically, with mode 0600 on POSIX and configuration-directory permissions on Windows. Treat them as login credentials: do not share or commit them. Run `leaf login` again if the session expires or is revoked.

## Recommended .gitignore for paper repositories

If you use Git to manage versions of your LaTeX paper locally, the following `.gitignore` is a suggested starting point for organizing your paper repository. Keep paper sources, bibliography files, and figures under version control, and ignore compilation outputs, local credentials, and LeafBridge metadata. Place these rules in the paper repository root and adapt them to your project, preserving any existing rules.

~~~gitignore
# LaTeX compilation files
*.aux
*.bbl
*.blg
*.log
*.out
*.toc
*.lof
*.lot
*.fls
*.fdb_latexmk
*.synctex
*.synctex.gz
*.xdv
*.dvi
*.nav
*.snm
*.vrb
*.bcf
*.run.xml
*.loa
*.lol
*.ilg
*.ind
*.idx
*.glg
*.glo
*.gls
*.acn
*.acr
*.alg
*.thm
_minted*/

# Compiled paper (keep PDF figures under version control)
/main.pdf

# Local authentication and environment files
.olauth
.env
.env.*
!.env.example

# Editor backups and operating system files
*~
*.swp
*.swo
*.bak
.DS_Store
Thumbs.db
Desktop.ini
/.leafbridge/
~~~

Change `/main.pdf` if your main LaTeX file has a different name. Avoid ignoring all `*.pdf` files, since figures may be PDFs. LeafBridge also uses these rules to exclude files from `status`, `push`, and `pull`; `.gitignore` itself stays local to Git and is not synced to Overleaf.

Git ignore rules do not stop Git from tracking files already committed. To stop tracking a generated file while keeping the local copy, use `git rm --cached <file>` and commit the change.
