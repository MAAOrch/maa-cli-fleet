# -*- coding: utf-8 -*-
r"""tools/dsh_mcp 本地工作区直控工具（ws_*）。

这 5 个工具由网关进程直接执行，**不经过第二层 DSH 子代理**：

  * ws_glob / ws_grep                 只读检索
  * ws_edit_file / ws_write_file      工作区写
  * ws_run_cmd                        受控命令执行

安全边界：
  * 所有路径先经 ``common.safe_read_path()``（白名单 + 防穿越 + 敏感文件黑名单）；
  * 所有调用经 ``common.audit()`` 逐条落 ``ops/dsh_mcp_audit.jsonl``
    （``server._audit_call`` 对 ws_* 不再重复记录）；
  * ``ws_run_cmd`` 不使用 ``shell=True``，cwd 固定为工作区根，
    env 固定使用 ``common.child_env()``（已剥离 DSH_MCP_TOKEN）。

说明：``ws_run_cmd`` 的程序白名单只约束 argv[0] 的名字，白名单内的
python / go / npm 等本身就能执行任意代码；它仍是一个“受控但不沙箱”的
执行入口，调用方必须持有网关 Bearer Token。
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

import common

# --------------------------------------------------------------------------- #
# 常量                                                                          #
# --------------------------------------------------------------------------- #

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", "runtime"})

MAX_GLOB_RESULTS = 2_000
MAX_GREP_MATCHES = 1_000
MAX_LINE_CHARS = 300
MAX_GREP_FILE_BYTES = 8 * 1024 * 1024

MAX_CMD_OUTPUT_CHARS = 12_000
MAX_CAPTURE_BYTES = 64 * 1024
MAX_ARGV_ITEMS = 128
MAX_ARGV_CHARS = 16_384
MAX_ARG_CHARS = 8_192

ALLOWED_PROGRAMS = frozenset({"python", "py", "go", "git", "npm", "npx", "rg"})

# git 全局选项：可能把命令重定向到工作区外，或注入配置/别名。
_GIT_REDIRECT_OPTS = (
    "-C", "-c", "--git-dir", "--work-tree", "--exec-path",
    "--namespace", "--config-env", "--super-prefix", "--attr-source",
)
# 破坏性子命令（push 放行普通快进推送，强推/删分支在 _check_git 里单独拦截）。
_GIT_BLOCKED_SUBCOMMANDS = frozenset({
    "clean", "filter-branch", "filter-repo", "send-email", "svn",
})
_GIT_REMOTE_MUTATIONS = frozenset({
    "add", "remove", "rm", "rename", "set-url", "set-head",
    "prune", "update", "set-branches",
})
# 写 shell=True 是红线；这里只为 Windows 的 npm/npx（.cmd shim）做显式包装。
_SHELL_SHIM_SUFFIXES = (".cmd", ".bat")


# --------------------------------------------------------------------------- #
# 参数 / 路径小工具                                                             #
# --------------------------------------------------------------------------- #

def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _workspace(args: dict) -> tuple[str, Path]:
    name = args.get("workspace")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("workspace 必填，且只能是：%s" % " / ".join(common.WORKSPACES))
    name = name.strip()
    root = common.resolve_workspace(name)
    if not root.is_dir():
        raise ValueError("工作区目录不存在：%s" % root)
    return name, root.resolve()


def _norm_pattern(pattern, label: str = "pattern") -> str:
    """规范化并校验 glob：相对路径、禁止绝对路径/盘符/UNC/`..`。"""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("%s 必填且不能为空" % label)
    raw = pattern.strip().replace("\\", "/")
    if "\x00" in raw:
        raise ValueError("%s 不能包含 NUL 字符" % label)
    if raw.startswith("/"):
        raise ValueError("%s 必须是工作区内的相对 glob，禁止绝对路径/UNC" % label)
    p = Path(raw)
    if p.is_absolute() or p.drive:
        raise ValueError("%s 必须是工作区内的相对 glob，禁止绝对路径/盘符" % label)
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("%s 禁止包含 '..' 路径穿越" % label)
        parts.append(part)
    if not parts:
        raise ValueError("%s 不能为空/工作区根" % label)
    return "/".join(parts)


def _is_junction(path: Path) -> bool:
    fn = getattr(path, "is_junction", None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except OSError:
        return False


def _is_linklike(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
    except OSError:
        pass
    fn = getattr(os.path, "isjunction", None)
    if fn is not None:
        try:
            if fn(path):
                return True
        except OSError:
            pass
    return False


def _safe_write_path(workspace: str, rel_path) -> Path:
    """复用 safe_read_path 的全部边界，并额外拒绝通过符号链接/联接点写入。"""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("rel_path 必填（工作区内的相对路径）")
    try:
        target = common.safe_read_path(workspace, rel_path)
    except PermissionError as ex:
        raise PermissionError("拒绝写入敏感文件：%s" % rel_path) from ex

    root = common.resolve_workspace(workspace).resolve()
    raw = rel_path.strip().replace("\\", "/")
    cur = root
    for part in Path(raw).parts:
        cur = cur / part
        try:
            if cur.is_symlink() or _is_junction(cur):
                raise PermissionError("拒绝通过符号链接/联接点写入：%s" % rel_path)
        except OSError:
            continue
    return target


def _iter_files(root: Path):
    """遍历工作区文件，剪掉忽略目录与符号链接/联接点目录。"""
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=True, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not _is_linklike(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _is_skipped_rel(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts)


def _match_parts(parts: tuple[str, ...], pats: tuple[str, ...]) -> bool:
    """支持 ``**`` 的逐段 glob；``*`` 不跨目录。"""

    @lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(pats):
            return i == len(parts)
        if pats[j] == "**":
            k = j
            while k + 1 < len(pats) and pats[k + 1] == "**":
                k += 1
            return match(i, k + 1) or (i < len(parts) and match(i + 1, j))
        if i == len(parts):
            return False
        return fnmatch.fnmatch(parts[i], pats[j]) and match(i + 1, j + 1)

    return match(0, 0)


def _match_glob(rel: Path, pattern: str) -> bool:
    # 不含 "/" 的 pattern 按 basename 匹配，方便 "*.py" 这种任意深度写法。
    if "/" not in pattern:
        return fnmatch.fnmatch(rel.name, pattern)
    return _match_parts(tuple(rel.parts), tuple(pattern.split("/")))


def _program_name(value: str) -> str:
    base = os.path.basename(value.strip())
    low = base.lower()
    if low.endswith(".exe"):
        low = low[:-4]
    return low


def _prepare_exec(argv: list[str]) -> tuple[list[str], bool]:
    """解析 argv[0]；Windows 的 npm/npx 是 .cmd shim，需要 cmd.exe 显式包装。

    这里仍然使用 ``shell=False``：cmd.exe 只是 .cmd/.bat 的解释器，
    argv 以列表形式传给 Popen，不经过 ``shell=True`` 的字符串拼接。
    """
    resolved = shutil.which(argv[0])
    exe = resolved or argv[0]
    if os.name == "nt" and exe.lower().endswith(_SHELL_SHIM_SUFFIXES):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", exe, *argv[1:]], True
    return [exe, *argv[1:]], False


def _check_git(args: list[str]) -> None:
    """拦截 git 危险子命令与可重定向仓库/配置的全局选项。"""
    sub_idx: int | None = None
    for i, item in enumerate(args):
        if item == "--":
            continue
        if item.startswith("-"):
            continue
        sub_idx = i
        break

    prefix = args[:sub_idx] if sub_idx is not None else args
    for item in prefix:
        low = item.lower()
        for opt in _GIT_REDIRECT_OPTS:
            opt_low = opt.lower()
            if low == opt_low or low.startswith(opt_low + "="):
                raise ValueError(
                    "拒绝 git 全局选项 %r：可能把命令重定向到工作区外或注入配置" % item
                )
            if opt_low in ("-c", "-C") and item.startswith(opt):
                raise ValueError(
                    "拒绝 git 全局选项 %r：可能把命令重定向到工作区外或注入配置" % item
                )

    if sub_idx is None:
        return
    sub = args[sub_idx].lower()
    lower_args = [a.lower() for a in args]

    if sub in _GIT_BLOCKED_SUBCOMMANDS:
        raise ValueError("拒绝危险 git 子命令：%s" % sub)
    if sub == "push":
        rest = args[sub_idx + 1:]
        for a in rest:
            al = a.lower()
            if (al in ("-f", "--force", "--force-with-lease", "--force-if-includes",
                       "-d", "--delete", "--mirror", "--prune")
                    or al.startswith("--force-with-lease=")):
                raise ValueError("拒绝危险 git push 选项：%s" % a)
            if not a.startswith("-") and (a.startswith("+") or a.startswith(":")):
                raise ValueError("拒绝强推或删除远端分支的 refspec：%s" % a)
    if sub == "reset" and "--hard" in lower_args:
        raise ValueError("拒绝危险 git 子命令：reset --hard")
    if sub in ("checkout", "switch") and any(a in ("-f", "--force") for a in lower_args):
        raise ValueError("拒绝危险 git 子命令：%s --force" % sub)
    if sub == "config":
        if any(a in ("--global", "--system") or a.startswith("--file")
               for a in lower_args):
            raise ValueError("拒绝修改 git 全局/系统/外部配置文件")
    if sub == "remote":
        rest = [a for a in args[sub_idx + 1:] if not a.startswith("-")]
        if rest and rest[0].lower() in _GIT_REMOTE_MUTATIONS:
            raise ValueError("拒绝远端写操作：git remote %s" % rest[0])


def _atomic_write_utf8(target: Path, text: str) -> int:
    """UTF-8 原子写入（同目录临时文件 + os.replace），返回写入字节数。"""
    data = text.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".ws_tmp_", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return len(data)


def _spawn_reader(pipe):
    """带容量上限的管道读取线程，避免子进程刷屏把网关内存打爆。"""
    sink: dict = {"chunks": [], "kept": 0, "total": 0}

    def _read() -> None:
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                sink["total"] += len(chunk)
                if sink["kept"] < MAX_CAPTURE_BYTES:
                    take = chunk[:MAX_CAPTURE_BYTES - sink["kept"]]
                    if take:
                        sink["chunks"].append(take)
                        sink["kept"] += len(take)
        except Exception:  # noqa: BLE001 —— 进程被 kill 时管道关闭是正常路径
            pass
        finally:
            try:
                pipe.close()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_read, name="ws_run_cmd_reader", daemon=True)
    thread.start()
    return thread, sink


# --------------------------------------------------------------------------- #
# 1) ws_glob                                                                    #
# --------------------------------------------------------------------------- #

def _ws_glob_impl(args: dict) -> dict:
    workspace, root = _workspace(args)
    pattern = _norm_pattern(args.get("pattern"), "pattern")
    max_results = _clamp_int(args.get("max_results", 200), 200, 1, MAX_GLOB_RESULTS)

    files: list[str] = []
    scanned = 0
    skipped_sensitive = 0
    skipped_unsafe = 0
    truncated = False

    for full in _iter_files(root):
        try:
            rel = full.relative_to(root)
        except ValueError:
            skipped_unsafe += 1
            continue
        if _is_skipped_rel(rel):
            continue
        scanned += 1
        if not _match_glob(rel, pattern):
            continue
        if common.is_sensitive(rel):
            skipped_sensitive += 1
            continue
        try:
            target = common.safe_read_path(workspace, rel.as_posix())
        except PermissionError:
            skipped_sensitive += 1
            continue
        except (ValueError, OSError):
            skipped_unsafe += 1
            continue
        if not target.is_file():
            continue

        files.append(rel.as_posix())
        if len(files) >= max_results + 1:
            truncated = True
            break

    if truncated:
        files = files[:max_results]
    return {
        "ok": True,
        "workspace": workspace,
        "root": str(root),
        "pattern": pattern,
        "files": files,
        "count": len(files),
        "max_results": max_results,
        "truncated": truncated,
        "scanned": scanned,
        "skipped_sensitive": skipped_sensitive,
        "skipped_unsafe": skipped_unsafe,
    }


# --------------------------------------------------------------------------- #
# 2) ws_grep                                                                    #
# --------------------------------------------------------------------------- #

def _ws_grep_impl(args: dict) -> dict:
    workspace, root = _workspace(args)

    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("pattern 必填且不能为空")
    if len(pattern) > 2_000:
        raise ValueError("pattern 过长（> 2000 字符）")

    rel_path_raw = args.get("rel_path", "")
    if rel_path_raw is None:
        rel_path_raw = ""
    if not isinstance(rel_path_raw, str):
        raise ValueError("rel_path 必须是字符串")
    rel_path = rel_path_raw.strip()

    glob_raw = args.get("glob_filter", "")
    if glob_raw is None:
        glob_raw = ""
    if not isinstance(glob_raw, str):
        raise ValueError("glob_filter 必须是字符串")
    glob_filter = _norm_pattern(glob_raw, "glob_filter") if glob_raw.strip() else ""

    max_matches = _clamp_int(args.get("max_matches", 100), 100, 1, MAX_GREP_MATCHES)
    ignore_case = _as_bool(args.get("ignore_case", False))
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as ex:
        raise ValueError("无效正则：%s" % ex) from ex

    if rel_path:
        scope = common.safe_read_path(workspace, rel_path)
    else:
        scope = root
    if not scope.exists():
        raise ValueError("路径不存在：%s" % (rel_path or "."))
    try:
        rel_scope = scope.relative_to(root)
    except ValueError as ex:
        raise ValueError("路径解析后越出白名单工作区") from ex

    raw_parts = Path(rel_path.replace("\\", "/")).parts if rel_path else ()
    if _is_skipped_rel(rel_scope) or any(p in SKIP_DIRS for p in raw_parts):
        return {
            "ok": True,
            "workspace": workspace,
            "pattern": pattern,
            "rel_path": rel_path,
            "glob_filter": glob_filter,
            "ignore_case": ignore_case,
            "max_matches": max_matches,
            "matches": [],
            "count": 0,
            "truncated": False,
            "skipped_scope": True,
            "files_scanned": 0,
            "skipped_binary": 0,
            "skipped_sensitive": 0,
            "skipped_unsafe": 0,
            "skipped_large": 0,
        }

    if scope.is_file():
        candidates = [scope]
    elif scope.is_dir():
        candidates = _iter_files(scope)
    else:
        raise ValueError("不是文件或目录：%s" % (rel_path or "."))

    matches: list[dict] = []
    files_scanned = 0
    skipped_binary = 0
    skipped_sensitive = 0
    skipped_unsafe = 0
    skipped_large = 0
    truncated = False

    for full in candidates:
        try:
            rel = full.relative_to(root)
        except ValueError:
            skipped_unsafe += 1
            continue
        if _is_skipped_rel(rel):
            continue
        if glob_filter and not _match_glob(rel, glob_filter):
            continue
        if common.is_sensitive(rel):
            skipped_sensitive += 1
            continue
        try:
            target = common.safe_read_path(workspace, rel.as_posix())
        except PermissionError:
            skipped_sensitive += 1
            continue
        except (ValueError, OSError):
            skipped_unsafe += 1
            continue
        if not target.is_file():
            continue
        try:
            size = target.stat().st_size
        except OSError:
            skipped_unsafe += 1
            continue
        if size > MAX_GREP_FILE_BYTES:
            skipped_large += 1
            continue
        try:
            data = target.read_bytes()
        except OSError:
            skipped_unsafe += 1
            continue
        if b"\x00" in data:
            skipped_binary += 1
            continue

        text = data.decode("utf-8", errors="replace")
        files_scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if not regex.search(line):
                continue
            matches.append({
                "path": rel.as_posix(),
                "line": lineno,
                "text": line[:MAX_LINE_CHARS],
                "line_truncated": len(line) > MAX_LINE_CHARS,
            })
            if len(matches) > max_matches:
                truncated = True
                break
        if truncated:
            break

    if truncated:
        matches = matches[:max_matches]
    return {
        "ok": True,
        "workspace": workspace,
        "pattern": pattern,
        "rel_path": rel_path,
        "glob_filter": glob_filter,
        "ignore_case": ignore_case,
        "max_matches": max_matches,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
        "files_scanned": files_scanned,
        "skipped_binary": skipped_binary,
        "skipped_sensitive": skipped_sensitive,
        "skipped_unsafe": skipped_unsafe,
        "skipped_large": skipped_large,
    }


# --------------------------------------------------------------------------- #
# 3) ws_edit_file                                                               #
# --------------------------------------------------------------------------- #

def _ws_edit_impl(args: dict) -> dict:
    workspace, root = _workspace(args)
    target = _safe_write_path(workspace, args.get("rel_path"))
    if not target.exists():
        raise ValueError("文件不存在：%s" % args.get("rel_path"))
    if not target.is_file():
        raise ValueError("不是普通文件：%s" % args.get("rel_path"))

    old_string = args.get("old_string")
    new_string = args.get("new_string")
    if not isinstance(old_string, str) or old_string == "":
        raise ValueError("old_string 必填且不能为空字符串")
    if not isinstance(new_string, str):
        raise ValueError("new_string 必须是字符串（可为空）")
    replace_all = _as_bool(args.get("replace_all", False))

    rel = target.relative_to(root).as_posix()
    bytes_before = 0
    try:
        data = target.read_bytes()
        bytes_before = len(data)
    except OSError as ex:
        raise ValueError("读取失败：%s: %s" % (type(ex).__name__, ex)) from ex
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise ValueError("文件不是有效 UTF-8 文本：%s" % ex) from ex

    occurrences = content.count(old_string)
    if occurrences == 0:
        return {
            "ok": False,
            "workspace": workspace,
            "rel_path": rel,
            "error": "old_string 在文件中出现 0 次，未做任何修改",
            "occurrences": 0,
            "replacements": 0,
            "replace_all": replace_all,
        }
    if not replace_all and occurrences != 1:
        return {
            "ok": False,
            "workspace": workspace,
            "rel_path": rel,
            "error": "old_string 出现 %d 次（replace_all=false 时要求恰好 1 次），拒绝修改"
                     % occurrences,
            "occurrences": occurrences,
            "replacements": 0,
            "replace_all": replace_all,
        }

    if replace_all:
        new_content = content.replace(old_string, new_string)
        replacements = occurrences
    else:
        new_content = content.replace(old_string, new_string, 1)
        replacements = 1

    try:
        bytes_after = _atomic_write_utf8(target, new_content)
    except OSError as ex:
        return {
            "ok": False,
            "workspace": workspace,
            "rel_path": rel,
            "error": "原子写入失败：%s: %s" % (type(ex).__name__, ex),
            "occurrences": occurrences,
            "replacements": 0,
            "replace_all": replace_all,
            "bytes_before": bytes_before,
        }

    return {
        "ok": True,
        "workspace": workspace,
        "rel_path": rel,
        "path": str(target),
        "occurrences": occurrences,
        "replacements": replacements,
        "replace_all": replace_all,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


# --------------------------------------------------------------------------- #
# 4) ws_write_file                                                              #
# --------------------------------------------------------------------------- #

def _ws_write_impl(args: dict) -> dict:
    workspace, root = _workspace(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise ValueError("content 必须是字符串")
    overwrite = _as_bool(args.get("overwrite", False))

    target = _safe_write_path(workspace, args.get("rel_path"))
    rel = target.relative_to(root).as_posix()
    existed = target.exists()
    if existed and target.is_dir():
        return {
            "ok": False,
            "workspace": workspace,
            "rel_path": rel,
            "error": "目标已存在且是目录，拒绝写入：%s" % rel,
        }
    if existed and not overwrite:
        return {
            "ok": False,
            "workspace": workspace,
            "rel_path": rel,
            "error": "目标已存在，拒绝覆盖：%s（如需覆盖请显式传 overwrite=true）" % rel,
            "overwritten": False,
            "created": False,
        }

    try:
        size = _atomic_write_utf8(target, content)
    except OSError as ex:
        return {
            "ok": False,
            "workspace": workspace,
            "rel_path": rel,
            "error": "原子写入失败：%s: %s" % (type(ex).__name__, ex),
            "overwritten": False,
            "created": False,
        }

    return {
        "ok": True,
        "workspace": workspace,
        "rel_path": rel,
        "path": str(target),
        "bytes": size,
        "chars": len(content),
        "overwritten": existed,
        "created": not existed,
    }


# --------------------------------------------------------------------------- #
# 5) ws_run_cmd                                                                 #
# --------------------------------------------------------------------------- #

def _ws_run_impl(args: dict) -> dict:
    workspace, root = _workspace(args)

    argv = args.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError("argv 必须是非空字符串列表")
    if len(argv) > MAX_ARGV_ITEMS:
        raise ValueError("argv 参数过多（最多 %d 个）" % MAX_ARGV_ITEMS)

    clean: list[str] = []
    total_chars = 0
    for i, item in enumerate(argv):
        if not isinstance(item, str):
            raise ValueError("argv[%d] 必须是字符串" % i)
        if i == 0:
            item = item.strip()
            if not item:
                raise ValueError("argv[0] 不能为空")
        if "\x00" in item:
            raise ValueError("argv 不能包含 NUL 字符")
        if len(item) > MAX_ARG_CHARS:
            raise ValueError("argv[%d] 过长（> %d 字符）" % (i, MAX_ARG_CHARS))
        total_chars += len(item)
        clean.append(item)
    if total_chars > MAX_ARGV_CHARS:
        raise ValueError("argv 总长度过长（> %d 字符）" % MAX_ARGV_CHARS)

    program = _program_name(clean[0])
    if program not in ALLOWED_PROGRAMS:
        raise ValueError(
            "程序不在白名单：%r（允许：%s）"
            % (clean[0], ", ".join(sorted(ALLOWED_PROGRAMS)))
        )
    if program == "git":
        _check_git(clean[1:])

    timeout = _clamp_int(args.get("timeout_sec", 30), 30, 1, 60)
    exec_argv, via_cmd = _prepare_exec(clean)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            exec_argv,
            cwd=str(root),
            env=common.child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=common.CREATE_NO_WINDOW,
        )
    except Exception as ex:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "workspace": workspace,
            "program": program,
            "argv": clean,
            "cwd": str(root),
            "rc": -1,
            "elapsed_ms": elapsed_ms,
            "timed_out": False,
            "killed": False,
            "stdout": "",
            "stderr": str(ex)[:MAX_CMD_OUTPUT_CHARS],
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "via_cmd": via_cmd,
            "timeout_sec": timeout,
            "error": "启动失败：%s: %s" % (type(ex).__name__, ex),
        }

    out_thread, out_sink = _spawn_reader(proc.stdout)
    err_thread, err_sink = _spawn_reader(proc.stderr)

    timed_out = False
    killed = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = common.kill_tree(proc.pid)
        if not killed:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rc = -1

    for thread in (out_thread, err_thread):
        thread.join(timeout=5)
    for pipe, thread in ((proc.stdout, out_thread), (proc.stderr, err_thread)):
        if thread.is_alive() and pipe is not None:
            try:
                pipe.close()
            except Exception:  # noqa: BLE001
                pass
    for thread in (out_thread, err_thread):
        thread.join(timeout=2)

    out_raw = b"".join(out_sink["chunks"])
    err_raw = b"".join(err_sink["chunks"])
    out_text = out_raw.decode("utf-8", errors="replace")
    err_text = err_raw.decode("utf-8", errors="replace")
    out_total = int(out_sink["total"])
    err_total = int(err_sink["total"])

    out_truncated = out_total > len(out_raw) or len(out_text) > MAX_CMD_OUTPUT_CHARS
    err_truncated = err_total > len(err_raw) or len(err_text) > MAX_CMD_OUTPUT_CHARS
    elapsed_ms = int((time.monotonic() - started) * 1000)

    result = {
        "ok": not timed_out,
        "workspace": workspace,
        "program": program,
        "argv": clean,
        "cwd": str(root),
        "rc": rc,
        "elapsed_ms": elapsed_ms,
        "timed_out": timed_out,
        "killed": killed,
        "stdout": out_text[:MAX_CMD_OUTPUT_CHARS],
        "stderr": err_text[:MAX_CMD_OUTPUT_CHARS],
        "stdout_chars": len(out_text),
        "stderr_chars": len(err_text),
        "stdout_bytes": out_total,
        "stderr_bytes": err_total,
        "stdout_truncated": out_truncated,
        "stderr_truncated": err_truncated,
        "via_cmd": via_cmd,
        "timeout_sec": timeout,
        "success": bool(rc == 0 and not timed_out),
    }
    if timed_out:
        result["error"] = "命令超时（%d 秒），已尝试结束整棵进程树" % timeout
    return result


# --------------------------------------------------------------------------- #
# 统一审计包装                                                                  #
# --------------------------------------------------------------------------- #

def _error_result(args: dict, message: str) -> dict:
    out = {"ok": False, "error": message}
    workspace = args.get("workspace")
    if isinstance(workspace, str):
        out["workspace"] = workspace
    return out


def _make_handler(name: str, apply: bool, audit_fields: tuple[str, ...], impl):
    """执行 impl，并在所有路径（成功/参数错误/异常）上写一条详细审计。"""

    def handler(args: dict) -> dict:
        if not isinstance(args, dict):
            args = {}
        try:
            res = impl(args)
        except PermissionError as ex:
            res = _error_result(args, str(ex))
        except ValueError as ex:
            res = _error_result(args, str(ex))
        except Exception as ex:  # noqa: BLE001
            res = _error_result(args, "%s: %s" % (type(ex).__name__, ex))

        if not isinstance(res, dict):
            res = _error_result(args, "内部错误：handler 返回了非 dict")

        workspace = res.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            candidate = args.get("workspace")
            workspace = candidate if isinstance(candidate, str) else None

        if res.get("ok"):
            status = "ok"
        elif res.get("timed_out"):
            status = "timeout"
        else:
            status = "error"

        extra: dict = {}
        for key in audit_fields:
            if key in res:
                val = res[key]
            elif key in args:
                val = args[key]
            else:
                continue
            if val is not None:
                extra[key] = val
        if res.get("error"):
            extra["error"] = str(res["error"])[:1_000]

        common.audit(name, workspace=workspace, apply=apply, status=status, **extra)
        return res

    handler.__name__ = name
    return handler


HANDLERS = {
    "ws_glob": _make_handler(
        "ws_glob", False,
        ("pattern", "count", "truncated", "scanned",
         "skipped_sensitive", "skipped_unsafe", "max_results"),
        _ws_glob_impl,
    ),
    "ws_grep": _make_handler(
        "ws_grep", False,
        ("pattern", "rel_path", "glob_filter", "ignore_case", "count",
         "truncated", "files_scanned", "skipped_binary", "skipped_sensitive",
         "skipped_unsafe", "skipped_large", "max_matches"),
        _ws_grep_impl,
    ),
    "ws_edit_file": _make_handler(
        "ws_edit_file", True,
        ("rel_path", "occurrences", "replacements", "replace_all",
         "bytes_before", "bytes_after"),
        _ws_edit_impl,
    ),
    "ws_write_file": _make_handler(
        "ws_write_file", True,
        ("rel_path", "bytes", "chars", "overwritten", "created"),
        _ws_write_impl,
    ),
    "ws_run_cmd": _make_handler(
        "ws_run_cmd", True,
        ("program", "argv", "rc", "elapsed_ms", "timed_out", "killed",
         "stdout_bytes", "stderr_bytes", "stdout_truncated",
         "stderr_truncated", "via_cmd", "timeout_sec"),
        _ws_run_impl,
    ),
}

TOOL_NAMES = frozenset(HANDLERS)
