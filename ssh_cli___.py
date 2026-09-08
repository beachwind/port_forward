from __future__ import annotations

import argparse
import getpass
import queue
import shlex
import sys
import threading
import time

from crypto_store import decrypt_text, load_profiles


def find_profile(profile_id: str) -> dict:
    for profile in load_profiles():
        if profile.get("id") == profile_id:
            return profile
    raise SystemExit("프로필을 찾을 수 없습니다.")


def reader(channel, output_queue: queue.Queue[str]) -> None:
    while True:
        try:
            data = channel.recv(4096)
        except Exception as exc:
            output_queue.put(f"\n[연결 종료: {exc}]\n")
            return
        if not data:
            output_queue.put("\n[연결 종료]\n")
            return
        output_queue.put(data.decode("utf-8", errors="replace"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Command Manager SSH CLI")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--cwd", default="")
    args = parser.parse_args()

    try:
        import paramiko
    except ImportError:
        print("paramiko가 설치되어 있지 않습니다.")
        print("다음 명령으로 설치하세요: py -m pip install -r requirements.txt")
        input("Enter 키를 누르면 닫습니다...")
        return 1

    profile = find_profile(args.profile_id)
    host = profile["host"]
    port = int(profile.get("port") or 22)
    username = profile["username"]
    key_path = profile.get("key_path", "")
    password = ""
    if profile.get("password"):
        password = decrypt_text(profile["password"])
    if not password and not key_path:
        password = getpass.getpass(f"{username}@{host} password: ")

    print(f"{username}@{host}:{port} 접속 중...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password or None,
            key_filename=key_path or None,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
        )
        channel = client.invoke_shell(term="xterm")
        output_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=reader, args=(channel, output_queue), daemon=True).start()

        print("로그인 완료. 종료하려면 exit 입력 후 Enter.")
        if args.cwd:
            time.sleep(0.3)
            channel.send(f"cd -- {shlex.quote(args.cwd)}\n")
        while True:
            while not output_queue.empty():
                print(output_queue.get(), end="", flush=True)
            if channel.closed:
                break
            try:
                command = input()
            except EOFError:
                break
            channel.send(command + "\n")
            if command.strip().lower() in {"exit", "logout"}:
                time.sleep(0.5)
    except Exception as exc:
        print(f"접속 실패: {exc}")
        input("Enter 키를 누르면 닫습니다...")
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
