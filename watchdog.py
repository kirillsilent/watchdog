#!/usr/bin/env python3
import os
import subprocess
import time
from datetime import datetime

# =========================
# НАСТРОЙКИ
# =========================

WG_IF = "wg0"
WG_PING_IP = "8.8.8.8"           # внешний IP для проверки WG
# WG_PING_IP = "172.16.105.2"    # можешь заменить на внутренний IP за WG, если так надежнее

SOWA_SIP_SERVICE = "sowa_sip.service"
SOWA_SIP_PING_IP = "172.16.105.2"

LOGFILE = "/var/log/watchdog.log"
STATE_DIR = "/run/watchdog"

CHECK_INTERVAL = 30              # секунд между итерациями
MAX_RESTARTS = 3                 # максимум попыток за окно RESET_INTERVAL
RESET_INTERVAL = 300             # 5 минут

DISK_WARN = 90
DISK_CRIT = 95
MAX_SYSLOAD = 10

WG_RECOVERY_WAIT = 5             # подождать после wg up
WG_DOWN_UP_SLEEP = 3             # пауза между wg down и wg up


# =========================
# ЛОГИ
# =========================

def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# =========================
# КОМАНДЫ
# =========================

def run(cmd: str) -> bool:
    return subprocess.call(
        cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ) == 0


def get_disk_usage() -> int:
    out = subprocess.check_output(
        "df -h / | awk 'NR==2 {print $5}'",
        shell=True
    )
    return int(out.decode().strip().replace("%", ""))


def get_sysload() -> float:
    with open("/proc/loadavg", encoding="utf-8") as f:
        return float(f.read().split()[0])


# =========================
# ПРОВЕРКИ
# =========================

def check_sowa_sip_journal() -> bool:
    """
    Если последние N строк журнала подряд содержат 'Registration successful',
    считаем, что сервис зациклился.
    """
    n = 5

    try:
        out = subprocess.check_output(
            ["journalctl", "-u", SOWA_SIP_SERVICE, "-n", str(n), "--no-pager"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="ignore").strip().splitlines()

        if len(out) < n:
            return False

        matched = [line for line in out if "Registration successful" in line]
        return len(matched) == n

    except Exception as e:
        log(f"journalctl error: {e}")
        return False


# =========================
# АНТИЦИКЛИЧНОСТЬ
# =========================

def should_restart(name: str) -> bool:
    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, f"{name}.count")

    now = int(time.time())
    count = 0
    lasttime = 0

    if os.path.exists(state_file):
        try:
            with open(state_file, encoding="utf-8") as f:
                parts = f.read().strip().split()
                if len(parts) == 2:
                    count = int(parts[0])
                    lasttime = int(parts[1])
        except Exception:
            count = 0
            lasttime = 0

    if now - lasttime > RESET_INTERVAL:
        count = 0

    count += 1

    with open(state_file, "w", encoding="utf-8") as f:
        f.write(f"{count} {now}")

    if count > MAX_RESTARTS:
        log(f"restart limit exceeded for {name}: {count}")
        return False

    return True


def reset_restart_counter(name: str) -> None:
    state_file = os.path.join(STATE_DIR, f"{name}.count")
    try:
        if os.path.exists(state_file):
            os.remove(state_file)
    except Exception:
        pass


# =========================
# ACTIONS
# =========================

def restart(service: str) -> None:
    log(f"restart {service}")
    run(f"systemctl restart {service}")


def cycle_wg() -> None:
    log(f"wg recovery: wg-quick down {WG_IF}")
    run(f"wg-quick down {WG_IF}")

    time.sleep(WG_DOWN_UP_SLEEP)

    log(f"wg recovery: wg-quick up {WG_IF}")
    run(f"wg-quick up {WG_IF}")

    time.sleep(WG_RECOVERY_WAIT)


def reboot_now(reason: str) -> None:
    log(f"reboot: {reason}")
    run("/sbin/reboot")


# =========================
# MAIN LOGIC
# =========================

def main() -> None:
    healthy = True

    # =========================
    # DISK CHECK
    # =========================
    disk = get_disk_usage()

    if disk > DISK_CRIT:
        log(f"disk critical {disk}%")
        return

    if disk > DISK_WARN:
        log(f"disk warn {disk}% cleanup")
        run("journalctl --vacuum-time=7d")
        run("find /var/log -type f -size +100M -delete")
        healthy = False

    # =========================
    # LOAD CHECK
    # =========================
    load = get_sysload()

    if load > MAX_SYSLOAD:
        reboot_now(f"system overload {load}")
        return

    # =========================
    # WIREGUARD FIRST
    # =========================
    if run(f"ip link show {WG_IF}"):
        if not run(f"ping -I {WG_IF} -c2 -W5 {WG_PING_IP}"):
            if should_restart("wg"):
                log(f"wg ping failed via {WG_IF} to {WG_PING_IP}")
                cycle_wg()
            else:
                reboot_now("wg failed after max retries")
            return
        else:
            reset_restart_counter("wg")
    else:
        if should_restart("wg"):
            log(f"wg interface missing: {WG_IF}")
            cycle_wg()
        else:
            reboot_now("wg interface missing after max retries")
        return

    # =========================
    # SIP SERVER PING
    # =========================
    if not run(f"ping -c2 -W2 {SOWA_SIP_PING_IP}"):
        if should_restart(SOWA_SIP_SERVICE):
            log(f"sip server unreachable {SOWA_SIP_PING_IP}")
            restart(SOWA_SIP_SERVICE)
        else:
            reboot_now("sip server unreachable after max retries")
        return
    else:
        reset_restart_counter(SOWA_SIP_SERVICE)

    # =========================
    # SIP LOOP CHECK
    # =========================
    if check_sowa_sip_journal():
        if should_restart(f"{SOWA_SIP_SERVICE}_loop"):
            log("sip registration loop detected")
            restart(SOWA_SIP_SERVICE)
        else:
            reboot_now("sip registration loop after max retries")
        return
    else:
        reset_restart_counter(f"{SOWA_SIP_SERVICE}_loop")

    # =========================
    # SIP SERVICE CHECK
    # =========================
    if not run(f"systemctl is-active --quiet {SOWA_SIP_SERVICE}"):
        if should_restart(f"{SOWA_SIP_SERVICE}_inactive"):
            log("sip service inactive")
            restart(SOWA_SIP_SERVICE)
        else:
            reboot_now("sip inactive after max retries")
        return
    else:
        reset_restart_counter(f"{SOWA_SIP_SERVICE}_inactive")

    # =========================
    # OK
    # =========================
    if healthy:
        log("system healthy")


# =========================
# LOOP
# =========================

if __name__ == "__main__":
    log("watchdog started")

    while True:
        try:
            main()
        except Exception as e:
            log(f"watchdog error: {e}")

        time.sleep(CHECK_INTERVAL)
