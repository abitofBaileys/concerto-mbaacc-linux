#!/usr/bin/env python3

# First dirty attempt at controlling caster from outside of wine
# For now, Concerto does not matter, if this proof of concept does not work,
# then gotta look into other means to check if this is possible to begin with

# Mainly this should run tests for a few things:
# - pexpect.spawn using pty
# - pexpect.popen_spawn.PopenSpawn using pipe
# - passing cccaster launch arguments
# - logging ANYTHING, even raw
# - improving logging
# - output detection for menus
# - terminating caster
# - verify termination with psutil

import argparse, datetime, time, os, re, shlex, shutil, signal, subprocess, sys
from typing import Dict, List, Tuple, Optional, Sequence

# just in case we need it, this matches ANSI escape sequences so we can remove them from streams
# from https://stackoverflow.com/a/33925550
ANSI_ESCAPE = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]")

# caster patterns to test
DEFAULT_PATTERNS = ["CCCaster", "Netplay", "Spectate", "Broadcast", "Offline", "Server", "Controls", "Settings", "Update", "Results", "Quit"]

# list of possible process names
# if we spawn caster within wine it's not guaranteed that the process is called the same as in windows so best is to cycle through a list of possible terms
PROCESS_KEYWORDS = ["cccaster", "mbaa", "wine", "wine64", "wineserver", "wineconsole"]

# =============================================================
# MAIN
# =============================================================

def main() -> int:
    args = parse_args()

    # setup paths and logs
    cwd = os.path.abspath(args.cwd)
    wine_debug = args.wine_debug if args.wine_debug != "" else None
    env = build_env(args.wine_prefix, wine_debug)

    # argument build based on caster arguments
    caster_args = build_caster_args(
        args.caster_mode,
        port=args.port,
        address=args.address,
        no_ui=args.no_ui,
        raw_args=args.caster_args
    )

    no_fork = args.no_fork or "no-fork" in args.launch_mode
    use_wineconsole = "wineconsole" in args.launch_mode

    argv = build_argv(args.wine_cmd, args.caster_exe, caster_args, no_fork=no_fork, use_wineconsole=use_wineconsole)

    if args.log_dir:
        logs_dir = os.path.abspath(args.log_dir)
    else:
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-logs", timestamp())

    enforce_dir(logs_dir)
    main_log = os.path.join(logs_dir, "test-summary.log")

    # add environment info to the log
    header_info = [
        "# ENVIRONMENT\n",
        "platform=%r\n" % sys.platform,
        "python=%r\n" % sys.version,
        "cwd=%r\n" % cwd,
        "argv=%r\n" % argv,
        "WINEPREFIX=%r\n" % env.get("WINEPREFIX"),
        "WINEDEBUG=%r\n" % env.get("WINEDEBUG"),
        "launch_mode=%r\n" % args.launch_mode,
        "caster_mode=%r\n" % args.caster_mode
    ]
    for msg in header_info: write_log(main_log, msg)

    # wine check
    wine_version_code, wine_version_output = run_command([args.wine_cmd, "--version"], cwd, env)
    write_log(main_log, "\n# WINE VERSION\nexit code=%r\noutput=%s\n" % (wine_version_code, wine_version_output))

    # requirement checks
    errors = check_requirements(args.wine_cmd, args.caster_exe, cwd, args.launch_mode)
    if errors:
        write_log(main_log, "\n# REQUIREMENT ERROR\n")
        print("Requirement check failed, see log:\n%s" % main_log)
        for error in errors:
            write_log(main_log, "- %s\n" % error)
            print("- %s" % error)
        return 2

    # make a snapshot of running processes before we start
    log_process_snapshot(main_log, "processes before", PROCESS_KEYWORDS)
    before_processes = snapshot_processes(PROCESS_KEYWORDS)

    # =============================================================
    # START TEST
    # =============================================================

    send_key = None if args.no_send else args.send_key
    results = {}

    # test pexpect.spawn (pty)
    if args.launch_mode == "spawn":
        print("Testing spawn backend...")
        results["spawn"] = test_spawn_backend(argv, cwd, env, logs_dir, DEFAULT_PATTERNS, send_key, args.read_seconds)

    # test pexpect.popen_spawn (pipe)
    elif args.launch_mode == "popen":
        print("Testing popen backend...")
        results["popen"] = test_popen_backend(argv, cwd, env, logs_dir, DEFAULT_PATTERNS, send_key, args.read_seconds)

    # test subprocess.Popen
    else:
        print("Testing subprocess backend (%s)..." % args.launch_mode)
        results["subprocess"] = test_subprocess_backend(argv, cwd, env, logs_dir, args.read_seconds)

    # diff new processes since first snapshot
    created_after_all = extract_new_processes(before_processes, snapshot_processes(PROCESS_KEYWORDS))

    # reporting & killing spawns
    write_log(main_log, "\nRESULTS\n")
    for backend, success in sorted(results.items()):
        write_log(main_log, "%s=%r\n" % (backend, success))

    write_log(main_log, "\nCREATED PROCESSES AFTER ALL\n")
    for row in created_after_all: write_log(main_log, "%r\n" % row)

    # try to kill whatever processes have been spawned
    terminate_created_processes(created_after_all, main_log)
    time.sleep(1)
    # make a snapshot of processes after killing spawns
    log_process_snapshot(main_log, "processes after killing spawns", PROCESS_KEYWORDS)

    print("\nTest complete.\nLogs:\n%s\n\nResults:" % logs_dir)
    for backend, success in sorted(results.items()):
        print("- %s: %s" % (backend, "PASS" if success else "FAIL"))

    return 0 if any(results.values()) else 1

