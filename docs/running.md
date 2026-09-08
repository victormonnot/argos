# Starting, returning to and stopping a session

## On the local machine

From the repository, activate `.venv` and start the console with its source options.
The [SITL guide](sitl-observation.md) covers Gazebo, the autopilot and the console.
`--port 8081` runs a second independent console; do not give it the same UDP
listening port or the TCP connection already reserved for the first console.
For manual simulation flight, the [web-control launcher](web-control.md) starts
its own Gazebo, SITL and opt-in console with separate ports and run files.

Settings changed through **Sources** in the interface are kept for the lifetime
of the process, not in a configuration file. Save the launch command to restore
the same settings after a restart. Completed recordings remain available in the
capture directory independently of the process.

## Keeping processes running in tmux

After installing `tmux` on the simulation machine:

```sh
tmux new-session -s argos
```

In this session, start Gazebo using the first launch command in the SITL guide.
Create two windows with **Ctrl-b then c**, and start SITL and the console in
their respective windows. Set the guide's environment variables in each window;
creating a window does not copy the exports from another window.
**Ctrl-b then n/p** switches windows. **Ctrl-b then d** detaches the terminal
while leaving the processes running.

To return to the session:

```sh
tmux attach-session -t argos
```

## From a remote laptop

Gazebo, SITL and the console stay on the simulation machine. From the laptop,
open a tunnel to your SSH account, replacing `user@host` with your
usual username and host:

```sh
ssh -N -L 8081:127.0.0.1:8080 user@host
```

Open **http://127.0.0.1:8081** on the laptop. Local port 8081 connects to port
8080 of the remote console; camera and recording paths refer to that remote
machine. No change to the ARGOS listening address is needed. Closing the tunnel
only disconnects the browser; an active capture continues on the server.
If the browser was piloting, its lease expires and the service requests landing;
control does not resume when the tunnel returns. The manual-control guide
describes the separate process/link-loss fallback.
To use tmux remotely, open a separate terminal with `ssh user@host`
and attach to the session there.

This setup uses existing SSH authentication. The console itself is a local
service, with no account management or intended public HTTP exposure.

## Stopping and restarting

When using Flight controls, land and wait for confirmed disarming before the following
shutdown sequence. With the all-in-one web-control launcher, Ctrl-C stops its
three children and retains its printed run directory.

1. Stop any active capture from **MAVLink recording** and verify that it has been finalized.
2. Press **Ctrl-C** in the console terminal and wait for it to exit.
3. Stop SITL, then Gazebo, with **Ctrl-C** in their terminals.
4. Close the tmux windows that have returned to a shell with `exit`.

A normal server shutdown also attempts to finalize the active recording.
An abrupt termination or a write error may leave an incomplete file on disk.
Avoid killing the entire tmux session for a routine shutdown.

To restart, launch the three processes in the order given in the guide, using
a fresh SITL directory. Recordings stay in the capture directory; do not delete
that directory to free a port. An "address already in use" message means you
need to identify the process still using the port, or explicitly choose other
ports with matching settings on the sender and receiver.
