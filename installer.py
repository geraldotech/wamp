from __future__ import annotations

import argparse
import datetime as dt
import locale
import os
import queue
import shlex
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk


if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT_DIR / "logs"
COMMAND_ENCODING = locale.getpreferredencoding(False) or "mbcs"
APACHE_SERVICE_NAME = "Apache2.4"
MYSQL_SERVICE_NAME = "MySQL"
SCHEDULER_SERVICE_NAME = "Agendador_SmartTasker"
WORKBENCH_COMPONENT_NAME = "MySQLWorkbench"
MYSQL_SERVICE_CANDIDATES = [
    "MySQL",
    "MySQL80",
    "MySQL57",
    "MySQL56",
]

SUBPROCESS_KWARGS: dict[str, object] = {}
if os.name == "nt":
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    SUBPROCESS_KWARGS = {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }

INSTALL_COMPONENT_DEFINITIONS = [
    ("vcredist", "Microsoft Visual C++ 2015-2022 Redistributable (x64) 14.44.35211"),
    ("apache", "Apache 2.4.68"),
    ("mysql", "MySQL server  8.4.10"),
    ("agendador", "Agendador"),
    ("workbench", "MySQL Workbench 8.0.43"),
]

class InstallError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class Logger:
    def __init__(self, path: Path, callback=None) -> None:
        self.path = path
        self.callback = callback
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a", encoding="utf-8", newline="\n")

    def write(self, message: str = "") -> None:
        print(message)
        self.handle.write(message + "\n")
        self.handle.flush()
        if self.callback:
            self.callback(message)

    def close(self) -> None:
        self.handle.close()


def run_command(
    logger: Logger,
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    logger.write(f"$ {' '.join(args)}")
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        encoding=COMMAND_ENCODING,
        errors="replace",
        shell=False,
        **SUBPROCESS_KWARGS,
    )
    if completed.stdout:
        logger.write(completed.stdout.rstrip())
    if completed.stderr:
        logger.write(completed.stderr.rstrip())
    if check and completed.returncode != 0:
        raise InstallError(
            f"Command failed with exit code {completed.returncode}: {' '.join(args)}",
            completed.returncode,
        )
    return completed


