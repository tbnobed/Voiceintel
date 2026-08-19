---
name: VoiceIntel GPU transcription
description: How Whisper GPU transcription is configured and what can break it.
---

## Detection
transcription_service.py uses `ctranslate2.get_supported_compute_types("cuda")` — no torch needed. Succeeds → cuda/float16, fails → cpu/int8.

## Docker requirement
The `app` service in docker-compose.yml needs the deploy block:
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```
Without it, CUDA is invisible inside the container even if the host driver is healthy.

## Host: nvidia-persistenced NOT required
RTX 4070 on this host (voice-ai.obtv.io). Ollama container proved GPU passthrough works without persistenced. Do not add that as a prerequisite.

## Common failure: driver/library version mismatch
`nvidia-smi` returns "Driver/library version mismatch" after a kernel/driver package upgrade that wasn't followed by a reboot. Fix: `sudo reboot`. The mismatch shows NVML library version (e.g. 535.309) differing from the loaded kernel module.

**Why:** The GPU block was commented out in a previous session to fix a container startup crash caused by the driver mismatch. It must be re-enabled once the host driver is healthy.
