# Quickstart

**Version:** 0.86

This guide takes you from a fresh install to a program that connects to the exchange, reads market data, places an order and cancels it. It then points you at the worked examples in `examples/` that you can run and adapt.

The commands are for macOS and Linux. On Windows, type `py` where this guide says `python3`, and activate a virtual environment with `.venv\Scripts\Activate.ps1` in PowerShell or `.venv\Scripts\activate.bat` in cmd, where it says `source .venv/bin/activate`. Step 1 has the details.

## What you need

- Python 3.11 or later.
- The `examples` folder of this repository, from a clone or the source zip of your release, but only to run or read the worked examples (step 11).
- The exchange's address, `wss://api.trading.queenstowerexchange.com/ws`, and, for past market data (step 10), the history service's address, `https://history.trading.queenstowerexchange.com`. A test exchange you run on your own machine is usually `ws://127.0.0.1:8080/ws`.
- Your team's token, one per team. The Head of Technology, Joshua, emailed it to your Team Lead, who shares it only with your own team's members; a Team Lead who does not have it emails Joshua. Step 2 saves it. Treat it like a password: never put it in a source file, a notebook, a chat, a screenshot or a repository.
- A strategy ID registered for your team, for any program that sends orders. Your Team Lead registers it, with its declared scope, on the Strategy registration page of the Team Lead console. A registration counts only on the term it is made in: one made while the October term runs stays on October's exchange and is not live in Winter. So for each new term, your Team Lead registers again once your Directors announce that its registration is open, and the registration counts for orders sent after it is accepted. Winter's first week, 26 to 30 October, is an unscored practice week run as two terms: the first from 26 October and a new one, `2026-10-28`, from the Wednesday 28 October open, which starts with no registrations; registering for it opens at the restart on the evening of Tuesday 27 October. Winter then starts afresh on 2 November as a new term. So a registration made from 26 to 27 October does not carry into 28 October, and one made in the practice week does not carry into 2 November: your Team Lead registers again before each. For that week, 26 to 30 October 2026, qte-platform DL-162 and DL-163 rule that "Market Making obligation breaches only warn", that "the Winter daily loss halts stay live", and that "the Options market maker penalty ladder ... is off too, so Options obligation breaches only warn". Before a session opens, an order is rejected `MARKET_CLOSED` when the earlier checks pass. In a session, when the earlier checks pass, a new order under a strategy ID that is not registered, or whose registration was retired, is rejected `STRATEGY_NOT_REGISTERED`, and a new order outside the declared scope `OUTSIDE_DECLARED_SCOPE`.

The exchange keeps the hours in the calendar it sends after you authenticate (step 3), so read them there rather than from this guide.

## 1. Install

Install the SDK into your own project's virtual environment. On macOS and Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

On Windows, first install Python 3.11 or later from python.org or with `winget install Python.Python.3.12`, then open a new terminal so it finds Python. Type `py`, not `python3`: on Windows `python3` is often only a stub that offers the Microsoft Store, even once Python is installed. (`python` works too if the installer added Python to your PATH.) In PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

In cmd, activate with `.venv\Scripts\activate.bat` instead. If PowerShell refuses to run `Activate.ps1` because running scripts is disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and activate again. Once the virtual environment is active, `python` and `pip` are its own, on Windows as elsewhere.

The `git+https` install needs git (pip says it cannot find the command `git` without it). Either install git (on Windows, `winget install Git.Git`, then open a new terminal), or install a release from its zip, which needs no git:

```sh
pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.5.1.zip
```

That is the latest release, v1.5.1, as this is written; the [changelog](../CHANGELOG.md) lists each release. Later releases have the same address with their own tag, and the update check below prints it for you.

If you use a coding agent, such as Claude Code or Codex, have it read [AGENTS.md](../AGENTS.md) first, the guidance for coding agents on setup, your token, updates and the SDK's message codes. It comes with the SDK from 1.2.0 (and in an install of `main`), where `python -m qte_sdk.agents` prints it.