# =============================================================
# Helpers
# =============================================================

def clean_output(text: str) -> str:
    text = ANSI_ESCAPE.sub("", text).replace("\x08", "").replace("\r", "\n")
    return " ".join([p.replace("*", "").strip() for p in text.split() if p.replace("*", "").strip()])

def detect_patterns(text: str, patterns: Sequence[str]) -> List[str]:
    cleaned = clean_output(text).lower()
    return [p for p in patterns if p.lower() in cleaned]

def timestamp() -> str: return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
def enforce_dir(path: str) -> None:
    if not os.path.isdir(path): os.makedirs(path)

def write_log(path: str, message: str) -> None:
    with open(path, "a", encoding="utf-8", errors="replace") as handle: handle.write(message)

# =============================================================
# Core functions
# =============================================================

# runs a command and returns a Tuple[int, str] with
# int status code
# string output / error message
# https://docs.python.org/3/library/subprocess.html
def run_command(args: Sequence[str], cwd: Optional[str], env: Dict[str, str]) -> Tuple[int, str]:
    try:
        proc = subprocess.Popen(list(args), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        output, _ = proc.communicate(timeout=10) # unused _ because we pipe stderr into stdout
        return proc.returncode, output
    except Exception as exc: return 1, repr(exc)

# we want to carry over any env variables present, especially existing wine prefixes
# then make a copy to not overwrite the global env so this can be passed to subprocess logic
def build_env(wine_prefix: Optional[str], wine_debug: Optional[str]) -> Dict[str, str]:
    env = os.environ.copy()
    if wine_prefix: env["WINEPREFIX"] = os.path.abspath(wine_prefix) # maps the prefix to a real path
    if wine_debug is not None: env["WINEDEBUG"] = wine_debug # sets env even if it's empty
    return env

# builds an argument string to be passed to subprocess
def build_argv(wine_cmd: str, caster_exe: str, caster_args: Sequence[str], no_fork: bool = False, use_wineconsole: bool = False) -> List[str]:
    if use_wineconsole: argv = ["wineconsole", caster_exe]
    else: argv = [wine_cmd, caster_exe]
    if no_fork: argv.append("--no-fork")
    argv.extend(caster_args)
    return argv

def build_caster_args(mode: str, port: str = "12345", address: str = "127.0.0.1", no_ui: bool = False, raw_args: Sequence[str] = []) -> List[str]:
    args = []
    if no_ui: args.append("-n")

    if mode == "offline-training": args.extend(["-o", "-t"])
    elif mode == "offline-versus": args.append("-o")
    elif mode == "tournament": args.append("-T")
    elif mode == "broadcast": args.extend(["-b", port])
    elif mode == "host": args.append(port)
    elif mode == "connect": args.append("%s:%s" % (address, port))
    elif mode == "spectate": args.extend(["-s", "%s:%s" % (address, port)])
    elif mode == "raw": args.extend(raw_args)
    return args

# returns a list of errors if requirements are not met
# most important is backend, since we want to test spawn, popen and both
def check_requirements(wine_cmd: str, caster_exe: str, cwd: str, launch_mode: str) -> List[str]:
    errors = []
    if not shutil.which(wine_cmd): errors.append("Wine not in PATH: %s" % wine_cmd)
    if not os.path.isfile(os.path.join(cwd, caster_exe)): errors.append("cccaster not found: %s" % os.path.join(cwd, caster_exe))
    if launch_mode == "spawn":
        try: import pexpect
        except ImportError: errors.append("Missing dependency pexpect (for spawn backend)")
    if launch_mode == "popen":
        try: from pexpect.popen_spawn import PopenSpawn
        except ImportError: errors.append("Missing dependency pexpect.popen_spawn (for popen backend)")
    try: import psutil
    except ImportError: errors.append("Missing dependency psutil")
    return errors

# captures processes that match keywords purely for gathering info because don't know what we can expect
# returns an array of possible processes
def snapshot_processes(keywords: Sequence[str]) -> List[Dict[str, object]]:
    try: import psutil
    except ImportError: return []
    rows = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd", "ppid"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            cmdline_list = info.get("cmdline") or []
            cmdline = " ".join(cmdline_list)
            haystack = ("%s %s" % (name, cmdline)).lower() #merge into lowercase string to better search for keywords
            if any(keyword.lower() in haystack for keyword in keywords):
                rows.append({"pid": info.get("pid"), "ppid": info.get("ppid"), "name": name, "cwd": info.get("cwd"), "cmdline": cmdline})
        except Exception: continue
    return rows

# logs processes from snapshot_processes as single line
def log_process_snapshot(path: str, label: str, keywords: Sequence[str]) -> None:
    rows = snapshot_processes(keywords)
    write_log(path, "\n[%s]\n" % label)
    if not rows: write_log(path, "No matches.\n")
    else:
        for row in rows: write_log(path, "pid={pid} ppid={ppid} name={name!r} cwd={cwd!r} cmdline={cmdline!r}\n".format(**row))

# diffs two process lists and returns any new processes as array (id, name)
def extract_new_processes(before: List[Dict[str, object]], after: List[Dict[str, object]]) -> List[Dict[str, object]]:
    before_pids = {row.get("pid") for row in before if row.get("pid") is not None}
    return [row for row in after if row.get("pid") is not None and row.get("pid") not in before_pids]

# attempt to kill the processes diffed since the test started that are also not in the process list before the test
def terminate_created_processes(created: List[Dict[str, object]], log_path: str) -> None:
    try: import psutil
    except ImportError: return
    pids = [row.get("pid") for row in created if isinstance(row.get("pid"), int)]
    if not pids:
        write_log(log_path, "\nCLEANUP: No processes to kill\n")
        return
    write_log(log_path, "\nCLEANUP: Trying to kill pids: %s\n" % pids)
    processes = []
    for pid in pids:
        try: processes.append(psutil.Process(pid))
        except psutil.Error: pass
    for proc in processes:
        try:
            write_log(log_path, "kill pid=%s name=%r\n" % (proc.pid, proc.name()))
            proc.terminate()
        except psutil.Error: pass
    gone, alive = psutil.wait_procs(processes, timeout=3)
    for proc in alive:
        try:
            write_log(log_path, "kill pid=%s name=%r\n" % (proc.pid, proc.name()))
            proc.kill()
        except psutil.Error: pass

# instead of waiting / blocking the whole process, we use read_nonblocking for specific duration
# logs both raw and cleaned up log
# see https://pexpect.readthedocs.io/en/stable/api/pexpect.html#pexpect.spawn.read_nonblocking
def read_pexpect_output(child, seconds: float, raw_log: str, clean_log: str) -> str:
    import pexpect
    deadline = time.time() + seconds
    combined = ""
    while time.time() < deadline:
        try: chunk = child.read_nonblocking(size=4096, timeout=0.25)
        except pexpect.TIMEOUT: continue
        except pexpect.EOF: break
        if not isinstance(chunk, str): chunk = chunk.decode("utf-8", "replace")
        combined += chunk
        write_log(raw_log, chunk); write_log(clean_log, clean_output(chunk) + "\n")
    return combined

# try to attach caster to a pty session, returns boolean for success
# See https://www.bx.psu.edu/~nate/pexpect/pexpect.html
def test_spawn_backend(argv: Sequence[str], cwd: str, env: Dict[str, str], logs_dir: str, patterns: Sequence[str], send_key: Optional[str], read_seconds: float) -> bool:
    import pexpect
    raw_log = os.path.join(logs_dir, "spawn-raw.log")
    clean_log = os.path.join(logs_dir, "spawn-clean.log")
    summary_log = os.path.join(logs_dir, "spawn-summary.log")
    write_log(summary_log, "SPAWN \nargv=%r\ncwd=%r\n" % (list(argv), cwd))
    child = None
    success = False
    try:
        child = pexpect.spawn(" ".join(shlex.quote(arg) for arg in argv), cwd=cwd, env=env, encoding="utf-8", codec_errors="replace", timeout=5)
        write_log(summary_log, "spawned pid=%r\n" % getattr(child, "pid", None))
        first_output = read_pexpect_output(child, read_seconds, raw_log, clean_log)
        found = detect_patterns(first_output, patterns)
        write_log(summary_log, "detected_patterns_before_send=%r\n" % found)
        if send_key is not None:
            write_log(summary_log, "sending_key=%r\n" % send_key); child.send(send_key); time.sleep(0.5)
            second_output = read_pexpect_output(child, read_seconds, raw_log, clean_log)
            found_after = detect_patterns(first_output + second_output, patterns)
            write_log(summary_log, "detected_patterns_after_send=%r\n" % found_after)
        else: found_after = found
        success = bool(found_after)
    except Exception as exc: write_log(summary_log, "error=%r\n" % exc); success = False
    finally:
        if child:
            try: write_log(summary_log, "force close child=True\n"); child.close(force=True)
            except Exception as exc: write_log(summary_log, "close_error=%r\n" % exc)
    write_log(summary_log, "success=%r\n" % success)
    return success

# read output from PopenSpawn
# behaves differently because subprocess pipe juggling but still with timeouts to have a nonblocking reader
def read_popen_output(child, seconds: float, raw_log: str, clean_log: str) -> str:
    import pexpect
    deadline = time.time() + seconds
    combined = ""
    while time.time() < deadline:
        try: index = child.expect([pexpect.TIMEOUT, pexpect.EOF, r".+"], timeout=0.25)
        except Exception: break
        if index == 0: continue
        if index == 1: break
        chunk = child.match.group(0)
        if not isinstance(chunk, str): chunk = chunk.decode("utf-8", "replace")
        combined += chunk
        write_log(raw_log, chunk); write_log(clean_log, clean_output(chunk) + "\n")
    return combined

# test pexpect.popen_spawn.PopenSpawn if pty causes any issues
# See https://pexpect.readthedocs.io/en/stable/api/popen_spawn.html
def test_popen_backend(argv: Sequence[str], cwd: str, env: Dict[str, str], logs_dir: str, patterns: Sequence[str], send_key: Optional[str], read_seconds: float) -> bool:
    from pexpect.popen_spawn import PopenSpawn
    import pexpect
    raw_log = os.path.join(logs_dir, "popen-raw.log")
    clean_log = os.path.join(logs_dir, "popen-clean.log")
    summary_log = os.path.join(logs_dir, "popen-summary.log")
    write_log(summary_log, "POPEN \nargv=%r\ncwd=%r\n" % (list(argv), cwd))
    child = None
    success = False
    try:
        child = PopenSpawn(" ".join(shlex.quote(arg) for arg in argv), cwd=cwd, env=env, encoding="utf-8", codec_errors="replace", timeout=5)
        first_output = read_popen_output(child, read_seconds, raw_log, clean_log)
        found = detect_patterns(first_output, patterns)
        write_log(summary_log, "detected patterns before send=%r\n" % found)
        if send_key is not None:
            write_log(summary_log, "send key=%r\n" % send_key); child.send(send_key); time.sleep(0.5)
            second_output = read_popen_output(child, read_seconds, raw_log, clean_log)
            found_after = detect_patterns(first_output + second_output, patterns)
            write_log(summary_log, "detected patterns after send=%r\n" % found_after)
        else: found_after = found
        success = bool(found_after)
    except Exception as exc: write_log(summary_log, "error=%r\n" % exc); success = False
    finally:
        if child:
            try: write_log(summary_log, "force close child=True\n"); child.close(force=True)
            except Exception as exc: write_log(summary_log, "close_error=%r\n" % exc)
    write_log(summary_log, "success=%r\n" % success)
    return success

# test subprocess.Popen (arg support)
def test_subprocess_backend(argv: Sequence[str], cwd: str, env: Dict[str, str], logs_dir: str, read_seconds: float) -> bool:
    import psutil
    raw_log = os.path.join(logs_dir, "subprocess-raw.log")
    summary_log = os.path.join(logs_dir, "subprocess-summary.log")
    write_log(summary_log, "SUBPROCESS \nargv=%r\ncwd=%r\n" % (list(argv), cwd))

    proc = None
    success = False
    try:
        # use a pipe for stdout/stderr
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1 # buffer possible?
        )
        write_log(summary_log, "spawned pid=%r\n" % proc.pid)

        # monitor loop
        deadline = time.time() + read_seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                write_log(summary_log, "process exited with code %r\n" % proc.returncode)
                break

            # log children
            try:
                p = psutil.Process(proc.pid)
                children = p.children(recursive=True)
                if children: write_log(summary_log, "active children: %s\n" % [c.pid for c in children])
            except psutil.Error:
                pass

            time.sleep(1.0)

        success = True
    except Exception as exc:
        write_log(summary_log, "error=%r\n" % exc)
        success = False
    finally:
        if proc and proc.poll() is None:
            write_log(summary_log, "terminating process tree\n")
            try:
                # kill everything
                p = psutil.Process(proc.pid)
                for child in p.children(recursive=True):
                    child.terminate()
                p.terminate()
                gone, alive = psutil.wait_procs(p.children() + [p], timeout=3)
                for a in alive: a.kill()
            except psutil.Error:
                proc.terminate()

    return success

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test linux native caster hook from outside wine")
    parser.add_argument("--cwd", default=os.getcwd(), help="Folder containing caster and melty. Default: current directory")
    parser.add_argument("--wine-cmd", default="wine", help="Wine command to execute. Default: wine")
    parser.add_argument("--wine-prefix", default=None, help="Optional WINEPREFIX path")
    parser.add_argument("--wine-debug", default="-all", help="WINEDEBUG value. Default: -all. Use empty string to inherit/noise")
    parser.add_argument("--caster-exe", default="cccaster.v3.1.exe", help="Caster executable. Default: cccaster.v3.1.exe")

    # launch options
    parser.add_argument("--launch-mode", choices=["subprocess", "spawn", "popen", "wine", "wine-no-fork", "wineconsole-no-fork"], default="subprocess", help="Launch strategy. Default: subprocess")
    parser.add_argument("--caster-mode", choices=["offline-training", "offline-versus", "tournament", "broadcast", "host", "connect", "spectate", "raw"], default="raw", help="CCCaster mode. Default: raw")
    parser.add_argument("--port", default="12345", help="Port for network modes")
    parser.add_argument("--address", default="127.0.0.1", help="Address for connect/spectate")
    parser.add_argument("--no-ui", action="store_true", help="Pass -n/--no-ui to CCCaster")
    parser.add_argument("--no-fork", action="store_true", help="Pass --no-fork to CCCaster")

    parser.add_argument("--send-key", default="0", help="Single key(string) to send after read (only for spawn/popen). Default: 0. --no-send to disable")
    parser.add_argument("--no-send", action="store_true", help="Don't send input to caster")
    parser.add_argument("--read-seconds", type=float, default=5.0, help="Read how many seconds to monitor. Default: 5.0.")
    parser.add_argument("--log-dir", default=None, help="Log dir. Default: tools/test-logs/<timestamp>")
    parser.add_argument("caster_args", nargs="*", help="Extra args passed to caster after the executable (used if caster-mode is raw)")
    return parser.parse_args()

if __name__ == "__main__": raise SystemExit(main())