def write_failure_log(step_name: str, detail: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = step_name.lower().replace(" ", "_")
    path = LOG_DIR / f"{safe_name}_error.log"
    path.write_text(detail + "\n", encoding="utf-8", newline="\n")


def normalize_slashes(path: Path) -> str:
    return str(path).replace("\\", "/")


def detect_apache_dir() -> Path:
    local = ROOT_DIR / "Apache24"
    if (local / "bin" / "httpd.exe").exists():
        return local.resolve()

    candidates = [
        Path(r"C:\Apache24"),
        Path(os.environ.get("ProgramFiles", "")) / "Apache24",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Apache24",
    ]
    for candidate in candidates:
        if (candidate / "bin" / "httpd.exe").exists():
            return candidate.resolve()

    raise InstallError("Could not find Apache24/bin/httpd.exe.")


def service_exists(service_name: str) -> bool:
    completed = subprocess.run(
        ["sc", "query", service_name],
        text=True,
        capture_output=True,
        encoding=COMMAND_ENCODING,
        errors="replace",
        shell=False,
        **SUBPROCESS_KWARGS,
    )
    combined = f"{completed.stdout}\n{completed.stderr}".lower()
    return completed.returncode == 0 and "does not exist" not in combined and "1060" not in combined


def service_installed(service_name: str) -> bool:
    return service_exists(service_name)


def command_exists(path: Path) -> bool:
    return path.exists()


def detect_service_state(service_name: str) -> str | None:
    completed = subprocess.run(
        ["sc", "query", service_name],
        text=True,
        capture_output=True,
        encoding=COMMAND_ENCODING,
        errors="replace",
        shell=False,
        **SUBPROCESS_KWARGS,
    )
    if completed.returncode != 0:
        return None

    output = f"{completed.stdout}\n{completed.stderr}".upper()
    if "RUNNING" in output:
        return "running"
    if "STOPPED" in output:
        return "stopped"
    return None


def detect_mysql_service_name() -> str | None:
    for candidate in MYSQL_SERVICE_CANDIDATES:
        if service_exists(candidate):
            return candidate
    return None


def mysql_service_installed() -> bool:
    return detect_mysql_service_name() is not None


def registry_value_exists(key: str, value_name: str, expected_fragment: str | None = None) -> bool:
    completed = subprocess.run(
        ["reg", "query", key, "/v", value_name],
        text=True,
        capture_output=True,
        encoding=COMMAND_ENCODING,
        errors="replace",
        shell=False,
        **SUBPROCESS_KWARGS,
    )
    if completed.returncode != 0:
        return False
    output = f"{completed.stdout}\n{completed.stderr}"
    if expected_fragment is None:
        return True
    return expected_fragment.lower() in output.lower()


def get_registry_value(key: str, value_name: str) -> str | None:
    completed = subprocess.run(
        ["reg", "query", key, "/v", value_name],
        text=True,
        capture_output=True,
        encoding=COMMAND_ENCODING,
        errors="replace",
        shell=False,
        **SUBPROCESS_KWARGS,
    )
    if completed.returncode != 0:
        return None

    for line in completed.stdout.splitlines():
        if value_name.lower() not in line.lower():
            continue
        parts = line.split(None, 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return None


def vcredist_installed() -> bool:
    key = r"HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    return registry_value_exists(key, "Installed", "0x1")


def workbench_installed() -> bool:
    candidates = [
        Path(r"C:\Program Files\MySQL\MySQL Workbench 8.0 CE\MySQLWorkbench.exe"),
        Path(r"C:\Program Files (x86)\MySQL\MySQL Workbench 8.0 CE\MySQLWorkbench.exe"),
    ]
    return any(command_exists(candidate) for candidate in candidates)


def detect_workbench_uninstall_command() -> str | None:
    uninstall_roots = [
        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for root in uninstall_roots:
        list_result = subprocess.run(
            ["reg", "query", root, "/s", "/f", "MySQL Workbench", "/d"],
            text=True,
            capture_output=True,
            encoding=COMMAND_ENCODING,
            errors="replace",
            shell=False,
            **SUBPROCESS_KWARGS,
        )
        if list_result.returncode != 0:
            continue

        current_key: str | None = None
        for raw_line in list_result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("HKEY_"):
                current_key = line
                continue
            if current_key and "DisplayName" in line and "MySQL Workbench" in line:
                uninstall_string = get_registry_value(current_key, "UninstallString")
                if uninstall_string:
                    return uninstall_string
    return None


def component_installed_map() -> dict[str, bool]:
    return {
        "vcredist": vcredist_installed(),
        "apache": service_installed(APACHE_SERVICE_NAME),
        "mysql": mysql_service_installed(),
        "agendador": service_installed(SCHEDULER_SERVICE_NAME),
        "workbench": workbench_installed(),
    }


def create_logger(command_name: str, callback=None) -> Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_name = {
        "install": f"instalacao_{timestamp}.log",
        "uninstall": f"uninstall_{timestamp}.log",
        "fix-mysql-dir": f"fix_mysql_dir_{timestamp}.log",
        "fix-php-dir": f"fix_php_dir_{timestamp}.log",
        "gui": f"gui_{timestamp}.log",
    }[command_name]
    return Logger(LOG_DIR / log_name, callback=callback)


def read_config_value(path: Path, prefixes: list[str]) -> str | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        lower = line.lower()
        for prefix in prefixes:
            if lower.startswith(prefix.lower()):
                _, _, value = line.partition("=")
                return value.strip()
    return None


def detect_apache_port() -> str | None:
    conf_path = ROOT_DIR / "Apache24" / "conf" / "httpd.conf"
    if not conf_path.exists():
        return None
    for raw_line in conf_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.lower().startswith("listen "):
            return line.split(None, 1)[1].strip()
    return None


def detect_mysql_port() -> str | None:
    return read_config_value(ROOT_DIR / "mysql" / "my.ini", ["port"])


def detect_agendador_port() -> str | None:
    return read_config_value(ROOT_DIR / "agendador" / "conf.ini", ["api_port"])


def service_port_map() -> dict[str, str | None]:
    return {
        "apache": detect_apache_port(),
        "mysql": detect_mysql_port(),
        "agendador": detect_agendador_port(),
    }


def fix_mysql_dir(logger: Logger) -> None:
    mysql_dir = (ROOT_DIR / "mysql").resolve()
    my_ini = mysql_dir / "my.ini"
    if not my_ini.exists():
        raise InstallError(f"Could not find {my_ini}")

    base_dir = normalize_slashes(mysql_dir)
    data_dir = f"{base_dir}/data"

    logger.write(f"MySQL dir : {mysql_dir}")
    logger.write(f"my.ini    : {my_ini}")
    logger.write(f"basedir   : {base_dir}")
    logger.write(f"datadir   : {data_dir}")

    lines = my_ini.read_text(encoding="utf-8", errors="replace").splitlines()
    output: list[str] = []
    base_found = False
    data_found = False

    for line in lines:
        stripped = line.strip().lower()
        if stripped.startswith("basedir=") or stripped.startswith("basedir ="):
            output.append(f"basedir={base_dir}")
            base_found = True
            continue
        if stripped.startswith("datadir=") or stripped.startswith("datadir ="):
            output.append(f"datadir={data_dir}")
            data_found = True
            continue
        output.append(line)

    if not base_found:
        output.extend(["", f"basedir={base_dir}"])
    if not data_found:
        output.extend(["", f"datadir={data_dir}"])

    my_ini.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")


def fix_php_dir(logger: Logger) -> None:
    apache_dir = detect_apache_dir()
    base_dir = apache_dir.parent.resolve()
    php_dir = (base_dir / "php").resolve()
    php_ini = php_dir / "php.ini"
    if not php_ini.exists():
        raise InstallError(f"Could not find {php_ini}")

    ext_dir = f'{normalize_slashes(php_dir)}/ext'
    logger.write(f"PHP dir   : {php_dir}")
    logger.write(f"php.ini   : {php_ini}")
    logger.write(f"ext_dir   : {ext_dir}")

    lines = php_ini.read_text(encoding="utf-8", errors="replace").splitlines()
    output: list[str] = []
    updated = False

    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("extension_dir"):
            output.append(f'extension_dir = "{ext_dir}"')
            updated = True
            continue

        if stripped.startswith("; On windows:") and "extension_dir" in stripped:
            output.append("; On windows:")
            output.append(f'extension_dir = "{ext_dir}"')
            updated = True
            continue

        if '; On windows:extension_dir = "' in line:
            output.append("; On windows:")
            output.append(f'extension_dir = "{ext_dir}"')
            updated = True
            continue

        output.append(line)

    if not updated:
        output.extend(["", f'extension_dir = "{ext_dir}"'])

    php_ini.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    run_command(logger, ["net", "stop", APACHE_SERVICE_NAME], check=False)
    run_command(logger, ["net", "start", APACHE_SERVICE_NAME])


def install_vcredist(logger: Logger) -> None:
    installer = ROOT_DIR / "utils" / "VC_redist.x64.exe"
    if not installer.exists():
        raise InstallError(f"Could not find {installer}")
    log_file = ROOT_DIR / "utils" / "vcredist_install.log"
    completed = run_command(
        logger,
        [
            str(installer),
            "/install",
            "/quiet",
            "/norestart",
            "/log",
            str(log_file),
        ],
        check=False,
    )
    if completed.returncode == 0:
        return
    if completed.returncode == 1638:
        logger.write("VC++ Redistributable already installed or blocked by an existing newer version. Continuing.")
        return
    raise InstallError(
        f"Command failed with exit code {completed.returncode}: {' '.join([str(installer), '/install', '/quiet', '/norestart', '/log', str(log_file)])}",
        completed.returncode,
    )


def install_apache(logger: Logger, service_name: str = APACHE_SERVICE_NAME) -> None:
    apache_dir = detect_apache_dir()
    conf_file = apache_dir / "conf" / "httpd.conf"
    if not conf_file.exists():
        raise InstallError(f"Could not find {conf_file}")

    base_dir = apache_dir.parent.resolve()
    php_dir = (base_dir / "php").resolve()
    if not php_dir.exists():
        raise InstallError(f"Could not find {php_dir}")

    apache_dir_fwd = normalize_slashes(apache_dir)
    php_dir_fwd = normalize_slashes(php_dir)
    httpd_exe = apache_dir / "bin" / "httpd.exe"

    logger.write(f"Service  : {service_name}")
    logger.write(f"Apache   : {apache_dir}")
    logger.write(f"Config   : {conf_file}")
    logger.write(f"SRVROOT  : {apache_dir_fwd}")
    logger.write(f"PHPDIR   : {php_dir_fwd}")

    run_command(
        logger,
        [
            str(httpd_exe),
            "-t",
            "-d",
            str(apache_dir),
            "-f",
            str(conf_file),
            "-C",
            f'Define SRVROOT "{apache_dir_fwd}"',
            "-C",
            f'Define PHPDIR "{php_dir_fwd}"',
        ],
        cwd=apache_dir / "bin",
    )

    run_command(
        logger,
        [
            str(httpd_exe),
            "-k",
            "install",
            "-n",
            service_name,
            "-d",
            str(apache_dir),
            "-f",
            str(conf_file),
            "-C",
            f'Define SRVROOT "{apache_dir_fwd}"',
            "-C",
            f'Define PHPDIR "{php_dir_fwd}"',
        ],
        cwd=apache_dir / "bin",
    )

    run_command(logger, ["net", "stop", service_name], check=False)
    run_command(logger, ["net", "start", service_name])


def install_mysql(logger: Logger) -> None:
    mysql_dir = (ROOT_DIR / "mysql").resolve()
    bin_dir = mysql_dir / "bin"
    my_ini = mysql_dir / "my.ini"
    err_log = mysql_dir / "init.err"
    mysqld_exe = bin_dir / "mysqld.exe"
    mysql_exe = bin_dir / "mysql.exe"

    for required in (mysqld_exe, mysql_exe, my_ini):
        if not required.exists():
            raise InstallError(f"Could not find {required}")

    # Always realign my.ini to the current bundle path before initialization.
    fix_mysql_dir(logger)

    data_dir = mysql_dir / "data"
    if data_dir.exists():
        backup_dir = mysql_dir / f"data_old_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        logger.write(f"Backing up existing data dir to: {backup_dir.name}")
        if backup_dir.exists():
            raise InstallError(f"Backup dir already exists: {backup_dir}")
        data_dir.rename(backup_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    init_result = run_command(
        logger,
        [
            str(mysqld_exe),
            f"--defaults-file={my_ini}",
            "--initialize-insecure",
            "--console",
            f"--log-error={err_log}",
        ],
        cwd=bin_dir,
        check=False,
    )
    if init_result.returncode != 0:
        err_detail = ""
        if err_log.exists():
            err_tail = "\n".join(err_log.read_text(encoding="utf-8", errors="replace").splitlines()[-10:])
            if err_tail:
                err_detail = f"\nMySQL init.err:\n{err_tail}"
        raise InstallError(
            f"MySQL initialization failed with exit code {init_result.returncode}.{err_detail}",
            init_result.returncode,
        )

    run_command(
        logger,
        [
            str(mysqld_exe),
            "--install",
            MYSQL_SERVICE_NAME,
            f"--defaults-file={my_ini}",
        ],
        cwd=bin_dir,
    )

    run_command(logger, ["net", "start", MYSQL_SERVICE_NAME])
    run_command(
        logger,
        [
            str(mysql_exe),
            "-u",
            "root",
            "-e",
            "ALTER USER 'root'@'localhost' IDENTIFIED BY 'root'; FLUSH PRIVILEGES;",
        ],
        cwd=bin_dir,
    )


def install_agendador(logger: Logger) -> None:
    agendador_dir = (ROOT_DIR / "agendador").resolve()
    nssm = agendador_dir / "nssm.exe"
    app = agendador_dir / "mainapp.exe"
    for required in (nssm, app):
        if not required.exists():
            raise InstallError(f"Could not find {required}")

    run_command(
        logger,
        [str(nssm), "install", SCHEDULER_SERVICE_NAME, str(app)],
        cwd=agendador_dir,
    )
    run_command(
        logger,
        [str(nssm), "set", SCHEDULER_SERVICE_NAME, "DisplayName", SCHEDULER_SERVICE_NAME],
        cwd=agendador_dir,
    )
    run_command(
        logger,
        [
            str(nssm),
            "set",
            SCHEDULER_SERVICE_NAME,
            "Description",
            "Agendador HTTP GET base em um arquivo config.json - https://github.com/geraldotech/AgendadorGET porta 5050 by default",
        ],
        cwd=agendador_dir,
    )
    run_command(
        logger,
        [str(nssm), "set", SCHEDULER_SERVICE_NAME, "AppDirectory", str(agendador_dir)],
        cwd=agendador_dir,
    )
    run_command(
        logger,
        [str(nssm), "start", SCHEDULER_SERVICE_NAME],
        cwd=agendador_dir,
    )


def install_workbench(logger: Logger) -> None:
    msi = ROOT_DIR / "utils" / "mysql-workbench-community-8.0.43-winx64.msi"
    if not msi.exists():
        raise InstallError(f"Could not find {msi}")
    log_file = ROOT_DIR / "utils" / "mysql-workbench-install.log"
    run_command(
        logger,
        [
            "msiexec",
            "/i",
            str(msi),
            "ALLUSERS=1",
            "/qn",
            "/norestart",
            "/L*v",
            str(log_file),
        ],
    )


def remove_apache_service(logger: Logger) -> None:
    run_command(logger, ["sc", "stop", APACHE_SERVICE_NAME], check=False)
    run_command(logger, ["sc", "delete", APACHE_SERVICE_NAME], check=False)


def remove_mysql_service(logger: Logger, service_name: str | None = None) -> None:
    target_service_name = service_name or detect_mysql_service_name() or MYSQL_SERVICE_NAME
    run_command(logger, ["sc", "stop", target_service_name], check=False)
    run_command(logger, ["sc", "delete", target_service_name], check=False)


def remove_agendador_service(logger: Logger) -> None:
    agendador_dir = (ROOT_DIR / "agendador").resolve()
    nssm = agendador_dir / "nssm.exe"
    if not nssm.exists():
        raise InstallError(f"Could not find {nssm}")
    run_command(logger, [str(nssm), "stop", SCHEDULER_SERVICE_NAME], check=False, cwd=agendador_dir)
    run_command(logger, [str(nssm), "remove", SCHEDULER_SERVICE_NAME, "confirm"], check=False, cwd=agendador_dir)


def remove_workbench(logger: Logger) -> None:
    uninstall_command = detect_workbench_uninstall_command()
    if not uninstall_command:
        raise InstallError("Could not find the MySQL Workbench uninstall command in Windows registry.")

    if uninstall_command.lower().startswith("msiexec"):
        args = shlex.split(uninstall_command, posix=False)
        normalized_args: list[str] = []
        for arg in args:
            lowered = arg.lower()
            if lowered == "/i":
                normalized_args.append("/x")
                continue
            if lowered.startswith("/i{"):
                normalized_args.append("/x")
                normalized_args.append(arg[2:])
                continue
            normalized_args.append(arg)
        args = normalized_args
        if all(arg.lower() != "/qn" for arg in args):
            args.append("/qn")
        if all(arg.lower() != "/norestart" for arg in args):
            args.append("/norestart")
        run_command(logger, args)
        return

    run_command(logger, ["cmd", "/c", uninstall_command])


def start_service(logger: Logger, service_name: str) -> None:
    run_command(logger, ["net", "start", service_name])


def stop_service(logger: Logger, service_name: str) -> None:
    run_command(logger, ["net", "stop", service_name], check=False)


def uninstall_services(logger: Logger) -> None:
    remove_apache_service(logger)
    remove_mysql_service(logger)
    if (ROOT_DIR / "agendador" / "nssm.exe").exists():
        remove_agendador_service(logger)
    else:
        logger.write("Skipping agendador removal because NSSM was not found.")
    if workbench_installed():
        remove_workbench(logger)


def run_step(logger: Logger, name: str, action) -> None:
    logger.write("")
    logger.write("=" * 60)
    logger.write(f"Running step: {name}")
    logger.write("=" * 60)
    try:
        action()
    except Exception as exc:
        detail = f"{name} failed: {exc}"
        write_failure_log(name, detail)
        raise InstallError(detail, getattr(exc, "exit_code", 1)) from exc


def step_actions() -> dict[str, tuple[str, callable]]:
    return {
        "vcredist": ("Microsoft Visual C++ 2015-2022 Redistributable (x64) 14.44.35211", install_vcredist),
        "apache": ("Apache", install_apache),
        "mysql": ("MySQL Server", install_mysql),
        "agendador": ("Agendador", install_agendador),
        "workbench": ("WorkBench", install_workbench),
    }


def prepare_install_steps(selected_steps: list[str]) -> tuple[list[str], list[str]]:
    ordered_steps = list(dict.fromkeys(selected_steps))
    if "apache" in ordered_steps and "vcredist" not in ordered_steps and not vcredist_installed():
        apache_index = ordered_steps.index("apache")
        ordered_steps.insert(apache_index, "vcredist")

    final_steps: list[str] = []
    if "mysql" in ordered_steps:
        final_steps.append("fix-mysql-dir")
    if "apache" in ordered_steps:
        final_steps.append("fix-php-dir")

    return ordered_steps, final_steps


def run_selected_install_steps(logger: Logger, selected_steps: list[str], progress_callback=None) -> int:
    actions = step_actions()
    if not selected_steps:
        raise InstallError("No installation step was selected.")

    execution_steps, final_steps = prepare_install_steps(selected_steps)
    logger.write(f"Selected steps: {', '.join(selected_steps)}")
    if execution_steps != selected_steps:
        logger.write(f"Adjusted execution order: {', '.join(execution_steps)}")
    if final_steps:
        logger.write(f"Final fix steps: {', '.join(final_steps)}")

    total_steps = len(execution_steps) + len(final_steps)
    if progress_callback:
        progress_callback(0, f"0% - 0/{total_steps} etapas concluidas")

    completed_steps = 0

    for step_key in execution_steps:
        if step_key not in actions:
            raise InstallError(f"Unknown installation step: {step_key}")
        step_name, action = actions[step_key]
        if progress_callback:
            started_progress = int((completed_steps / total_steps) * 100)
            progress_callback(started_progress, f"{started_progress}% - instalando: {step_name}")
        run_step(logger, step_name, lambda action=action: action(logger))
        completed_steps += 1
        if progress_callback:
            completed_progress = int((completed_steps / total_steps) * 100)
            progress_callback(
                completed_progress,
                f"{completed_progress}% - {completed_steps}/{total_steps} etapas concluidas",
            )

    for step_key in final_steps:
        if step_key == "fix-mysql-dir":
            step_name = "Corrigir MySQL path"
            action = fix_mysql_dir
        elif step_key == "fix-php-dir":
            step_name = "Corrigir PHP path"
            action = fix_php_dir
        else:
            raise InstallError(f"Unknown final installation step: {step_key}")

        if progress_callback:
            started_progress = int((completed_steps / total_steps) * 100)
            progress_callback(started_progress, f"{started_progress}% - finalizando: {step_name}")
        run_step(logger, step_name, lambda action=action: action(logger))
        completed_steps += 1
        if progress_callback:
            completed_progress = int((completed_steps / total_steps) * 100)
            progress_callback(
                completed_progress,
                f"{completed_progress}% - {completed_steps}/{total_steps} etapas concluidas",
            )

    logger.write("")
    logger.write("Selected steps completed successfully.")
    return 0


def command_install(logger: Logger) -> int:
    default_steps = [step_key for step_key, _ in INSTALL_COMPONENT_DEFINITIONS]
    return run_selected_install_steps(logger, default_steps)


def command_uninstall(logger: Logger) -> int:
    uninstall_services(logger)
    logger.write("Services removed if they existed.")
    return 0


class InstallerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self._icon_image = self._create_tools_icon()
        self.root.iconphoto(True, self._icon_image)
        self.root.title("WAMP Installer")
        self.root.geometry("920x700")
        self.root.minsize(840, 620)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.busy = False
        self.install_vars: dict[str, tk.BooleanVar] = {}
        self.install_checkbuttons: dict[str, ttk.Checkbutton] = {}
        self.install_status_labels: dict[str, ttk.Label] = {}
        self.install_port_labels: dict[str, ttk.Label] = {}
        self.install_toggle_buttons: dict[str, ttk.Button] = {}
        self.install_remove_buttons: dict[str, ttk.Button] = {}
        self.install_fix_buttons: dict[str, ttk.Button] = {}
        self.mysql_service_runtime_name: str | None = None
        self.install_button: ttk.Button | None = None
        self.remove_all_button: ttk.Button | None = None
        self.refresh_button: ttk.Button | None = None
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_label_var = tk.StringVar(value="0% - aguardando")
        self.progress_bar: ttk.Progressbar | None = None

        self._build_ui()
        self.refresh_status()
        self.root.after(150, self._poll_logs)

    def _create_tools_icon(self) -> tk.PhotoImage:
        icon = tk.PhotoImage(width=32, height=32)
        icon.put("#000000", to=(0, 0, 32, 32))

        # Crossed tools silhouette.
        steel = "#c9d1d9"
        dark = "#6e7681"
        accent = "#f0b429"

        for x, y in [
            (8, 22), (9, 21), (10, 20), (11, 19), (12, 18), (13, 17), (14, 16),
            (15, 15), (16, 14), (17, 13), (18, 12), (19, 11), (20, 10), (21, 9),
        ]:
            icon.put(steel, (x, y))
            icon.put(steel, (x + 1, y))

        for x, y in [
            (9, 8), (10, 9), (11, 10), (12, 11), (13, 12), (14, 13), (15, 14),
            (16, 15), (17, 16), (18, 17), (19, 18), (20, 19), (21, 20), (22, 21),
        ]:
            icon.put(accent, (x, y))
            icon.put(accent, (x, y + 1))

        for px in [(6, 23), (7, 22), (8, 21), (22, 8), (23, 7), (24, 6)]:
            icon.put(dark, px)

        for px in [
            (6, 24), (7, 24), (8, 24), (23, 6), (24, 7), (24, 8),
            (7, 7), (8, 6), (9, 6), (22, 24), (23, 24), (24, 23),
        ]:
            icon.put(steel, px)

        return icon

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        title = ttk.Label(
            frame,
            text="WAMP Installer",
            font=("Segoe UI", 16, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        subtitle = ttk.Label(
            frame,
            text="Windows, Apache, MySQL, PHP, Workbench and Agendador",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(4, 14))

        install_options = ttk.LabelFrame(frame, text="Instalacao", padding=12)
        install_options.grid(row=2, column=0, sticky="ew")
        for column in range(6):
            install_options.columnconfigure(column, weight=1 if column == 0 else 0)

        defaults = self._default_step_selection()
        installed_components = component_installed_map()
        for index, (step_key, label) in enumerate(INSTALL_COMPONENT_DEFINITIONS):
            var = tk.BooleanVar(value=defaults.get(step_key, True))
            var.trace_add("write", self._on_selection_changed)
            self.install_vars[step_key] = var
            self._add_install_component_row(
                install_options,
                row=index,
                step_key=step_key,
                label=label,
                variable=var,
            )
            if installed_components.get(step_key):
                var.set(False)
                self.install_checkbuttons[step_key].config(state="disabled")

        actions = ttk.Frame(frame, padding=(0, 14, 0, 0))
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        buttons = ttk.Frame(actions)
        buttons.grid(row=0, column=0, sticky="w")

        self.install_button = ttk.Button(
            buttons,
            text="Instalar Selecionados",
            command=self.install_selected,
        )
        self.install_button.grid(row=0, column=0, padx=(0, 8))

        self.remove_all_button = ttk.Button(
            buttons,
            text="Remover Todos os Servicos",
            command=self.remove_all_services,
        )
        self.remove_all_button.grid(row=0, column=1, padx=(0, 8))

        self.refresh_button = ttk.Button(
            buttons,
            text="Atualizar Status",
            command=self.refresh_status,
        )
        self.refresh_button.grid(row=0, column=2)

        progress_frame = ttk.Frame(frame, padding=(0, 10, 0, 0))
        progress_frame.grid(row=4, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)

        self.progress_bar = ttk.Progressbar(
            progress_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
        )
        self.progress_bar.grid(row=0, column=0, sticky="ew")

        progress_label = ttk.Label(progress_frame, textvariable=self.progress_label_var)
        progress_label.grid(row=1, column=0, sticky="w", pady=(4, 0))

        log_frame = ttk.LabelFrame(frame, text="Log", padding=12)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(14, 0))
        frame.rowconfigure(5, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=18, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(frame, padding=(0, 10, 0, 0))
        footer.grid(row=6, column=0, sticky="e")

        about_button = ttk.Button(footer, text="About", command=self.show_about_dialog)
        about_button.grid(row=0, column=0, sticky="e")
        self._update_install_button_state()

    def _add_install_component_row(
        self,
        parent,
        *,
        row: int,
        step_key: str,
        label: str,
        variable: tk.BooleanVar,
    ) -> None:
        checkbox = ttk.Checkbutton(parent, text=label, variable=variable)
        checkbox.grid(row=row, column=0, sticky="w", pady=6)
        self.install_checkbuttons[step_key] = checkbox

        status_label = ttk.Label(parent, text="Verificando...")
        status_label.grid(row=row, column=1, sticky="w", padx=(12, 12))
        self.install_status_labels[step_key] = status_label

        port_label = ttk.Label(parent, text="-")
        port_label.grid(row=row, column=2, sticky="w", padx=(0, 12))
        self.install_port_labels[step_key] = port_label

        toggle_button = ttk.Button(
            parent,
            text="Iniciar",
            command=lambda key=step_key, text=label: self.run_component_toggle_action(key, text),
        )
        toggle_button.grid(row=row, column=3, sticky="e", padx=(0, 8))
        self.install_toggle_buttons[step_key] = toggle_button

        remove_button = ttk.Button(
            parent,
            text="Remover",
            command=lambda key=step_key, text=label: self.run_component_remove_action(key, text),
        )
        remove_button.grid(row=row, column=4, sticky="e", padx=(0, 8))
        self.install_remove_buttons[step_key] = remove_button

        fix_button = ttk.Button(parent)
        if step_key == "apache":
            fix_button.config(
                text="Corrigir PHP path",
                command=lambda: self.run_service_action(
                    "fix-apache",
                    "Corrigir PHP path",
                    lambda logger: fix_php_dir(logger),
                ),
            )
            fix_button.grid(row=row, column=5, sticky="e")
            self.install_fix_buttons[step_key] = fix_button
        elif step_key == "mysql":
            fix_button = ttk.Button(
                parent,
                text="Corrigir MySQL path",
                command=lambda: self.run_service_action(
                    "fix-mysql",
                    "Corrigir MySQL path",
                    lambda logger: fix_mysql_dir(logger),
                ),
            )
            fix_button.grid(row=row, column=5, sticky="e")
            self.install_fix_buttons[step_key] = fix_button

    def run_component_toggle_action(self, step_key: str, label: str) -> None:
        service_name = self._get_runtime_service_name(step_key)
        if not service_name:
            return
        service_state = detect_service_state(service_name)
        if service_state == "running":
            self.run_service_action(
                f"stop-{step_key}",
                f"Parar {label}",
                lambda logger, svc=service_name: stop_service(logger, svc),
            )
            return
        self.run_service_action(
            f"start-{step_key}",
            f"Iniciar {label}",
            lambda logger, svc=service_name: start_service(logger, svc),
        )

    def run_component_remove_action(self, step_key: str, label: str) -> None:
        remove_actions = {
            "apache": lambda logger: remove_apache_service(logger),
            "mysql": lambda logger: remove_mysql_service(logger, self.mysql_service_runtime_name),
            "agendador": lambda logger: remove_agendador_service(logger),
            "workbench": lambda logger: remove_workbench(logger),
        }
        action = remove_actions.get(step_key)
        if action is None:
            return
        self.run_service_action(f"remove-{step_key}", f"Remocao de {label}", action)

    def _get_runtime_service_name(self, step_key: str) -> str | None:
        if step_key == "apache":
            return APACHE_SERVICE_NAME
        if step_key == "mysql":
            return self.mysql_service_runtime_name or detect_mysql_service_name()
        if step_key == "agendador":
            return SCHEDULER_SERVICE_NAME
        return None

    def _default_step_selection(self) -> dict[str, bool]:
        installed_components = component_installed_map()
        return {
            "vcredist": not installed_components["vcredist"],
            "apache": not installed_components["apache"],
            "mysql": not installed_components["mysql"],
            "agendador": not installed_components["agendador"],
            "workbench": not installed_components["workbench"],
        }

    def _has_any_selection(self) -> bool:
        return any(var.get() for var in self.install_vars.values())

    def _update_install_button_state(self) -> None:
        if self.install_button is None:
            return
        if self.busy:
            self.install_button.config(state="disabled")
            return
        self.install_button.config(state="normal" if self._has_any_selection() else "disabled")

    def _on_selection_changed(self, *_args) -> None:
        self._update_install_button_state()

    def _set_progress(self, value: int, message: str) -> None:
        self.progress_var.set(max(0, min(100, value)))
        self.progress_label_var.set(message)

    def show_about_dialog(self) -> None:
        about = tk.Toplevel(self.root)
        about.title("About")
        about.resizable(False, False)
        about.transient(self.root)
        about.grab_set()

        container = ttk.Frame(about, padding=16)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text="By Geraldo Dev",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")

        ttk.Label(
            container,
            text="GitHub:",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))

        link = tk.Label(
            container,
            text="https://github.com/geraldotech",
            fg="blue",
            cursor="hand2",
        )
        link.grid(row=2, column=0, sticky="w")
        link.bind("<Button-1>", lambda _event: webbrowser.open("https://github.com/geraldotech"))

        ttk.Label(
            container,
            text="Copyright 2026",
            foreground="#6e7681",
        ).grid(row=3, column=0, sticky="w", pady=(14, 0))

        ttk.Button(container, text="Fechar", command=about.destroy).grid(
            row=4,
            column=0,
            sticky="e",
            pady=(14, 0),
        )

        about.update_idletasks()
        parent_x = self.root.winfo_rootx()
        parent_y = self.root.winfo_rooty()
        parent_width = self.root.winfo_width()
        parent_height = self.root.winfo_height()
        dialog_width = about.winfo_width()
        dialog_height = about.winfo_height()
        x = parent_x + max((parent_width - dialog_width) // 2, 0)
        y = parent_y + max((parent_height - dialog_height) // 2, 0)
        about.geometry(f"+{x}+{y}")

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        action_state = "disabled" if busy else "normal"
        self.remove_all_button.config(state=action_state)
        self.refresh_button.config(state=action_state)
        for checkbox in self.install_checkbuttons.values():
            checkbox.config(state="disabled" if busy else checkbox.cget("state"))
        for button in self.install_toggle_buttons.values():
            if busy:
                button.config(state="disabled")
        for button in self.install_remove_buttons.values():
            if busy:
                button.config(state="disabled")
        for button in self.install_fix_buttons.values():
            if busy:
                button.config(state="disabled")
        self._update_install_button_state()

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_logs(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(message)
        self.root.after(150, self._poll_logs)

    def run_background(self, command_name: str, worker, success_message: str) -> None:
        if self.busy:
            messagebox.showwarning("Em execucao", "Ja existe uma operacao em andamento.")
            return

        self.set_busy(True)
        self.append_log("")
        self.append_log(f"[START] {success_message}")

        def callback(message: str) -> None:
            self.log_queue.put(message)

        def progress_callback(value: int, message: str) -> None:
            self.root.after(0, lambda v=value, m=message: self._set_progress(v, m))

        def target() -> None:
            logger = create_logger("gui", callback=callback)
            try:
                worker(logger, progress_callback)
                self.log_queue.put(f"[DONE] {success_message}")
                self.root.after(
                    0,
                    lambda message=success_message: messagebox.showinfo("Concluido", message),
                )
            except InstallError as exc:
                logger.write("")
                logger.write(str(exc))
                logger.write(f"See logs in: {LOG_DIR}")
                error_message = str(exc)
                self.root.after(
                    0,
                    lambda message=error_message: messagebox.showerror("Erro", message),
                )
            finally:
                logger.close()
                self.root.after(0, self._finish_background)

        threading.Thread(target=target, daemon=True).start()

    def _finish_background(self) -> None:
        self.set_busy(False)
        self.refresh_status()

    def refresh_status(self) -> None:
        self.mysql_service_runtime_name = detect_mysql_service_name()
        installed_components = component_installed_map()
        ports = service_port_map()
        for step_key, checkbox in self.install_checkbuttons.items():
            installed = installed_components.get(step_key, False)
            self.install_vars[step_key].set(False if installed else self.install_vars[step_key].get())
            checkbox.config(state="disabled" if installed or self.busy else "normal")
            runtime_service_name = self._get_runtime_service_name(step_key)
            service_state = detect_service_state(runtime_service_name) if runtime_service_name else None
            if step_key == "mysql" and installed and self.mysql_service_runtime_name:
                if service_state == "running":
                    status_text = f"Iniciado ({self.mysql_service_runtime_name})"
                elif service_state == "stopped":
                    status_text = f"Parado ({self.mysql_service_runtime_name})"
                else:
                    status_text = f"Instalado ({self.mysql_service_runtime_name})"
            elif step_key in {"apache", "agendador"} and installed:
                if service_state == "running":
                    status_text = "Iniciado"
                elif service_state == "stopped":
                    status_text = "Parado"
                else:
                    status_text = "Instalado"
            else:
                status_text = "Instalado" if installed else "Nao instalado"
            self.install_status_labels[step_key].config(text=status_text)
            port_value = ports.get(step_key)
            self.install_port_labels[step_key].config(
                text=f"Porta {port_value}" if port_value and step_key in ports else "-"
            )
            toggle_button = self.install_toggle_buttons.get(step_key)
            if toggle_button:
                can_toggle = installed and step_key in {"apache", "mysql", "agendador"} and not self.busy
                if service_state == "running":
                    toggle_button.config(text="Parar", state="normal" if can_toggle else "disabled")
                else:
                    toggle_button.config(text="Iniciar", state="normal" if can_toggle else "disabled")
            remove_button = self.install_remove_buttons.get(step_key)
            if remove_button:
                can_remove = installed and step_key in {"apache", "mysql", "agendador", "workbench"} and not self.busy
                remove_button.config(state="normal" if can_remove else "disabled")
            fix_button = self.install_fix_buttons.get(step_key)
            if fix_button:
                fix_button.config(state="normal" if installed and not self.busy else "disabled")

    def install_selected(self) -> None:
        selected = [step for step, var in self.install_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("Nada selecionado", "Selecione ao menos uma etapa para instalar.")
            return

        self._set_progress(0, "0% - iniciando")
        self.run_background(
            "install-selected",
            lambda logger, progress_callback: run_selected_install_steps(
                logger,
                selected,
                progress_callback=progress_callback,
            ),
            "Instalacao concluida.",
        )

    def remove_all_services(self) -> None:
        if not messagebox.askyesno("Confirmar", "Deseja remover todos os itens instalados suportados?"):
            return

        self.run_background(
            "remove-all",
            lambda logger, _progress_callback: uninstall_services(logger),
            "Remocao concluida.",
        )

    def run_service_action(self, action_name: str, success_message: str, action) -> None:
        self.run_background(
            action_name,
            lambda logger, _progress_callback: action(logger),
            success_message,
        )

    def run(self) -> int:
        self.root.mainloop()
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Python installer for the local WAMP bundle.")
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser("gui", help="Open the Tkinter installer interface.")
    subparsers.add_parser("install", help="Run the full installation sequence.")
    subparsers.add_parser("uninstall", help="Remove installed Windows services.")
    subparsers.add_parser("fix-mysql-dir", help="Update basedir/datadir in mysql/my.ini.")
    subparsers.add_parser("fix-php-dir", help="Update extension_dir in php/php.ini and restart Apache.")

    parser.set_defaults(command="gui")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "gui":
        app = InstallerApp()
        return app.run()

    logger = create_logger(args.command)
    try:
        if args.command == "install":
            return command_install(logger)
        if args.command == "uninstall":
            return command_uninstall(logger)
        if args.command == "fix-mysql-dir":
            fix_mysql_dir(logger)
            logger.write("fix_mysql_dir completed successfully.")
            return 0
        if args.command == "fix-php-dir":
            fix_php_dir(logger)
            logger.write("fix_php_dir completed successfully.")
            return 0
        raise InstallError(f"Unknown command: {args.command}")
    except InstallError as exc:
        logger.write("")
        logger.write(str(exc))
        logger.write(f"See logs in: {LOG_DIR}")
        return exc.exit_code
    finally:
        logger.close()


if __name__ == "__main__":
    sys.exit(main())
