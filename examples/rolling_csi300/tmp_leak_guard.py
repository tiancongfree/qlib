"""Runtime guard against the qlib ``Recorder.save_objects`` /tmp leak.

Problem
-------
``qlib/workflow/recorder.py`` ``Recorder.save_objects`` does::

    temp_dir = Path(tempfile.mkdtemp()).resolve()   # -> /tmp/tmpXXXXXXXX/
    for name, data in kwargs.items():
        Serializable.general_dump(data, path)
        self.client.log_artifact(...)
    shutil.rmtree(temp_dir)                          # NOT in a finally block

If ``general_dump`` or ``log_artifact`` raises (unpicklable object, mlflow
async-log error, read-only FS, ...), ``shutil.rmtree`` is skipped and the
``/tmp/tmpXXXXXXXX/`` directory (holding a partial/empty ``pred.pkl``) is left
behind forever.  The daily pipeline calls ``save_objects`` many times per run,
so these accumulate into gigabytes (observed: 9602 dirs / ~19 GB on the 244 box
after ~10 days), eventually filling the host disk and switching the WSL ext4
mount to read-only.

Fix
---
``install()`` monkey-patches ``Recorder.save_objects`` with a ``try/finally``
version that always removes the temp dir.  This is done in OUR code (not by
editing qlib source), following the same "own the fix in examples/rolling_csi300"
convention used for ``SortedPosition`` / ``AppendRolling``.

Import this module (and call ``install()``) once from every entrypoint that
saves mlflow objects directly (run_rolling.py, daily_predict.py).
"""
import shutil
import sys
import tempfile
from pathlib import Path


def _patched_save_objects(self, local_path=None, artifact_path=None, **kwargs):
    """Copy of qlib Recorder.save_objects with a try/finally cleanup.

    Behaviour is otherwise byte-for-byte identical to the upstream method:
    the ``local_path`` branch is untouched; the kwargs branch always rmtrees
    the mkdtemp directory even when dumping / logging raises.
    """
    from qlib.utils.serial import Serializable

    assert self.uri is not None, "Please start the experiment and recorder first before using recorder directly."
    if local_path is not None:
        path = Path(local_path)
        if path.is_dir():
            self.client.log_artifacts(self.id, local_path, artifact_path)
        else:
            self.client.log_artifact(self.id, local_path, artifact_path)
    else:
        temp_dir = Path(tempfile.mkdtemp()).resolve()
        try:
            for name, data in kwargs.items():
                path = temp_dir / name
                Serializable.general_dump(data, path)
                self.client.log_artifact(self.id, temp_dir / name, artifact_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def install() -> bool:
    """Idempotently patch every ``save_objects`` implementation in qlib's
    ``workflow.recorder`` module (both ``Recorder`` and the overriding
    ``MLflowRecorder`` subclass, which is the one actually used at runtime).

    Returns True if the patch is in place (installed now or previously).
    """
    import inspect
    from qlib.workflow import recorder as rec_mod

    _patched_save_objects._tmp_leak_guard = True
    patched_any = False
    for _name, cls in vars(rec_mod).items():
        if not inspect.isclass(cls):
            continue
        # only patch classes defined in this module that implement save_objects
        cls_mod = getattr(cls, "__module__", "")
        mod = sys.modules.get(cls_mod)
        if mod is None or getattr(mod, "__file__", None) != rec_mod.__file__:
            continue
        if "save_objects" not in cls.__dict__:
            continue
        existing = cls.__dict__["save_objects"]
        if getattr(existing, "_tmp_leak_guard", False):
            patched_any = True
            continue
        cls.save_objects = _patched_save_objects
        patched_any = True
    return patched_any
