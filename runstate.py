"""
Trạng thái chạy dùng chung cho deploy_qa / op_autofix / op_dashboard / cleanup (chỉ stdlib).

- merge_running(): op-merge.service đang chạy? Service là Type=oneshot → lúc chạy ở trạng thái "activating", nên
  `systemctl is-active --quiet` trả mã khác 0 (trước đây mọi chỗ kiểm tra kiểu đó đều luôn thấy "không merge").
- qa_state(root): đọc khoá <root>/.qa-run.lock mà deploy_qa giữ (deploy = khoá ghi, QA = khoá đọc, nhiều lượt QA
  giữ cùng lúc được). Thay cho file đánh dấu .qa-active dùng chung: lượt QA xong trước xoá mất dấu của lượt còn chạy.
"""
import fcntl
import os
import subprocess

QA_RUN_LOCK_NAME = ".qa-run.lock"


def merge_running():
    try:
        state = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "--value", "op-merge.service"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return state in ("active", "activating", "reloading", "deactivating")


def qa_state(root):
    """"free" | "qa" (có lượt QA giữ khoá đọc) | "deploy" (có lượt deploy giữ khoá ghi)."""
    path = os.path.join(os.path.expanduser(root), QA_RUN_LOCK_NAME)
    try:
        fd = open(path, "a")
    except OSError:
        return "free"
    with fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "free"
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "qa"
        except OSError:
            return "deploy"


def qa_busy(root):
    return qa_state(root) != "free"


def ticket_lock_path(root, display_id):
    return os.path.join(os.path.expanduser(root), ".qa-ticket-%s.lock" % display_id)


def remove_ticket_lock(root, display_id):
    """Xoá file khoá QA của ticket khi không ai giữ (đang giữ thì để nguyên, tránh 2 lượt khoá 2 file khác nhau)."""
    path = ticket_lock_path(root, display_id)
    if not os.path.exists(path):
        return False
    with open(path, "a") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        os.remove(path)
        return True
