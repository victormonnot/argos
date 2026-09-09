# Walking-person scene

This fragment adds an animated person to the launcher's isolated runway world.
The person walks a short loop 8–10 metres ahead of the vehicle's initial position,
with lateral motion and changes in orientation. Coordinates specify simulation
content only: ARGOS perception receives the onboard RGB image, not the actor's
name, pose, route, segmentation or a simulated bounding box.

The forward camera uses a 640 × 480 image and a fixed 1.2 radian horizontal field
of view, as declared in the pinned camera model. The person scene removes the
optional `CameraZoomPlugin` from its private copied gimbal: that upstream plugin
starts with a 2.0 radian goal and changes the rendered view without a command,
while camera-info intrinsics retain their declared values (see the pinned
[plugin implementation](https://github.com/ArduPilot/ardupilot_gazebo/blob/082a0fe231f6e63bc8d1598f1cba461d9e2ea7f5/src/CameraZoomPlugin.cc)). The private copy
therefore keeps the declared optics fixed; the installed gimbal and default
runway scene remain unchanged. Joint, mounting and other camera plugins are
preserved. This does not resize the person or alter detector thresholds.

Pilot manually to change viewpoint. There is no automatic camera aiming or
zoom, detector-specific texture, artificial marker or downward optical flow.

From a source checkout, prepare the optional asset once, then launch the scene:

```bash
python examples/setup_vision_scene.py
python examples/run_web_control.py --scene person
```

The cache defaults to `$XDG_CACHE_HOME/argos/gazebo` when `XDG_CACHE_HOME` is an
absolute path, otherwise `~/.cache/argos/gazebo`. Use setup's `--assets-dir DIR`
and the launcher's matching `--person-assets DIR` for a different cache.
`--scene runway` remains the default. `--vision-model PATH` forwards an explicit
local detector model to the console; the scene also works without perception.
The model variant defaults to `tiny`; use `--vision-variant s` with the verified
S model path for the optional larger CPU detector. Model setup, pinned checksums
and interpretation are documented in the [perception guide](../../docs/vision.md).
Choosing a detector does not alter the scene, camera optics or actor.

## Asset provenance

`examples/setup_vision_scene.py` downloads the unmodified `walk.dae` from
[Mingfei's actor, Gazebo Fuel version 1](https://fuel.gazebosim.org/1.0/Mingfei/models/actor/1).
Fuel declares the model **Creative Commons Attribution 4.0 International**
([license](https://creativecommons.org/licenses/by/4.0/)). Attribution is retained
here and beside the cached mesh in `NOTICE.txt`. ARGOS defines the trajectory;
the skin and skeleton animation are unmodified.

- Download: `https://fuel.gazebosim.org/1.0/Mingfei/models/actor/1/files/meshes/walk.dae`
- Size: 2,277,322 bytes
- SHA-256: `49af0df3a319d1cb8ca2cebf02dbd00f625e5d5bec820bc5e109925b18b65c6e`

The mesh has no external texture files. It is an optional cached dependency,
not a Git-tracked binary. Setup checks the exact size and SHA-256; launch checks
the local asset again and never downloads a substitute. The pinned ArduPilot
Gazebo runway, vehicle and camera keep their existing provenance documented in
`docs/sitl-observation.md`.

## Limits

This is one rendered person in a simple outdoor simulation, not evidence of
performance on real people, backgrounds, weather, clothing or camera hardware.
Gazebo actors are scripted visual entities: their motion does not respond to
collisions or the drone. This scene therefore cannot validate clearance or
collision avoidance. Gazebo documents the animation and physics distinction in
its [actor guide](https://gazebosim.org/docs/harmonic/actors/).

The route is deliberately repeatable for inspecting the pixel-processing
pipeline. Neither its coordinates nor the person's model dimensions may be used
as operational tracking or metric-distance inputs. A manual pilot still controls
every flight command.
