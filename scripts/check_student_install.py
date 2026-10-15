"""Check, in CI's windows-install job, the SDK as a student installs and runs it on Windows.

    python scripts/check_student_install.py install KIND PATH
    python scripts/check_student_install.py help PREFIX
    python scripts/check_student_install.py update FILE CODE

The student's folder is `STUDENT_HOME` from the environment; CI makes it a path with a
space and a non-ASCII character (`Student Ü`), which CI's own temp folder never has.

`install` makes a fresh virtual environment in `STUDENT_HOME/KIND` (`wheel`, `sdist` or
`zip`), installs PATH (the built wheel or sdist, or a zip of the source tree as GitHub's
release archive is) into it with that environment's pip, and then, from
`STUDENT_HOME/work`, outside the checkout, checks that:

- the `qte_sdk` imported is the one installed in that environment;
- `python -m qte_sdk.update --json` exits 0, 1 or 2 (current, behind, can't tell; it may
  have no network) and prints one JSON document whose `exit_code` is that status;
- `python -m qte_sdk.agents` exits 0 and prints the packaged AGENTS.md;
- `python -m qte_sdk.token check`, with no token anywhere, exits 1 and says
  QTE-TOKEN-MISSING on its `token:` line.

`help` and `update` check what a shell step wrote, for the steps that run the SDK straight
from Git Bash and cmd, so that the shell, not this script, starts it: `help PREFIX` that
each example in examples/ has a file `PREFIX<name>.txt` in the working directory holding
its `--help` (a `usage:` line), so a loop that ran no example fails; `update FILE CODE`
that `python -m qte_sdk.update` exited 0, 1 or 2 and wrote two lines, `installed:` and the
result on one line, which is the QTE-UPDATE-AVAILABLE line when the status is 1.

Nothing is set to change how Python encodes its output: the children get the environment
as it is, without QTE_ variables, PYTHONPATH, PYTHONUTF8 or PYTHONIOENCODING, and their
output is decoded in the locale's encoding, as their stdout on a pipe encodes it, so a
character the student's code page cannot hold fails here as it would for the student.
Prints what it checked and exits 0, or prints the first problem and exits 1.
"""

import json
import locale
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
AGENTS_DOC = ROOT / "qte_sdk" / "AGENTS.md"
KINDS = ("wheel", "sdist", "zip")
# The update check's own limit on the network, and how long any command may take.
UPDATE_TIMEOUT = "5"
TIMEOUT = 120
INSTALL_TIMEOUT = 600
UPDATE_CODES = (0, 1, 2)
UPDATE_AVAILABLE = "QTE-UPDATE-AVAILABLE"
# Left out of the children's environment: none is set on a student's computer, and each
# would change what is tested.
UNSET = ("PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "PYTHONIOENCODING")


class Failed(Exception):
    """A check failed; the message says which and what was seen."""


def venv_python(folder: Path) -> Path:
    """The Python of the virtual environment in `folder`, as a student runs it."""
    if sys.platform == "win32":
        return folder / "Scripts" / "python.exe"
    return folder / "bin" / "python"


def child_env() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("QTE_") and name.upper() not in UNSET
    }


def decode(data: bytes) -> str:
    """A child's output, in the encoding its stdout on a pipe uses, with Windows line
    endings made plain."""
    try:
        text = data.decode(locale.getencoding())
    except UnicodeDecodeError as error:
        raise Failed(
            f"output not in the locale's encoding {locale.getencoding()}: {error}"
        ) from None
    return text.replace("\r\n", "\n")


def run(python: Path, *args: str, cwd: Path) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            [str(python), *args],
            cwd=cwd,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise Failed(f"{' '.join(args)} was still running after {TIMEOUT} s") from None
    return done.returncode, decode(done.stdout), decode(done.stderr)


def check_import(python: Path, home: Path, cwd: Path) -> None:
    code = (
        "import locale, sys, qte_sdk; "
        "print(qte_sdk.__file__); "
        "print(qte_sdk.__version__, sys.flags.utf8_mode, locale.getencoding(), "
        "sys.stdout.encoding, sys.getfilesystemencoding())"
    )
    status, out, err = run(python, "-c", code, cwd=cwd)
    if status != 0:
        raise Failed(f"importing qte_sdk exited {status}:\n{out}{err}")
    lines = out.splitlines()
    if len(lines) != 2:
        raise Failed(f"importing qte_sdk printed {lines!r}")
    imported = Path(lines[0]).resolve()
    if not imported.is_relative_to(home.resolve()):
        raise Failed(f"the qte_sdk imported is {imported}, not the one installed in {home}")
    print(f"  qte_sdk from the environment; version, utf8 mode, encodings: {lines[1]}")


