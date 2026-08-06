# Data collection configurations for OpenArm with dora-rs

This repository provides data collection configurations for [OpenArm](https://openarm.dev/) with [dora-rs](https://dora-rs.ai/).

## Configurations

[`metadata.yaml`](metadata.yaml) is metadata used by all configurations.

Each task in `metadata.yaml` may carry `prompt_ja` / `description_ja`. They are
shown to the operator instead of `prompt` / `description`, which stay in English
because they are recorded as the dataset's language instruction.

### Desktop launcher

[`launcher/`](launcher/README.md) starts a configuration from a desktop
shortcut, so operators do not need a terminal: it configures the CAN interfaces
(needs `sudo`), runs `dora build` / `dora run`, opens the task screen, and shows
failures on screen in Japanese instead of on a terminal.

```console
$ ./launcher/install.sh  # once per machine, by an administrator
```

[`docs/operation-manual-ja.yaml`](docs/operation-manual-ja.yaml) is the operation
manual shown on the task screen (see the `MANUAL_FILE` environment variable of
the `ui` node).

### Real configuration

TODO

### Dummy configuration

[`dataflow_dummy.yaml`](dataflow_dummy.yaml) is a configuration that doesn't use real OpenArm. We can use this for testing a dataflow without real OpenArm.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

Copyright 2026 Enactic, Inc.

## Code of Conduct

All participation in the OpenArm project is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
