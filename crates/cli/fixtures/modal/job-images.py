# What modal_launcher.py and modal_plane.py start a job's sandbox from: GitHub's runner image, or an image of the
# control plane's own (`superci runner_images`). Run by `modal::tests` with the file's source on stdin and its kind
# ("launcher" or "plane") as the argument, against stand-ins for Modal and FastAPI: nothing reaches Modal.
import asyncio, os, subprocess, sys, tempfile, time, types
from unittest import mock

# Modal and FastAPI as stand-ins: what the file asks of them is recorded, nothing is called.
modal = mock.MagicMock()
class NotFoundError(Exception): pass
class RemoteError(Exception): pass
class InvalidError(Exception): pass
modal.exception.NotFoundError, modal.exception.RemoteError, modal.exception.InvalidError = NotFoundError, RemoteError, InvalidError
class HTTPException(Exception):
    def __init__(self, status_code, detail): self.status_code, self.detail = status_code, detail
routes = {}
class FastAPI:
    def _route(self, path, **_):
        def keep(f): routes[path] = f; return f
        return keep
    get = post = api_route = _route
fastapi = types.ModuleType("fastapi")
fastapi.FastAPI, fastapi.HTTPException, fastapi.Request, fastapi.Response = FastAPI, HTTPException, object, object
sys.modules["modal"], sys.modules["fastapi"] = modal, fastapi
# (Modal's decorators give back what they decorate, so the launcher's routes are its own functions.)
same = lambda *_, **__: (lambda f: f)
modal.App.return_value.function, modal.concurrent, modal.asgi_app = same, same, same
kind, source = sys.argv[1], sys.stdin.read()
ns = {"__name__": "superci_" + kind}
exec(compile(source, kind, "exec"), ns)
job_image, image_refused, NO_RUNNER = ns["job_image"], ns["image_refused"], ns["NO_RUNNER"]
# (A control plane's jobs' own app: found by Modal's client, here a stand-in.)
if "jobs" in ns:
    async def jobs(): return modal.App.lookup.return_value
    ns["jobs"] = jobs

# GitHub's runner image unless one is named; one named is from its registry, with its Secret, checked for the runner.
assert job_image() is ns["runner_image"] and job_image(None, "s") is ns["runner_image"]
modal.reset_mock()
job_image("ghcr.io/acme/ci@sha256:0f1e", "ghcr-read")
modal.Secret.from_name.assert_called_once_with("ghcr-read")
modal.Image.from_registry.assert_called_once_with("ghcr.io/acme/ci@sha256:0f1e", secret=modal.Secret.from_name.return_value)
built = modal.Image.from_registry.return_value
built.dockerfile_commands.assert_called_once_with(["USER root", "RUN " + NO_RUNNER])
built.dockerfile_commands.return_value.env.assert_called_once_with({"RUNNER_ALLOW_RUNASROOT": "1"})
modal.reset_mock()
job_image("docker.io/acme/node-ci:22")
modal.Image.from_registry.assert_called_once_with("docker.io/acme/node-ci:22", secret=None)
modal.Secret.from_name.assert_not_called()

# The check fails an image without the runner, saying why, and passes one with it.
no = subprocess.run(["sh", "-c", NO_RUNNER.replace("/home/runner/run.sh", "/nonexistent/run.sh")], capture_output=True, text=True)
assert no.returncode == 1 and "has no GitHub runner" in no.stderr and "FROM ghcr.io/actions/actions-runner" in no.stderr, no
with tempfile.TemporaryDirectory() as d:
    run = os.path.join(d, "run.sh")
    open(run, "w").write("#!/bin/sh\n"); os.chmod(run, 0o755)
    assert subprocess.run(["sh", "-c", NO_RUNNER.replace("/home/runner/run.sh", run)]).returncode == 0