def check_update_json(status: int, out: str, err: str) -> None:
    if status not in UPDATE_CODES:
        raise Failed(f"update --json exited {status}, not one of {UPDATE_CODES}:\n{out}{err}")
    lines = out.splitlines()
    if len(lines) != 1:
        raise Failed(f"update --json printed {len(lines)} lines, not one JSON document:\n{out}")
    try:
        document = json.loads(lines[0])
    except ValueError as error:
        raise Failed(f"update --json printed what is not JSON ({error}):\n{out}") from None
    if not isinstance(document, dict) or document.get("exit_code") != status:
        raise Failed(f"update --json exited {status} but its document says:\n{out}")
    print(f"  update --json: exit {status}, status {document.get('status')!r}")


def check_agents(status: int, out: str, err: str) -> None:
    if status != 0:
        raise Failed(f"agents exited {status}:\n{err}")
    expected = AGENTS_DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    if out != expected:
        raise Failed(f"agents printed {len(out)} characters, not the {len(expected)} of AGENTS.md")
    print("  agents: exit 0, the packaged AGENTS.md")


def check_token(status: int, out: str, err: str, home: Path) -> None:
    if status != 1:
        raise Failed(f"token check with no token exited {status}, not 1:\n{out}{err}")
    found = [line for line in out.splitlines() if line.startswith("token:")]
    if len(found) != 1 or "QTE-TOKEN-MISSING" not in found[0]:
        raise Failed(f"token check did not say QTE-TOKEN-MISSING on one token: line:\n{out}")
    if str(home) in out + err:
        raise Failed(f"token check printed the student's folder:\n{out}{err}")
    print("  token check: exit 1, QTE-TOKEN-MISSING")


def install(kind: str, artifact: Path, home: Path) -> None:
    folder = home / kind
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)
    print(f"{kind}: {artifact.name} into {folder}")
    venv.EnvBuilder(clear=True, with_pip=True).create(folder)
    python = venv_python(folder)
    try:
        done = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(artifact)],
            cwd=work,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            timeout=INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise Failed(f"installing {artifact} took over {INSTALL_TIMEOUT} s") from None
    if done.returncode != 0:
        raise Failed(f"installing {artifact} exited {done.returncode}")
    check_import(python, folder, work)
    check_update_json(
        *run(python, "-m", "qte_sdk.update", "--json", "--timeout", UPDATE_TIMEOUT, cwd=work)
    )
    check_agents(*run(python, "-m", "qte_sdk.agents", cwd=work))
    check_token(*run(python, "-m", "qte_sdk.token", "check", cwd=work), home=home)


def check_help(prefix: str, cwd: Path) -> None:
    examples = sorted(EXAMPLES.glob("*.py"))
    if not examples:
        raise Failed(f"no example found in {EXAMPLES}")
    for example in examples:
        path = cwd / f"{prefix}{example.stem}.txt"
        if not path.is_file():
            raise Failed(f"{example.name} --help left no {path.name}")
        text = decode(path.read_bytes())
        if not text.startswith("usage: "):
            raise Failed(f"{example.name} --help did not start with a usage: line:\n{text}")
    print(f"--help: {len(examples)} examples, each with a usage: line")


def check_update_text(path: Path, status: int) -> None:
    text = decode(path.read_bytes())
    if status not in UPDATE_CODES:
        raise Failed(f"update exited {status}, not one of {UPDATE_CODES}:\n{text}")
    lines = text.splitlines()
    if len(lines) != 2 or not lines[0].startswith("installed: "):
        raise Failed(f"update printed {len(lines)} lines, not installed: and the result:\n{text}")
    if (status == 1) != lines[1].startswith(UPDATE_AVAILABLE + ": "):
        raise Failed(f"update exited {status} but its result line is:\n{lines[1]}")
    print(f"update: exit {status}, one result line: {lines[1][:100]}")


def student_home() -> Path:
    value = os.environ.get("STUDENT_HOME")
    if not value:
        raise Failed("STUDENT_HOME is not set")
    return Path(value)


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "install" and argv[1] in KINDS:
            install(argv[1], Path(argv[2]).resolve(), student_home())
        elif len(argv) == 2 and argv[0] == "help":
            check_help(argv[1], Path.cwd())
        elif len(argv) == 3 and argv[0] == "update" and argv[2].lstrip("-").isdigit():
            check_update_text(Path(argv[1]), int(argv[2]))
        else:
            print("\n".join(__doc__.strip().splitlines()[2:5]), file=sys.stderr)
            return 2
    except Failed as failure:
        print(f"FAILED: {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
