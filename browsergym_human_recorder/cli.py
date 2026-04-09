"""
CLI entry point for browsergym-human-recorder.

Commands:
    install  — Install dependencies (sub-packages, playwright, Docker image)
    launch   — Run the trajectory recorder
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# Root of the repository (one level up from this file)
REPO_ROOT = Path(__file__).resolve().parent.parent

# Google Drive file ID for the pre-populated Mattermost Docker image
MATTERMOST_DOCKER_GDRIVE_ID = "1aM2zyvCgONH0pD8MpYKj6i2e1Xo4VueL"
MATTERMOST_IMAGE_NAME = "mattermost-populated"
MATTERMOST_CONTAINER_NAME = "mattermost"
MATTERMOST_PORT = 8065


def _run(cmd, **kwargs):
    """Run a shell command, printing it first."""
    print(f"  $ {cmd}")
    return subprocess.run(cmd, shell=True, check=True, **kwargs)


def _docker_available():
    """Check if docker is available."""
    return shutil.which("docker") is not None


def _docker_image_exists(image_name):
    """Check if a Docker image is already loaded."""
    result = subprocess.run(
        f"docker images -q {image_name}",
        shell=True, capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def _docker_container_running(container_name):
    """Check if a Docker container is running."""
    result = subprocess.run(
        f"docker ps -q -f name=^/{container_name}$",
        shell=True, capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def cmd_install(args):
    """Install all dependencies: sub-packages, playwright, and Docker image."""
    print("[install] Installing BrowserGym sub-packages...")
    _run(
        f"{sys.executable} -m pip install"
        f" -e {REPO_ROOT / 'browsergym' / 'core'}"
        f" -e {REPO_ROOT / 'browsergym' / 'experiments'}"
        f" -e {REPO_ROOT / 'browsergym' / 'webarena_pro'}"
    )

    print("\n[install] Installing Playwright Chromium...")
    _run("playwright install chromium")

    print("\n[install] Setting up Mattermost Docker image...")
    if not _docker_available():
        print("  [warn] Docker is not installed or not in PATH.")
        print("  Please install Docker and re-run: browsergym-human-recorder install")
        return

    if _docker_image_exists(MATTERMOST_IMAGE_NAME):
        print(f"  Docker image '{MATTERMOST_IMAGE_NAME}' already exists. Skipping download.")
    else:
        # Download from Google Drive using gdown (install if needed)
        tar_path = REPO_ROOT / "mattermost-populated.tar"
        if not tar_path.exists():
            print("  Downloading Mattermost Docker image from Google Drive...")
            _run(f"{sys.executable} -m pip install -q gdown")
            _run(f"gdown {MATTERMOST_DOCKER_GDRIVE_ID} -O {tar_path}")
        print("  Loading Docker image...")
        _run(f"docker load -i {tar_path}")
        print(f"  Cleaning up {tar_path}...")
        tar_path.unlink(missing_ok=True)

    print("\n[install] Done! Run 'browsergym-human-recorder launch --task_id 0' to start recording.")


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

def cmd_launch(args):
    """Launch the trajectory recorder."""
    # Ensure Mattermost container is running
    if _docker_available():
        if _docker_container_running(MATTERMOST_CONTAINER_NAME):
            if args.reset:
                print("[launch] Resetting Mattermost container...")
                _run(f"docker rm -f {MATTERMOST_CONTAINER_NAME}")
                _run(
                    f"docker run -d --name {MATTERMOST_CONTAINER_NAME}"
                    f" -p {MATTERMOST_PORT}:{MATTERMOST_PORT} {MATTERMOST_IMAGE_NAME}"
                )
            else:
                print("[launch] Mattermost container already running.")
        else:
            print("[launch] Starting Mattermost container...")
            # Remove stopped container if it exists
            subprocess.run(
                f"docker rm -f {MATTERMOST_CONTAINER_NAME}",
                shell=True, capture_output=True,
            )
            _run(
                f"docker run -d --name {MATTERMOST_CONTAINER_NAME}"
                f" -p {MATTERMOST_PORT}:{MATTERMOST_PORT} {MATTERMOST_IMAGE_NAME}"
            )
            print("  Waiting for Mattermost to be ready...")
            import time
            time.sleep(5)
    else:
        print("[launch] Docker not found. Assuming Mattermost is running externally.")

    # Set environment variable
    mattermost_url = args.mattermost_url or f"http://localhost:{MATTERMOST_PORT}"
    os.environ["WAP_MATTERMOST"] = mattermost_url
    print(f"[launch] WAP_MATTERMOST={mattermost_url}")

    # Import and run the recorder
    # Add repo root to path so record_trajectory.py can be imported
    sys.path.insert(0, str(REPO_ROOT))
    from record_trajectory import record

    print(f"[launch] Starting recorder for task {args.task_id}...")
    record(
        task_id=args.task_id,
        output_dir=args.output_dir,
        max_steps=args.max_steps,
        timeout=args.timeout,
    )


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def cmd_reset(args):
    """Reset the Mattermost Docker container to a clean state."""
    if not _docker_available():
        print("[reset] Docker is not available.")
        return
    print("[reset] Resetting Mattermost container...")
    _run(f"docker rm -f {MATTERMOST_CONTAINER_NAME}")
    _run(
        f"docker run -d --name {MATTERMOST_CONTAINER_NAME}"
        f" -p {MATTERMOST_PORT}:{MATTERMOST_PORT} {MATTERMOST_IMAGE_NAME}"
    )
    print("[reset] Done.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="browsergym-human-recorder",
        description="CLI for recording human browser trajectories with BrowserGym",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- install ---
    subparsers.add_parser("install", help="Install dependencies (sub-packages, playwright, Docker)")

    # --- launch ---
    launch_parser = subparsers.add_parser("launch", help="Launch the trajectory recorder")
    launch_parser.add_argument(
        "--task_id", type=int, required=True, help="WebArena-Pro task ID (0-9)"
    )
    launch_parser.add_argument(
        "--output_dir", type=str, default="./trajectories", help="Output directory"
    )
    launch_parser.add_argument(
        "--max_steps", type=int, default=50, help="Max steps per trajectory"
    )
    launch_parser.add_argument(
        "--timeout", type=int, default=600, help="Max recording time in seconds"
    )
    launch_parser.add_argument(
        "--reset", action="store_true", help="Reset Mattermost container before recording"
    )
    launch_parser.add_argument(
        "--mattermost_url", type=str, default=None,
        help="Mattermost URL (default: http://localhost:8065)",
    )

    # --- reset ---
    subparsers.add_parser("reset", help="Reset the Mattermost Docker container")

    args = parser.parse_args()

    commands = {
        "install": cmd_install,
        "launch": cmd_launch,
        "reset": cmd_reset,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
