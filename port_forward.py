# -*- coding: utf-8 -*-
"""
Windows 포트 포워딩 관리 프로그램 (GUI, tkinter)
-------------------------------------------------------------------
netsh interface portproxy / Windows 방화벽 규칙을 GUI에서 관리합니다.

- 포트 포워딩 추가 / 삭제 / 목록 조회 (netsh interface portproxy)
- 방화벽 인바운드 규칙 추가 / 삭제 (New-NetFirewallRule / Remove-NetFirewallRule)
- WSL2 내부 IP 자동 감지 버튼
- 등록한 규칙을 JSON으로 저장해두고, 재부팅 후(WSL2 IP 변경 등) 일괄 재적용

반드시 "관리자 권한"으로 실행해야 합니다.
실행: python port_forward_gui.py
(더블클릭 실행 시 관리자 권한이 아니면 자동으로 관리자 권한 재실행을 시도합니다)

빌드(선택): pyinstaller --onefile --noconsole --uac-admin port_forward_gui.py
"""

import ctypes
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

CONFIG_PATH = os.path.join(os.path.expanduser("~"), "port_forward_rules.json")

# 버튼 액션에서 실제 실행되는 명령어를 GUI 상태바에 표시하기 위한 콜백
_command_logger = None


def set_command_logger(callback):
    """GUI가 실행되는 명령어 문자열을 전달받을 콜백을 등록."""
    global _command_logger
    _command_logger = callback


def _log_command(cmd: str):
    if _command_logger:
        try:
            _command_logger(cmd)
        except Exception:
            pass


# ------------------------------------------------------------------
# 관리자 권한 처리
# ------------------------------------------------------------------
def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    """현재 스크립트를 관리자 권한으로 다시 실행하고 현재 프로세스는 종료."""
    params = " ".join(f'"{a}"' for a in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{os.path.abspath(sys.argv[0])}" {params}', None, 1
    )
    sys.exit(0)


# ------------------------------------------------------------------
# 명령 실행 유틸
# ------------------------------------------------------------------
def run_command(cmd: str) -> tuple[int, str, str]:
    _log_command(cmd)
    # 주의: ["cmd", "/c", cmd] 리스트 형태로 실행하면 subprocess가 cmd 문자열 전체를
    # 다시 한번 따옴표로 감싸면서 내부의 "(큰따옴표)를 \"로 이스케이프해버려서
    # tasklist /FI "PID eq 1234" 같이 따옴표가 포함된 명령어가 콘솔에서 직접 실행할 때와
    # 다르게 깨져서 전달되는 문제가 있었다. shell=True + 문자열 그대로 실행하면
    # {COMSPEC} /c "명령어" 형태로만 감싸고 내부 따옴표를 건드리지 않아 cmd 콘솔에서
    # 직접 타이핑하는 것과 동일하게 동작한다.
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="cp949",
        errors="ignore",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def run_powershell(cmd: str) -> tuple[int, str, str]:
    _log_command(f"powershell -Command \"{cmd}\"")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ------------------------------------------------------------------
# 규칙 저장(JSON)
# ------------------------------------------------------------------
def load_rules() -> list[dict]:
    if not os.path.exists(CONFIG_PATH):
        return []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_rules(rules: list[dict]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# netsh / 방화벽 액션
# ------------------------------------------------------------------
def build_add_portproxy_command(listenport, connectport, connectaddress, listenaddress="0.0.0.0"):
    """netsh interface portproxy add v4tov4 명령어 문자열만 생성 (실행하지 않음)."""
    return (
        f"netsh interface portproxy add v4tov4 "
        f"listenport={listenport} listenaddress={listenaddress} "
        f"connectport={connectport} connectaddress={connectaddress}"
    )


def add_portproxy(listenport, connectport, connectaddress, listenaddress="0.0.0.0"):
    cmd = build_add_portproxy_command(listenport, connectport, connectaddress, listenaddress)
    return run_command(cmd)


def delete_portproxy(listenport, listenaddress="0.0.0.0"):
    cmd = (
        f"netsh interface portproxy delete v4tov4 "
        f"listenport={listenport} listenaddress={listenaddress}"
    )
    return run_command(cmd)


def show_portproxy_all():
    return run_command("netsh interface portproxy show all")


def add_firewall_rule(name, port, protocol="TCP"):
    cmd = (
        f'New-NetFirewallRule -Name "{name}" -DisplayName "{name}" '
        f'-Direction Inbound -Protocol {protocol} -LocalPort {port} -Action Allow'
    )
    return run_powershell(cmd)


def delete_firewall_rule(name):
    cmd = f'Remove-NetFirewallRule -Name "{name}"'
    return run_powershell(cmd)


def parse_portproxy_output(output: str) -> list[dict]:
    """`netsh interface portproxy show all` 출력을 파싱해서
    [{listenaddress, listenport, connectaddress, connectport}, ...] 형태로 반환."""
    rules = []
    lines = output.splitlines()
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("---"):
            in_table = True
            continue
        if not stripped:
            in_table = False
            continue
        if in_table:
            parts = stripped.split()
            if len(parts) == 4:
                addr, port, caddr, cport = parts
                try:
                    rules.append({
                        "listenaddress": addr,
                        "listenport": int(port),
                        "connectaddress": caddr,
                        "connectport": int(cport),
                    })
                except ValueError:
                    continue
    return rules


def get_wsl2_ip():
    code, out, err = run_command("wsl hostname -I")
    if code != 0 or not out:
        return None
    return out.split()[0].strip()


def get_wsl_distro_list() -> tuple[int, str]:
    """`wsl -l -v` 결과를 조회하여 (returncode, 출력문자열) 로 반환.

    wsl.exe는 콘솔로 리다이렉트될 때 UTF-16LE로 출력하는 경우가 많아
    (subprocess text=cp949 로 그대로 읽으면 글자가 깨짐) 바이트를 직접 받아
    NUL 바이트 비율로 UTF-16LE 여부를 판단한 뒤 적절히 디코딩한다.
    """
    _log_command("wsl -l -v")
    try:
        result = subprocess.run(
            ["wsl", "-l", "-v"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        return -1, str(e)

    def decode(b: bytes) -> str:
        if not b:
            return ""
        if b.count(b"\x00") > len(b) // 4:
            try:
                return b.decode("utf-16-le", errors="ignore")
            except Exception:
                pass
        for enc in ("utf-8", "cp949"):
            try:
                return b.decode(enc, errors="ignore")
            except Exception:
                continue
        return b.decode(errors="ignore")

    out = decode(result.stdout).strip()
    err = decode(result.stderr).strip()
    if result.returncode != 0 and not out:
        out = err
    return result.returncode, out


# ------------------------------------------------------------------
# WSL2 mirrored 네트워킹 모드 확인
# ------------------------------------------------------------------
def get_wslconfig_networking_mode() -> str | None:
    """%USERPROFILE%\\.wslconfig 의 [wsl2] 섹션에서 networkingMode 값을 읽는다."""
    path = os.path.join(os.path.expanduser("~"), ".wslconfig")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None

    section = None
    mode = None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]").strip().lower()
            continue
        if section == "wsl2" and "=" in line:
            key, _, val = line.partition("=")
            if key.strip().lower() == "networkingmode":
                mode = val.strip().lower()
    return mode


def set_wslconfig_networking_mode(mode: str) -> None:
    """%USERPROFILE%\\.wslconfig 의 [wsl2] 섹션을 지정된 네트워킹 모드로 설정한다.

    mode == "mirrored":
        [wsl2]
        networkingMode=mirrored
        dnsTunneling=true
        firewall=true
        autoProxy=true

    mode == "nat":
        [wsl2]
        networkingMode=nat
        (mirrored 전용 키인 dnsTunneling / firewall / autoProxy 는 제거)

    기존 파일에 다른 섹션/키가 있다면 최대한 보존한다.
    """
    path = os.path.join(os.path.expanduser("~"), ".wslconfig")

    if mode == "mirrored":
        new_keys = {
            "networkingMode": "mirrored",
            "dnsTunneling": "true",
            "firewall": "true",
            "autoProxy": "true",
        }
        remove_keys: set[str] = set()
    else:
        new_keys = {
            "networkingMode": "nat",
        }
        # mirrored 모드 전용 키는 nat 전환 시 제거한다.
        remove_keys = {"dnstunneling", "firewall", "autoproxy"}

    content = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            content = ""

    # 섹션 단위로 파싱 (다른 섹션/알 수 없는 키는 그대로 보존)
    sections: list[tuple[str, list[str]]] = []
    current_section = None
    current_lines: list[str] = []

    def flush():
        if current_section is not None:
            sections.append((current_section, current_lines[:]))

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            flush()
            current_section = stripped.strip("[]").strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(raw_line)
        # 최초 섹션 선언 이전의 라인은 무시한다 (일반적으로 없음).
    flush()

    found_wsl2 = False
    new_sections: list[tuple[str, list[str]]] = []
    for name, lines in sections:
        if name.strip().lower() == "wsl2":
            found_wsl2 = True
            kept_lines: list[str] = []
            applied: set[str] = set()
            for raw_line in lines:
                s = raw_line.strip()
                if not s or s.startswith(("#", ";")) or "=" not in s:
                    kept_lines.append(raw_line)
                    continue
                key, _, _ = s.partition("=")
                key_norm = key.strip().lower()
                if key_norm in remove_keys:
                    continue  # 다른 모드 전용 키 삭제
                matched_new_key = None
                for nk in new_keys:
                    if nk.lower() == key_norm:
                        matched_new_key = nk
                        break
                if matched_new_key:
                    kept_lines.append(f"{matched_new_key}={new_keys[matched_new_key]}")
                    applied.add(matched_new_key)
                else:
                    kept_lines.append(raw_line)  # 관리 대상이 아닌 키는 보존
            for nk, nv in new_keys.items():
                if nk not in applied:
                    kept_lines.append(f"{nk}={nv}")
            new_sections.append((name, kept_lines))
        else:
            new_sections.append((name, lines))

    if not found_wsl2:
        new_sections.append(("wsl2", [f"{k}={v}" for k, v in new_keys.items()]))

    out_lines: list[str] = []
    for name, lines in new_sections:
        out_lines.append(f"[{name}]")
        out_lines.extend(lines)
        out_lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines).rstrip() + "\n")

    _log_command(f"(.wslconfig 갱신) [wsl2] networkingMode={mode}")


