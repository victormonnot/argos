"""Twenty-second read-only console trace; no camera/serial writes or images."""
import http.client
import json
import time


def watch(seconds=20, *, clock=time.monotonic, sleep=time.sleep):
    started = clock()
    last_signature = None
    last_print = -1.
    summary = {"samples": 0, "tracking_samples": 0, "paused_samples": 0, "stopped_samples": 0,
               "max_analysis_age_ms": None, "max_turnaround_ms": None,
               "max_result_interval_ms": None}
    print("Read-only preview trace. Keep ARGOS visible beside this terminal, then select a person.", flush=True)
    while clock() - started < seconds:
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=1.)
        request_at = clock()
        try:
            connection.request("GET", "/api/state", headers={"Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            data = response.read(262145)
            if len(data) > 262144:
                raise ValueError("Response too large")
            state = json.loads(data)
            video, vision, yaw = (state.get(name) or {} for name in ("video", "vision", "yaw_preview"))
            timing = vision.get("timing") or {}
            def ms(value):
                return round(value * 1000, 1) if type(value) in (int, float) else None
            result = {
                "t_s": round(clock() - started, 2),
                "request_ms": round((clock() - request_at) * 1000, 1),
                "camera": video.get("state"), "camera_age_ms": ms(video.get("rx_age_s")),
                "camera_detail": str(video.get("detail", ""))[:240],
                "vision": vision.get("state"), "inference_ms": vision.get("inference_ms"),
                "processed": vision.get("processed"),
                "submit_age_ms": timing.get("submit_age_ms"),
                "turnaround_ms": timing.get("turnaround_ms"),
                "result_interval_ms": timing.get("result_interval_ms"),
                "analysis_age_ms": ms(vision.get("frame_age_s")),
                "phase": yaw.get("phase"), "target": yaw.get("target_id"),
                "revision": yaw.get("revision"), "frame": yaw.get("frame_sequence"),
                "preview_age_ms": ms(yaw.get("frame_age_s")),
                "detail": str(yaw.get("detail", ""))[:240],
            }
            summary["samples"] += 1
            if result["phase"] in ("tracking", "paused", "stopped"):
                summary[result["phase"] + "_samples"] += 1
            for name in ("analysis_age_ms", "turnaround_ms", "result_interval_ms"):
                value = result[name]
                if type(value) in (int, float):
                    key = "max_" + name
                    summary[key] = value if summary[key] is None else max(summary[key], value)
            signature = tuple(result[key] for key in
                              ("camera", "camera_detail", "vision", "phase", "target", "revision", "detail"))
            if signature != last_signature or clock() - last_print >= 1.:
                print(json.dumps(result, ensure_ascii=True), flush=True)
                last_print = clock()
                last_signature = signature
        except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError) as exc:
            print(json.dumps({"t_s": round(clock() - started, 2), "error": str(exc)[:240]}), flush=True)
        finally:
            connection.close()
        sleep(min(.2, max(0., seconds - (clock() - started))))
    print(json.dumps({"summary": summary}), flush=True)
    print("Trace finished. No command or selection change was sent.", flush=True)


if __name__ == "__main__":
    try:
        watch()
    except KeyboardInterrupt:
        print("Trace interrupted; no command sent.")
