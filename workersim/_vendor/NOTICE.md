Derived from GPUStack v2.3.1, commit 458030e88718ce47bd31f7cdee9cbc8994a6760b.
Copyright GPUStack contributors. Apache License 2.0; see LICENSE-GPUStack.txt.
command.py contains extracted pure argv helpers. vllm_topology.py changes only the command import path (plus formatting).
No GPUStack Server or UI dependency is installed or modified by this code.
../data/metrics_config.yaml is GPUStack's built-in metrics config (`gpustack/assets/metrics_config/metrics_config.yaml`), unmodified, used as the native fallback when `/v2/metrics/config` is unavailable.