def restart_wsl(distro: str = "Ubuntu", delay_sec: int = 5, on_wait=None):
    """WSL을 종료(wsl --shutdown)하고 delay_sec초 대기 후 지정 배포판을 새 콘솔에서 실행.

    on_wait: 대기 시작 시 호출할 콜백(선택). 백그라운드 스레드에서 호출되므로
             UI 갱신이 필요하면 콜백 내부에서 적절히 after() 등을 사용해야 한다.
    """
    code, out, err = run_command("wsl --shutdown")

    if on_wait:
        try:
            on_wait()
        except Exception:
            pass

    time.sleep(delay_sec)

    launch_cmd = f"wsl -d {distro}"
    _log_command(launch_cmd)
    subprocess.Popen(
        ["wsl", "-d", distro],
        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
    )

    return code, out, err


def get_windows_ipv4_addresses() -> list[str]:
    """ipconfig 결과에서 실제 연결된 어댑터들의 IPv4 주소를 추출."""
    code, out, err = run_command("ipconfig")
    if code != 0:
        return []
    ips = re.findall(r"IPv4[^:\n]*:\s*([\d]{1,3}(?:\.[\d]{1,3}){3})", out)
    return ips


def get_wsl2_interface_ips() -> list[tuple[str, str]]:
    """`wsl ip a` 결과에서 활성 상태인 인터페이스명, IPv4 주소 목록을 추출.
    (loopback, docker, veth 계열은 제외)"""
    code, out, err = run_command("wsl ip a")
    if code != 0:
        return []

    result = []
    current_iface = None
    is_up = False
    for line in out.splitlines():
        m = re.match(r"^\d+:\s+(\S+):\s+<([^>]*)>", line)
        if m:
            current_iface = m.group(1)
            flags = m.group(2).split(",")
            is_up = "UP" in flags
            continue
        if is_up and current_iface and not current_iface.startswith(
            ("lo", "docker", "veth", "br-")
        ):
            m2 = re.search(r"inet\s+([\d]{1,3}(?:\.[\d]{1,3}){3})/\d+", line)
            if m2:
                result.append((current_iface, m2.group(1)))
    return result


def check_mirrored_status() -> tuple[bool, dict]:
    """mirrored 네트워킹 설정 및 실제 동작 여부를 확인.
    반환: (정상 여부, 상세정보 dict)"""
    mode = get_wslconfig_networking_mode()
    configured = (mode == "mirrored")

    windows_ips = get_windows_ipv4_addresses()
    wsl_ifaces = get_wsl2_interface_ips()
    wsl_ips = [ip for _, ip in wsl_ifaces]

    common_ips = sorted(set(windows_ips) & set(wsl_ips))
    working = configured and len(common_ips) > 0

    detail = {
        "wslconfig_mode": mode or "(설정 없음)",
        "configured": configured,
        "windows_ips": sorted(set(windows_ips)),
        "wsl_interfaces": wsl_ifaces,
        "wsl_ips": sorted(set(wsl_ips)),
        "common_ips": common_ips,
    }
    return working, detail


