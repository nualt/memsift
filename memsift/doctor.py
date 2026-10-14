"""Read-only diagnostics: probe once per service without changing pause state."""

from pathlib import Path
import sys
import tempfile

from . import breaker, cognee, compactor, jev, store
from .vendor.logfiles import error
from .vendor.redact import redact


def check(root: Path, config: dict) -> str:
    lines = []

    def result(ok, text):
        lines.append(("✓ " if ok else "✗ ") + text)

    def failure(label, exc, instruction):
        error("doctor-" + label, exc, getattr(exc, "service", ""), getattr(exc, "status", 0),
              getattr(exc, "duration", 0.0), getattr(exc, "response", ""))
        status = getattr(exc, "status", 0)
        detail = "HTTP " + str(status) if status else type(exc).__name__
        result(False, label + ": " + detail + ", " + instruction)

    version = "%s.%s" % tuple(sys.version_info[:2])
    result(sys.version_info[:2] >= (3, 9), "Python " + version +
           ("" if sys.version_info[:2] >= (3, 9) else ", install Python 3.9 or later"))
    required = ("COGNEE_BASE_URL", "COGNEE_API_KEY", "OPENROUTER_API_KEY")
    missing = [name for name in required if not config.get(name)]
    result(not missing, "configuration found: " + ", ".join(required) if not missing else
           "configuration: set " + ", ".join(missing))

    datasets = None
    if all(config.get(name) for name in required[:2]):
        try:
            datasets = cognee.list_datasets(config, probe=True)
            target = config.get("MEMSIFT_DATASET", "memsift")
            found = any(row["name"] == target for row in datasets)
            result(found, "Cognee reachable, key accepted, dataset " + target if found else
                   "Cognee reachable, key accepted; create dataset " + target)
        except Exception as exc:
            failure("Cognee", exc, "check COGNEE_BASE_URL and COGNEE_API_KEY")
    else:
        result(False, "Cognee: skipped, set COGNEE_BASE_URL and COGNEE_API_KEY")
    for label, module, key in (("Jev", jev, "OPENROUTER_API_KEY"),
                               ("compactor", compactor, "MEMSIFT_COMPACTOR_API_KEY")):
        if not config.get(key):
            result(False, label + ": skipped, set " + key)
            continue
        try:
            module.probe(config)
            result(True, label + " reachable (one minimal request)")
        except Exception as exc:
            failure(label, exc, "check " + key)
    try:
        store.home().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(prefix="doctor-", dir=store.home()) as stream:
            stream.write(b"memsift writable\n")
            stream.flush()
        result(True, "~/.memsift writable")
    except Exception as exc:
        failure("~/.memsift", exc, "allow writing to ~/.memsift")
    try:
        paused = [name for name, value in breaker.state()["services"].items() if value["paused"]]
        pending = len(store.pending_turns(root))
        packets = len(list((store.home() / "repos").glob("*/outbox/*.txt")))
        result(not paused, ("paused: " + ", ".join(paused) + "; run memsift-resume" if paused else
                            "nothing paused") + " · %s turns pending · %s packets waiting" % (pending, packets))
    except Exception as exc:
        failure("memory queues", exc, "check ~/.memsift permissions and debug.log")
    if datasets is not None:
        try:
            descriptions = cognee.descriptions()
            undescribed = [row["name"] for row in datasets if not descriptions.get(row["name"])]
            for name in undescribed:
                result(False, 'datasets.md: "%s" has no description; add a description or IGNORED' % name)
            if not undescribed:
                result(True, "datasets.md: all server datasets described")
        except Exception as exc:
            failure("datasets.md", exc, "check ~/.memsift/datasets.md")
    else:
        result(False, "datasets.md: skipped, restore Cognee configuration and connectivity")
    try:
        found = (root / "jev.md").is_file()
        result(found, "this repo: jev.md found" if found else "this repo: create jev.md with project facts")
    except Exception as exc:
        failure("this repo", exc, "allow reading jev.md")
    return redact("\n".join(lines))
