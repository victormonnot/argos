# Offline inference thread comparison

Use `examples/benchmark_vision_threads.py` to compare the existing YOLOX-Tiny
CPU thread limits 1, 2 and 4 on the computer that will run ARGOS. The model and
ARGOS vision dependency must already be installed; this tool downloads nothing
and does not change the console or its configuration.

Stop the console in its terminal with **Ctrl+C** and wait for its shutdown.
Close other inference jobs. Keep the computer's power conditions consistent
with the intended use, and record whether it is on battery or mains power.
No aircraft, video adapter or radio needs to be connected.

```sh
.venv/bin/python examples/benchmark_vision_threads.py
```

If the model is outside the normal ARGOS cache, pass
`--model /absolute/path/to/yolox_tiny.onnx`. The helper uses the production
detector's size/SHA-256 verification. A copied standalone script also works
when launched with the Python environment in which ARGOS is installed.

The default comparison uses three passes, rotating the order of the thread
settings. Each batch runs in a separate process, loads the same pinned model,
discards three warmup detections and measures ten detections. It reports the
median and nearest-rank 90th percentile over 30 samples per setting, plus CPU,
available logical CPUs, Python and OpenCV versions. `--rounds 1 --samples 2`
provides a short execution check, not a useful performance comparison.

All batches use the same generated 640×480 JPEG. The network still receives
the detector's fixed 416×416 input. `inference_ms` covers the network forward;
`detector_ms` additionally covers JPEG decode, preprocessing and person-box
postprocessing. The generated image is not a recognition-quality test, and
its postprocessing cost is not representative of a scene with people.

These isolated timings exclude live capture, appearance encoding, tracking,
worker queues, console/browser load, HTTP and USB. They cannot establish a
working command cadence or validate aircraft control. A faster result is only
a candidate for a subsequent live run with that `--vision-threads` value and
the existing [read-only preview trace](vision.md#model-and-data-flow).
The console separately defaults to five analyses per second. Its `--vision-hz`
option can set an integer ceiling from 1 to 10; `--vision-threads 4 --vision-hz 8`
is a candidate only when measured live worker time leaves enough room. Faster
inference alone cannot exceed that scheduling ceiling. Conversely, a higher
ceiling cannot make a slow job complete faster. One pending job still prevents
backlogs; original image-age, command lifetime and session limits are unchanged.