# ------------------------------------------------------------------
# 포트 점유(LISTENING) 관리
# ------------------------------------------------------------------
def get_listening_ports() -> list[dict]:
    """`netstat -ano` 결과에서 LISTENING 상태인 TCP 포트 목록을 추출.
    반환: [{proto, address, port, pid}, ...]"""
    code, out, err = run_command("netstat -ano -p TCP")
    if code != 0:
        return []

    results = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("TCP"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[0]
        local = parts[1]
        state = parts[-2]
        pid_str = parts[-1]
        if state.upper() != "LISTENING":
            continue
        if ":" not in local:
            continue
        addr, _, port_str = local.rpartition(":")
        try:
            port = int(port_str)
            pid = int(pid_str)
        except ValueError:
            continue
        results.append({"proto": proto, "address": addr, "port": port, "pid": pid})
    return results


def get_process_name(pid: int) -> str:
    """tasklist /FI "PID eq <pid>" 명령으로 해당 PID의 프로세스 이름을 조회."""
    cmd = f'tasklist /FI "PID eq {pid}" /FO CSV /NH'
    code, out, err = run_command(cmd)
    if code != 0 or not out.strip():
        return "(알 수 없음)"
    try:
        reader = csv.reader(io.StringIO(out.strip().splitlines()[0]))
        row = next(reader)
        return row[0] if row else "(알 수 없음)"
    except Exception:
        return "(알 수 없음)"


def get_listening_ports_with_process() -> list[dict]:
    """LISTENING 포트 목록에 PID별 프로세스 이름을 붙여서 반환."""
    ports = get_listening_ports()
    name_cache: dict[int, str] = {}
    result = []
    for p in ports:
        pid = p["pid"]
        if pid not in name_cache:
            name_cache[pid] = get_process_name(pid)
        result.append({**p, "process": name_cache[pid]})
    return result


def kill_process(pid: int) -> tuple[int, str, str]:
    """taskkill /F /PID <pid> 명령으로 프로세스를 강제 종료."""
    return run_command(f"taskkill /F /PID {pid}")


# ------------------------------------------------------------------
# WSL 내부 포트 점유(LISTENING) 조회
# ------------------------------------------------------------------
def _parse_ss_output(out: str) -> list[dict]:
    """`ss -tulnpH` 출력을 파싱.
    반환: [{proto, state, address, port, pid, process}, ...]"""
    results = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        proto = parts[0].lower()
        if not proto.startswith(("tcp", "udp")):
            continue
        state = parts[1].upper()
        if proto.startswith("tcp") and state != "LISTEN":
            continue

        local = parts[4]
        extra = parts[5] if len(parts) > 5 else ""
        addr, _, port_str = local.rpartition(":")
        if not port_str.isdigit():
            continue
        port = int(port_str)
        addr = addr.strip("[]") or "*"

        m = re.search(r'\("([^"]+)",pid=(\d+)', extra)
        if m:
            process = m.group(1)
            pid = int(m.group(2))
        else:
            process = "(권한 필요)"
            pid = None

        results.append({
            "proto": proto,
            "state": state if proto.startswith("tcp") else "LISTEN",
            "address": addr,
            "port": port,
            "pid": pid,
            "process": process,
        })
    return results


def _parse_wsl_netstat_output(out: str) -> list[dict]:
    """`netstat -tulnpn` (Linux) 출력을 파싱. (ss를 사용할 수 없을 때의 대안)"""
    results = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        proto = parts[0].lower()
        if proto.startswith("tcp"):
            if len(parts) < 7:
                continue
            local = parts[3]
            state = parts[5].upper()
            pidprog = parts[6]
            if state != "LISTEN":
                continue
        elif proto.startswith("udp"):
            if len(parts) < 6:
                continue
            local = parts[3]
            state = "LISTEN"
            pidprog = parts[5]
        else:
            continue

        addr, _, port_str = local.rpartition(":")
        if not port_str.isdigit():
            continue
        port = int(port_str)
        addr = addr.strip("[]") or "*"

        if pidprog and pidprog != "-" and "/" in pidprog:
            pid_str, _, process = pidprog.partition("/")
            pid = int(pid_str) if pid_str.isdigit() else None
            process = process or "(알 수 없음)"
        else:
            pid = None
            process = "(권한 필요)"

        results.append({
            "proto": proto,
            "state": state,
            "address": addr,
            "port": port,
            "pid": pid,
            "process": process,
        })
    return results


def get_wsl_listening_ports() -> list[dict]:
    """WSL(리눅스) 내부에서 LISTEN 중인 TCP/UDP 포트 목록을 조회.
    `ss -tulnp`를 우선 사용하고, 사용할 수 없으면 `netstat -tulnpn`으로 대체한다.
    반환: [{proto, state, address, port, pid, process}, ...]"""
    code, out, err = run_command('wsl bash -c "ss -tulnpH 2>/dev/null"')
    if code == 0 and out.strip():
        parsed = _parse_ss_output(out)
        if parsed:
            return parsed

    code2, out2, err2 = run_command('wsl bash -c "netstat -tulnpn 2>/dev/null"')
    if code2 == 0 and out2.strip():
        return _parse_wsl_netstat_output(out2)

    return []


def kill_wsl_process(pid: int) -> tuple[int, str, str]:
    """WSL 내부 프로세스를 강제 종료 (kill -9)."""
    return run_command(f'wsl bash -c "kill -9 {pid}"')


# ------------------------------------------------------------------
# Windows 방화벽 규칙 조회
# ------------------------------------------------------------------
def get_windows_firewall_rules() -> list[dict]:
    """PowerShell로 Windows 방화벽 규칙 전체 목록을 조회.
    각 규칙에 연결된 포트 필터(프로토콜/포트)까지 함께 매핑해서 반환한다.
    반환: [{DisplayName, Enabled, Direction, Action, Protocol, LocalPort, RemotePort, Profile}, ...]"""
    ps_cmd = (
        "$rules = Get-NetFirewallRule; "
        "$ports = Get-NetFirewallPortFilter; "
        "$portMap = @{}; "
        "foreach ($p in $ports) { $portMap[$p.InstanceID] = $p }; "
        "$rules | ForEach-Object { "
        "$pf = $portMap[$_.InstanceID]; "
        "[PSCustomObject]@{ "
        "DisplayName = $_.DisplayName; "
        "Enabled = $_.Enabled; "
        "Direction = $_.Direction; "
        "Action = $_.Action; "
        "Protocol = $(if ($pf) { $pf.Protocol } else { '' }); "
        "LocalPort = $(if ($pf) { $pf.LocalPort } else { '' }); "
        "RemotePort = $(if ($pf) { $pf.RemotePort } else { '' }); "
        "Profile = $_.Profile "
        "} "
        "} | ConvertTo-Csv -NoTypeInformation"
    )
    code, out, err = run_powershell(ps_cmd)
    if code != 0 or not out.strip():
        return []

    rows = []
    reader = csv.reader(io.StringIO(out))
    header = next(reader, None)
    if not header:
        return rows
    for row in reader:
        if len(row) < len(header):
            continue
        rows.append(dict(zip(header, row)))
    return rows


# ------------------------------------------------------------------
# WSL 내부 방화벽(ufw / iptables) 현황 조회
# ------------------------------------------------------------------
def _parse_ufw_status(out: str) -> list[dict]:
    """`ufw status numbered` 출력을 파싱.
    반환: [{chain, no, action, to, from, note}, ...]"""
    rows = []
    for line in out.splitlines():
        line = line.rstrip()
        m = re.match(r"^\[\s*(\d+)\]\s+(.*)$", line)
        if not m:
            continue
        no = m.group(1)
        rest = m.group(2)
        fields = re.split(r"\s{2,}", rest.strip())
        to = fields[0] if len(fields) > 0 else ""
        action = fields[1] if len(fields) > 1 else ""
        frm = fields[2] if len(fields) > 2 else ""
        rows.append({"chain": "", "no": no, "action": action, "to": to, "from": frm, "note": ""})
    return rows


def _parse_iptables_status(out: str) -> list[dict]:
    """`iptables -L -n --line-numbers` 출력을 파싱.
    반환: [{chain, no, action, to, from, note}, ...]"""
    rows = []
    current_chain = None
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m_chain = re.match(r"^Chain (\S+) \(policy (\S+)\)", stripped)
        if m_chain:
            current_chain = m_chain.group(1)
            continue
        if stripped.startswith(("num", "target")):
            continue
        parts = stripped.split(None, 6)
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        no, target, prot, opt, source, destination = parts[:6]
        extra = parts[6] if len(parts) > 6 else ""
        rows.append({
            "chain": current_chain or "",
            "no": no,
            "action": target,
            "to": destination,
            "from": source,
            "note": f"{prot} {extra}".strip(),
        })
    return rows


def get_wsl_firewall_status() -> tuple[str, list[dict]]:
    """WSL 내부 방화벽 상태를 조회. ufw가 있으면 ufw를, 없으면 iptables를 확인한다.
    반환: (상태 설명 문자열, 규칙 목록)"""
    code, out, err = run_command('wsl bash -c "ufw status numbered 2>/dev/null"')
    m_status = re.search(r"^Status:\s+(\S+)", out, re.MULTILINE) if code == 0 else None
    if m_status:
        active = m_status.group(1).lower() == "active"
        mode = "ufw (활성)" if active else "ufw (비활성)"
        return (mode, _parse_ufw_status(out))

    # ufw가 없거나 비활성 판단이 안 되면 iptables로 시도
    # (일반 사용자는 권한이 없으므로 passwordless sudo -> 실패 시 무권한 시도 순으로 확인)
    code2, out2, err2 = run_command(
        'wsl bash -c "sudo -n iptables -L -n --line-numbers 2>/dev/null '
        '|| iptables -L -n --line-numbers 2>/dev/null"'
    )
    if code2 == 0 and "Chain" in out2:
        return ("iptables", _parse_iptables_status(out2))

    return ("확인 불가 (ufw/iptables 미설치 또는 권한 부족)", [])


# ------------------------------------------------------------------
# GUI
# ------------------------------------------------------------------
class PortForwardApp(tk.Tk):
    COLUMNS = ("listenaddress", "listenport", "connectaddress", "connectport", "note", "created_at")
    HEADERS = ("대기 IP", "대기 포트", "대상 IP", "대상 포트", "메모", "등록일시")

    def __init__(self):
        super().__init__()
        self.title("Windows 포트 포워딩 / 점유 관리자")
        self.geometry("920x620")
        self.minsize(800, 520)

        # 하단 상태바 (두 탭 공통으로 화면 맨 아래 고정)
        self.command_var = tk.StringVar(value="실행된 명령어 없음")
        self.last_command = ""  # 클립보드 복사용 원본 명령어 문자열 (cmd 콘솔 테스트용)

        command_frame = ttk.Frame(self)
        command_frame.pack(fill="x", side="bottom")

        ttk.Button(
            command_frame, text="클립보드에 복사", command=self.copy_command_to_clipboard
        ).pack(side="right", padx=(4, 6), pady=2)

        command_bar = ttk.Label(
            command_frame, textvariable=self.command_var, relief="sunken", anchor="w",
            padding=4, foreground="#0b5394",
        )
        command_bar.pack(side="left", fill="x", expand=True)

        self.status_var = tk.StringVar(value="준비됨")
        status_bar = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=4)
        status_bar.pack(fill="x", side="bottom")

        # 탭 컨트롤
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.tab_forward = ttk.Frame(self.notebook)
        self.tab_ports = ttk.Frame(self.notebook)
        self.tab_wsl_ports = ttk.Frame(self.notebook)
        self.tab_win_fw = ttk.Frame(self.notebook)
        self.tab_wsl_fw = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_forward, text="포트 포워딩 관리")
        self.notebook.add(self.tab_ports, text="포트 점유 관리")
        self.notebook.add(self.tab_wsl_ports, text="WSL 포트 점유 현황")
        self.notebook.add(self.tab_win_fw, text="Windows 방화벽 현황")
        self.notebook.add(self.tab_wsl_fw, text="WSL 방화벽 현황")

        self._build_forward_tab(self.tab_forward)
        self._build_port_usage_tab(self.tab_ports)
        self._build_wsl_port_usage_tab(self.tab_wsl_ports)
        self._build_win_firewall_tab(self.tab_win_fw)
        self._build_wsl_firewall_tab(self.tab_wsl_fw)

        self.last_mirror_detail = None
        self._win_fw_all_rules: list[dict] = []
        set_command_logger(self.log_command)

        self.refresh_table()
        self.refresh_mirror_status()
        self.refresh_ports()
        self.refresh_wsl_ports()
        self.refresh_windows_firewall()
        self.refresh_wsl_firewall()

    # ---------------- 탭 1: 포트 포워딩 관리 ----------------
    def _build_forward_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="포트 포워딩 규칙", font=("맑은 고딕", 13, "bold")).pack(side="left")
        ttk.Button(top, text="새 규칙 추가", command=self.open_add_dialog).pack(side="right", padx=4)
        ttk.Button(top, text="netsh 원본 목록", command=self.show_raw_list).pack(side="right", padx=4)
        ttk.Button(top, text="새로고침", command=self.refresh_table).pack(side="right", padx=4)
        ttk.Separator(top, orient="vertical").pack(side="right", fill="y", padx=6)
        ttk.Button(top, text="열기", command=self.open_load_dialog).pack(side="right", padx=4)
        ttk.Button(top, text="저장", command=self.open_save_dialog).pack(side="right", padx=4)

        # WSL2 mirrored 네트워킹 상태 LED
        status_frame = ttk.Frame(parent, padding=(10, 4, 10, 6))
        status_frame.pack(fill="x")

        self.mirror_canvas = tk.Canvas(status_frame, width=18, height=18, highlightthickness=0)
        self.mirror_led = self.mirror_canvas.create_oval(2, 2, 16, 16, fill="#95a5a6", outline="")
        self.mirror_canvas.pack(side="left", padx=(0, 6))

        self.mirror_label = ttk.Label(status_frame, text="Mirrored 네트워킹: 확인 중...")
        self.mirror_label.pack(side="left")

        # Mirrored(on) / NAT(off) 전환 스위치
        self.mirror_switch_var = tk.BooleanVar(value=False)
        self.mirror_switch = ttk.Checkbutton(
            status_frame,
            text="Mirrored 모드 (해제 시 NAT)",
            variable=self.mirror_switch_var,
            command=self.on_toggle_mirror_switch,
        )
        self.mirror_switch.pack(side="left", padx=(10, 0))

        ttk.Button(status_frame, text="상태 새로고침", command=self.refresh_mirror_status).pack(side="left", padx=10)
        ttk.Button(status_frame, text="상세 보기", command=self.show_mirror_detail).pack(side="left")
        ttk.Button(status_frame, text="변경 적용", command=self.apply_networking_change).pack(side="left", padx=(6, 0))
        ttk.Separator(status_frame, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(status_frame, text="설정 저장", command=self.open_save_dialog).pack(side="left")
        ttk.Button(status_frame, text="설정 불러오기", command=self.load_settings_into_grid).pack(side="left", padx=(4, 0))

        # 테이블
        table_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        table_frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(table_frame, columns=self.COLUMNS, show="headings", selectmode="browse")
        for col, header in zip(self.COLUMNS, self.HEADERS):
            self.tree.heading(col, text=header)
            width = 90 if col in ("listenport", "connectport") else 150
            self.tree.column(col, width=width, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        # 하단 버튼
        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x")

        ttk.Button(bottom, text="선택 규칙 삭제", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(bottom, text="선택 규칙 재적용", command=self.reapply_selected).pack(side="left", padx=4)
        ttk.Button(bottom, text="전체 규칙 재적용", command=self.reapply_all).pack(side="left", padx=4)
        ttk.Button(bottom, text="선택 규칙 명령어 변환", command=self.convert_selected_to_command).pack(side="left", padx=4)

        ttk.Button(bottom, text="방화벽 규칙 추가", command=self.open_firewall_add_dialog).pack(side="right", padx=4)
        ttk.Button(bottom, text="방화벽 규칙 삭제", command=self.open_firewall_delete_dialog).pack(side="right", padx=4)

    # ---------------- 탭 2: 포트 점유 관리 ----------------
    PORT_COLUMNS = ("proto", "address", "port", "pid", "process")
    PORT_HEADERS = ("프로토콜", "주소", "포트", "PID", "프로세스명")

    def _build_port_usage_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="LISTENING 포트 목록", font=("맑은 고딕", 13, "bold")).pack(side="left")
        ttk.Button(top, text="새로고침", command=self.refresh_ports).pack(side="right", padx=4)

        table_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        table_frame.pack(fill="both", expand=True)

        self.port_tree = ttk.Treeview(
            table_frame, columns=self.PORT_COLUMNS, show="headings", selectmode="browse"
        )
        for col, header in zip(self.PORT_COLUMNS, self.PORT_HEADERS):
            self.port_tree.heading(col, text=header)
            width = 70 if col in ("proto", "port", "pid") else 200
            self.port_tree.column(col, width=width, anchor="center")
        self.port_tree.pack(side="left", fill="both", expand=True)

        port_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.port_tree.yview)
        self.port_tree.configure(yscrollcommand=port_scrollbar.set)
        port_scrollbar.pack(side="right", fill="y")

        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="선택 프로세스 종료 (taskkill /F)", command=self.kill_selected_process).pack(
            side="left", padx=4
        )

    # ---------------- 탭 3: WSL 포트 점유 현황 ----------------
    WSL_PORT_COLUMNS = ("proto", "state", "address", "port", "pid", "process")
    WSL_PORT_HEADERS = ("프로토콜", "상태", "주소", "포트", "PID", "프로세스명")

    def _build_wsl_port_usage_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="WSL 내부 LISTEN 포트 목록", font=("맑은 고딕", 13, "bold")).pack(side="left")
        ttk.Button(top, text="새로고침", command=self.refresh_wsl_ports).pack(side="right", padx=4)

        note = ttk.Label(
            parent,
            text="※ wsl ss(또는 netstat)로 조회합니다. 다른 사용자/루트 소유 프로세스는 권한상 이름이 보이지 않을 수 있습니다.",
            padding=(10, 0, 10, 4),
            foreground="#666666",
        )
        note.pack(fill="x")

        table_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        table_frame.pack(fill="both", expand=True)

        self.wsl_port_tree = ttk.Treeview(
            table_frame, columns=self.WSL_PORT_COLUMNS, show="headings", selectmode="browse"
        )
        for col, header in zip(self.WSL_PORT_COLUMNS, self.WSL_PORT_HEADERS):
            self.wsl_port_tree.heading(col, text=header)
            width = 70 if col in ("proto", "state", "port", "pid") else 220
            self.wsl_port_tree.column(col, width=width, anchor="center")
        self.wsl_port_tree.pack(side="left", fill="both", expand=True)

        wsl_port_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.wsl_port_tree.yview)
        self.wsl_port_tree.configure(yscrollcommand=wsl_port_scrollbar.set)
        wsl_port_scrollbar.pack(side="right", fill="y")

        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x")
        ttk.Button(
            bottom, text="선택 프로세스 종료 (wsl kill -9)", command=self.kill_selected_wsl_process
        ).pack(side="left", padx=4)

    def refresh_wsl_ports(self):
        self.set_status("WSL 포트 점유 조회 중...")

        def worker():
            return get_wsl_listening_ports()

        def done(ports):
            for item in self.wsl_port_tree.get_children():
                self.wsl_port_tree.delete(item)

            if not ports:
                self.set_status("WSL 포트 조회 결과가 없습니다 (WSL 미실행이거나 ss/netstat을 사용할 수 없습니다)")
                return

            for p in sorted(ports, key=lambda x: (x["proto"], x["port"])):
                self.wsl_port_tree.insert(
                    "", "end",
                    values=(
                        p["proto"],
                        p["state"],
                        p["address"],
                        p["port"],
                        p["pid"] if p["pid"] is not None else "-",
                        p["process"],
                    ),
                )
            self.set_status(f"WSL LISTEN 포트 {len(ports)}건 조회 완료")

        self.run_bg(worker, on_done=done)

    def kill_selected_wsl_process(self):
        sel = self.wsl_port_tree.selection()
        if not sel:
            messagebox.showinfo("안내", "종료할 프로세스를 목록에서 선택해주세요.")
            return

        vals = self.wsl_port_tree.item(sel[0], "values")
        proto, state, address, port, pid, process = vals

        if pid in ("-", "", None):
            messagebox.showwarning("안내", "PID를 확인할 수 없어 종료할 수 없습니다 (권한 문제일 수 있습니다).")
            return
        pid = int(pid)

        if not messagebox.askyesno(
            "프로세스 종료 확인",
            f"WSL 내부 포트 {port} ({address})을(를) 사용 중인 프로세스를 종료할까요?\n\n"
            f"PID: {pid}\n프로세스명: {process}\n\n"
            f"wsl kill -9 {pid} 명령이 실행됩니다."
        ):
            return

        self.set_status(f"WSL PID {pid} 프로세스 종료 중...")

        def worker():
            return kill_wsl_process(pid)

        def done(result):
            code, out, err = result
            self.set_status("준비됨")
            if code != 0:
                messagebox.showerror("실패", f"프로세스 종료 실패:\n{err or out}")
            else:
                messagebox.showinfo("완료", f"WSL PID {pid} ({process}) 프로세스를 종료했습니다.")
            self.refresh_wsl_ports()

        self.run_bg(worker, on_done=done)

    # ---------------- 탭 4: Windows 방화벽 현황 ----------------
    WIN_FW_COLUMNS = ("displayname", "enabled", "direction", "action", "protocol", "localport", "remoteport", "profile")
    WIN_FW_HEADERS = ("표시 이름", "사용", "방향", "동작", "프로토콜", "로컬 포트", "원격 포트", "프로파일")

    def _build_win_firewall_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Windows 방화벽 규칙", font=("맑은 고딕", 13, "bold")).pack(side="left")
        ttk.Button(top, text="새로고침", command=self.refresh_windows_firewall).pack(side="right", padx=4)

        self.win_fw_enabled_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top, text="사용(Enabled) 규칙만 보기",
            variable=self.win_fw_enabled_only, command=self._populate_win_firewall_grid,
        ).pack(side="right", padx=10)

        table_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        table_frame.pack(fill="both", expand=True)

        self.win_fw_tree = ttk.Treeview(
            table_frame, columns=self.WIN_FW_COLUMNS, show="headings", selectmode="browse"
        )
        for col, header in zip(self.WIN_FW_COLUMNS, self.WIN_FW_HEADERS):
            self.win_fw_tree.heading(col, text=header)
            width = 230 if col == "displayname" else 80
            self.win_fw_tree.column(col, width=width, anchor="center" if col != "displayname" else "w")
        self.win_fw_tree.pack(side="left", fill="both", expand=True)

        win_fw_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.win_fw_tree.yview)
        self.win_fw_tree.configure(yscrollcommand=win_fw_scrollbar.set)
        win_fw_scrollbar.pack(side="right", fill="y")

    def refresh_windows_firewall(self):
        self.set_status("Windows 방화벽 규칙 조회 중... (규칙이 많으면 다소 시간이 걸립니다)")

        def worker():
            return get_windows_firewall_rules()

        def done(rules):
            self._win_fw_all_rules = rules
            self._populate_win_firewall_grid()

        self.run_bg(worker, on_done=done)

    def _populate_win_firewall_grid(self):
        for item in self.win_fw_tree.get_children():
            self.win_fw_tree.delete(item)

        rules = self._win_fw_all_rules
        if self.win_fw_enabled_only.get():
            rules = [r for r in rules if r.get("Enabled", "").strip().lower() == "true"]

        for r in rules:
            self.win_fw_tree.insert(
                "", "end",
                values=(
                    r.get("DisplayName", ""),
                    r.get("Enabled", ""),
                    r.get("Direction", ""),
                    r.get("Action", ""),
                    r.get("Protocol", ""),
                    r.get("LocalPort", ""),
                    r.get("RemotePort", ""),
                    r.get("Profile", ""),
                ),
            )

        total = len(self._win_fw_all_rules)
        shown = len(rules)
        self.set_status(f"Windows 방화벽 규칙 {shown}건 표시 (전체 {total}건)")

    # ---------------- 탭 5: WSL 방화벽 현황 ----------------
    WSL_FW_COLUMNS = ("chain", "no", "action", "to", "from", "note")
    WSL_FW_HEADERS = ("체인", "번호", "동작", "대상(To)", "출발지(From)", "비고")

    def _build_wsl_firewall_tab(self, parent):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="WSL 방화벽 현황", font=("맑은 고딕", 13, "bold")).pack(side="left")
        ttk.Button(top, text="새로고침", command=self.refresh_wsl_firewall).pack(side="right", padx=4)

        self.wsl_fw_status_var = tk.StringVar(value="상태: 확인 중...")
        ttk.Label(parent, textvariable=self.wsl_fw_status_var, padding=(10, 0, 10, 4)).pack(fill="x")

        note = ttk.Label(
            parent,
            text="※ ufw가 설치되어 있으면 ufw 규칙을, 없으면 iptables 규칙을 표시합니다. "
                 "iptables 조회는 root 권한(sudo)이 필요할 수 있습니다.",
            padding=(10, 0, 10, 4),
            foreground="#666666",
        )
        note.pack(fill="x")

        table_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        table_frame.pack(fill="both", expand=True)

        self.wsl_fw_tree = ttk.Treeview(
            table_frame, columns=self.WSL_FW_COLUMNS, show="headings", selectmode="browse"
        )
        for col, header in zip(self.WSL_FW_COLUMNS, self.WSL_FW_HEADERS):
            self.wsl_fw_tree.heading(col, text=header)
            width = 60 if col in ("chain", "no") else 160
            self.wsl_fw_tree.column(col, width=width, anchor="center")
        self.wsl_fw_tree.pack(side="left", fill="both", expand=True)

        wsl_fw_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.wsl_fw_tree.yview)
        self.wsl_fw_tree.configure(yscrollcommand=wsl_fw_scrollbar.set)
        wsl_fw_scrollbar.pack(side="right", fill="y")

    def refresh_wsl_firewall(self):
        self.set_status("WSL 방화벽 상태 조회 중...")
        self.wsl_fw_status_var.set("상태: 확인 중...")

        def worker():
            return get_wsl_firewall_status()

        def done(result):
            mode, rows = result
            self.wsl_fw_status_var.set(f"상태: {mode} (규칙 {len(rows)}건)")

            for item in self.wsl_fw_tree.get_children():
                self.wsl_fw_tree.delete(item)

            for r in rows:
                self.wsl_fw_tree.insert(
                    "", "end",
                    values=(r["chain"], r["no"], r["action"], r["to"], r["from"], r["note"]),
                )

            self.set_status(f"WSL 방화벽 규칙 {len(rows)}건 조회 완료 ({mode})")

        self.run_bg(worker, on_done=done)

    # ---------------- 공통 ----------------
    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.update_idletasks()

    def log_command(self, cmd: str):
        """백그라운드 스레드에서도 안전하게 호출 가능. 하단 명령어 상태바를 갱신."""
        def update():
            self.last_command = cmd
            self.command_var.set(f"실행 명령어: {cmd}")
        self.after(0, update)

    def copy_command_to_clipboard(self):
        """하단 바에 표시된 마지막 실행 명령어를 클립보드로 복사 (cmd 콘솔 테스트용)."""
        cmd = self.last_command
        if not cmd:
            messagebox.showinfo("안내", "아직 실행된 명령어가 없습니다.")
            return
        self.clipboard_clear()
        self.clipboard_append(cmd)
        # 위젯/창이 사라져도 클립보드 내용이 유지되도록 클립보드 소유권을 확정시킴
        self.update()
        self.set_status(f"클립보드에 복사됨: {cmd}")

    def refresh_table(self):
        """실제 시스템에 등록된 포트 포워딩 목록(netsh)을 조회해서 그리드에 표시.
        메모/등록일시는 로컬 JSON에 저장된 정보가 있으면 함께 보여준다."""
        self.set_status("현재 등록된 포트 포워딩 조회 중...")

        def worker():
            return show_portproxy_all()

        def done(result):
            code, out, err = result
            for item in self.tree.get_children():
                self.tree.delete(item)

            if code != 0:
                self.set_status(f"조회 실패: {err or out}")
                return

            actual_rules = parse_portproxy_output(out)
            local_rules = load_rules()
            local_map = {
                (r.get("listenaddress", "0.0.0.0"), r.get("listenport")): r
                for r in local_rules
            }

            for r in actual_rules:
                meta = local_map.get((r["listenaddress"], r["listenport"]), {})
                self.tree.insert(
                    "", "end",
                    values=(
                        r["listenaddress"],
                        r["listenport"],
                        r["connectaddress"],
                        r["connectport"],
                        meta.get("note", ""),
                        meta.get("created_at", ""),
                    ),
                )

            # 로컬 JSON에는 있지만 실제 시스템에는 없는(외부에서 삭제된) 항목은 정리
            actual_keys = {(r["listenaddress"], r["listenport"]) for r in actual_rules}
            pruned = [
                r for r in local_rules
                if (r.get("listenaddress", "0.0.0.0"), r.get("listenport")) in actual_keys
            ]
            if len(pruned) != len(local_rules):
                save_rules(pruned)

            self.set_status(f"현재 시스템에 등록된 규칙 {len(actual_rules)}건")

        self.run_bg(worker, on_done=done)

    # ---------------- WSL2 mirrored 네트워킹 상태 ----------------
    def set_mirror_led(self, color: str, text: str):
        self.mirror_canvas.itemconfig(self.mirror_led, fill=color)
        self.mirror_label.config(text=text)

    def refresh_mirror_status(self):
        self.set_mirror_led("#95a5a6", "Mirrored 네트워킹: 확인 중...")

        def worker():
            return check_mirrored_status()

        def done(result):
            working, detail = result
            self.last_mirror_detail = detail
            # .wslconfig 상의 실제 설정값으로 on/off 스위치 상태를 동기화한다.
            # (변수 set()은 command 콜백을 재호출하지 않으므로 순환 호출 걱정은 없다.)
            self.mirror_switch_var.set(bool(detail.get("configured")))
            if working:
                self.set_mirror_led("#2ecc71", "Mirrored 네트워킹: 정상 (녹색)")
            else:
                self.set_mirror_led("#e74c3c", "Mirrored 네트워킹: 비정상 (빨강)")

        self.run_bg(worker, on_done=done)

    def on_toggle_mirror_switch(self):
        """on/off 스위치 조작 시 .wslconfig 의 networkingMode 를 변경한다.
        (실제 WSL에 적용하려면 '변경 적용' 버튼으로 재시작이 필요하다.)"""
        turn_on = self.mirror_switch_var.get()
        mode = "mirrored" if turn_on else "nat"
        mode_label = "Mirrored" if turn_on else "NAT"

        if not messagebox.askyesno(
            "네트워킹 모드 변경",
            f"WSL2 네트워킹 모드를 '{mode_label}' 로 설정합니다.\n"
            f"(%USERPROFILE%\\.wslconfig 파일이 수정됩니다)\n\n"
            f"실제로 적용하려면 이후 '변경 적용' 버튼으로 WSL을 재시작해야 합니다.\n"
            f"계속할까요?",
        ):
            # 취소 시 스위치를 원래 상태로 되돌린다.
            self.mirror_switch_var.set(not turn_on)
            return

        self.set_status(f"{mode_label} 모드로 .wslconfig 설정 중...")

        def worker():
            set_wslconfig_networking_mode(mode)
            return mode

        def done(result):
            self.set_status("준비됨")
            messagebox.showinfo(
                "완료",
                f".wslconfig 파일이 '{mode_label}' 모드로 설정되었습니다.\n"
                f"'변경 적용' 버튼을 눌러 WSL을 재시작하면 실제로 적용됩니다.",
            )
            self.refresh_mirror_status()

        self.run_bg(worker, on_done=done)

    def apply_networking_change(self):
        """'wsl --shutdown' 실행 후 5초 대기, 이어서 'wsl -d Ubuntu' 를 실행하여
        변경된 .wslconfig 네트워킹 설정을 실제로 적용한다."""
        if not messagebox.askyesno(
            "변경 적용",
            "WSL을 종료합니다 (wsl --shutdown).\n"
            "5초 후 'wsl -d Ubuntu' 를 실행하여 다시 시작합니다.\n\n"
            "현재 실행 중인 WSL 작업이 있다면 함께 종료됩니다. 계속할까요?",
        ):
            return

        self.set_status("WSL 종료 중 (wsl --shutdown)...")

        def on_wait():
            self.after(0, lambda: self.set_status("5초 대기 중... (wsl -d Ubuntu 재시작 예정)"))

        def worker():
            return restart_wsl(distro="Ubuntu", delay_sec=5, on_wait=on_wait)

        def done(result):
            code, out, err = result
            self.set_status("준비됨")
            if code != 0:
                messagebox.showwarning(
                    "경고",
                    f"'wsl --shutdown' 실행 중 문제가 있었습니다:\n{err or out}\n"
                    f"(그래도 'wsl -d Ubuntu' 재시작을 시도했습니다)",
                )
            else:
                messagebox.showinfo(
                    "완료",
                    "WSL을 재시작했습니다.\n(wsl --shutdown → 5초 대기 → wsl -d Ubuntu)",
                )
            self.refresh_mirror_status()

        self.run_bg(worker, on_done=done)

    def show_mirror_detail(self):
        detail = self.last_mirror_detail
        if not detail:
            messagebox.showinfo("안내", "먼저 '상태 새로고침'을 눌러 확인해주세요.")
            return

        self.set_status("wsl -l -v 조회 중...")

        def worker():
            return get_wsl_distro_list()

        def done(result):
            code, out = result
            self.set_status("준비됨")

            wsl_iface_lines = "\n  ".join(
                f"{iface}: {ip}" for iface, ip in detail["wsl_interfaces"]
            ) or "(없음)"

            msg = (
                f"[.wslconfig]\n"
                f"  networkingMode = {detail['wslconfig_mode']}\n"
                f"  mirrored 설정 여부 : {'예' if detail['configured'] else '아니오'}\n\n"
                f"[Windows 호스트 IPv4 목록 (ipconfig)]\n"
                f"  " + ("\n  ".join(detail["windows_ips"]) or "(없음)") + "\n\n"
                f"[WSL2 인터페이스 (ip a)]\n"
                f"  {wsl_iface_lines}\n\n"
                f"[공통 IP - 미러링 동작 여부 판단]\n"
                f"  " + ("\n  ".join(detail["common_ips"]) or "(없음, 미러링 미동작)") + "\n\n"
                f"[wsl -l -v]\n"
                + (out if out else "(결과 없음)")
            )

            win = tk.Toplevel(self)
            win.title("Mirrored 네트워킹 상세")
            win.geometry("580x520")
            win.transient(self)

            text = tk.Text(win, wrap="word", font=("Consolas", 10))
            text.pack(fill="both", expand=True, padx=6, pady=6)
            text.insert("1.0", msg)
            text.config(state="disabled")

            ttk.Button(win, text="닫기", command=win.destroy).pack(pady=(0, 8))

        self.run_bg(worker, on_done=done)

    # ---------------- 포트 점유 관리 ----------------
    def refresh_ports(self):
        self.set_status("LISTENING 포트 조회 중...")


        def worker():
            return get_listening_ports_with_process()

        def done(ports):
            for item in self.port_tree.get_children():
                self.port_tree.delete(item)
            for p in sorted(ports, key=lambda x: x["port"]):
                self.port_tree.insert(
                    "", "end",
                    values=(p["proto"], p["address"], p["port"], p["pid"], p["process"]),
                )
            self.set_status(f"LISTENING 포트 {len(ports)}건 조회 완료")

        self.run_bg(worker, on_done=done)

    def kill_selected_process(self):
        sel = self.port_tree.selection()
        if not sel:
            messagebox.showinfo("안내", "종료할 프로세스를 목록에서 선택해주세요.")
            return

        vals = self.port_tree.item(sel[0], "values")
        proto, address, port, pid, process = vals
        pid = int(pid)

        if not messagebox.askyesno(
            "프로세스 종료 확인",
            f"포트 {port} ({address})을(를) 사용 중인 프로세스를 종료할까요?\n\n"
            f"PID: {pid}\n프로세스명: {process}\n\n"
            f"taskkill /F /PID {pid} 명령이 실행됩니다."
        ):
            return

        self.set_status(f"PID {pid} 프로세스 종료 중...")

        def worker():
            return kill_process(pid)

        def done(result):
            code, out, err = result
            self.set_status("준비됨")
            if code != 0:
                messagebox.showerror("실패", f"프로세스 종료 실패:\n{err or out}")
            else:
                messagebox.showinfo("완료", f"PID {pid} ({process}) 프로세스를 종료했습니다.")
            self.refresh_ports()

        self.run_bg(worker, on_done=done)

    def get_grid_rules(self) -> list[dict]:
        """현재 그리드(메인 화면)에 표시된 모든 행을 규칙 목록으로 반환."""
        rules = []
        for item in self.tree.get_children():
            vals = self.tree.item(item, "values")
            rules.append({
                "listenaddress": vals[0],
                "listenport": int(vals[1]),
                "connectaddress": vals[2],
                "connectport": int(vals[3]),
                "note": vals[4],
                "created_at": vals[5],
            })
        return rules

    # ---------------- 파일로 저장 / 파일에서 열기 ----------------
    def open_save_dialog(self):
        rules = self.get_grid_rules()
        if not rules:
            messagebox.showinfo("안내", "저장할 규칙이 없습니다.")
            return

        file_path = filedialog.asksaveasfilename(
            title="포트 포워딩 설정 저장",
            defaultextension=".json",
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")],
            initialfile="port_forward_config.json",
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(rules, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("실패", f"파일 저장 중 오류가 발생했습니다:\n{e}")
            return

        messagebox.showinfo("완료", f"{len(rules)}건의 규칙을 저장했습니다.\n{file_path}")

    def open_load_dialog(self):
        file_path = filedialog.askopenfilename(
            title="포트 포워딩 설정 열기",
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")],
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                rules = json.load(f)
        except Exception as e:
            messagebox.showerror("실패", f"파일을 읽는 중 오류가 발생했습니다:\n{e}")
            return

        if not isinstance(rules, list) or not rules:
            messagebox.showwarning("안내", "파일에 적용할 규칙이 없습니다.")
            return

        if not messagebox.askyesno(
            "규칙 적용",
            f"불러온 {len(rules)}건의 규칙을 현재 시스템에 적용(netsh 등록)할까요?\n"
            "(WSL2 IP가 바뀌었다면 적용 전에 파일 내용을 직접 수정해주세요)"
        ):
            return

        self.set_status(f"{len(rules)}건 적용 중...")

        def worker():
            results = []
            for r in rules:
                try:
                    listenaddress = r.get("listenaddress", "0.0.0.0")
                    listenport = int(r["listenport"])
                    connectaddress = r["connectaddress"]
                    connectport = int(r["connectport"])
                except (KeyError, ValueError, TypeError):
                    results.append((r, -1, "", "잘못된 규칙 형식"))
                    continue
                code, out, err = add_portproxy(listenport, connectport, connectaddress, listenaddress)
                results.append((r, code, out, err))
            return results

        def done(results):
            self.set_status("준비됨")
            success = [r for r, c, o, e in results if c == 0]
            fail = [r for r, c, o, e in results if c != 0]

            if success:
                local_rules = load_rules()
                existing_keys = {
                    (x.get("listenaddress", "0.0.0.0"), x.get("listenport")) for x in local_rules
                }
                for r in success:
                    key = (r.get("listenaddress", "0.0.0.0"), int(r["listenport"]))
                    if key not in existing_keys:
                        local_rules.append({
                            "listenaddress": r.get("listenaddress", "0.0.0.0"),
                            "listenport": int(r["listenport"]),
                            "connectaddress": r["connectaddress"],
                            "connectport": int(r["connectport"]),
                            "note": r.get("note", ""),
                            "created_at": r.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        existing_keys.add(key)
                save_rules(local_rules)

            self.refresh_table()

            msg = f"{len(success)}건 적용 완료"
            if fail:
                msg += f"\n{len(fail)}건 실패 (이미 등록되어 있거나 형식이 잘못되었을 수 있습니다)"
            messagebox.showinfo("완료", msg)

        self.run_bg(worker, on_done=done)

    def get_selected_rule(self):
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0], "values")
        return {
            "listenaddress": vals[0],
            "listenport": int(vals[1]),
            "connectaddress": vals[2],
            "connectport": int(vals[3]),
            "note": vals[4],
            "created_at": vals[5],
        }

    def run_bg(self, func, *args, on_done=None):
        """블로킹될 수 있는 작업을 백그라운드 스레드에서 실행."""
        def wrapper():
            result = func(*args)
            if on_done:
                self.after(0, lambda: on_done(result))
        threading.Thread(target=wrapper, daemon=True).start()

    # ---------------- netsh 원본 목록 ----------------
    def show_raw_list(self):
        self.set_status("netsh 목록 조회 중...")

        def done(result):
            code, out, err = result
            self.set_status("준비됨")
            win = tk.Toplevel(self)
            win.title("netsh interface portproxy show all")
            win.geometry("640x400")
            text = tk.Text(win, wrap="word")
            text.pack(fill="both", expand=True)
            text.insert("1.0", out if out else "(등록된 규칙이 없습니다)")
            text.config(state="disabled")

        self.run_bg(show_portproxy_all, on_done=done)

    # ---------------- 규칙 추가 ----------------
    def open_add_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("포트 포워딩 추가")
        dialog.geometry("400x320")
        dialog.transient(self)
        dialog.grab_set()

        fields = {}

        def add_row(label, key, default=""):
            row = ttk.Frame(dialog, padding=(10, 6))
            row.pack(fill="x")
            ttk.Label(row, text=label, width=16).pack(side="left")
            entry = ttk.Entry(row)
            entry.insert(0, default)
            entry.pack(side="left", fill="x", expand=True)
            fields[key] = entry

        add_row("대기 IP", "listenaddress", "0.0.0.0")
        add_row("대기 포트", "listenport")
        add_row("대상 IP", "connectaddress")
        add_row("대상 포트", "connectport")
        add_row("메모(선택)", "note")

        def detect_wsl_ip():
            self.set_status("WSL2 IP 확인 중...")

            def done(ip):
                self.set_status("준비됨")
                if ip:
                    fields["connectaddress"].delete(0, "end")
                    fields["connectaddress"].insert(0, ip)
                else:
                    messagebox.showwarning("WSL2 IP", "WSL2 IP를 가져오지 못했습니다.\nWSL이 설치/실행 중인지 확인하세요.")

            self.run_bg(get_wsl2_ip, on_done=done)

        ttk.Button(dialog, text="WSL2 내부 IP 자동 감지 → 대상 IP에 채우기", command=detect_wsl_ip).pack(pady=6)

        fw_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(dialog, text="방화벽 인바운드 규칙도 함께 추가", variable=fw_var).pack(pady=4)

        def submit():
            try:
                listenport = int(fields["listenport"].get().strip())
                connectport = int(fields["connectport"].get().strip())
            except ValueError:
                messagebox.showerror("입력 오류", "포트 번호는 숫자로 입력해주세요.")
                return

            listenaddress = fields["listenaddress"].get().strip() or "0.0.0.0"
            connectaddress = fields["connectaddress"].get().strip()
            note = fields["note"].get().strip()

            if not connectaddress:
                messagebox.showerror("입력 오류", "대상 IP를 입력해주세요.")
                return

            add_fw = fw_var.get()
            dialog.destroy()
            self.set_status("포트 포워딩 등록 중...")

            def worker():
                code, out, err = add_portproxy(listenport, connectport, connectaddress, listenaddress)
                fw_result = None
                if code == 0 and add_fw:
                    fw_name = f"PortForward_{listenport}"
                    fw_result = add_firewall_rule(fw_name, listenport)
                return code, out, err, add_fw, fw_result

            def done(result):
                code, out, err, add_fw, fw_result = result
                self.set_status("준비됨")
                if code != 0:
                    messagebox.showerror("실패", f"포트 포워딩 추가 실패:\n{err or out}")
                    return

                rules = load_rules()
                rules.append({
                    "listenaddress": listenaddress,
                    "listenport": listenport,
                    "connectaddress": connectaddress,
                    "connectport": connectport,
                    "note": note,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                save_rules(rules)
                self.refresh_table()

                msg = f"{listenaddress}:{listenport} -> {connectaddress}:{connectport} 등록 완료"
                if add_fw and fw_result:
                    fcode, fout, ferr = fw_result
                    if fcode == 0:
                        msg += f"\n방화벽 규칙 'PortForward_{listenport}' 추가 완료"
                    else:
                        msg += f"\n(방화벽 규칙 추가 실패: {ferr or fout})"
                messagebox.showinfo("완료", msg)

            self.run_bg(worker, on_done=done)

        ttk.Button(dialog, text="등록", command=submit).pack(pady=10)

    # ---------------- 규칙 삭제 ----------------
    def delete_selected(self):
        rule = self.get_selected_rule()
        if not rule:
            messagebox.showinfo("안내", "삭제할 규칙을 목록에서 선택해주세요.")
            return

        if not messagebox.askyesno(
            "삭제 확인",
            f"{rule['listenaddress']}:{rule['listenport']} 규칙을 삭제할까요?"
        ):
            return

        self.set_status("삭제 중...")

        def worker():
            return delete_portproxy(rule["listenport"], rule["listenaddress"])

        def done(result):
            code, out, err = result
            self.set_status("준비됨")
            if code != 0:
                messagebox.showerror("실패", f"삭제 실패:\n{err or out}")
                return
            rules = load_rules()
            rules = [
                r for r in rules
                if not (r["listenport"] == rule["listenport"] and r["listenaddress"] == rule["listenaddress"])
            ]
            save_rules(rules)
            self.refresh_table()
            messagebox.showinfo("완료", "삭제되었습니다.")

        self.run_bg(worker, on_done=done)

    # ---------------- 규칙 재적용 ----------------
    def reapply_selected(self):
        rule = self.get_selected_rule()
        if not rule:
            messagebox.showinfo("안내", "재적용할 규칙을 목록에서 선택해주세요.")
            return
        self._reapply([rule])

    def reapply_all(self):
        rules = load_rules()
        if not rules:
            messagebox.showinfo("안내", "저장된 규칙이 없습니다.")
            return
        self._reapply(rules)

    def _reapply(self, rules):
        self.set_status(f"{len(rules)}건 재적용 중...")

        def worker():
            results = []
            for r in rules:
                code, out, err = add_portproxy(
                    r["listenport"], r["connectport"], r["connectaddress"], r["listenaddress"],
                )
                results.append((r, code, out, err))
            return results

        def done(results):
            self.set_status("준비됨")
            fail = [r for r, c, o, e in results if c != 0]
            if fail:
                messagebox.showwarning("일부 실패", f"{len(fail)}건 재적용 실패 (이미 등록되어 있을 수 있습니다)")
            else:
                messagebox.showinfo("완료", f"{len(results)}건 재적용 완료")
            self.refresh_table()

        self.run_bg(worker, on_done=done)

    def convert_selected_to_command(self):
        """그리드에서 선택된 규칙(들)에 대한 netsh 명령어를 생성하여
        하단 '실행 명령어' 표시란에 보여주고 클립보드에 복사한다.
        (실제로 실행하지는 않는다.)"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("안내", "명령어로 변환할 규칙을 목록에서 선택해주세요.")
            return

        cmds = []
        for item_id in sel:
            vals = self.tree.item(item_id, "values")
            listenaddress, listenport, connectaddress, connectport = vals[0], vals[1], vals[2], vals[3]
            cmds.append(
                build_add_portproxy_command(listenport, connectport, connectaddress, listenaddress)
            )

        full_cmd = "\n".join(cmds)

        # 하단 실행 명령어 표시란 갱신 (클립보드 복사용 원본은 self.last_command)
        self.last_command = full_cmd
        if len(cmds) == 1:
            self.command_var.set(f"실행 명령어: {cmds[0]}")
        else:
            self.command_var.set(f"실행 명령어 ({len(cmds)}건, 줄바꿈으로 구분됨): {cmds[0]}  ...")

        # 클립보드에 복사
        self.clipboard_clear()
        self.clipboard_append(full_cmd)
        self.update()  # 창이 바로 닫혀도 클립보드 내용이 유지되도록 보장

        self.set_status(f"선택한 {len(cmds)}건의 명령어를 표시하고 클립보드에 복사했습니다.")

    # ---------------- 설정 불러오기 (그리드 전용, 시스템 미반영) ----------------
    def load_settings_into_grid(self):
        """저장된 설정 파일(JSON)을 불러와 기존 그리드 내용을 모두 지운 뒤
        불러온 내용으로 그리드를 채운다. (실제 시스템/netsh에는 반영하지 않음)"""
        file_path = filedialog.askopenfilename(
            title="포트 포워딩 설정 불러오기",
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")],
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                rules = json.load(f)
        except Exception as e:
            messagebox.showerror("실패", f"파일을 읽는 중 오류가 발생했습니다:\n{e}")
            return

        if not isinstance(rules, list):
            messagebox.showwarning("안내", "올바른 설정 파일이 아닙니다.")
            return

        # 기존 그리드 내용을 모두 삭제한다.
        for item in self.tree.get_children():
            self.tree.delete(item)

        loaded_count = 0
        for r in rules:
            try:
                listenaddress = r.get("listenaddress", "0.0.0.0")
                listenport = r["listenport"]
                connectaddress = r["connectaddress"]
                connectport = r["connectport"]
                note = r.get("note", "")
                created_at = r.get("created_at", "")
            except (KeyError, TypeError, AttributeError):
                continue
            self.tree.insert(
                "", "end",
                values=(listenaddress, listenport, connectaddress, connectport, note, created_at),
            )
            loaded_count += 1

        self.set_status(f"설정 파일을 그리드로 불러왔습니다. ({loaded_count}건, 시스템 미반영)")
        messagebox.showinfo(
            "완료",
            f"{loaded_count}건의 설정을 불러와 그리드에 표시했습니다.\n"
            f"※ 실제 시스템(netsh)에는 반영되지 않았습니다.\n"
            f"필요하면 '선택 규칙 재적용' 또는 '전체 규칙 재적용' 버튼을 사용해 실제로 적용하세요.",
        )

    # ---------------- 방화벽 규칙 관리 ----------------
    def open_firewall_add_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("방화벽 규칙 추가")
        dialog.geometry("340x180")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="규칙 이름").pack(pady=(14, 2))
        name_entry = ttk.Entry(dialog)
        name_entry.pack(fill="x", padx=20)

        ttk.Label(dialog, text="포트 번호").pack(pady=(10, 2))
        port_entry = ttk.Entry(dialog)
        port_entry.pack(fill="x", padx=20)

        def submit():
            name = name_entry.get().strip()
            port_str = port_entry.get().strip()
            if not name or not port_str.isdigit():
                messagebox.showerror("입력 오류", "규칙 이름과 포트 번호를 올바르게 입력해주세요.")
                return
            dialog.destroy()
            self.set_status("방화벽 규칙 추가 중...")

            def done(result):
                code, out, err = result
                self.set_status("준비됨")
                if code != 0:
                    messagebox.showerror("실패", f"방화벽 규칙 추가 실패:\n{err or out}")
                else:
                    messagebox.showinfo("완료", f"방화벽 규칙 '{name}' 추가 완료")

            self.run_bg(add_firewall_rule, name, int(port_str), on_done=done)

        ttk.Button(dialog, text="추가", command=submit).pack(pady=14)

    def open_firewall_delete_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("방화벽 규칙 삭제")
        dialog.geometry("340x140")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="삭제할 규칙 이름").pack(pady=(14, 2))
        name_entry = ttk.Entry(dialog)
        name_entry.pack(fill="x", padx=20)

        def submit():
            name = name_entry.get().strip()
            if not name:
                messagebox.showerror("입력 오류", "규칙 이름을 입력해주세요.")
                return
            dialog.destroy()
            self.set_status("방화벽 규칙 삭제 중...")

            def done(result):
                code, out, err = result
                self.set_status("준비됨")
                if code != 0:
                    messagebox.showerror("실패", f"방화벽 규칙 삭제 실패:\n{err or out}")
                else:
                    messagebox.showinfo("완료", f"방화벽 규칙 '{name}' 삭제 완료")

            self.run_bg(delete_firewall_rule, name, on_done=done)

        ttk.Button(dialog, text="삭제", command=submit).pack(pady=14)


def main():
    if os.name != "nt":
        print("이 프로그램은 Windows 전용입니다 (netsh, PowerShell 필요).")
        sys.exit(1)

    if not is_admin():
        relaunch_as_admin()
        return

    app = PortForwardApp()
    app.mainloop()


if __name__ == "__main__":
    main()
