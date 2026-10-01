#!/usr/bin/env bash
# Throwaway Debian VM for the README screenshots: real sorto TUI, generic demo data.
#
#   docs/demo/vm.sh create                 download Debian 13 cloud image, make disk + cloud-init seed
#   docs/demo/vm.sh start | stop | ssh [CMD]
#   docs/demo/vm.sh capture [inbox reorg doctor]
#                                          build a wheel from this checkout, install it in the VM,
#                                          run capture.sh there, copy frames to $VM_DIR/shots/<time>/
#
# The VM runs as an unprivileged user-mode QEMU/KVM guest with no shared folders; files go in
# and out with scp only. sorto in the VM reaches the host's Ollama through an SSH reverse
# tunnel (guest 127.0.0.1:11434, sorto's default, to the host's OLLAMA_PORT), so it still
# talks to 127.0.0.1 (sorto refuses any non-loopback model URL).
#
# Env: VM_DIR (default ~/.cache/sorto-demo-vm), SSH_PORT (2227), OLLAMA_PORT (host Ollama, 11434),
#      SORTO_ARGS / SWITCH_MODEL_AT (passed to capture.sh), IMAGE_URL.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VM_DIR="${VM_DIR:-$HOME/.cache/sorto-demo-vm}"
SSH_PORT="${SSH_PORT:-2227}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o "UserKnownHostsFile=$VM_DIR/known_hosts"
          -o ServerAliveInterval=30 -i "$VM_DIR/id_demo")

vm_ssh() { ssh "${SSH_OPTS[@]}" -p "$SSH_PORT" demo@127.0.0.1 "$@"; }
vm_scp() { local a=(); for x in "$@"; do a+=("${x/#vm:/demo@127.0.0.1:}"); done; scp "${SSH_OPTS[@]}" -P "$SSH_PORT" "${a[@]}"; }

create() {
  mkdir -p "$VM_DIR/seed"
  local base="$VM_DIR/$(basename "$IMAGE_URL")"
  [ -f "$base" ] || curl -fL -o "$base" "$IMAGE_URL"
  [ -f "$VM_DIR/id_demo" ] || ssh-keygen -q -t ed25519 -N '' -C demo-vm -f "$VM_DIR/id_demo"
  [ -f "$VM_DIR/demo-vm.qcow2" ] || qemu-img create -q -f qcow2 -F qcow2 -b "$base" "$VM_DIR/demo-vm.qcow2" 12G
  printf 'instance-id: sorto-demo-1\nlocal-hostname: sorto-demo\n' > "$VM_DIR/seed/meta-data"
  cat > "$VM_DIR/seed/user-data" <<EOF
#cloud-config
hostname: sorto-demo
users:
  - name: demo
    gecos: Demo User
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$VM_DIR/id_demo.pub")
ssh_pwauth: false
package_update: true
packages: [python3, python3-venv, pipx, xvfb, xterm, fonts-dejavu-core, imagemagick,
           libimage-exiftool-perl, poppler-utils, ffmpeg, mediainfo, file, xdotool, tmux,
           x11-apps, x11-utils, pngquant, ghostscript]
EOF
  xorriso -as mkisofs -quiet -o "$VM_DIR/seed.iso" -V cidata -J -r "$VM_DIR/seed"
  echo "created in $VM_DIR; next: $0 start"
}

start() {
  if [ -f "$VM_DIR/vm.pid" ] && kill -0 "$(cat "$VM_DIR/vm.pid")" 2>/dev/null; then
    echo "VM already running"
  else
    qemu-system-x86_64 -name sorto-demo -enable-kvm -cpu host -smp 2 -m 3072 \
      -drive file="$VM_DIR/demo-vm.qcow2",if=virtio,format=qcow2 \
      -drive file="$VM_DIR/seed.iso",media=cdrom,readonly=on \
      -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22 -device virtio-net-pci,netdev=n0 \
      -display none -serial file:"$VM_DIR/serial.log" -daemonize -pidfile "$VM_DIR/vm.pid"
  fi
  for _ in $(seq 1 120); do vm_ssh -o ConnectTimeout=3 true 2>/dev/null && break; sleep 3; done
  vm_ssh 'cloud-init status --wait >/dev/null 2>&1 || true'
  if ! [ -f "$VM_DIR/tunnel.pid" ] || ! kill -0 "$(cat "$VM_DIR/tunnel.pid")" 2>/dev/null; then
    ssh "${SSH_OPTS[@]}" -p "$SSH_PORT" -f -N -o ExitOnForwardFailure=yes \
      -R "127.0.0.1:11434:127.0.0.1:$OLLAMA_PORT" demo@127.0.0.1
    pgrep -n -f "R 127.0.0.1:11434:127.0.0.1:$OLLAMA_PORT demo@127.0.0.1" > "$VM_DIR/tunnel.pid"
  fi
  echo "VM up; host Ollama :$OLLAMA_PORT is the guest's 127.0.0.1:11434"
}

stop() {
  [ -f "$VM_DIR/tunnel.pid" ] && { kill "$(cat "$VM_DIR/tunnel.pid")" 2>/dev/null || true; rm -f "$VM_DIR/tunnel.pid"; }
  [ -f "$VM_DIR/vm.pid" ] || { echo "not running"; return; }
  local pid; pid="$(cat "$VM_DIR/vm.pid")"
  vm_ssh 'sudo systemctl poweroff' 2>/dev/null || true
  for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill "$pid" 2>/dev/null || true
  rm -f "$VM_DIR/vm.pid"
  echo stopped
}

capture() {
  start
  local src="$VM_DIR/src" dist="$VM_DIR/dist"
  rm -rf "$src" "$dist" && mkdir -p "$src" "$dist"
  # Build from a copy so no build/ or *.egg-info lands in the checkout.
  tar -C "$REPO" --exclude=.git --exclude=build --exclude=dist --exclude=.venv --exclude=docs \
      --exclude=__pycache__ --exclude='*.egg-info' -cf - . | tar -C "$src" -xf -
  (cd "$src" && python3 -m pip wheel --no-deps . -w "$dist" -q)
  vm_ssh 'mkdir -p ~/bin ~/dist && rm -f ~/dist/*.whl'
  vm_scp "$dist"/sorto-*.whl vm:dist/
  vm_scp "$HERE"/make-demo.sh "$HERE"/capture.sh vm:bin/
  vm_ssh 'pipx install --force ~/dist/sorto-*.whl >/dev/null && ~/.local/bin/sorto --version'
  vm_ssh "rm -rf ~/shots; SORTO_ARGS=$(printf '%q' "${SORTO_ARGS:-}") SWITCH_MODEL_AT=${SWITCH_MODEL_AT:-0} bash ~/bin/capture.sh $*"
  local out; out="$VM_DIR/shots/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$out" && vm_scp 'vm:shots/*' "$out/"
  echo "frames in $out (each .png has a .txt dump of the screen); copy the good ones to docs/screenshots/"
}

case "${1:-}" in
  create) create ;;
  start) start ;;
  stop) stop ;;
  ssh) shift; vm_ssh "$@" ;;
  capture) shift; capture "$@" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
