# OpenArm Libraries

These submodules contain the OpenArm-specific libraries used by `nodes/`.
The release baselines below are for local development; the parent repository's
Git submodule commits identify the exact source revisions. Installed library
versions, along with general dependencies such as NumPy, PyArrow, and the MuJoCo
engine, are managed by the parent `pyproject.toml` and `uv.lock`.

| Repository | Release tag | Python package directory | Used by |
|---|---|---|---|
| [openarm_driver](openarm_driver) | `0.5.1` | `lib/openarm_driver` | `dora-openarm` |
| [openarm_can](openarm_can) | `1.4.0` | `lib/openarm_can/python` | `openarm_driver`, `openarm_ker`, `dora-openarm-cell-lifter` |
| [openarm_ker](openarm_ker) | `0.3.0` | `lib/openarm_ker` | `dora-openarm-ker` |
| [openarm_control](openarm_control) | `0.4.0` | `lib/openarm_control` | `dora-openarm-kinematics` |
| [openarm_mujoco](openarm_mujoco) | `2.3.0` | `lib/openarm_mujoco` | `openarm_control`, `dora-openarm-mujoco` |

`uv sync --locked` installs the package-index versions recorded in `uv.lock`.
Refresh these submodule pins when updating the dependency baseline; the pins
only select installed library sources when an editable path is configured.

Initialize the source checkouts from the repository root:

```bash
git submodule update --init --recursive
```

To debug a library, add its checkout as an editable project dependency from the
repository root. Choose only the libraries you need:

```bash
uv add --editable lib/openarm_driver
# uv add --editable lib/openarm_can/python
# uv add --editable lib/openarm_ker
# uv add --editable lib/openarm_control
# uv add --editable lib/openarm_mujoco
```

This updates the parent's TOML, lockfile, and environment without editing the
dataflows. See the parent [README](../README.md#debugging-openarm-libraries) for
switching back to released packages and rebuilding C++ bindings.
