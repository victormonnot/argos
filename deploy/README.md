# Hosted ARGOS demo

[Documentation](../docs/README.md) · [Project overview](../README.md)

The public demo uses the real console and Python archive readers. Visitors can
explore Observation, Flight controls, Live MAVLink and Sessions, including the
source, telemetry, video, reception and capture inspectors. Live operations are
disabled with an explanation; unavailable measurements stay empty.

In **Sessions**, visitors can replay the bundled simulated flight, inspect its
camera and detections, filter recorded MAVLink messages, and explore reception
analysis and framing reports. Each browser has its own playback cursor.

Observation loads behind a blurred welcome overlay with a **Try ARGOS** button
and a short demo-limit note. Entering dismisses the overlay in place. The `/demo`
URL opens Observation directly. The bundled flight opens automatically when
the visitor first enters Sessions; its images and measurements are currently
available there, while Observation and Live MAVLink have no live input.
This mode does not run flight control, live sources, recording, or detection.
It does not start a simulator and cannot change the recorded flight.

## Try locally

From a source checkout with `.[console,mavlink]` installed:

```sh
python -m argos.console --demo --port 8082
```

Open `http://127.0.0.1:8082`. The default demo directory is the published
`examples/demo-flight` fixture, not your personal recording directory. For an
installed wheel, pass `--recordings-dir` with an explicit copy of that fixture.
Only place recordings intended for public distribution in a hosted directory.

The ordinary command without `--demo` retains the local console workflow.
Public bind addresses and additional allowed hostnames are restricted to demo
mode. The demo server rejects write requests and exposes archive reads only;
hiding controls in the browser is not its access boundary.

## Container

```sh
docker build -t argos-demo .
docker run --rm --name argos-demo \
  --read-only --tmpfs /tmp:size=16m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --cpus 1 --pids-limit 128 \
  -p 127.0.0.1:8082:8080 argos-demo
```

The image runs as an unprivileged user. Its build verifies the bundled recording
and includes no model, personal recordings, private notes or Git metadata.
The `.dockerignore` file uses an explicit build-context allowlist. No persistent
volume is needed: the published recording is part of the image and archive
queries are read-only.

The health check reads `/api/demo-config` through loopback. The container's
default allowed public hostname is `localhost`; loopback addresses remain
accepted for health checks and local previews.

## Coolify on an existing server

Create a separate application with the repository root as Base Directory and
`/deploy/compose.coolify.yaml` as its Docker Compose file. Configure:

| Setting | Value |
| --- | --- |
| Runtime variable `APP_HOST` | The exact demo hostname, such as `argos.example.com` |
| Public domain for service `app` | `https://argos.example.com:8080` |
| Build context | Repository root |
| Persistent storage | None |
| Host port mapping | None |
| Automatic deployment | Leave disabled until the publication workflow is selected |

The domain's DNS must point to the server. Coolify handles HTTPS and routes to
port 8080 on its internal network; the application does not need a separately
published host port. Set a hostname, without a scheme, path or wildcard, in
`APP_HOST`. The CLI receives it through `--public-host` and continues rejecting
unrecognized Host headers.

The Compose service limits memory to 512 MiB, CPU to one core and processes to
128. These are deployment bounds, not a visitor-capacity claim. Start with the
single bundled recording; large archives and sustained concurrent analysis
require their own measurements.

Deploy reviewed source through your normal commit/push workflow. Installing this
application does not require changing other applications, volumes or proxy
configuration on the server. Keep operational credentials outside this repo.

### Deploying a prebuilt image

You can also build and review an image before publishing a Git commit. Give it
an immutable release tag, transfer it to the server, and load it with
`docker load`. Retain the exact image ID and source hashes in your deployment
record.

For this path, create a separate **Docker Compose Empty** service in Coolify.
Use the same Compose settings, replace `build` with `image: your-release-tag`
and `pull_policy: never`, and provide the exact public hostname in the command.
Assign the service's domain and start it normally. Do not select **Pull Latest
Images** for an image that exists only on this server. Keep the image archive
or a reproducible source snapshot for recovery on another server.

This is a managed Coolify service; it is not the Docker Image application type,
whose deployment can attempt a registry pull. Updates require loading a new
reviewed release and changing only this service's image tag. Publishing a Git
commit alone does not update an image-based deployment.

## Check a deployment

Confirm that Observation appears behind the blurred welcome and **Try ARGOS**
dismisses it in place. Check all workspaces and inspectors, visibly disabled live
controls, and keyboard/mobile navigation. Open Sessions and verify playback,
recorded messages, analysis and framing reports. Check that a second browser can
seek independently. Confirm the demo rejects `POST /api/sources`,
`POST /api/recordings/start` and `POST /api/control/claim`, and that live state
is unavailable at `/api/state`.

Check HTTPS and the allowed domain after configuring DNS. Retain the previous
image or source revision for rollback. The demo has no mutable user data to
migrate or restore.

The [fixture guide](../examples/demo-flight/README.md) documents the captured
simulation, its limits and rendered-asset attribution. Keep those notices with
redistributed recordings.