# The runner's start in a real shell, whatever the machine that runs this (stand-ins for `id -u runner`, setpriv,
# dockerd and docker, first in PATH): as the user `runner` where the image has one, else as root; a dockerd that ends at
# once (gVisor's) holds it back no longer than that, and its last words are said; a GPU's starts none; the failing hook
# is given.
with tempfile.TemporaryDirectory() as d:
    for n, body in [("id", '[ "$1 $2" = "-u runner" ] && { [ -n "$HAS_RUNNER" ]; exit; }; exec /usr/bin/id "$@"'),
                    ("setpriv", 'shift 3; echo "as runner"; exec "$@"'),
                    ("dockerd", 'echo "dockerd said no"; touch "$MARK"; exit 1'), ("docker", "exit 1"),
                    ("run.sh", 'echo "$HOME $ACTIONS_RUNNER_HOOK_JOB_STARTED $*"')]:
        open(os.path.join(d, n), "w").write("#!/bin/sh\n" + body + "\n"); os.chmod(os.path.join(d, n), 0o755)
    mark = os.path.join(d, "dockerd-started")
    for fail, docker, has_runner in [(None, True, ""), ("d2h5", True, "1"), (None, False, "1")]:
        command = [a.replace("/tmp/", d + "/").replace("/home/runner/run.sh", os.path.join(d, "run.sh")) for a in ns["runner_command"]("J", fail, docker)]
        env = {"PATH": d + ":/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/root", "HAS_RUNNER": has_runner, "MARK": mark}
        started = time.monotonic()
        ran = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
        said = (["as", "runner", "/home/runner"] if has_runner else ["/root"]) + ([os.path.join(d, "superci-fail.sh")] if fail else []) + ["--jitconfig", "J"]
        assert ran.returncode == 0 and ran.stdout.split() == said, ran
        assert os.path.exists(mark) == docker and (("Docker did not start" in ran.stderr and "dockerd said no" in ran.stderr) == docker), ran
        assert time.monotonic() - started < 5, "a dockerd that ended held the runner back"
        if os.path.exists(mark): os.remove(mark)

# What Modal says of an image it could not make is said with the image; a build cut short is not (it is tried again).
assert "ghcr.io/acme/ci:1" in image_refused("ghcr.io/acme/ci:1", RemoteError("Image build for im-1 failed. See build logs for more details."))
assert "FROM ghcr.io/actions/actions-runner" in image_refused("x/y:1", NotFoundError("Secret 'ghcr-read' not found"))
assert image_refused("x/y:1", RemoteError("Image build for im-1 terminated due to external shut-down. Please try again.")) is None
assert image_refused("x/y:1", RemoteError("Image build for im-1 timed out.")) is None
assert image_refused("x/y:1", RemoteError("something else failed")) is None

async def launches(fails, **asked):
    """A sandbox started as /launch (the launcher) or sandbox_start (a Modal control plane) asks: what Modal was asked,
    and the answer, as the control plane gets it."""
    modal.reset_mock()
    modal.Sandbox.create.aio = mock.AsyncMock(side_effect=fails, return_value=mock.Mock(object_id="sb-1"))
    q = {"jit": "J", "cpu": 2, "ram_gb": 8, "max_minutes": 60, **asked}
    if kind == "launcher":
        ns["verify"] = lambda *_: None
        ns["web"]()
        request = mock.Mock(headers={"authorization": "Bearer t"}, url=mock.Mock(hostname="l.example"), json=mock.AsyncMock(return_value=q))
        try:
            said = await routes["/launch"](request)
        except HTTPException as e:
            said = {"error": e.detail, "status": e.status_code}
    else:
        said = await ns["plane"].answer({"op": "sandbox_start", **q})
    return modal.Sandbox.create.aio.call_args, said

def launched(*a, **k): return asyncio.run(launches(*a, **k))

for image in [None, "ghcr.io/acme/ci:1"]:
    call, said = launched(None, **({"image": image, "image_secret": "ghcr-read"} if image else {}))
    assert said == ({"id": "sb-1", "kind": "sandbox", "image": image} if kind == "launcher" else {"id": "sb-1"}), said
    assert call.args == tuple(ns["runner_command"]("J")) and call.kwargs["workdir"] == "/home/runner", call
    # In a VM, which runs Docker; a GPU's sandbox in gVisor, where Modal has GPUs.
    assert call.kwargs["runtime"] == "vm", call
    if image:
        modal.Image.from_registry.assert_called_once_with(image, secret=modal.Secret.from_name.return_value)
        modal.Secret.from_name.assert_called_once_with("ghcr-read")
    else:
        modal.Image.from_registry.assert_not_called()
call, _ = launched(None, gpu="T4")
assert call.kwargs["gpu"] == "T4" and call.kwargs["runtime"] is None and call.args == tuple(ns["runner_command"]("J", None, False)), call
# An image Modal could not make fails the job, saying why; the same with no image of its own is Modal's to say.
_, said = launched(RemoteError("Image build for im-1 failed. See build logs for more details."), image="ghcr.io/acme/ci:1")
assert said["error"].startswith("Modal could not start from the image ghcr.io/acme/ci:1:") and said.get("status", 400) == 400, said
try:
    launched(RemoteError("Image build for im-1 failed."))
    raise AssertionError("not raised")
except RemoteError:
    pass
print("ok", kind)
