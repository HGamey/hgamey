#!/usr/bin/env python3
"""Supervise the node service with startup arguments and credentials held in memory."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time


CONFIG_LIMIT = 64 << 10
TOKEN_LIMIT = 4096
STOP_SECONDS = 5


class LaunchError(ValueError):
    pass


def read_bounded(path, limit, label):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise LaunchError(label + " must be a regular file")
            raw = source.read(limit + 1)
    except OSError as error:
        raise LaunchError(f"cannot read {label} (errno={error.errno})") from None
    if len(raw) > limit:
        raise LaunchError(label + " exceeds its byte limit")
    return raw


def absolute_path(value, label):
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise LaunchError(label + " must be an absolute path")
    return Path(value)


def load_config(path):
    raw = read_bounded(absolute_path(path, "config path"), CONFIG_LIMIT, "launch config")
    try:
        config = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise LaunchError("launch config must be UTF-8 JSON") from None
    if (not isinstance(config, dict)
            or set(config) != {"schema", "service_args", "node_token_file"}
            or config.get("schema") != "hgamey/node-launch@1"):
        raise LaunchError("launch config requires schema hgamey/node-launch@1, service_args and node_token_file")
    return config["service_args"], absolute_path(config["node_token_file"], "node_token_file")


def validate_args(args):
    if (not isinstance(args, list) or len(args) > 256
            or any(not isinstance(arg, str) or "\0" in arg for arg in args)):
        raise LaunchError("service_args must be an array of at most 256 strings without NUL")
    try:
        if sum(len(arg.encode("utf-8")) for arg in args) > CONFIG_LIMIT:
            raise LaunchError("service_args exceeds its byte limit")
    except UnicodeError:
        raise LaunchError("service_args must contain valid UTF-8 strings") from None
    # Use the service's real grammar, but do not echo supplied values on failure.
    with open(os.devnull, "w") as sink, contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
        try:
            from node_service import argument_parser
            argument_parser().parse_args(args)
        except SystemExit as error:
            if error.code == 0:
                return False
            raise LaunchError("invalid service arguments; consult node_service.py --help") from None
        except Exception:
            raise LaunchError("cannot load or validate the node service argument parser") from None
    return True


def load_token(path):
    try:
        token = read_bounded(path, TOKEN_LIMIT, "node token file").decode("utf-8").strip()
    except UnicodeError:
        raise LaunchError("node token file must be UTF-8") from None
    if len(token) < 32 or "\0" in token or any(c.isspace() for c in token):
        raise LaunchError("node token must contain at least 32 characters without whitespace or NUL")
    return token


def log(message):
    print("node-launch: " + message, file=sys.stderr, flush=True)


def close_children(children):
    active = [(name, proc) for name, proc in children.items() if proc is not None and proc.poll() is None]
    for name, proc in active:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + STOP_SECONDS
    while any(proc.poll() is None for _, proc in active) and time.monotonic() < deadline:
        time.sleep(0.05)
    for name, proc in active:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 2
    for name, proc in active:
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
            log(f"{name} stopped pid={proc.pid} rc={proc.returncode}")
        except subprocess.TimeoutExpired:
            log(f"{name} termination unconfirmed pid={proc.pid}")


def supervise(service_args, token, workdir, env, no_tunnel):
    service = Path(__file__).resolve().with_name("node_service.py")
    commands = {"service": [sys.executable, str(service), *service_args]}
    if not no_tunnel:
        commands["tunnel"] = [str(workdir / "bin/cloudflared"), "tunnel", "--no-autoupdate",
                              "--config", str(Path.home() / ".cloudflared/hgamey-node.yml"),
                              "run", "hgamey-node"]
    children = {name: None for name in commands}
    restart_at = {name: 0.0 for name in commands}
    stopped = 0

    def stop(signum, _frame):
        nonlocal stopped
        stopped = stopped or signum

    old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        with contextlib.ExitStack() as stack:
            logs = {name: stack.enter_context(open(workdir / filename, "ab", buffering=0))
                    for name, filename in (("service", "node_service.log"), ("tunnel", "cloudflared.log"))
                    if name in commands}
            while not stopped:
                for name, command in commands.items():
                    if stopped:
                        break
                    proc = children[name]
                    if proc is not None:
                        code = proc.poll()
                        if code is None:
                            continue
                        log(f"{name} exited pid={proc.pid} rc={code}")
                        children[name] = None
                        if name == "service" and code == 2:
                            return 2
                        restart_at[name] = time.monotonic() + (2 if name == "service" else 5)
                    if time.monotonic() < restart_at[name]:
                        continue
                    child_env = env.copy()
                    child_env.pop("PHONEFARM_NODE_TOKEN", None)
                    if name == "service":
                        child_env["PHONEFARM_NODE_TOKEN"] = token
                    try:
                        children[name] = subprocess.Popen(command, cwd=workdir, env=child_env,
                            stdin=subprocess.DEVNULL, stdout=logs[name], stderr=subprocess.STDOUT,
                            start_new_session=True)
                        log(f"{name} started pid={children[name].pid}")
                    except OSError as error:
                        log(f"cannot start {name} (errno={error.errno})")
                        if name == "service":
                            return 2
                        restart_at[name] = time.monotonic() + 5
                time.sleep(0.1)
        return 128 + stopped
    finally:
        close_children(children)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        epilog="Without --config, remaining arguments go unchanged to node_service.py. Logs and cwd remain ~/node.")
    parser.add_argument("--config", metavar="ABS", help="external hgamey/node-launch@1 JSON; exclusive with service arguments")
    parser.add_argument("--no-tunnel", action="store_true", help="run the node service without a cloudflared tunnel")
    options, service_args = parser.parse_known_args(argv)
    try:
        workdir = Path.home() / "node"
        env = os.environ.copy()
        config_dir = Path(env.get("HG_CONFIG_DIR") or "~/.config/hgamey").expanduser()
        if not config_dir.is_absolute():
            config_dir = workdir / config_dir
        env["HG_CONFIG_DIR"] = str(config_dir)
        if options.config is not None:
            if service_args:
                raise LaunchError("--config cannot be combined with direct service arguments")
            service_args, token_file = load_config(options.config)
        else:
            token_file = config_dir / "node.token"
        if not validate_args(service_args):
            return 0
        token = load_token(token_file)
        return supervise(tuple(service_args), token, workdir, env, options.no_tunnel)
    except (LaunchError, OSError) as error:
        # Never include file contents, supplied argv, or token values in diagnostics.
        detail = str(error) if isinstance(error, LaunchError) else f"host operation failed (errno={error.errno})"
        log(detail)
        return 2


if __name__ == "__main__":
    sys.exit(main())
