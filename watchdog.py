#!/usr/bin/env python3
import os
import subprocess
import time
from datetime import datetime

# === Настройки ===
WG_IF = "wg0"
WG_PING_IP = "8.8.8.8"

SOWA_SERVICE = "sowa.service"
SOWA_SIP_SERVICE = "sowa_sip.service"
SOWA_SIP_PING_IP = "172.16.105.2"   # <-- замени на свой IP

LOGFILE = "/var/log/watchdog.log"
STATE_DIR = "/run/watchdog"

MAX_RESTARTS = 3
RESET_INTERVAL = 30   # сек (5 минут)

DISK_WARN = 90
DISK_CRIT = 95
MAX_SYSLOAD = 10


def log(msg):
    with open(LOGFILE, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}\n")


def run(cmd):
    """Выполнить команду и вернуть True/False по exit code"""
    return subprocess.call(cmd, shell=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def get_disk_usage():
    out = subprocess.check_output("df -h / | awk 'NR==2 {print $5}'", shell=True)
    return int(out.decode().strip().replace("%", ""))


def get_sysload():
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0])
def check_sowa_sip_journal():
    """
    Проверяет последние строки журнала sowa_sip.service.
    Если подряд N строк = 'Registration successful', возвращает True.
    """
    N = 5
    try:
        out = subprocess.check_output(
            ["sudo", "journalctl", "-u", SOWA_SIP_SERVICE, "-n", str(N), "--no-pager"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8", errors="ignore").strip().splitlines()

        if len(out) < N:
            return False

        # оставим только содержимое сообщений
        logs = [line for line in out if "Registration successful" in line]

        return len(logs) == N  # все N строк совпали
    except Exception as e:
        log(f"⚠️ Ошибка при анализе журнала {SOWA_SIP_SERVICE}: {e}")
        return False

def should_restart(name):
    """Антицикличность рестартов"""
    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, f"{name}.count")

    now = int(time.time())
    count, lasttime = 0, 0

    if os.path.exists(state_file):
        with open(state_file) as f:
            parts = f.read().strip().split()
            if len(parts) == 2:
                count, lasttime = int(parts[0]), int(parts[1])

    # сброс если прошло больше RESET_INTERVAL
    if now - lasttime > RESET_INTERVAL:
        count = 0

    count += 1
    with open(state_file, "w") as f:
        f.write(f"{count} {now}")

    if count > MAX_RESTARTS:
        log(f"❌ Лимит рестартов для {name} превышен ({count}). Пропускаю.")
        return False
    return True


def restart(service):
    log(f"Перезапускаю {service}")
    run(f"sudo systemctl restart {service}")


def main():
    healthy = True  # флаг, что все проверки прошли

    # === Проверка диска ===
    disk_used = get_disk_usage()
    if disk_used > DISK_CRIT:
        log(f"❌ Диск заполнен на {disk_used}% — рестарты блокированы!")
        return
    elif disk_used > DISK_WARN:
        log(f"⚠️ Диск заполнен на {disk_used}% — чищу логи.")
        run("sudo journalctl --vacuum-time=7d")
        run("sudo find /var/log -type f -size +100M -delete")
        healthy = False

    # === Проверка нагрузки ===
    sysload = get_sysload()
    if sysload > MAX_SYSLOAD:
        log(f"⚠️ Высокая нагрузка (load={sysload}) — пропускаю итерацию.")
        run("reboot")
        return

    # === WireGuard ===
    if run(f"ip link show {WG_IF}"):
        # интерфейс существует, проверяем пинг
        if not run(f"ping -I {WG_IF} -c2 -W5 {WG_PING_IP}"):
            if should_restart("wg"):
                log(f"⚠️ Нет пинга {WG_PING_IP} через {WG_IF} — рестарт wg-quick@{WG_IF}")
                restart(f"wg-quick@{WG_IF}")
            else:
                log(f"🚨 {WG_IF} не восстановился после {MAX_RESTARTS} рестартов — перезагружаю RPi")
                run("sudo /sbin/reboot")
            healthy = False
    else:
        # интерфейс вообще отсутствует
        if should_restart("wg"):
            log(f"⚠️ Интерфейс {WG_IF} отсутствует — рестарт wg-quick@{WG_IF}")
            restart(f"wg-quick@{WG_IF}")
        else:
            log(f"🚨 {WG_IF} так и не поднялся после {MAX_RESTARTS} попыток — перезагружаю RPi")
            run("sudo /sbin/reboot")
        healthy = False
    # === Sowa_SIP ===
    if not run(f"ping -c2 -W2 {SOWA_SIP_PING_IP}"):
        if should_restart(SOWA_SIP_SERVICE):
            log(f"⚠️ Нет пинга до {SOWA_SIP_PING_IP} — рестарт {SOWA_SIP_SERVICE}.")
            restart(SOWA_SIP_SERVICE)
        healthy = False
    
    elif check_sowa_sip_journal():
        if should_restart(SOWA_SIP_SERVICE):
            log(f"⚠️ {SOWA_SIP_SERVICE} зациклился на 'Registration successful' — рестарт.")
            restart(SOWA_SIP_SERVICE)
        healthy = False
    
    elif not run(f"systemctl is-active --quiet {SOWA_SIP_SERVICE}"):
        if should_restart(SOWA_SIP_SERVICE):
            log(f"⚠️ {SOWA_SIP_SERVICE} не активен — рестарт.")
            restart(SOWA_SIP_SERVICE)
        healthy = False

    # === Если всё ок ===
    if healthy:
        log("✅ Все проверки пройдены успешно.")


if __name__ == "__main__":
    main()
