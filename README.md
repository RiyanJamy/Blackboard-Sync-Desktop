# Blackboard Sync Desktop

A Windows GUI app that downloads Blackboard Ultra course files into your chosen local or OneDrive folders.

**Developed by Riyan Hashem Jamy**  
[Portfolio](https://RiyanJamy.github.io)

## Features

- Browser-based Blackboard login.
- Course selection and separate destination folders.
- Filters for course materials, assignment attachments, and selected folders.
- One-click sync and optional daily scheduling.
- A configurable file-size limit, defaulting to 50 MB for new courses.
- Backups before replacing existing files, preserving saved annotations.
- Manual recovery for failed downloads and size-skipped files.
- Recognition of unchanged renamed files and newly placed manual downloads.

## Installation

Designed for Windows and requires internet access.

1. Download the packaged ZIP from [Releases](https://github.com/RiyanJamy/Blackboard-Sync-Desktop/releases).
2. Extract the entire ZIP.
3. Open the `BlackboardSync` folder and double-click `Install.cmd`.
4. Click **Install**, wait for setup to finish, then click **Open App**.

The installer checks for compatible Python and installs it if needed through Windows Package Manager. It also installs the required dependencies and browser.

If no release is available yet, use **Code → Download ZIP**, extract it, and open the folder containing `Install.cmd`.

## First sync

1. Enter your institution’s Blackboard Ultra URL.
2. Click **Sign in / Re-login** and complete authentication.
3. Select courses and choose a separate destination folder for each.
4. Adjust **Content Options** or **Choose Content**, then click **Save Choices**.
5. Close any files being annotated and let OneDrive finish syncing.
6. Click **Sync Now**.

To schedule syncing, choose a time and click **Enable Daily Sync**. The time follows your computer’s timezone, and Windows must be signed in.

## File protection and limitations

- Differing existing files are backed up before replacement.
- Annotations are preserved in backups; they are not merged into revised slides.
- Update detection depends on Blackboard’s reported modification timestamps.
- Filename matches do not prove that a local file is the current Blackboard version.
- Size filtering uses reported file sizes and is not a strict bandwidth limit.
- University login expiry, MFA, and download restrictions still apply.
- Compatibility may vary between institutions.

## Updates

Close the app, download and extract the newer release, then run `Install.cmd` again. Saved course selections, destination folders, and download history are preserved.

## Help and feedback

See [USER-GUIDE.txt](USER-GUIDE.txt) for detailed instructions.

Report problems through [GitHub Issues](https://github.com/RiyanJamy/Blackboard-Sync-Desktop/issues). Include the app version and error message, but do not share passwords, cookies, browser profiles, or private session files.

## Project status

**Preview release.** Backend tests have passed; broader Windows and institution-specific testing is still needed.

## Credits and license

Developed by **Riyan Hashem Jamy**, based on [bbsync by sammyschen](https://github.com/sammyschen/bbsync).

The upstream MIT license and copyright notice are retained in [LICENSE](LICENSE). See [CREDITS.txt](CREDITS.txt) for attribution.