This installs the `qte_sdk` package only. The worked examples (step 11) are not installed with it. To run or read them, clone this repository or, without git, download the source zip of the release you installed (the address above, or the repository's Tags page on GitHub) and unzip it, so the examples match your SDK. A zip of `main` may be newer than the release you installed. Then copy its `examples` folder into your project folder, the one that will hold your `.env` (step 2), and run the examples from the project folder, inside the virtual environment where the SDK is installed: the SDK reads `.env` only from the folder you run in. You do not need the examples for anything else in this guide.

The SDK has numbered releases, each a `vX.Y.Z` tag listed in the [changelog](../CHANGELOG.md). To see whether yours is the latest, run:

```sh
python -m qte_sdk.update
```

It prints the version you have installed, with its commit or the release zip it came from, and one of three results: current (exit status 0), behind a newer release (1), with the command that updates, or that it cannot tell (2), when GitHub cannot be reached, say. Behind a release, the line has the code `QTE-UPDATE-AVAILABLE`, and says when the update is a recommended one, and why, as [`releases.json`](../releases.json) in the repository marks it, for example:

```text
QTE-UPDATE-AVAILABLE: qte-sdk 1.1.0 is behind 1.1.1, a recommended update on Windows: <why>. Update with <command>.
```

A recommended update fixes something serious: take it soon. When the update is not recommended, the line is the same without the part from "a recommended update" to the reason. After an install from a release zip, the command installs the latest release's zip, so it needs no git either. An install it cannot trace to the repository, such as an editable install of a local copy, is never called current: it is behind (1) when its version is lower than the latest release, and otherwise it cannot tell (2). A git install that follows `main`, the default, is also told when `main` has newer commits than the release; an install from a zip of `main` is not, since a zip records no commit. Importing `qte_sdk` never uses the network. To install one release and stay on it, name its tag:

```sh
pip install "git+https://github.com/josh-g-s/qte-sdk@v1.0.0"
```

In a program, `qte_sdk.update.check_for_update()` returns the same result, if you want to log it when your program starts. The smoke test (step 2) runs the same check.

**The automatic check.** You need not remember to run it: `open_session`, and a `ReconnectingSession` when it first connects, start the same check in the background, at most once a day on your computer. It never delays or fails your session and never prints. When your SDK is behind a release, it logs that `QTE-UPDATE-AVAILABLE` line as a WARNING through the `qte_sdk.update` logger, which Python shows on stderr unless your program configures logging otherwise; when your SDK is current, or the check cannot tell or cannot reach GitHub, it says nothing. The warning is written from the check's own thread, never on your session's event loop, and with the standard stream handlers it never holds up your session or your program's exit: when stderr is a pipe or terminal that cannot take the whole line at that moment (a full pipe that nothing reads, say), the line is not written there, and the next day's check says it again. On Windows, a pipe that cannot take the line is also skipped, not waited on; there the SDK goes by the room the pipe reports, and a pipe whose reader is already waiting for output (as a Python program reading it is) can report none, so the line may be skipped there although it would have fit. A Windows console that is paused, or in which text is being selected, can hold the line until it resumes, as it holds your program's own output. (If something else fills the pipe in the instant between that check and the write, the line waits in the background instead; on Windows, your program's own later writes to stderr then wait behind it until the pipe is read.) A line over 512 bytes (a long reason for a recommended update in a script other than Latin, say, or a long logging format of your own) is written there without the reason, which `python -m qte_sdk.update` gives, when that fits, and skipped otherwise; it is never cut. Handlers your program configures (a log file, pytest's caplog, a JSON formatter) still receive it, with its `code`; a handler the SDK cannot see into, such as a custom emit or a queue listener, writes as it always has. A stderr your program has replaced or wrapped (colorama, a rich progress display, a tee of your own) gets the same check when its `fileno()` leads to the pipe or terminal, and then can wait only if something else fills the pipe in the instant between that check and the write, or if the wrapper writes output of your program's own that it held back (rich keeps a partial line until it ends) along with it; one with no usable `fileno()` writes as it always has. Like the command, it sends GitHub nothing about you beyond the SDK's version (`qte-sdk/<version>` as its user agent): it reads the repository's list of releases and, only when you are behind, `releases.json`. It records the time of the last check in `qte-sdk/update-check` in your cache folder (`%LOCALAPPDATA%` on Windows, `~/Library/Caches` on macOS, `$XDG_CACHE_HOME` or `~/.cache` on Linux). A program that calls `check_for_update()` itself is not checked again, and replaying past market data never starts it. To turn it off, set `QTE_UPDATE_CHECK=0` in the environment, for example with `export QTE_UPDATE_CHECK=0` (PowerShell: `$env:QTE_UPDATE_CHECK = "0"`).

## 2. Set the exchange address and your token

The SDK needs two things: the exchange address, `QTE_URL`, and your team token, `QTE_TOKEN`. The simplest way to keep both is a `.env` file in your project folder, the folder you run your programs from. It lasts across terminals and needs no shell profile.

### A `.env` file (recommended)

From your project folder, with your virtual environment active, run the SDK's setup helper:

```sh
python -m qte_sdk.token set
```

It asks for the exchange address (a test exchange on your own machine is usually `ws://127.0.0.1:8080/ws`; otherwise use the exchange's address, `wss://api.trading.queenstowerexchange.com/ws`) and then for the token: paste it and press Enter. Nothing is shown as you paste, and since the token is typed at a prompt rather than on the command line, it never reaches your shell history. The helper writes both to `.env` as `QTE_URL` and `QTE_TOKEN`, in a file created readable only by you, keeping any other lines already in it. If the folder is a git repository that does not ignore `.env`, it offers to add `.env` to `.gitignore`; say yes. If git already tracks a `.env`, it stops before asking for the token and tells you to run `git rm --cached .env`, since `.gitignore` alone does not stop git committing a file it tracks. Inside a git repository it also stops if it cannot ask git (git is not installed, say), rather than guess. Run it again whenever the token changes.

On Windows the helper cannot make the file readable only by you: Windows does not apply the file's private mode, and the helper does not change Windows access lists. A file takes its access from its folder, so keep your project, and any token file, in a folder under your user profile (`%USERPROFILE%`, such as `C:\Users\you\qte-project`), which only you can open by default, and do not share that folder. A folder on another drive, such as `D:\`, is usually readable by every local user, and often changeable by them too. To check a folder, run `icacls <folder>` and look for `Everyone`, `NT AUTHORITY\Authenticated Users` or `BUILTIN\Users` in what it prints. When the helper saves the file, it reads the access lists and owners of the file and its folder, and says whether any of these groups can read or change the file, or add or remove files in its folder (and so replace the file with one of their own), or whether another account owns the file or the folder, or whether Windows would not let it see a list. The SDK warns you when it reads a `.env` or token file for which any of these is so. A later release will refuse such a file on Windows, rather than warn. On macOS and Linux the SDK refuses only a `.env` holding the token that other users can read. If `QTE_URL` is already set, press Enter at the address prompt to keep it.

To see where the SDK will take the token and the address from, without showing the token, run:

```sh
python -m qte_sdk.token check
```

It exits 0 when both are found and nothing needs fixing, 1 when something must be fixed and 2 when it could not tell; each problem it prints starts with a code such as `QTE-DOTENV-NOT-IGNORED`, which [errors.md](errors.md) explains, with every other code and exit status.

To set it up by hand instead, first make sure git will never commit the file, then create it readable only by you:

```sh
printf '\n.env\n' >> .gitignore
touch .env && chmod 600 .env
ls -l .env
```

The last line should show `-rw-------`. If your project is a git repository, also check that git does not already track a `.env`, since adding it to `.gitignore` does not stop git committing a file it already tracks:

```sh
git ls-files --error-unmatch .env
```

An error saying `.env` did not match any file is what you want. If it prints `.env` instead, run `git rm --cached .env` and commit, before you put the token in.

Open `.env` in your editor, put in the address and your token, and save it. The token never passes through your shell, so it stays out of your shell history:

```sh
QTE_URL=ws://127.0.0.1:8080/ws
QTE_TOKEN=paste-your-token-here
```

The SDK reads `.env` itself, with no extra package: `open_session()` takes the address and the token from it, and so do the worked examples. It reads only the `.env` in the working directory (not a parent folder), so run your programs from the folder that holds it. It reads only `QTE_URL` and `QTE_TOKEN`; other lines are left alone, and nothing is put in your environment. Blank lines, `#` comments, an `export ` prefix and single or double quotes around a value are fine.

Three safeguards protect the token:

- If other users can read a `.env` that holds `QTE_TOKEN`, the SDK uses nothing in it and raises `MissingToken` (or `MissingURL`, when it was reading the address), saying to run `chmod 600 .env`. This check applies on macOS and Linux. On Windows the SDK reads the file's access list instead, and if Everyone, Authenticated Users, Users, INTERACTIVE or Domain Users may read or change it, it gives a `TokenFileShared` warning, once, naming the groups and how to fix it, and still uses the file for now. It also reads the access list of the file's folder, and warns the same way if one of those groups may add or remove files there, since they could replace the file with one of their own, and the owners of the file and its folder, and warns if either is another account than you, Administrators, SYSTEM or TrustedInstaller (an account of Windows itself), since an owner can change who may open the file, or add or remove files in the folder. When the file is reached through symbolic links or junctions, at the file itself, at a folder on its path, or in what a link points to, it checks the file they lead to and that file's folder, and each folder that holds a link on the way, and its owner, too, since whoever may replace a link may point it elsewhere. If a link on the way cannot be followed, such as one in a loop, or Windows will not let the check see the access list of the file or of one of those folders, or a list is in a form the check cannot read, or an owner cannot be told, the warning says the file could not be fully checked, and why: whoever controls a list can hide it from you. It does the same for the file named by `QTE_TOKEN_FILE`. For a `.env` that sets only `QTE_URL`, it gives an `AddressFileShared` warning if one of those groups may change or replace it, or another account owns it: whoever changes the address could point it at a server of their own and capture your token when you next connect. Both warnings are kinds of `FileShared`, so catching `FileShared` handles either.
- On macOS and Linux, if your group or other users can write to a `.env` that holds `QTE_TOKEN` or sets `QTE_URL`, or to its folder or any folder above it (unless the folder has the sticky bit, as `/tmp` does, and you or the system own it), or if another account than you or root owns the file or any of those folders, the SDK warns you once with the code `QTE-DOTENV-WRITABLE`, naming the paths and the `chmod go-w` command that fixes it, or, when you do not own them all, telling you to move the `.env` to a folder you own, and still uses the file. Whoever can change or replace the file could point `QTE_URL` at a server of their own and capture your token when you next connect. It is a warning, not a refusal, since on Linux a umask of 002 makes files group-writable, which is harmless when each user has a group of their own. When the `.env` is a symbolic link, or its path leads through a link to a folder, it judges the file it points to, and the folders that hold that file and each link on the way, and the folders above them.
- If the `.env` (or, when it is a symbolic link, the file it points to) is inside a git repository and git tracks it or does not ignore it, the SDK warns you once, saying what to do: add `.env` to `.gitignore`, and if git already tracks it, run `git rm --cached .env` too. It still uses the file. The `.gitignore` of this SDK's repository protects only a clone of this repository, not your project. The same warning appears if you run a program inside a repository someone else made that ships a `.env`: check the address in it before you use it.

### Where the SDK looks, in order

For the token, the SDK uses the first of these that is set:

1. `token=` passed to `open_session` (or `ReconnectingSession` or `HistoryClient`);
2. the `QTE_TOKEN` environment variable;
3. the file named by the `QTE_TOKEN_FILE` environment variable;
4. `QTE_TOKEN` in `./.env`.

For the exchange address: the address passed to `open_session`, then the `QTE_URL` environment variable, then `QTE_URL` in `./.env`. If there is none, `open_session` raises `MissingURL`, which names both. A real environment variable always wins over `.env`, so an old `export QTE_TOKEN=...` in your terminal or shell profile hides the token in `.env`: run `unset QTE_TOKEN` and remove the line from your profile. On Windows, run `Remove-Item Env:QTE_TOKEN` in PowerShell or `set QTE_TOKEN=` in cmd, and if you saved it as a user variable, also run `[Environment]::SetEnvironmentVariable('QTE_TOKEN', $null, 'User')` in PowerShell. If `QTE_TOKEN_FILE` is set but its file cannot be used, the SDK raises `MissingToken` rather than falling back to `.env`.

The history service (step 10) has its own address, which comes from `QTE_HISTORY_URL` in the environment, never from `.env`. Its token comes from the same places as above.

### Other ways

Environment variables work too. To try the SDK in one terminal, set the address and read the token without echoing it: run the second line, paste the token (nothing is shown) and press Enter.

```sh
export QTE_URL=ws://127.0.0.1:8080/ws
read -rs QTE_TOKEN && export QTE_TOKEN
```

An exported variable lasts only for that shell and the programs it starts. A new terminal does not have it, so a program run there falls back to `.env`, or raises `MissingToken` if there is none.

To keep the token in one place for all your projects, put it in a file outside any repository, readable only by you, and name that file in `QTE_TOKEN_FILE`. The helper does this with `python -m qte_sdk.token set --file`: it writes the token alone to `~/.qte/token` (or the path you give after `--file`) in a directory only you can open, and prints the `export QTE_TOKEN_FILE=...` line to add to your shell profile (on Windows, the PowerShell and cmd commands that set `QTE_TOKEN_FILE`, for the current window and for new ones). If the path you give is inside a git repository, it applies the same checks as for `.env`: it stops if git tracks the file or cannot be asked, and offers to add the file to `.gitignore`. To do it by hand, the first command below makes a directory only you can open, then creates the file readable only by you before the token is written, replacing any old one. It reads the token without echo, so the token never appears on screen or in your shell history: run it, paste the token (nothing is shown) and press Enter. It works in zsh and bash, and running it again replaces the token. The second command checks the result, which should start with `-rw-------`.

```sh
(umask 077 && mkdir -p "$HOME/.qte" && chmod 700 "$HOME/.qte" && read -rs T && rm -f "$HOME/.qte/token" && printf '%s\n' "$T" > "$HOME/.qte/token")
ls -l "$HOME/.qte/token"
```

The parentheses run it in a subshell, so the `umask` and the variable `T` end with it. Then add this line to your shell profile, which is `~/.zshrc` for zsh (the macOS default) or `~/.bashrc` for bash (`~/.bash_profile` on macOS), and run it in your current terminal too:

```sh
export QTE_TOKEN_FILE="$HOME/.qte/token"
```

The line holds a path, not the token. The SDK reads the file each time it needs the token, removing one trailing newline. If the file is missing, unreadable, empty or not UTF-8 text, it raises `MissingToken` with a message that says which, and never shows the file's contents. You can still keep `QTE_URL` in a `.env`.

On macOS you can also keep the token in the Keychain. Store it once with `security add-generic-password -a "$USER" -s qte-token -w`, which prompts for it without echo, and load it with `export QTE_TOKEN="$(security find-generic-password -a "$USER" -s qte-token -w)"` in each terminal or in your shell profile.

Whichever you choose, never put the token in a source file or a notebook. The SDK never logs your token or puts it in an exception message, and a `.env` it cannot parse is reported by line number, never by its contents.

### Check your setup

Before you write any code, run the smoke test from your project folder, with the `examples` folder copied into it as step 1 says, naming an instrument or two:

```sh
python examples/smoke_test.py --instruments AAPL MSFT
```

On Windows, type `py examples\smoke_test.py --instruments AAPL MSFT`. To run them from any other folder, such as a clone, the SDK must find the address and token there some other way: run `python -m qte_sdk.token set` in that folder too, or set `QTE_URL` and `QTE_TOKEN_FILE` (a token file) in the environment, as above.

It first says whether your SDK is the latest release, as `python -m qte_sdk.update` does (step 1): a `FAIL` with the command that updates when a newer release is out, saying whether it is a recommended update and why, or when your SDK is too old to have the check. It then reports where the SDK finds the token and the address, without showing either; connects and names your team; reads the calendar; subscribes to each instrument and watches the market for `--seconds` (5 by default), and during a session waits up to `--book-wait` (60 by default, never less than `--seconds`) for the first book of an instrument that has none yet, since one with no valid quote has none; asks for your team's account; says whether the exchange's heartbeats arrived, and about how far apart (a `SKIP` if none came, since the interval may be longer than the run); and, when `QTE_HISTORY_URL` is set, reads the start of the last closed session's books from the history service. Each check prints `PASS`, `FAIL` or `SKIP` with a one-line reason, then a summary, and the exit status is not 0 if any check failed. A `SKIP` is something it could not check, such as an official close for an instrument that has none yet, or an account query the exchange does not answer; the reason says which. Its output never shows your token or any account figure (only a fill of the test order is named, with its quantity and price), so you can send it to the course team when you ask for help.

It sends no orders unless you add `--place-test-order --strat-id <your strategy> --tick <tick>`, giving the instruments' tick in dollars (for example `0.01`). The instruments table gives each instrument's tick, but the script still asks for it so that it works with an exchange that sends no table. Then, only while the market is open and no outage is in force, it places one buy of one share one tick above the wall's best bid, at least three ticks below every ask, waits for the exchange to report it resting, cancels exactly that price level and confirms the cancel; it never sends a mass cancel. It is a real order and can fill, since there is no post-only order: a fill fails the check, and a line on stderr names the position your team then holds. In a scored session it places nothing unless you also add `--allow-scored`. Use a strategy nothing else is trading during the test: if something else acts on its buy orders on the instrument meanwhile, the test order can no longer be told apart and the check fails. If it cannot confirm the cancel, it fails and names the level where the order may still rest; if you interrupt it while the order may rest (Ctrl+C, or a SIGTERM or SIGHUP such as a closed terminal), it first tries to cancel that level. A SIGKILL cannot be caught, so after one, check your team's orders yourself.

### Machine-readable output

For a coding agent or a script, each command can print one JSON document instead of text, with the same exit status:

```sh
python -m qte_sdk.token check --json
python -m qte_sdk.update --json
python examples/smoke_test.py --instruments AAPL MSFT --json
```

The document is one line on stdout, and nothing else is printed there; it gives the exit status as `exit_code`, and each finding's code, message and next step as keys of their own. Set `QTE_LOG_FORMAT=json` in the environment to have the SDK's log records (such as the automatic update check's `QTE-UPDATE-AVAILABLE` line) written to stderr as one JSON object per line too, with `level`, `code`, `message`, `next_step` and `fields`. The commands and examples read it when they start; in a program of your own, call `qte_sdk.logs.configure()` once at the start, before you open a session (nothing happens on import):

```python
import qte_sdk.logs

qte_sdk.logs.configure()  # JSON lines on stderr when QTE_LOG_FORMAT=json; plain text otherwise
```

It adds one handler to the `qte_sdk` logger and changes nothing else, so a log file of your own still gets every record; if you already log to the terminal through the root logger, put `qte_sdk.logs.JsonFormatter()` on your own handler instead, or each record shows twice. Neither the documents nor the lines are ever coloured, and they are plain ASCII, the same on every platform; neither ever holds your token. docs/errors.md gives every key.

## 3. Open a session

Everything in the SDK is `async`. A session is an authenticated connection: `open_session` connects, sends your token and waits for the exchange to acknowledge it.

```python
import asyncio

from qte_sdk.session import open_session


async def main() -> None:
    session = await open_session()  # the address and token come from .env or the environment
    async with session:
        print("team:", session.info.team)
        print("unscored session:", session.info.unscored)


asyncio.run(main())
```

A program meant to run for hours, overnight included, starts from `qte_sdk.reconnect.ReconnectingSession` (step 9) instead, as the worked examples `examples/quote_both_sides.py`, `examples/options_bot.py` and `examples/print_book.py` do, though each of them still stops after `--seconds` (30 by default), so a long-running bot sets its own run length or loops.

`session.info.unscored` is `True` when nothing in this session counts towards any score. `session.info.session_id` names the session, for your logs and when you ask for help. It is unique only within one run of the exchange: after the exchange restarts, an id can come round again, so never use it as a key across restarts or days.

If the exchange refuses the session, `open_session` raises `SessionRejected` (from `qte_sdk.connection`), whose `reason_name` says why. With no token set it raises `MissingToken` before connecting, and with no address `MissingURL`.

Two rules about sessions:

- **Send on the session and read from it.** Every send function takes the session. Iterate the session itself, not its connection, so that you also receive anything the exchange sent before it acknowledged you.
- **Read from one place.** Have one loop read the session's events and do everything from there. Two loops reading the same session each get only some of the events.

### Reading the calendar

Right after it acknowledges your session, at any hour, the exchange sends a `calendar` message: every session of the term with its `open_time` and `close_time` (`early_close` marks the day that closes early), the named holidays inside the term, and the term's first and last dates. It is the only source of the term's full schedule; outside a session, the `SessionState` that answers a subscribe also names the next open and close (step 4). Never hard-code trading days, holidays or hours.

`open_session` does not wait for the calendar. `session.calendar` is `None` until it arrives, and is set as you iterate the session. To wait for it first:

```python
from qte_sdk.calendar import next_close, next_open
from qte_sdk.units import to_datetime, to_timedelta

calendar = await session.wait_for_calendar(timeout=5)
if calendar is None:
    print("no calendar from this exchange")
else:
    now = session.info.server_time  # the exchange's clock, not your computer's
    opens, closes = next_open(calendar, now), next_close(calendar, now)
    if opens is not None:
        print("next open:", to_datetime(opens), "in", to_timedelta(opens - now))
    if closes is not None:
        print("next close:", to_datetime(closes), "in", to_timedelta(closes - now))
```

- `wait_for_calendar` keeps every event it reads while it waits, so iterating the session afterwards still delivers all of them, the calendar included. Call it from the loop that reads the session, not from a second task.
- It returns `None` if no calendar arrives in time. An older exchange never sends one, so your program must still work without it.
- The times are the exchange's own timestamps, like `session.info.server_time`. A timestamp is a signed 64-bit count of milliseconds since the Unix epoch, in UTC, so the difference of two is a number of milliseconds. `qte_sdk.units` converts them exactly: `to_datetime` gives a timezone-aware `datetime` in UTC, `to_timedelta` turns a difference into a `timedelta`, and `to_timestamp` turns a `datetime` that has a time zone back into a timestamp. For "now", use `session.info.server_time` or a later timestamp from the exchange, never your computer's clock: the exchange's clock is the one that opens and closes the market. Show a time in New York time if you like, but take the trading hours from the calendar, never from time zone rules of your own.
- `next_open` skips days with no session. It returns `None` once the term's last session has opened, and `next_close` returns `None` once it has closed.
- The calendar is the schedule. Whether the market is open right now is what `SessionState` reports (step 4).
- A `ReconnectingSession` (step 9) keeps the calendar of its current session in its `calendar` attribute, which is `None` again after each reconnect until the new session's calendar arrives.

### Reading the instruments

Straight after the calendar, the exchange sends an `instruments` message: every instrument it runs, with its kind (equity or option), tick size, lot size and status, whether your team may trade it, and its sector for the sector limit, plus each option underlying with its strike increment and listed contracts. Build your list of instruments, and your tick sizes, from it rather than writing them into your code. It is the whole table, not a change: the exchange sends it again whenever an instrument is listed, delisted or changes status, and each one replaces the last. `session.instrument_table` keeps the latest, and `qte_sdk.instruments` reads it:

```python
from qte_sdk.instruments import can_trade, instrument_info, tradable_instruments
from qte_sdk.units import to_decimal

table = await session.wait_for_instrument_table(timeout=5)
if table is None:
    print("no instruments table from this exchange")
else:
    print("you may trade:", tradable_instruments(table))
    info = instrument_info(table, "AAPL")
    if info is not None and can_trade(info):
        print("AAPL tick:", to_decimal(info.tick_size), "lot:", info.lot_size)
```

- Like `wait_for_calendar`, `wait_for_instrument_table` keeps every event it reads. It returns `None` as soon as the message after the calendar is something else, or at its timeout, since an older exchange never sends the table, so your program must still work without it.
- `can_trade` is true only when the instrument's status is `INSTRUMENT_TRADING` and `tradable` is true for your team. A reducing-only option contract (`INSTRUMENT_REDUCING_ONLY`) still accepts orders that reduce a position. A kind or status from a newer contract than your SDK knows decodes as unspecified: treat it as one you cannot trade.
- `tradable` says only what your team's arm and assignment allow: it is an entitlement, not a status. An instrument can be `tradable` and still `INSTRUMENT_DISABLED`, so never decide to send an order on `tradable` alone; use `can_trade`, or `tradable_instruments` for the list. Limits, the price collar and the session's state still apply to every order.
- `sector_limit` is the sector the Fundamentals sector limit counts the instrument against, the same for every team: a GICS sector name, or `Unsectored` for one the limit leaves out. It is empty when the exchange has none to give, as for every option contract; `qte_sdk.instruments.sector_of(info)` returns `None` then. Treat a sector name you do not know as unknown, never as an error.
- `display_name` is a name to show, and may be absent: the exchange can leave it unset for every instrument. Show `instrument` when it is (`info.display_name if info.HasField("display_name") else info.instrument`), and never parse it.
- A `ReconnectingSession` keeps the latest table of its current session in `instrument_table`, `None` again after each reconnect until the new session's table arrives.
- From Wednesday 28 October 2026, a Market Making desk must display at least one round lot of its instrument at its best bid and at its best offer, each side on its own, for a second to count towards its presence, and an Options Market Making desk at least 1 contract on each obligated side. Each name's round lot is shown in the console's obligations panel; this SDK cannot read it yet. The minimum is not scaled, and there is no exemption where one round lot is more than the desk's name cap allows; size at other prices does not make up for an undersized best bid or offer, a fill that takes a side below it fails the next second unless the desk restores it, and orders need not be multiples of the round lot. The round lot is not `lot_size`: `lot_size` in the instruments table is the smallest order size and the step order sizes go in, set by the exchange (currently 1 share or 1 contract), and every order must still respect it. `examples/quote_both_sides.py` quotes 1 share per side unless told otherwise, below the minimum wherever the round lot is more than 1 share: a Market Making desk passes `--size` equal to its name's round lot.

## 4. Subscribe to market data

The snippets from here on run inside the `async with session:` block of step 3.

```python
from qte_sdk.market_data import Book, subscribe, market_data
from qte_sdk.units import to_decimal

await subscribe(session, ["AAPL"])
async for item in market_data(session):  # runs until you stop it
    if isinstance(item, Book) and item.bid_levels and item.ask_levels:
        bid, ask = item.bid_levels[0], item.ask_levels[0]
        print(
            f"{item.instrument}: {to_decimal(bid.price)} x {bid.size} | "
            f"{to_decimal(ask.price)} x {ask.size}"
        )
```

There is **one conflated market-data feed, the same for every participant**. Book, trades and the market session state are published on one 100 ms grid, and each carries the `grid_time` of the grid point it belongs to; the mark is published on its own, slower grid. During a session, the market session state is the one message sent at every grid point. Trades come only when there are prints, and a book only when it has changed:

- A `Book` for an instrument is published at a grid point only if it differs from the last one published for that instrument this session. No `Book` at a grid point means that instrument's book is unchanged, so keep the last one you received.
- The first grid point of each session publishes the book of every instrument that has one.
- A subscribe during a session is answered at once with the last book published this session for each instrument you named that has one, carrying the `grid_time` it was published at. An instrument with no book yet gets none on subscribe; its book arrives when it is first published. The book you get on subscribe can be older than the latest `SessionState`, and the same book can then arrive again at the same `grid_time`. Keep, per instrument, the book with the latest `grid_time`.

`qte_sdk.books.LatestBooks` keeps the latest book of each instrument for you:

```python
from qte_sdk.books import LatestBooks

books = LatestBooks()
async for item in market_data(session):
    if books.update(item):  # True only when item is a newer book for its instrument
        print("new book for", item.instrument)
    aapl = books.get("AAPL")  # the latest book held, or None before the first one
```

It ignores a book whose `grid_time` is the same as or older than the one it holds, and every message that is not a book. After a `SeqGap` or `Disconnected` it keeps the books but lists them in `books.stale`, since a change may have been missed. An instrument leaves `stale` when a newer book for it arrives or, after a reconnect, when the subscribe answer brings its last published book again at the same `grid_time`.

A `Book` is the state of one instrument at the end of an interval, not a stream of individual changes. It shows two kinds of depth: `bid_levels` and `ask_levels` are the wall ladder, best price first, and `student_bid_levels` and `student_ask_levels` are the orders participants have resting, one entry per price, with no identity attached.

`market_data` yields `Book`, `Trades`, `Mark`, `SessionState` and `OfficialClose` messages, a `Reject` if a subscription is refused (an unknown instrument, for example), and two warnings:

- `SeqGap`: messages were missed, so anything you built from them may be wrong.
- `DecodeFailed`: a message could not be decoded.

**Outside a session** you can still connect and subscribe, but there is no live market. A subscribe is answered once, not on the grid, with a `SessionState` whose `state` is `CLOSED`.

That one `SessionState` can also name the next scheduled session in three optional fields: `next_session_date`, `next_open_time` and `next_close_time`. They are set together, only on this out-of-hours reply, never on the `SessionState` of a running session, and are absent when the term has no later session; an exchange from before these fields does not send them either, so write code that works without them. `until_next_open(state, session.info.server_time)` from `qte_sdk.market_data` gives the time until that open in milliseconds (`to_timedelta` turns it into a `timedelta`), or `None` when the fields are absent. `server_time` is the time your session was acknowledged; pass a later exchange timestamp instead if you have one. They are a convenience: the calendar is still where to read the full schedule.

The reply also carries an `OfficialClose` for each subscribed instrument that has one. Do not rely on its order relative to the `SessionState`, and write your code so it works without one: an instrument with no official close yet gets none. `OfficialClose.value` is that instrument's last official close, the time-weighted average of the mark over the final five minutes of its session, in micro-dollars like every price; `frozen` is set if any of those marks was frozen. [Using the SDK outside session hours](out-of-hours.md) walks through a whole run when no session is open.

No `Book`, `Trades` or `Mark` arrives until a session opens, so a loop that waits for a book waits until then.

`market_data` skips everything that is not market data, including your order events. When you also trade, loop over the session yourself and sort each event with `as_market_data(event)` and `is_order_event(event)`, as in the next step.

## 5. Prices and sizes

Every price on the wire is a whole number of **micro-dollars** in a 64-bit integer: $199.97 is `199_970_000`. Sizes are whole shares. Never use `float` for prices. Convert with `qte_sdk.units`:

```python
from qte_sdk.units import to_decimal, to_micros

to_micros("199.97")  # 199970000
to_decimal(199_970_000)  # Decimal('199.970000')
```

An option contract's instrument id is its OCC symbol, without spaces: the underlying, the expiry as YYMMDD, `C` or `P`, and the strike in thousandths of a dollar as eight digits. Build and read one with `qte_sdk.options`, which keeps the strike in micro-dollars like every other price:

```python
from datetime import date

from qte_sdk.options import CALL, is_option_symbol, option_symbol, parse_option_symbol

option_symbol("SPY", date(2024, 1, 19), CALL, to_micros("470"))  # 'SPY240119C00470000'
parse_option_symbol("SPY240119C00470000").strike  # 470000000
is_option_symbol("XOM")  # False: an equity
```

An option contract is otherwise an instrument like any other:
- Subscribe to it, and order it with `send_new`, by its symbol.
- Its `book`, `trades`, `mark` and `official_close` are the usual messages.
- Every size is a whole number of **contracts**; the 100-share multiplier is never applied on the wire.
- Prices, the mark included, stay in micro-dollars per share.

Two more market-data messages are for options only, and reach a connection only once it is subscribed to an option contract:
- `OptionChain`, the day's listed contracts with each one's role;
- `OptionGreeks`, a contract's published delta, gamma, vega and theta, with the forward and implied volatility, as fixed-point integers.

An option contract's book also carries `trading_state`: trading, reducing-only, suspended or halted. Halted (`OPTION_HALTED`) means a Director has halted the contract's whole chain, every contract on its underlying, before a split ex-date: send no `new` or `amend` there, only cancels, until the book shows another state. The orders resting in the chain when the halt started were cancelled with the reason `CHAIN_HALTED`, the end of the halt restores none, so once the book shows another state, place again only what that state allows; the mark and Greeks still publish meanwhile. Decide what you may send from this state alone, never from the book's `condition` and the Greeks' `status`: a contract can be suspended while its `condition` is `LIVE`. Read the state with `qte_sdk.options.trading_state`, which treats a state it does not know as suspended:

```python
from qte_sdk.options import LatestGreeks, chain_contracts, greek_to_decimal, trading_state
from qte_sdk.options import OPTION_ROLE_OBLIGATED, OPTION_TRADING, OptionChain

greeks = LatestGreeks()  # the latest Greeks per contract, as LatestBooks keeps books
# in your one loop, for each market-data item:
greeks.update(item)
if isinstance(item, OptionChain):
    quote_these = chain_contracts(item, underlying="SPY", role=OPTION_ROLE_OBLIGATED)
g = greeks.get("SPY261120C00665000")
if g is not None and g.HasField("delta"):
    delta = greek_to_decimal(g.delta)  # Decimal('0.500000000000'), never a float
book = books.get("SPY261120C00665000")
may_open = book is not None and trading_state(book) == OPTION_TRADING
```

The `qte_sdk.options` docstring covers when each message arrives. There is no request for the chain, so your first option subscribe must name a listed contract; a contract that is not listed is rejected `UNKNOWN_INSTRUMENT`. Take it from the instruments table (step 3): `qte_sdk.options.listed_contracts(table, "SPY")` gives the underlying's listed contracts, and `strike_increment(table, "SPY")` its strike increment. An underlying with no contract listed yet has none. With an exchange that sends no table, work out a listed contract from the symbol rules.

A `new` or `amend` in an option contract has four reject reasons of its own: `CONTRACT_NOT_LISTED` (the contract is not listed in this session), `CONTRACT_SUSPENDED` (the contract was suspended when the message was applied; a cancel still applies), `CONTRACT_REDUCING_ONLY` (the contract is reducing-only and the order would grow your position in it or take it across zero) and `CONTRACT_HALTED` (a Director's halt of its chain was in force; a cancel still applies). A resting order in a reducing-only contract that would do either is cancelled, with the reason `CONTRACT_REDUCING_RECHECK_FAILED`. A session logs `QTE-OPTIONS-CHAIN-HALTED`, at most once a minute for each underlying, when it reads a `CONTRACT_HALTED` reject or a `CHAIN_HALTED` cancel: do not retry until the chain trades again ([errors](errors.md#qte-options-chain-halted)). The `qte_sdk.options` docstring has the details.

An option contract's print in live `trades` may carry `feed_only`. It is present, and true, only on a `RESIDUAL` print that went through at the live print's own premium because the contract had no wall on the side the aggressor took. It is never sent as false, and every other print, an equity's included, leaves it out, so test it with `print_.HasField("feed_only")` or `qte_sdk.options.is_feed_only(print_)`: absent means an ordinary print.

`examples/options_bot.py` puts these together (step 11): it subscribes to an underlying's listed contracts, keeps the chain and the latest Greeks, picks the nearest expiry and the strike nearest the forward, passes over a contract whose `trading_state` is not `OPTION_TRADING`, rests one limit buy of one contract by its OCC symbol, prints any reject by name, and cancels the order before it exits. With `--watch-only` it does the same reading and choosing but sends nothing, no order and no cancel: it prints the order it would send and exits 0, or 1 with the reason no contract could be chosen.

Not settled yet: options in the history service.

## 6. Place and cancel an order

There is **no order ID** on the wire. Your team's orders are addressed by **instrument, side and price**, and your team holds **at most one resting order per instrument, side and price**, across all of its strategies. So to cancel an order you name its level, not an ID. A second `new` at a level where your team already rests is rejected (`DUPLICATE_ORDER_AT_LEVEL`); to change its size, send an amend.

Like the snippets of step 4, this runs inside the `async with session:` block of step 3 and uses its `session`; it needs nothing else from earlier steps.

```python
import asyncio

from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT
from qte_sdk.market_data import as_market_data
from qte_sdk.orders import (
    is_order_event,
    reason_code_name,
    request_ref_of,
    send_cancel,
    send_new,
)
from qte_sdk.units import to_micros

STRATEGY = "my-strategy"  # a strategy ID registered for your team
INSTRUMENT = "AAPL"
price = to_micros("199.97")
new_ref = await send_new(
    session,
    strat_id=STRATEGY,
    instrument=INSTRUMENT,
    side=BUY,
    order_type=LIMIT,
    price=price,
    size=10,
)
cancel_ref = None


def is_this_order(message, price_field):
    """Whether an order_state, execution or order_cancelled is about the order above.
    Your teammates' orders arrive on the same stream, so check every field."""
    return (
        message.strat_id == STRATEGY
        and message.instrument == INSTRUMENT
        and message.side == BUY
        and getattr(message, price_field) == price
    )


try:
    async with asyncio.timeout(10):  # never wait for ever
        async for event in session:
            if as_market_data(event) is not None or not is_order_event(event):
                continue  # a real program handles market data here too
            message = event.message
            ref = request_ref_of(message)
            if event.type == "reject" and ref is not None and ref in (new_ref, cancel_ref):
                print("rejected:", reason_code_name(message.reason_code))
                break  # if it was the cancel, the order may still rest
            if event.type == "accepted" and ref is not None and ref in (new_ref, cancel_ref):
                print("accepted:", "new" if ref == new_ref else "cancel")
            elif event.type == "order_state" and is_this_order(message, "price"):
                print("resting:", message.remaining_size)
                if cancel_ref is None:
                    # Cancel it by naming its level: instrument, side and price.
                    cancel_ref = await send_cancel(
                        session, instrument=INSTRUMENT, side=BUY, price=price
                    )
            elif event.type == "execution" and is_this_order(message, "order_price"):
                print("filled", message.fill_size, "left", message.remaining_size)
                if message.remaining_size == 0:
                    break
            elif event.type == "order_cancelled" and is_this_order(message, "price"):
                print("cancelled:", reason_code_name(message.reason_code))
                break
except TimeoutError:
    print("no final outcome within 10 seconds: check your orders")
```

Every send returns the `request_ref` it put on the message. The `accepted` or `reject` that answers the message echoes it. An `order_cancelled` is meant to carry it when its `reason_code` is `CANCEL_REQUEST` (your cancel), `MASS_CANCEL` (your mass cancel) or `AMEND_CUT` (your amend cut the order to nothing), echoing the `request_ref` of that message. These rules are provisional: the contract has not specified them yet, so your program must not rely on `request_ref` being present, or absent, for any reason code. Keep a fallback: when `request_ref` is missing, match the cancellation by its strategy, instrument, side and price, as `examples/quote_both_sides.py` does. Match on `request_ref` with `request_ref_of`.

An amend changes the orders at one level: `send_amend(session, instrument=..., side=..., price=..., new_size=...)`. `new_size` is the new total remaining size, not an amount to add. `send_mass_cancel(session)` cancels **every order your team has on the exchange**, including those of your teammates' strategies.

Because a cancel or amend names a level, not an order, it acts on whichever of your team's orders rests at that level when the exchange applies it, after the order delay. If your order fills in the meantime and a teammate's strategy enters an order at the same price, your cancel removes theirs. Agree within your team who trades which instruments or prices.

A send checks its identifiers before anything leaves your machine: `strat_id` and `request_ref` must be 1 to 32 bytes of UTF-8 and `instrument` at most 32 bytes. A send that breaks this raises `ValueError`.

`send_new` also takes `parent_ticket_id`, which is for Execution desks only: it names the working parent ticket a child order works. Leave it out on any other team: the exchange rejects a `new` from any other team that carries it, with `PARENT_NOT_WORKING` (or `MALFORMED_MESSAGE` if the value itself is malformed). A fill of such a child order names its ticket too: `qte_sdk.tickets.parent_ticket_of(execution)`. When a ticket stops, the desk's child orders still resting for it are cancelled. If the pod cancelled the ticket or the mark moved beyond its limit, each child's `order_cancelled` has the reason `PARENT_STOPPED`. If the term's final close expired it, the close cancels the children itself, with `SESSION_CLOSE`. If a cure ticket replaced it (`TICKET_CANCELLED_BY_ENGINE`), each child still resting is cancelled `PARENT_STOPPED` when the cure is accepted. If a breach cancelled an increasing ticket (`TICKET_CANCELLED_BY_ENGINE` too), that follows the close, which has already cancelled the children `SESSION_CLOSE`. If a split cancelled it (`TICKET_SPLIT_CANCELLED`, at the open of the instrument's split ex-date, where the term posts splits), no child is resting then, its fills so far stand, and `stopped_mark` is 0; send a new ticket in post-split shares if you still want the trade. A desk only works tickets routed to it (`executor` `DESK`): it is sent nothing about a ticket the automatic execution service works.

A Fundamentals pod sends no orders. It sends tickets with `qte_sdk.tickets` (`send_ticket`, `send_ticket_cancel`, `send_ticket_urgency`), and the exchange answers with `ticket_accepted` or `ticket_reject`, then reports each ticket's state in `ticket_state`, to the pod and, while the ticket has one, its desk. The exchange's routing at acceptance decides who works a new ticket: its automatic execution service (`executor` `SERVICE`, no desk), by the `algorithm` you give `send_ticket` (`TWAP`, the default, or `VWAP`), or your Execution desk (`executor` `DESK`, always TWAP). Your session is sent the routing in `execution_route` after each `session_ack` and again whenever an automatic execution control is logged or a pending one comes into force: pick it out with `is_execution_route`, or read `LatestTickets.route`. `ticket_accepted` says the `algorithm` and `executor` a ticket was accepted with. VWAP is offered from 26 October 2026. A VWAP ticket is refused `TICKET_ALGORITHM_UNAVAILABLE` (1713) while the instrument has no current VWAP profile, and the refusal's `reason_detail` says why and ends "TWAP is available". Nothing is resent for you: the exchange never switches a ticket to TWAP, so send it again as TWAP yourself if you still want it. A ticket under the automatic route keeps the VWAP it names; a cure ticket and a ticket routed to a desk are TWAP, as `ticket_accepted.algorithm` shows. In a term that pairs no pod with an Execution desk, a ticket sent while `routing_in_force` is not `ROUTING_SERVICE`, a cure ticket included, is refused `TICKET_SERVICE_OFF` (1714). There is no ticket amend message: to change a ticket, send `send_ticket_cancel(session, ticket_id=..., replacing=True)`, and once the ticket's `ticket_state` shows `TICKET_CANCELLED_BY_POD`, send the new ticket with `replaces_ticket_id` naming it. If that cancel is refused `TICKET_SERVICE_OFF` (1714), the route is off and the replacement would be refused too: send no replacement, because the original ticket is still working with its fills and children, and cancel it plainly (without `replacing`) if you want it stopped. While the route is on, a replacing cancel acts exactly like a plain one. A `SERVICE` ticket's state says what the service is doing (`activity`, such as `POSTING`, `TAKING` or `WAITING_LIMIT`), whether it is `behind_schedule`, and a `revised_completion_time` once a suspension has shifted the schedule. From 1.5.0, every ticket's state also carries `progress`, its history of shares due against shares filled: points with `time`, `due_shares` and `filled_shares`, cumulative from the ticket's start, at each session's open, every 30 minutes after it in session, the session's close and the ticket's stop. Each `ticket_state` carries the whole list, so it replaces the list held and is never appended to; it is empty when the exchange holds no history for the ticket (and until qte-platform #2349 is running). Fills of the service's child orders come to the pod as `execution` events with `origin` `AUTO_EXECUTION` and the ticket in `parent_ticket_id`; nothing else of those orders is sent, and under `SERVICE` nothing at all of the engine's own cure ticket is sent. Those fills are already in the ticket's `filled_shares`, so never add them on top of ticket totals; take the position from `account_state` (`qte_sdk.account`), and keep it between queries with `AccountReports`, which applies each later fill once. Pick the ticket answers out in your one loop with `is_ticket_event` (the fills are order events: `is_order_event`), and keep the newest state of each ticket with `LatestTickets`, passing it every event. A `ticket_reject` never stops a ticket: a ticket stops only when a `ticket_state` arrives whose `status` is not `TICKET_WORKING`. A Director may amend a `SERVICE` ticket: it stops `TICKET_AMENDED_BY_DIRECTOR` (named from 1.5.0), and the pod is then sent, with no `ticket_accepted`, the `ticket_state` of a new working ticket whose `replaces_ticket_id` names the old one; follow that one from then on. A pod's `ticket_reject` never carries the Director-only codes 1717 to 1720: `TICKET_RAISE_PENDING` (1718) refuses a Director's cancel because your urgency raise arrived at the same instant, your raise stands, and you need do nothing. Ticket answers carry no `report_seq` and a resume does not replay them. Instead, once on each connection, straight after the `session_ack`, the calendar and the instruments table, the exchange sends a whole `ticket_state` for each ticket of the current term, and those replace what you held. It does not send them again after a resume, so pass every event to `LatestTickets` from the first: a session that resumes keeps the states it reads while waiting for the `resume_ack` and delivers them in order. `LatestTickets` forgets every ticket on a `Disconnected` so that they rebuild it. The `qte_sdk.tickets` docstring has the rules.

In an inter-term break, after the close of a term's final session and before the next term's first session, a pod may ask the Directors to have the exchange reduce one of its positions at the break days' official closes: in Winter, from the close on Friday 4 December 2026, with break days 7 to 31 December. Send it with `qte_sdk.tickets.send_break_request(session, instrument=..., side=..., shares=..., thesis_id=..., thesis_version=..., risk_statement=...)`: the side that reduces the position, at most the position's shares, the thesis the position was opened under, and a `risk_statement` of 1 to 280 characters saying what risk needs the reduction. Withdraw one with `send_break_request_withdraw(session, break_request_id=...)`; a request is never amended, so withdraw it and send a new one. The exchange answers with `break_request_state`, the request's whole state at each change (pending on acceptance, then approved, refused or withdrawn), or `break_request_reject`, whose `reason_code` `ticket_reason_code_name` names: `BREAK_REQUEST_NOT_IN_BREAK` (1721) outside the break, `BREAK_REQUEST_EXISTS` (1722) when the pod already holds a pending or approved request in the instrument, and `TICKET_CROSSES_ZERO` (1702) for a side or size that does not only reduce the position. Pick them out with `is_break_request_event`. Neither carries a `report_seq` or is replayed, and a session that connects later is not sent the state, so keep each one as it arrives. An approved request is not filled yet (`BREAK_REQUEST_FILLED` and `BREAK_REQUEST_EXPIRED` are defined but not sent): that comes with qte-platform #2059's second part.

## 7. Read your order events

The exchange sends these events about your team's own orders:

| Event | Meaning |
|---|---|
| `accepted` | Your message was applied. Echoes your `request_ref`. |
| `reject` | Your message was refused. `reason_code` says why; use `reason_code_name` to print it. |
| `order_state` | The state of one of your orders: its instrument, side, price, strategy and remaining size. It is sent when an order rests and for every amend the exchange accepts. An amend's `order_state` also carries `old_price`, the order's price before the amend, and reads `FILLED` or `CANCELLED` with a remaining size of 0 when the amend ended the order. |
| `execution` | A fill: price, size, remaining size, fee or rebate. A fill of a limit order carries the order's price as `order_price`; a fill of a market order has none. |
| `order_cancelled` | One of your orders left the book unfilled, with the reason (your cancel, a mass cancel, the close, and others). |
| `risk_notice` | A risk warning for your team. |
| `term_close_fill` | A position of yours closed by the term-end close at the final session's official close (see "The term-end close" in step 8). |

`qte_sdk.resting.RestingOrders` keeps a view of your team's resting orders built only from these events. Read its docstring for what it cannot know. When an amend's `order_state` arrives, the view removes the order from `old_price` and, if it still rests, records it at `price` with its remaining size. So an amend that moves the price moves the entry to the new level, a size-only amend (where `old_price` equals `price`) updates it in place, and an amend that fills the order completely or ends it some other way leaves nothing at either price. An older exchange that does not send `old_price` leaves the entry at the old price in the view; against one, cancel and re-enter instead of amending the price if you rely on the view.

### Query your account

You can ask the exchange for your team's own account at any hour, inside a session or not: its positions, its cash and, for most teams, its equity, daily profit and loss and limit use. This needs an exchange that serves the query. Like step 6, it runs inside the `async with session:` block of step 3:

```python
import asyncio

from qte_sdk.account import LIVE_MARK, is_account_state, send_account_query
from qte_sdk.orders import is_order_event, reason_code_name, request_ref_of
from qte_sdk.units import to_decimal

ref = await send_account_query(session)
try:
    async with asyncio.timeout(10):  # never wait for ever
        async for event in session:
            if is_account_state(event) and event.message.request_ref == ref:
                state = event.message
                live = state.valuation_basis == LIVE_MARK
                print("inside a session" if live else "outside a session")
                if state.HasField("session_date"):
                    print("trading date:", state.session_date)
                if state.HasField("cash"):
                    print("cash:", to_decimal(state.cash))
                for position in state.positions:
                    print(position.instrument, position.quantity, to_decimal(position.price))
                if state.HasField("summary"):
                    print("equity:", to_decimal(state.summary.equity))
                    print("daily P&L:", to_decimal(state.summary.daily_pnl))
                break
            if is_order_event(event) and event.type == "reject":
                if request_ref_of(event.message) == ref:
                    print("refused:", reason_code_name(event.message.reason_code))
                    break
            # a real program handles market data and order events here too
except TimeoutError:
    print("no answer within 10 seconds")
```

The reply, `account_state`, echoes your `request_ref` and arrives on the same stream as your order events, so read it in your one loop. A program that reads only `market_data(session)` never sees it. Positions are your team's, not one strategy's: every instrument you hold a nonzero quantity of, positive for long and negative for short, in order of instrument. `valuation_basis` says what the prices and the summary are valued at: `LIVE_MARK` inside a session, at each instrument's mark, or at its last official close until it has a valid mark in that session; `LAST_OFFICIAL_CLOSE` outside a session, at each instrument's latest official close, a break day's close included, or at 0 for an instrument that has never had an official close. An option is named by its OCC option symbol without spaces, such as `SPY240119C00470000` (see step 5). `session_date` is a trading date. Inside a session it is the current session's date. Outside a session it is the date of the official closes the values use, those of the latest session or break day: on a break day, once that day's close has run, it is that day's date. That holds between terms too, when positions carried over from the term before are returned. What carries over into a new term depends on your team's arm: Fundamentals positions carry over, while Market Making, Market Taking and Options teams start each term with a fresh book. One exception is already fixed: 6 to 23 October 2026 is one term, but positions and cash start afresh at the start of the 7 October session. A second is fixed: Winter's practice week, 26 to 30 October 2026, does not carry over, and every team, Fundamentals included, starts the 2 November term flat, with a fresh book and cash. Query your account at the start of each session and each term rather than assume what carried over. Outside a session `daily_pnl` is the profit and loss of the session just finished. `session_date` is always there after the competition's first session, even if no instrument got an official close in it, and is absent only before that first session, when you hold nothing. `summary` is absent for an Execution desk and the house, and `cash` is absent only for an Execution desk, so check `HasField("summary")` and `HasField("cash")` first. The exchange sends `summary` from 6 October 2026; an exchange from before then omits it for every team. Treat its absence as unknown, never as zero. A refused query is a `reject` with `request_type` `ACCOUNT_QUERY` that echoes your `request_ref` whenever the exchange could read it. Read the `qte_sdk.account` docstring for every field.

The reply also carries `as_of_report_seq`: the newest of your private order reports it already reflects. Each private report (`accepted`, a delayed `reject`, `execution`, `order_cancelled`, `order_state`, `risk_notice` and `term_close_fill`) carries a report number, `event.report_seq` (None on other messages). The cut is for your account only: an `execution`, `risk_notice` or `term_close_fill` at or below `as_of_report_seq` is already in the reply's cash, positions and summary, while `accepted`, `reject`, `order_cancelled` and `order_state` still apply to your view of your resting orders whatever their `report_seq`. `as_of_report_seq` is absent when your team has had no private report this term, and then every report applies.

Where a fill arrives in the stream does not tell you whether the reply counts it; only its number does. A fill the reply already counts can arrive after the reply, and would be counted twice if you added it to the reply's positions. A fill the reply does not count can arrive between your query and the reply, and would be lost when you replace your positions with the reply's. `qte_sdk.account.AccountReports` applies the cut for you and handles both. Send the query with it, pass it every event, and apply what it returns, in order:

```python
from qte_sdk.account import (
    AccountReports,
    is_account_state,
    is_term_close_fill,
    term_close_position_change,
)
from qte_sdk.contract.v1.common_pb2 import BUY

account = AccountReports()
positions: dict[str, int] = {}
await account.query(session)
async for event in session:
    for item in account.update(event):  # every event, before you act on it
        if is_account_state(item):
            positions = {p.instrument: p.quantity for p in item.message.positions}
        elif item.type == "execution":
            fill = item.message
            change = fill.fill_size if fill.side == BUY else -fill.fill_size
            positions[fill.instrument] = positions.get(fill.instrument, 0) + change
        elif is_term_close_fill(item):  # the term-end close; its proceeds go to cash
            close = item.message
            change = term_close_position_change(close)
            positions[close.instrument] = positions.get(close.instrument, 0) + change
    # a real program handles market data and order events here too
```

For the reply to your query, `update` returns the reply itself, then the reports that arrived while you waited and that the reply does not include: replace your positions with the reply's, then apply those again on top, as above. For an `execution`, `risk_notice` or `term_close_fill`, it returns the event unless the latest reply already includes it, so a closing fill is applied once whichever side of the reply it arrives on. It returns nothing for anything else, so apply `accepted`, `reject`, `order_cancelled` and `order_state` to your resting orders as usual. That includes a reply to a query sent with `send_account_query`, or to an earlier query that a later one replaced: only the latest query sent with `account.query` is waited for, so wait for its answer before you query again, and take a reply as your positions only when `update` returns it. If you keep the reply yourself, `qte_sdk.account.covers(state, event)` says whether it already includes an event.

Report numbers start again each term, so a reply from the term before would take a new term's first reports for ones it already includes: before you trade in a new term, query again and wait for the reply, or start a new `AccountReports`. An exchange that does not number its reports sends no `as_of_report_seq`, and then every report applies; a reply can then be ordered against your fills only by when they arrive, which that exchange does not promise, so a fill close to a reply can be counted twice or missed.

The query is not an order message: the exchange does not hold it for the order delay or count it in your message budgets. It is a good way to check your positions again after a `SeqGap` or a dropped connection.

### Stock splits

Where the term posts splits (SPEC.md 12.2), a split takes effect at the open of the instrument's ex-date. The current case is MTUM, 4 new shares for 1 old at the open on Thursday 5 November 2026.

- Your position in the instrument is scaled at that open (for `a` new shares for `b` old, `a/b` times as many, rounding toward zero), and a fraction of a share is paid to a long, or charged to a short, as cash. No report and no report number comes with it: only an `account_state` shows it. After that open, query your account and wait for the reply before you trade. A program that keeps its own positions from fills, or with `AccountReports`, keeps the pre-split shares until it does.
- Orders resting in the instrument were already cancelled at the close before (`SESSION_CLOSE`). A pod's ordinary working tickets in it stop with `TICKET_SPLIT_CANCELLED`, their fills standing: if you still want the trade, send a new ticket in post-split shares, with any limit price in post-split prices. A cure ticket keeps working, its unfilled shares scaled to post-split shares, rounding toward zero, while at least one unfilled share remains after scaling. Its next `ticket_state` mixes units: `shares` is the shares filled plus the scaled unfilled shares (and, under `SERVICE` routing, `due_shares` is scaled too), while `filled_shares`, `decision_price` and `average_fill_price` stay in pre-split terms, and `progress` is empty from then on. So the shares still to fill are `shares - filled_shares`, in post-split shares.
- Until the instrument's first live quote at or after that open, it shows no wall and has no valid mark (a quote from before the open is dropped), and your position is valued at the scaled close. A ticket sent before that quote is refused `REFERENCE_UNAVAILABLE`, so wait for it before you resend. A book you hold from before the open, in `LatestBooks` say, is the last session's, in pre-split prices. At the open's first grid point the exchange sends a new book for the instrument, with no wall and condition `REFERENCE_UNAVAILABLE`, which replaces it: check a held book's `grid_time` before you read prices from it.
- Prices you kept from before the ex-date, and history, are in pre-split terms: history is not adjusted for splits. Multiply earlier prices by `b/a`, and earlier share counts by `a/b`, to compare them with later ones.

## 8. Values the exchange sets

**Every order message you send (new, cancel, amend and mass cancel) is held by the exchange for its order delay before it is applied. The delay is currently 150 ms.** An `accepted` therefore arrives at least that long after you send, and the book you acted on is at least that old by the time your order reaches it. Plan for it rather than around it.

The delay, the minimum time an order must rest before you may cancel or amend it, the price collar, your team's message budgets, and each instrument's tick and lot size are all set by the exchange and can change. Read tick and lot sizes from the instruments table (step 3). Do not build them into your code as constants. Instead:

- act on what the exchange reports: cancel or amend an order after its `order_state` arrives, not after a fixed sleep;
- keep your message rate well inside your budgets, and handle the rejects that say you went over one.

Some rejects you are likely to meet while learning (the exchange's rules decide exactly when each applies):

| Reason | What happened |
|---|---|
| `MARKET_CLOSED` | You sent an order message before the open, or on a day with no session at all (a weekend or an exchange holiday, for example). Check that `SessionState.state` is `OPEN` before you trade. |
| `RELEASE_AFTER_CLOSE` | On a day that had a session, your order message would have been applied after the close, once its order delay had passed. This includes anything sent after the close. |
| `MIN_REST_VIOLATION` | You cancelled or amended an order too soon after sending it. |
| `PRICE_COLLAR` | The price is outside the price collar. |
| `DUPLICATE_ORDER_AT_LEVEL` | Your team already has an order at that instrument, side and price. |
| `MESSAGE_BUDGET_EXCEEDED`, `BURST_CAP_EXCEEDED`, `NEW_ORDER_CAP_EXCEEDED` | You sent too many messages. Slow down. |

A `reject` can also carry `CURE_WINDOW`, a reason you otherwise see on `order_cancelled`: a new still waiting out its order delay when a cure window opens on a limit it falls under is rejected when it is released, so that it never rests. A reason from a newer contract than your SDK knows decodes as `REASON_CODE_UNSPECIFIED`; `event.unknown_enum_names()` returns the name the exchange sent.

**The term-end trading cutoff.** On the final session of a term, Market Making, Market Taking, Options MM and Options Taking teams stop trading five minutes before the close, and so do Agentic AI teams in a term whose file sets the exchange's `term_end.agentic_ai` to `true` (false or absent means off; Agentic AI sits out the Winter term and starts in Spring). Other teams trade on to the close. At the cutoff the exchange cancels each of your resting orders with an `order_cancelled` whose reason is `TERM_CUTOFF`. A new or amend still waiting out its order delay at that moment is dropped with no answer, while a cancel or mass cancel you sent before the cutoff, still waiting out its delay, still applies. Any order message of yours that the exchange receives at or after the cutoff is rejected `TRADING_CUTOFF`, even one you sent just before it. No `SessionState` phase marks the cutoff, so these two reasons are how your program learns of it: compare a reason with `ReasonCodes.TRADING_CUTOFF` or `ReasonCodes.TERM_CUTOFF` (from `qte_sdk.contract.v1.common_pb2`), or name it with `reason_code_name`. An SDK of 1.0.2 or older does not know them and decodes them as `REASON_CODE_UNSPECIFIED`, with the name in `event.unknown_enum_names()`.

**The term-end close.** At the official close of a term's final session (in Winter, Friday 4 December 2026), the exchange closes every remaining position of every Market Making and Market Taking team, and of every Agentic AI team in a term that sets `term_end.agentic_ai`, at the instrument's official close. Each position closed arrives as one `term_close_fill` (`TermCloseFill`, picked out with `qte_sdk.account.is_term_close_fill`): `instrument`, `side` (`SELL` closes a long, `BUY` a short), `size`, `price`, `proceeds` (the signed cash booked), `session_date`, and the flags `frozen` and `prior_close` when the close is a fallback. It is not an `execution`: no `strat_id`, order id or fee. It carries a `report_seq` like a fill, a resume replays it, and an `account_state` covers it up to `as_of_report_seq`, so `AccountReports` applies it once; `qte_sdk.account.term_close_position_change(event.message)` gives its signed position change. Fundamentals and Execution teams are not closed. Once the close has run, query `account_state` again and take its positions and cash as the term's last word, then stop trading: in the break between terms no session runs, a Fundamentals pod may send break requests (step 6), and what carries into the next term is as "Query your account" in step 7 says. Director sessions are also sent the house's closing fills (`house` true, unnumbered) and a `term_end`; a team never is.

## 9. If the connection drops

Iterating a session ends normally when the exchange closes the connection, and raises `websockets.exceptions.ConnectionClosedError` if it drops. A session from `open_session` does not reconnect, and neither do the worked examples `examples/take_liquidity.py`, `examples/smoke_test.py`, `examples/fundamentals_pod.py` and `examples/out_of_hours.py`: open a new session yourself, or use `ReconnectingSession` below, as `examples/quote_both_sides.py`, `examples/options_bot.py` and `examples/print_book.py` do. Your orders may still be resting after a drop, so check before you trade again.

The exchange sends a heartbeat at a regular interval, at any hour, so a working connection is never silent for long. The SDK absorbs heartbeats: they never appear among your events, and you send nothing back. Once the first heartbeat has arrived, if nothing at all arrives for `liveness_timeout` seconds, the SDK treats the link as dead, drops it and raises `qte_sdk.connection.LivenessTimeout`. The check starts only with that first heartbeat, so an exchange that does not send heartbeats is never dropped for being quiet. The default, 45 seconds, is the SDK's own choice, not a value the exchange sends; pass `liveness_timeout=` to `open_session` or `ReconnectingSession` to change it, or `None` to turn the check off.

To check that heartbeats are arriving, read `session.heartbeats_received` (how many the connection has read since it opened), `session.last_heartbeat_at` (when the latest was read, on the `time.monotonic()` clock, so `time.monotonic() - session.last_heartbeat_at` is how many seconds ago) and `session.last_heartbeat_sent_at` (the exchange's send time of the latest, in milliseconds since the epoch, or `None` if that heartbeat carried none; `qte_sdk.units.to_datetime` shows it as a time). They are 0 and `None` until the first heartbeat, and start again on each new connection. A heartbeat is counted when the session reads it: while `open_session` waits for the acknowledgement, while `wait_for_calendar` or `resume` reads ahead, and then as your loop iterates the session. So they can be above 0 before your first event, and they stop moving while nothing reads the session. On a `ReconnectingSession` they describe the connection of the latest `Connected`, and keep their values after its `Disconnected` until the next `Connected`. How often the exchange sends heartbeats is its own setting, so do not build an interval into your program.

The exchange also drops a connection that has sent it nothing for a while, with close code 4000 and reason `heartbeat timeout`. You do not need to send anything: in the background, the `websockets` library answers the exchange's pings and sends pings of its own. It can only answer a ping, or see the reply to its own, while it is reading the connection, and it pauses reading once more than 16 frames (its `max_queue` option) are waiting for your loop. If your loop stops reading for long, the connection is closed: by the library itself, with code 1011 and reason `keepalive ping timeout`, or by the exchange with 4000. Either way iterating raises `websockets.exceptions.ConnectionClosedError`, which `ReconnectingSession` treats as a drop and reconnects. Keep the loop that reads events quick, and do slow work in another task.

When one term ends and the next begins, the exchange closes every connection open at that moment, with close code 4001 (`qte_sdk.connection.TERM_CHANGE_CLOSE_CODE`) and reason `term change`, so no connection carries reports of two terms. It first sends each connection every message already queued for it, so the close is the last thing on the wire and never overtakes a report of the old term already on its way. It does not hold the close back for a loop that has fallen behind: that connection gets what was queued for it, then the close. Iterating raises `ConnectionClosedError` as for any drop. The code itself tells you the term changed, without waiting for the next calendar: on a `Disconnected` whose `error` is a `ConnectionClosedError`, `error.rcvd.code` is 4001. Report numbers start again at 1 in the new term, so the old term's number means nothing there. `ReconnectingSession` follows the exchange's rule for this: on a 4001 close it forgets the number at once, with or without a resume and whatever the calendar says, and reconnects. The new session asks from 0 for a snapshot, or, with `resume=False`, counts the new term's reports from the first it reads, as a first session does. It does the same when the exchange's 4001 cuts off an attempt to reconnect before the session is up, or arrives while the SDK is closing a connection it has given up on, even if that attempt is then cancelled. If the exchange rejected the session just before the 4001, the rejection stays the error and is not retried, though the number is still forgotten.

When your team's token is revoked or rotated, the exchange closes every connection authenticated with it, with close code 1008 and reason `credential revoked` (`qte_sdk.connection.CREDENTIAL_REVOKED_CLOSE_CODE` and `CREDENTIAL_REVOKED_CLOSE_REASON`). The old token is refused from then on, so `ReconnectingSession` does not reconnect: iterating raises the `ConnectionClosedError`, or, if the close came while it was connecting or resuming, a `SessionNotAcknowledged` or `ResumeNotAcknowledged` whose `credential_revoked` is True. Get the new token and start again. An exchange that is stopping closes with 1001 and reason `exchange stopping`, and one whose sequencer has stopped with 1011 and `sequencer stopped`; both are retried like any drop. Those reasons, `term change`, `heartbeat timeout` and `keepalive ping timeout` are the close reasons the SDK shows; any other shows as `<withheld>`, because a close reason is free text that could echo the token.

The SDK acts on a 4001 once the `websockets` library has read it. If your loop has fallen so far behind that reading paused (more than `max_queue` frames waiting) and the connection then drops with the 4001 still unread, nothing tells the SDK that the term changed, and the number is kept. Such a program is then in the same position as one that was not connected at the change, below.

A program that is not connected when the term changes, because it was stopped or its connection had already dropped, is not sent the close. For it, and for one whose 4001 went unread, `ReconnectingSession` forgets the number by checking the new session's calendar against the term the number was counted in (see below); with `resume=False` it can do so only when both terms are known. If you resume a session you opened yourself, pass 0 after a 4001 close, and otherwise compare the terms the same way before you pass a number to `resume`. A 4001 close can also cut off `open_session` or `session.resume`. The error raised then carries the code: as `close_code` on a `SessionNotAcknowledged` or `ResumeNotAcknowledged`, or as `rcvd.code` on a `ConnectionClosedError`.

Each of your team's private order reports (`accepted`, a `reject` sent once the order delay is over, `execution`, `order_cancelled`, `order_state`, `risk_notice` and `term_close_fill`) carries a report number, `event.report_seq`, which counts up by one for each report your team receives. A session delivers reports in that order and keeps `session.last_report_seq`, the number up to which it has delivered every one. If a number is skipped, a `ReportGap` event comes first. A report the exchange fails to build is dropped without a number, so it causes no gap, nothing tells you it is missing, and a replay does not bring it back. Only a snapshot puts your resting orders right again (a resume answered with one; `session.resume(0)` asks for one), and the dropped report itself, a fill for example, is never delivered.

`qte_sdk.reconnect.ReconnectingSession` reconnects for you and resumes each new session, so the reports you missed are not lost:

1. On a drop it delivers `Disconnected`. Treat it as the moment your positions, resting orders and book became uncertain.
2. It opens a new session, authenticates, and sends `resume` with the last report number it delivered. The exchange answers with `resume_ack`, and the new session subscribes again. You get `Connected`, whose `resume` is that answer.
3. The exchange then either replays every report you missed, exactly as first sent, or, if it no longer holds them all, sends one `order_snapshot` event per resting order of your team instead. Either way a `ResumeComplete` event follows, and then the reports that arrived meanwhile. A snapshot with no orders means your team has none resting, which is always so outside a session.

The first session resumes too, from 0, so it always starts with a snapshot of your resting orders. Report numbers start again each term, so after a reconnect in a new term (as the calendar's `term_start` and `term_end` say) the session also asks from 0 and gets a snapshot, rather than sending a number from the old term. With `resume=False` the number is still carried over, so that a report missed while disconnected is flagged with a `ReportGap`, and it is forgotten in a new term, so the new term's first reports are not dropped as duplicates. To check the term, a new session waits briefly for the calendar before it carries a number over. Report numbers also belong to one event log, which each session's `session.info.log_id` names: when a new session names a different log than the one the number was counted under, the number is forgotten the same way, and `QTE-SESSION-LOG-CHANGED` is logged. When either `log_id` is unknown (absent or malformed), the number is forgotten too, without a warning, as the contract asks: keep a number only under the same, known `log_id`. A `RestingOrders` view you pass as `resting=`, or that follows the session, reads incomplete from each `Connected` until that resume's `ResumeComplete`, is loaded from that snapshot, and is marked incomplete on `Disconnected`. A snapshot always makes it complete again at `ResumeComplete`. A replay does so only if the disconnect was the only thing that made it uncertain: after a `SeqGap`, a `ReportGap` or an order message that could not be decoded, it stays incomplete until a snapshot. Check `view.incomplete` before you trust it.

Market data is not replayed: what was published while you were disconnected is lost, and the new session's subscription delivers the latest books from then on. An order in flight when the connection dropped may or may not have reached the exchange, and the SDK never sends it again. If it arrived and the exchange replays what you missed, the replayed reports tell you what became of it; a snapshot shows only whether it is resting now, not whether it filled or was rejected. Either way, the resume covers only what the exchange had applied when it answered, up to its `as_of_report_seq`. A message the exchange received before the drop but had not applied yet, such as a new still waiting out its order delay, can be applied just after that: neither the replay nor the snapshot shows it, and its reports come after `ResumeComplete`, with the reports that arrived meanwhile. So an order missing from the resume may still rest. In that window `examples/quote_both_sides.py` takes the side's order as gone and may send a second new, which can leave the first resting (its drop message names each order by the request_ref of its new, so you can find it), and `examples/options_bot.py` may report that nothing of its order rests and stop while it does. To resume a session you opened yourself, call `await session.resume(last_report_seq)` right after `open_session`, before reading events, passing the previous session's `last_report_seq` if the new session's calendar names the same term and its `session.info.log_id` is known and the same as the one you saved with that number, or 0 for a snapshot. `await session.wait_for_calendar()` before it is fine, since it keeps every event it reads for you. A `RestingOrders` view that follows such a session learns of the resume only when the `resume_ack` event reaches it, and other events, such as the calendar, can come first, so call `view.mark_incomplete()` before `resume(0)`: the snapshot then makes the view complete.

An exchange that does not offer resume yet answers `resume` with a `reject`. `session.resume` then raises `qte_sdk.session.ResumeRejected` and the session carries on. `ReconnectingSession` carries on too: its `Connected.resume` is None, the `reject` is delivered as an event, and it behaves as it did before resume existed, so what you missed while disconnected is not recovered and the resting view stays incomplete after a reconnect.

## 10. Past market data (history)

The exchange's history service serves the market data of sessions that have closed, going back to the first session held, at any hour. It serves exactly what the live feed published to everyone, message for message, so you get the same `Book`, `Trades`, `Mark` and `SessionState` classes as from `market_data` and the same handling code works on both. It holds nothing else: no raw feed, and nothing about other teams.

It is a separate service with its own address, `https://history.trading.queenstowerexchange.com`. Set it as `QTE_HISTORY_URL`; the SDK has no default. Your token comes from `QTE_TOKEN` as before. No session is needed: this runs in any `async` function.

```python
from qte_sdk.connection import DecodeFailed, Unknown
from qte_sdk.history import HistoryClient, HistoryPending, HistoryUnavailable
from qte_sdk.market_data import Book
from qte_sdk.units import to_datetime, to_decimal

client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN
try:
    async for item in client.fetch("2026-01-05", "AAPL", "book"):
        if isinstance(item, Book) and item.bid_levels:
            print(to_datetime(item.grid_time), to_decimal(item.bid_levels[0].price))
        elif isinstance(item, (Unknown, DecodeFailed)):
            print("could not use a message:", item)
except HistoryUnavailable:
    print("there is no such data: not a session day, or an unknown instrument")
except HistoryPending as error:
    print("not ready yet; ask again in", error.retry_after, "seconds")
```

- `fetch(date, instrument, channel)` takes a session date, one instrument and one of `book`, `trades` or `mark`, or `option_greeks` for an option contract. `fetch_session_state(date)` gives the market session state, and `fetch_option_chain(date)` the session's option chain. Messages arrive in the order they were published, as they download, so a whole day never has to fit in memory.
- A session that has closed is not ready straight away. The client waits and asks again after as long as the service says, spending at most a minute waiting in all (`max_wait`), then raises `HistoryPending`. A session still in progress, or one in the future, is not served until it closes: `fetch` raises `HistoryNotClosed` at once, without waiting, so ask again after the close.
- `HistoryUnavailable` means the data will never exist: a weekend or holiday, a date before the service began, or an instrument or channel it does not know. `HistoryNotImplemented` means the service does not serve that yet, which may change in a later release. Other answers from the service raise the other `HistoryError` classes in `qte_sdk.history`, such as `HistoryUnauthenticated` for a bad token; the service's own status word is in the error's `status`, and it may add new ones. If the service cannot be reached, you get the usual Python error, such as `ConnectionRefusedError`, or `TimeoutError` when one network step (connecting, or one read) takes longer than `timeout` seconds (30 by default). A download whose connection drops part way is resumed where it stopped, up to `max_resumes` times (3 by default); once those are used up you get `HistoryInterrupted`, and if the service cannot be reached to resume, the network error.
- `fetch_range(from_date, to_date, instruments, channels)` fetches several instruments and channels over a span of dates in one download. It yields a `Manifest` first, listing every entry you asked for as `ready`, `pending` (still being built), `not_closed` (its session has not closed yet) or `unavailable` (will never exist); only the ready ones follow, in the manifest's order. It never waits: ask again later for the pending ones (`Manifest.pending`), and after the close for the `not_closed` ones (`Manifest.not_closed`).
- An option contract is an instrument here too, named by its OCC symbol (such as `SPY261120C00650000`), and `instruments` may mix it with equities. Its channels are `book`, `trades`, `mark` and `option_greeks`, every Greeks message published for it; an equity has no `option_greeks`, so asking for it is unavailable. A contract the session's chain did not list is unavailable for that session. `option_greeks` and `fetch_option_chain` follow the history contract of qte-platform #2298, whose server build is live. A session with options cached before it had no Greeks, no chain and nothing for any channel on which a listed contract published nothing (a contract that quoted but never traded has no `trades`) until an upgrade of that session was run; an upgrade adds the missing objects and leaves every object already cached unchanged, so nothing needs re-fetching. The sessions of 15 to 21 October 2026 were the only ones with options cached before that build, and all five have been upgraded, so every session with options now serves its chain and Greeks and an unavailable answer is final. For a channel that is unavailable, `fetch` raises `HistoryUnavailable`, and so does a default `replay` of the contract unless you pass `skip_unavailable=True`.
- If the connection drops, the client resumes the download where it stopped, and checks what arrived against the digest the service states for it. If the service sends the whole data again, unchanged, the client skips what you already have, so no message arrives twice; if the data has changed meanwhile, you get `HistoryChanged` and should start again.
- A manifest entry may carry a status word this SDK does not know yet. It is passed on in the `Manifest` as it is, and its data is not included.
- A `book` stream is sparse, like the live feed: it starts with the session's first book of the instrument and then holds only the books that changed. The book in force at time `t` is the last one with `grid_time` at or before `t`. To get it, feed the stream to a `LatestBooks` and stop at the first book later than `t`; `aclosing` closes the download when you stop early:

  ```python
  from contextlib import aclosing
  from datetime import UTC, datetime
  from qte_sdk.books import LatestBooks
  from qte_sdk.units import to_timestamp

  t = to_timestamp(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))  # 15:00 UTC, 10:00 in New York
  books = LatestBooks()
  async with aclosing(client.fetch("2026-01-05", "AAPL", "book")) as items:
      async for item in items:
          if isinstance(item, Book) and item.grid_time > t:
              break
          if isinstance(item, (Unknown, DecodeFailed)):
              raise RuntimeError(f"a message could not be used, so the book may be wrong: {item}")
          books.update(item)
  book_at_t = books.get("AAPL")  # None if t is before the session's first book
  ```

- History tells you what the market published, not how your own orders would have filled against it.

To run your strategy's loop over a whole past session, `replay(client, date, instruments)` from `qte_sdk.replay` merges that session's books, trades, marks and market session state into one stream, as the same classes `market_data` yields, so the code that handles live market data runs on it unchanged. Grid points come in order, as on a live connection; the messages of one grid point come in no promised order, live or replayed, so treat them as a set. It is market data only: it sends no orders and fills nothing. If a stream's times go back, it raises `qte_sdk.replay.ReplayOutOfOrder` rather than deliver anything out of order. [Developing your algo](developing-your-algo.md) shows how to use it, and the path from there to the exchange.

## 11. Worked examples

Run the examples from your project folder, with the `examples` folder copied into it (step 1), and on Windows type `py examples\print_book.py` and so on. Each example reads `QTE_URL` and `QTE_TOKEN` as step 2 describes, from the environment or a `.env` in the folder you run it from, runs for a bounded time and then stops by itself, prints every reject with its reason, and exits with status 0 when it has run cleanly. The instrument comes from `--instrument` (`--instruments` for the smoke test) or `QTE_INSTRUMENT`, and the examples that send orders take your strategy ID from `--strat-id` or `QTE_STRAT_ID`. Run any of them with `--help` for its options.

| Example | What it shows |
|---|---|
| `examples/smoke_test.py` | Run this first. Check a setup end to end: whether the SDK is the latest release, where the token and address come from, the session, the calendar, the market, the account query and past data, with `PASS`, `FAIL` or `SKIP` for each check (step 2). Sends no orders unless given `--place-test-order`. |
| `examples/print_book.py` | Connect, subscribe and print the book, trades, mark and market session state, or the official close outside a session. Sends no orders. Stops after `--seconds` or `--max-messages`. |
| `examples/quote_both_sides.py` | Rest a limit order on each side, inside the wall's best prices, and manage them: cancel and re-enter when the wall moves, amend the size back up after a partial fill, re-enter after a full fill. Keeps the latest book with `LatestBooks` and acts on it after each order event and on its own timer, not only when a new book arrives, since the exchange publishes a book only when it changes. Cancels its own orders when `--seconds` are up. |
| `examples/take_liquidity.py` | Send one market order once the latest book shows the side it trades against, and report its fills. Sends at most one order and never retries. Stops when the order is finished or after `--seconds`. |
| `examples/options_bot.py` | Read an option underlying's chain from the instruments table and the subscribe, keep the latest Greeks with `LatestGreeks`, pick the contract nearest the forward in the nearest expiry, passing over one that is not trading, and rest one limit buy of one contract one tick above the wall's bid. Cancels it once it rests, and on `--seconds` or Ctrl+C, and names the level if the cancel is not confirmed. Prints a reject by name and never retries the order. With `--watch-only` it sends nothing: it prints the order it would send (`WOULD SEND (watch-only, not sent):`) and exits 0, or 1 with the reason no contract could be chosen. |
| `examples/out_of_hours.py` | Outside a session: read the calendar, subscribe, and print the closed market's session state and the wait until the next open. Sends no orders, and stops at once if a session is under way. The runnable part of [Using the SDK outside session hours](out-of-hours.md). Stops after `--seconds`. |
| `examples/replay_book.py` | Replay one instrument's book from a past session with `qte_sdk.replay` and print the best bid and ask each time it changes. It reads the history service, so it needs `QTE_HISTORY_URL` and your token but not `QTE_URL`, and works at any hour for a session that has closed. Sends no orders. Stops after `--max-books`, `--seconds` or the end of the session's data. |
| `examples/fundamentals_pod.py` | For a Fundamentals pod's token, while the exchange's automatic execution service is on. Print the `execution_route` and the position from `account_state`, send one ticket of `--shares` at the hard limit `--limit` (a VWAP ticket refused 1713 is sent once more as TWAP), and print its states until it stops, then the position again. With `ROUTING_DESK` or no route it sends nothing and exits with status 1. Stops following the ticket after `--seconds`; a ticket still working keeps working on the exchange. |

```sh
python examples/smoke_test.py --instruments AAPL MSFT
python examples/print_book.py --instrument AAPL --seconds 10
python examples/quote_both_sides.py --instrument AAPL --strat-id my-strategy --seconds 30
python examples/take_liquidity.py --instrument AAPL --strat-id my-strategy --side buy --size 1
python examples/options_bot.py --underlying SPY --strat-id my-strategy --seconds 30
python examples/options_bot.py --underlying SPY --strat-id my-strategy --seconds 30 --watch-only
python examples/out_of_hours.py --instrument AAPL
python examples/replay_book.py --date 2026-01-05 --instrument AAPL
python examples/fundamentals_pod.py --instrument AAPL --side buy --shares 500 --limit 201.50 --thesis-id t-aapl --thesis-version 1
```

For a Fundamentals pod: from the Winter term, the exchange refuses a ticket that opens or increases a position unless it names a signed thesis your pod has filed that covers the ticket's instrument and side, at its current version (SPEC 23.1). The refusal is `TICKET_THESIS_NOT_FILED`, with a `reason_detail` saying what is missing. Your Team Lead files and signs the thesis on the Research page of the Team Lead console, which gives it its thesis ID and version; pass its ID and its latest signed version as `--thesis-id` and `--thesis-version`. A version that a later revision has replaced is refused too. `t-aapl` in the examples is a placeholder that the exchange refuses: use your pod's own filed thesis ID. A ticket that reduces a position needs no thesis.

The examples are for learning the SDK, not strategies: they make no attempt to make money. When you are ready to build your own, [Developing your algo](developing-your-algo.md) walks through the path from past market data to the exchange.
