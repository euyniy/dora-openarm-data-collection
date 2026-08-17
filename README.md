# Data collection configurations for OpenArm with dora-rs

This repository provides data collection configurations for [OpenArm](https://openarm.dev/) with [dora-rs](https://dora-rs.ai/).

## Configurations

[`metadata.yaml`](metadata.yaml) is metadata used by all configurations.

Each task in `metadata.yaml` may carry `prompt_ja` / `description_ja`. They are
shown to the operator instead of `prompt` / `description`, which stay in English
because they are recorded as the dataset's language instruction. `description`
states the initial state the task expects, so the operator can set the table up
before pressing start.

[`metadata_tableware.yaml`](metadata_tableware.yaml) and
[`metadata_pillowcase.yaml`](metadata_pillowcase.yaml) are further (provisional)
task sets. Which metadata a session uses is chosen from the desktop launcher:
[`launcher/launcher.yaml`](launcher/launcher.yaml) has one entry per teleoperation
setup (KER, VR), each listing the task sets it can record, and the operator picks
the task on screen after starting the shortcut.

### Desktop launcher

[`launcher/`](launcher/README.md) starts a configuration from a desktop
shortcut, so operators do not need a terminal: it configures the CAN interfaces
(needs `sudo`), runs `uv run dora build` / `uv run dora run` (with `--uv`),
opens the task screen, and shows
failures on screen in Japanese instead of on a terminal.

```console
$ ./launcher/install.sh  # once per machine, by an administrator
```

[`docs/operation-manual-ja.yaml`](docs/operation-manual-ja.yaml) is the operation
manual shown on the task screen (see the `MANUAL_FILE` environment variable of
the `ui` node).

### Real configuration

[`dataflow-ker.yaml`](dataflow-ker.yaml) teleoperates OpenArm with the KER
leader arms, [`dataflow-vr.yaml`](dataflow-vr.yaml) with a VR headset (Quest).
Both record the same task sets, so the launcher has one entry each and
lists the task sets inside it.

### Dummy configuration

[`dataflow_dummy.yaml`](dataflow_dummy.yaml) is a configuration that doesn't use real OpenArm. We can use this for testing a dataflow without real OpenArm.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

Copyright 2026 Enactic, Inc.

## Code of Conduct

All participation in the OpenArm project is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
