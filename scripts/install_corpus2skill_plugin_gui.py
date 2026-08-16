"""Corpus2Skill Memory Provider plugin installer (standalone GUI, no PowerShell needed).

Installs the plugin into a Windows-native Hermes install (User Provider
tier: %HERMES_HOME%\\plugins\\corpus2skill\\). Mirrors
scripts/install_corpus2skill_plugin.ps1 but ships as a single compiled
.exe (via PyInstaller) for machines where PowerShell script execution is
blocked. Design doc: docs/hh-agent/03_Architecture.md section 13.
"""

import datetime
import os
import shutil
import subprocess
import tkinter as tk
import urllib.request
from tkinter import messagebox, scrolledtext, simpledialog

PLUGIN_RAW_BASE = (
    "https://raw.githubusercontent.com/hayashi0711-sketch/hermes-agent/"
    "hh-agent/.hermes/plugins/corpus2skill"
)
PLUGIN_FILES = ["__init__.py", "plugin.yaml", "README.md"]


def resolve_hermes_home() -> str:
    home = os.environ.get("HERMES_HOME")
    if home:
        return home
    return os.path.join(os.environ["LOCALAPPDATA"], "hermes")


def resolve_hermes_exe(hermes_home: str) -> str:
    candidate = os.path.join(hermes_home, "hermes-agent", "venv", "Scripts", "hermes.exe")
    if os.path.exists(candidate):
        return candidate
    found = shutil.which("hermes.exe") or shutil.which("hermes")
    if found:
        return found
    raise RuntimeError(
        "Could not find hermes.exe (looked in "
        f"{candidate} and PATH). Is Hermes Agent installed?"
    )


def backup_file(path: str) -> None:
    if os.path.exists(path):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, f"{path}.bak.{ts}")


def install(api_key: str, log) -> str:
    hermes_home = resolve_hermes_home()
    log(f"Hermes home: {hermes_home}")
    if not os.path.isdir(hermes_home):
        raise RuntimeError(
            f"Hermes home not found: {hermes_home}\n"
            "Install the Hermes Agent Windows app first."
        )

    hermes_exe = resolve_hermes_exe(hermes_home)
    log(f"hermes.exe: {hermes_exe}")

    plugin_dir = os.path.join(hermes_home, "plugins", "corpus2skill")
    os.makedirs(plugin_dir, exist_ok=True)
    log(f"Installing plugin files to {plugin_dir}")
    for fname in PLUGIN_FILES:
        url = f"{PLUGIN_RAW_BASE}/{fname}"
        dest = os.path.join(plugin_dir, fname)
        log(f"  downloading {fname} ...")
        urllib.request.urlretrieve(url, dest)  # noqa: S310 - fixed https github raw URL
    log("Plugin files installed.")

    env_path = os.path.join(hermes_home, ".env")
    backup_file(env_path)
    existing = ""
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8-sig", errors="replace") as f:
            existing = f.read()

    if "CORPUS2SKILL_API_KEY=" in existing:
        log("CORPUS2SKILL_API_KEY already present in .env -- leaving unchanged.")
    else:
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nCORPUS2SKILL_API_KEY={api_key}\n")
        log("Added CORPUS2SKILL_API_KEY to .env")

    config_path = os.path.join(hermes_home, "config.yaml")
    backup_file(config_path)
    log("Setting memory.provider = corpus2skill ...")
    result = subprocess.run(
        [hermes_exe, "config", "set", "memory.provider", "corpus2skill"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.stdout:
        log(result.stdout.strip())
    if result.returncode != 0:
        raise RuntimeError(f"hermes config set failed:\n{result.stderr}")
    log("memory.provider set.")
    return hermes_exe


def restart_gateway(hermes_exe: str, log) -> None:
    log("Restarting Hermes gateway ...")
    result = subprocess.run(
        [hermes_exe, "gateway", "restart"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.stdout:
        log(result.stdout.strip())
    if result.returncode != 0:
        log(f"gateway restart returned exit code {result.returncode}: {result.stderr}")
    else:
        log("Gateway restarted.")


def main() -> None:
    root = tk.Tk()
    root.title("Corpus2Skill Memory Provider Installer")
    root.geometry("560x420")

    tk.Label(
        root,
        text="Corpus2Skill Memory Provider - Installer",
        font=("Segoe UI", 12, "bold"),
    ).pack(pady=(12, 4))
    tk.Label(
        root,
        text="Shares memory between this PC's Hermes and the Modal-hosted Hermes.",
    ).pack()

    log_box = scrolledtext.ScrolledText(root, height=16, state="disabled", font=("Consolas", 9))
    log_box.pack(fill="both", expand=True, padx=12, pady=12)

    state = {"hermes_exe": None}

    def log(message: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", message + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")
        root.update_idletasks()

    button_frame = tk.Frame(root)
    button_frame.pack(pady=(0, 12))

    def on_install() -> None:
        api_key = simpledialog.askstring(
            "Corpus2Skill API Key",
            "Enter the Corpus2Skill API key (Bearer token):",
            show="*",
            parent=root,
        )
        if not api_key or not api_key.strip():
            messagebox.showwarning("Cancelled", "No API key entered. Installation cancelled.")
            return
        install_btn.configure(state="disabled")
        try:
            hermes_exe = install(api_key.strip(), log)
            state["hermes_exe"] = hermes_exe
            log("")
            log("Done. Click 'Restart Hermes Gateway' to apply, or restart the app manually.")
            restart_btn.configure(state="normal")
            messagebox.showinfo("Success", "Corpus2Skill Memory Provider plugin installed.")
        except Exception as exc:  # noqa: BLE001 - surface any failure to the user directly
            log(f"ERROR: {exc}")
            messagebox.showerror("Install failed", str(exc))
        finally:
            install_btn.configure(state="normal")

    def on_restart() -> None:
        if not state["hermes_exe"]:
            return
        restart_btn.configure(state="disabled")
        try:
            restart_gateway(state["hermes_exe"], log)
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR: {exc}")
        finally:
            restart_btn.configure(state="normal")

    install_btn = tk.Button(button_frame, text="Install", width=16, command=on_install)
    install_btn.grid(row=0, column=0, padx=6)

    restart_btn = tk.Button(
        button_frame, text="Restart Hermes Gateway", width=22, command=on_restart, state="disabled"
    )
    restart_btn.grid(row=0, column=1, padx=6)

    close_btn = tk.Button(button_frame, text="Close", width=10, command=root.destroy)
    close_btn.grid(row=0, column=2, padx=6)

    root.mainloop()


if __name__ == "__main__":
    main()
