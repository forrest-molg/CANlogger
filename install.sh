#!/usr/bin/env bash
set -euo pipefail

REPO="${CANLOGGER_REPO:-}"
TAG="latest"
INSTALL_DIR="/opt/CANlogger"
ENABLE_SERVICE=1
LOCAL_MODE=0
NO_SUDO=0

usage() {
	cat <<'EOF'
CANlogger installer

Usage:
	./install.sh --repo OWNER/REPO [--tag vX.Y.Z] [--install-dir /opt/CANlogger]
	./install.sh --local [--install-dir /opt/CANlogger]

Options:
	--repo OWNER/REPO      GitHub repository for release downloads
	--tag TAG              Release tag to install (default: latest)
	--install-dir PATH     Install destination (default: /opt/CANlogger)
	--no-service           Do not install/start systemd service
	--local                Install from current local checkout
	--no-sudo              Do not use sudo for privileged actions
	-h, --help             Show this help text
EOF
}

require_cmd() {
	local cmd="$1"
	if ! command -v "$cmd" >/dev/null 2>&1; then
		echo "Missing required command: $cmd" >&2
		exit 1
	fi
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--repo)
			REPO="${2:-}"
			shift 2
			;;
		--tag)
			TAG="${2:-}"
			shift 2
			;;
		--install-dir)
			INSTALL_DIR="${2:-}"
			shift 2
			;;
		--no-service)
			ENABLE_SERVICE=0
			shift
			;;
		--local)
			LOCAL_MODE=1
			shift
			;;
		--no-sudo)
			NO_SUDO=1
			shift
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "Unknown argument: $1" >&2
			usage
			exit 1
			;;
	esac
done

require_cmd tar
require_cmd cp
require_cmd rm

if [[ "$LOCAL_MODE" -eq 0 ]]; then
	require_cmd curl
fi

if [[ "$NO_SUDO" -eq 1 || "$(id -u)" -eq 0 ]]; then
	SUDO=""
else
	require_cmd sudo
	SUDO="sudo"
fi

TMP_DIR=""
SRC_DIR=""
cleanup() {
	if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
		rm -rf "$TMP_DIR"
	fi
}
trap cleanup EXIT

if [[ "$LOCAL_MODE" -eq 1 ]]; then
	SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
	if [[ -z "$REPO" ]]; then
		echo "--repo is required unless --local is used" >&2
		exit 1
	fi

	ASSET="canlogger-linux-x86_64.tar.gz"
	if [[ "$TAG" == "latest" ]]; then
		URL="https://github.com/${REPO}/releases/latest/download/${ASSET}"
	else
		URL="https://github.com/${REPO}/releases/download/${TAG}/${ASSET}"
	fi

	TMP_DIR="$(mktemp -d)"
	BUNDLE_PATH="$TMP_DIR/$ASSET"

	echo "Downloading CANlogger bundle from $URL"
	curl -fL "$URL" -o "$BUNDLE_PATH"

	tar -xzf "$BUNDLE_PATH" -C "$TMP_DIR"
	SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n1)"

	if [[ -z "$SRC_DIR" || ! -d "$SRC_DIR" ]]; then
		echo "Failed to unpack CANlogger bundle" >&2
		exit 1
	fi
fi

if ! command -v docker >/dev/null 2>&1; then
	echo "Docker is required but not found. Install Docker first." >&2
	exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
	echo "Docker Compose plugin is required but not found." >&2
	exit 1
fi

echo "Installing to $INSTALL_DIR"
$SUDO mkdir -p "$INSTALL_DIR"
$SUDO find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
$SUDO cp -a "$SRC_DIR"/. "$INSTALL_DIR"/

$SUDO chmod +x "$INSTALL_DIR/start.sh" "$INSTALL_DIR/stop.sh" "$INSTALL_DIR/install.sh"
if [[ -d "$INSTALL_DIR/scripts" ]]; then
	$SUDO find "$INSTALL_DIR/scripts" -type f -name "*.sh" -exec chmod +x {} +
fi

if [[ "$ENABLE_SERVICE" -eq 1 ]]; then
	SERVICE_SRC="$INSTALL_DIR/systemd/canlogger.service"
	SERVICE_DST="/etc/systemd/system/canlogger.service"

	if [[ ! -f "$SERVICE_SRC" ]]; then
		echo "Missing service file at $SERVICE_SRC" >&2
		exit 1
	fi

	echo "Installing and starting systemd service"
	$SUDO cp "$SERVICE_SRC" "$SERVICE_DST"
	$SUDO systemctl daemon-reload
	$SUDO systemctl enable --now canlogger.service
else
	echo "Starting CANlogger without systemd"
	$SUDO bash "$INSTALL_DIR/start.sh"
fi

echo "Install complete"
echo "GUI: http://localhost:8000"
echo "API docs: http://localhost:8000/docs"
