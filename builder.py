#!/usr/bin/env python3
"""
Termux Flutter APK Builder
Build APK dari file ZIP project Flutter di Termux/Android.
"""
from __future__ import annotations

import os
import sys
import re
import shutil
import signal
import subprocess
import traceback
import zipfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

import config as cfg

# ====================== ANSI / TTY fallback =======================
def _supports_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        # tetap coba ANSI, tapi tanpa cursor control
        return True
    return True

ANSI = _supports_ansi()

C = {
    "reset": "\033[0m" if ANSI else "",
    "bold":  "\033[1m" if ANSI else "",
    "dim":   "\033[2m" if ANSI else "",
    "red":   "\033[31m" if ANSI else "",
    "green": "\033[32m" if ANSI else "",
    "yellow":"\033[33m" if ANSI else "",
    "blue":  "\033[34m" if ANSI else "",
    "cyan":  "\033[36m" if ANSI else "",
    "white": "\033[37m" if ANSI else "",
}

def c(text: str, color: str) -> str:
    return f"{C.get(color,'')}{text}{C['reset']}"

def clear_screen() -> None:
    if ANSI and sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

# ====================== Logging ==================================
class Logger:
    def __init__(self, name: str):
        cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = cfg.LOGS_DIR / f"{name}.log"
        self._fh = open(self.path, "a", encoding="utf-8", errors="replace")

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime(cfg.HUMAN_TS_FORMAT)
        line = f"[{ts}] {msg}"
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception:
            pass

    def section(self, title: str) -> None:
        self.log("")
        self.log("=" * 70)
        self.log(title)
        self.log("=" * 70)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

# ====================== Command runner ===========================
class CommandError(RuntimeError):
    def __init__(self, cmd: List[str], code: int, log_path: Path):
        super().__init__(f"Command gagal (exit {code}): {' '.join(cmd)}")
        self.cmd = cmd
        self.code = code
        self.log_path = log_path

def run_stream(cmd: List[str], cwd: Optional[Path], logger: Logger,
               timeout: Optional[int] = None,
               env: Optional[dict] = None,
               capture: bool = False) -> Tuple[int, str]:
    """
    Jalankan command, stream output realtime ke stdout.
    Jika capture=True, kembalikan juga gabungan output.
    Mengembalikan (exit_code, output_str).
    """
    logger.log(f"$ {' '.join(cmd)}  (cwd={cwd})")
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    out_buf: List[str] = []
    start = time.time()

    def _reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            out_buf.append(line)
            try:
                logger._fh.write(line)
                logger._fh.flush()
            except Exception:
                pass

    try:
        import threading
        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        while True:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if timeout and (time.time() - start) > timeout:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    logger.log(f"TIMEOUT setelah {timeout}s")
                    return 124, "".join(out_buf)
                continue
        t.join(timeout=5)
    except KeyboardInterrupt:
        logger.log("KeyboardInterrupt; menghentikan proses...")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        raise

    logger.log(f"exit code: {proc.returncode}")
    return proc.returncode or 0, "".join(out_buf)

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def run_quiet(cmd: List[str]) -> Tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)

# ====================== UI helpers ===============================
BOX_W = 52

def box_top():    print("╔" + "═" * BOX_W + "╗")
def box_bot():    print("╚" + "═" * BOX_W + "╝")
def box_mid():    print("╠" + "═" * BOX_W + "╣")
def box_line(text: str = ""):
    inner = BOX_W - 2
    if len(text) > inner:
        text = text[:inner]
    print("║" + text.ljust(inner) + "║")
def box_center(text: str):
    inner = BOX_W - 2
    if len(text) > inner:
        text = text[:inner]
    print("║" + text.center(inner) + "║")

def banner():
    print()
    box_top()
    box_line()
    box_center("TERMUX FLUTTER APK BUILDER")
    box_line()
    box_center(f"Flutter {cfg.FLUTTER_VERSION_TARGET}")
    box_center("Android APK Builder")
    box_line()
    box_mid()
    box_line()
    box_line("  [1] Build APK")
    box_line("  [2] Close")
    box_line("  [3] Check Environment")
    box_line()
    box_bot()
    print()

def ok(msg):    print(c("[✓] ", "green") + msg)
def warn(msg):  print(c("[!] ", "yellow") + msg)
def err(msg):   print(c("[✗] ", "red") + msg)
def info(msg):  print(c("[i] ", "cyan") + msg)

def prompt(msg: str) -> str:
    try:
        return input(c(msg, "cyan"))
    except EOFError:
        return ""

# ====================== Environment checks =======================
def check_env() -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []

    # Python
    results.append(("Python", True, sys.version.split()[0]))

    # Node
    if which("node"):
        _, out = run_quiet(["node", "--version"])
        results.append(("Node.js", True, out.strip()))
    else:
        results.append(("Node.js", False, "tidak ditemukan"))

    # npm
    if which("npm"):
        _, out = run_quiet(["npm", "--version"])
        results.append(("npm", True, out.strip()))
    else:
        results.append(("npm", False, "tidak ditemukan"))

    # Java
    if which("java"):
        _, out = run_quiet(["java", "-version"])
        line = out.strip().splitlines()[0] if out.strip() else "unknown"
        results.append(("Java", True, line))
    else:
        results.append(("Java", False, "tidak ditemukan"))

    # Gradle (global opsional)
    if which("gradle"):
        _, out = run_quiet(["gradle", "--version"])
        m = re.search(r"Gradle (\S+)", out)
        results.append(("Gradle", True, m.group(1) if m else "unknown"))
    else:
        # cek gradle lokal
        local_g = cfg.BIN_DIR / f"gradle-{cfg.GRADLE_VERSION_TARGET}" / "bin" / "gradle"
        if local_g.exists():
            results.append(("Gradle", True, f"lokal {local_g}"))
        else:
            results.append(("Gradle", False, "global tidak ada (wrapper project akan dipakai)"))

    # Flutter
    if which("flutter"):
        _, out = run_quiet(["flutter", "--version"])
        first = out.strip().splitlines()[0] if out.strip() else "unknown"
        results.append(("Flutter", True, first))
    else:
        results.append(("Flutter", False, "tidak ditemukan"))

    # Android SDK
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk and Path(sdk).is_dir():
        results.append(("Android SDK", True, sdk))
    else:
        home_sdk = Path.home() / "android-sdk"
        if home_sdk.is_dir():
            results.append(("Android SDK", True, str(home_sdk)))
        else:
            results.append(("Android SDK", False, "tidak ditemukan"))

    # adb
    if which("adb"):
        results.append(("adb", True, which("adb") or ""))
    else:
        results.append(("adb", False, "tidak ditemukan (opsional)"))

    # sdkmanager
    if which("sdkmanager"):
        results.append(("sdkmanager", True, which("sdkmanager") or ""))
    else:
        results.append(("sdkmanager", False, "tidak ditemukan di PATH"))

    # Git
    if which("git"):
        _, out = run_quiet(["git", "--version"])
        results.append(("Git", True, out.strip()))
    else:
        results.append(("Git", False, "tidak ditemukan"))

    # PM2
    if which("pm2"):
        _, out = run_quiet(["pm2", "--version"])
        results.append(("PM2", True, out.strip().splitlines()[-1] if out.strip() else "ok"))
    else:
        results.append(("PM2", False, "tidak ditemukan (opsional)"))

    # Storage
    if cfg.STORAGE_ROOT.is_dir():
        results.append(("Storage", True, str(cfg.STORAGE_ROOT)))
    else:
        results.append(("Storage", False, "termux-setup-storage belum dijalankan"))

    return results

def show_env_check():
    print()
    print(c("── Environment Check ──", "bold"))
    print()
    res = check_env()
    for name, okv, detail in res:
        mark = c("[✓]", "green") if okv else c("[✗]", "red")
        print(f"{mark} {name:<14} {detail}")

    print()
    warn_count = sum(1 for _, o, _ in res if not o)
    if warn_count == 0:
        ok("Semua komponen inti tersedia.")
    else:
        warn(f"{warn_count} komponen tidak tersedia / opsional.")
    print()

# ====================== Storage/RAM checks =======================
def check_resources() -> None:
    try:
        total, used, free = shutil.disk_usage(str(Path.home()))
        free_gb = free / (1024 ** 3)
        if free_gb < cfg.LOW_STORAGE_WARN_GB:
            warn(f"Storage rendah: sisa {free_gb:.2f} GB. Build Flutter butuh beberapa GB.")
    except Exception:
        pass

    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            txt = meminfo.read_text()
            m = re.search(r"MemAvailable:\s+(\d+)\s+kB", txt)
            if m:
                avail_mb = int(m.group(1)) / 1024
                if avail_mb < cfg.LOW_RAM_WARN_MB:
                    warn(f"RAM tersedia rendah: {avail_mb:.0f} MB. Build bisa gagal.")
    except Exception:
        pass

    ws = cfg.WORKSPACE_DIR
    try:
        ws.mkdir(parents=True, exist_ok=True)
        test = ws / ".write_test"
        test.write_text("x")
        test.unlink()
    except Exception:
        err(f"Workspace tidak writable: {ws}")
        raise

# ====================== ZIP Validation ===========================
class ZipValidationError(Exception):
    pass

def validate_zip(path: Path) -> dict:
    if not path.exists():
        raise ZipValidationError("File tidak ditemukan.")
    if not path.is_file():
        raise ZipValidationError("Path bukan file.")
    if path.suffix.lower() != ".zip":
        raise ZipValidationError("Ekstensi file bukan .zip.")
    try:
        with open(path, "rb") as f:
            f.read(4)
    except PermissionError:
        raise ZipValidationError("File tidak dapat dibaca (permission denied).")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ZipValidationError(f"ZIP corrupt: entry rusak -> {bad}")
            count = len(zf.infolist())
            if count > cfg.MAX_ZIP_ENTRIES:
                raise ZipValidationError(f"ZIP memiliki {count} entry (melebihi batas aman).")
    except zipfile.BadZipFile as e:
        raise ZipValidationError(f"ZIP tidak valid: {e}")

    size = path.stat().st_size
    return {"size": size, "count": count}

def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

# ====================== Secure Extraction ========================
def _safe_member_path(member_name: str, target_dir: Path) -> Optional[Path]:
    # Normalisasi
    name = member_name.replace("\\", "/")
    if name.startswith("/") or name.startswith("\\"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    # Windows drive
    if re.match(r"^[a-zA-Z]:", name):
        return None
    dest = (target_dir / Path(*parts)).resolve()
    try:
        dest.relative_to(target_dir.resolve())
    except ValueError:
        return None
    return dest

def secure_extract(zip_path: Path, dest: Path, logger: Logger) -> None:
    logger.section("Extract ZIP")
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()

    total_uncompressed = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        # Batas total ukuran
        for info in infos:
            total_uncompressed += max(0, info.file_size)
            if total_uncompressed > cfg.MAX_EXTRACT_SIZE_GB * (1024 ** 3):
                raise ZipValidationError(
                    f"Total ukuran ekstrak melebihi {cfg.MAX_EXTRACT_SIZE_GB} GB. Dibatalkan."
                )

        for info in infos:
            name = info.filename
            if not name or name.endswith("/"):
                # direktori
                safe = _safe_member_path(name, dest)
                if safe is not None:
                    safe.mkdir(parents=True, exist_ok=True)
                continue

            safe = _safe_member_path(name, dest)
            if safe is None:
                warn(f"Melewati entry tidak aman: {name}")
                logger.log(f"SKIP unsafe member: {name}")
                continue
            safe.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(safe, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 256)

    logger.log(f"Extract selesai ke {dest_resolved}")

# ====================== Flutter project detection ================
IGNORE_DIR_PARTS = {
    "build", ".dart_tool", ".git", ".gradle", "node_modules",
    ".idea", ".vscode", "ios", "web", "linux", "windows", "macos",
}

def find_pubspec_roots(root: Path) -> List[Path]:
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        # skip vendor / cache
        parts = set(dp.relative_to(root).parts)
        if parts & IGNORE_DIR_PARTS:
            dirnames[:] = []
            continue
        if "pubspec.yaml" in filenames:
            found.append(dp)
    return found

def looks_like_flutter_root(p: Path) -> int:
    score = 0
    if (p / "lib").is_dir(): score += 3
    if (p / "android").is_dir(): score += 4
    if (p / "pubspec.yaml").is_file(): score += 2
    if (p / "android" / "app" / "build.gradle").is_file(): score += 3
    if (p / "android" / "app" / "build.gradle.kts").is_file(): score += 3
    if (p / "android" / "settings.gradle").is_file(): score += 2
    if (p / "android" / "settings.gradle.kts").is_file(): score += 2
    if (p / "android" / "gradle" / "wrapper" / "gradle-wrapper.properties").is_file(): score += 2
    # Kurangi jika di subdir aneh
    depth = len(p.parts)
    score -= min(depth, 5)
    return score

def choose_project_root(candidates: List[Path]) -> Optional[Path]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    ranked = sorted(candidates, key=looks_like_flutter_root, reverse=True)
    print()
    info("Ditemukan beberapa pubspec.yaml:")
    for i, p in enumerate(ranked, 1):
        mark = c("→", "cyan") if i == 1 else " "
        print(f"  {mark} [{i}] {p}  (score={looks_like_flutter_root(p)})")
    print()
    ans = prompt(f"Pilih project [default 1]: ").strip()
    if not ans:
        return ranked[0]
    if ans.isdigit() and 1 <= int(ans) <= len(ranked):
        return ranked[int(ans) - 1]
    warn("Input tidak valid, memakai pilihan pertama.")
    return ranked[0]

def validate_flutter_project(p: Path) -> bool:
    if not (p / "pubspec.yaml").is_file():
        err("pubspec.yaml tidak ditemukan di project root.")
        return False
    txt = (p / "pubspec.yaml").read_text(errors="replace")
    if "flutter:" not in txt and "sdk: flutter" not in txt:
        warn("pubspec.yaml tidak menyebut flutter/sdk. Mungkin bukan project Flutter.")

    has_lib = (p / "lib").is_dir()
    has_android = (p / "android").is_dir()
    if not has_lib and not has_android:
        err("Struktur project Flutter tidak lengkap (lib/ dan android/ tidak ada).")
        return False
    if not has_android:
        err("Folder android/ tidak ditemukan.")
        err("Project ini mungkin bukan project Flutter mobile lengkap.")
        return False
    return True

# ====================== Tool path resolution =====================
def find_gradle_wrapper(project_root: Path) -> Optional[Path]:
    gw = project_root / "android" / "gradlew"
    if gw.is_file():
        try:
            gw.chmod(gw.stat().st_mode | 0o111)
        except Exception:
            pass
        return gw
    return None

# ====================== Environment preparation ==================
def prepare_environment(logger: Logger) -> dict:
    env = os.environ.copy()

    # JAVA_HOME
    if not env.get("JAVA_HOME"):
        for cand in [
            os.path.expandvars("$PREFIX/opt/openjdk"),
            os.path.expandvars("$PREFIX/lib/jvm/java-17-openjdk"),
            os.path.expandvars("$PREFIX/lib/jvm/openjdk-17"),
        ]:
            if Path(cand).is_dir():
                env["JAVA_HOME"] = cand
                break

    # ANDROID_HOME
    if not env.get("ANDROID_HOME"):
        home_sdk = Path.home() / "android-sdk"
        if home_sdk.is_dir():
            env["ANDROID_HOME"] = str(home_sdk)
            env["ANDROID_SDK_ROOT"] = str(home_sdk)

    # PATH augment
    extras: List[str] = []
    for k in ("JAVA_HOME", "FLUTTER_HOME", "GRADLE_HOME"):
        v = env.get(k)
        if v:
            extras.append(str(Path(v) / "bin"))
    sdk = env.get("ANDROID_HOME")
    if sdk:
        extras += [
            str(Path(sdk) / "cmdline-tools" / "latest" / "bin"),
            str(Path(sdk) / "platform-tools"),
            str(Path(sdk) / "build-tools" / "34.0.0"),
        ]
    local_gradle = cfg.BIN_DIR / f"gradle-{cfg.GRADLE_VERSION_TARGET}" / "bin"
    if local_gradle.is_dir():
        extras.append(str(local_gradle))
    extras.append(str(cfg.BIN_DIR))

    env["PATH"] = os.pathsep.join(extras + [env.get("PATH", "")])
    logger.log(f"JAVA_HOME={env.get('JAVA_HOME')}")
    logger.log(f"ANDROID_HOME={env.get('ANDROID_HOME')}")
    logger.log(f"FLUTTER_HOME={env.get('FLUTTER_HOME')}")
    logger.log(f"GRADLE_HOME={env.get('GRADLE_HOME')}")
    return env

def require_tools(env: dict, logger: Logger) -> Tuple[Optional[str], Optional[str]]:
    """Return (flutter_path, java_path) — None jika tidak ada."""
    flutter = shutil.which("flutter", path=env.get("PATH"))
    java = shutil.which("java", path=env.get("PATH"))
    if not flutter:
        err("Flutter tidak ditemukan di PATH.")
        warn("Jalankan install.sh lalu `source ~/.bashrc`.")
    if not java:
        err("Java tidak ditemukan di PATH.")
        warn("Install: pkg install openjdk-17")
    return flutter, java

# ====================== Build pipeline ===========================
def safe_apk_name(zip_base: str) -> str:
    # Buang karakter aneh, sisakan alnum, dash, underscore
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", zip_base).strip("._")
    if not name:
        name = "output"
    return name

def unique_dest(dir_: Path, base_name: str, ext: str = ".apk") -> Path:
    p = dir_ / f"{base_name}{ext}"
    if not p.exists():
        return p
    i = 1
    while True:
        p = dir_ / f"{base_name}_{i}{ext}"
        if not p.exists():
            return p
        i += 1

def find_apk(build_dir: Path) -> Optional[Path]:
    """Cari APK release di build/app/outputs/flutter-apk."""
    target = build_dir / "app" / "outputs" / "flutter-apk"
    if not target.is_dir():
        # fallback cari semua apk di build/
        cands = list((build_dir).rglob("*.apk"))
    else:
        cands = list(target.glob("*.apk"))

    if not cands:
        return None

    # Prioritas: release, lalu yang paling baru
    def score(p: Path) -> Tuple[int, float]:
        name = p.name.lower()
        s = 0
        if "release" in name: s += 10
        if "profile" in name: s += 5
        if "debug"   in name: s += 1
        return (s, p.stat().st_mtime)

    cands.sort(key=score, reverse=True)
    return cands[0]

def build_apk(zip_path: Path) -> bool:
    zip_path = zip_path.resolve()

    ts = datetime.now().strftime(cfg.LOG_TS_FORMAT)
    logger = Logger(f"build_{ts}")
    logger.section(f"BUILD START: {zip_path}")
    print()
    info(f"Log: {logger.path}")

    ws = cfg.WORKSPACE_DIR / f"build_{ts}"
    try:
        ws.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        err(f"Gagal membuat workspace: {e}")
        logger.close()
        return False

    env = prepare_environment(logger)
    build_start = time.time()

    try:
        # ------- 1. Validasi ZIP -------
        print()
        info("[1/8] Validasi ZIP...")
        try:
            meta = validate_zip(zip_path)
        except ZipValidationError as e:
            err(str(e))
            logger.log(f"ZIP validation failed: {e}")
            return False

        ok("ZIP ditemukan")
        ok("File dapat dibaca")
        ok("ZIP valid")
        print()
        print(f"  {'Nama':<8}: {zip_path.name}")
        print(f"  {'Ukuran':<8}: {human_size(meta['size'])}")
        print(f"  {'Folder':<8}: {zip_path.parent}")
        print(f"  {'Entries':<8}: {meta['count']}")
        print()

        # ------- 2. Extract -------
        info("[2/8] Extract ZIP ke workspace...")
        try:
            secure_extract(zip_path, ws, logger)
        except ZipValidationError as e:
            err(f"Extract gagal: {e}")
            logger.log(f"Extract failed: {e}")
            _offer_debug(ws, logger)
            return False
        except Exception as e:
            err(f"Extract gagal: {e}")
            logger.log(traceback.format_exc())
            _offer_debug(ws, logger)
            return False
        ok(f"Extract selesai: {ws}")

        # ------- 3. Deteksi project Flutter -------
        info("[3/8] Mencari project Flutter...")
        roots = find_pubspec_roots(ws)
        if not roots:
            err("Tidak ditemukan pubspec.yaml di ZIP.")
            _offer_debug(ws, logger)
            return False

        project_root = choose_project_root(roots)
        if project_root is None:
            err("Tidak bisa menentukan project root.")
            _offer_debug(ws, logger)
            return False

        ok(f"Project root: {project_root}")

        # ------- 4. Validasi project -------
        info("[4/8] Validasi project Flutter...")
        if not validate_flutter_project(project_root):
            err("Project Flutter tidak valid.")
            _offer_debug(ws, logger)
            return False
        ok("Struktur project valid")

        # ------- 5. Tools check -------
        info("[5/8] Cek Flutter & Java...")
        flutter_bin, java_bin = require_tools(env, logger)
        if not flutter_bin or not java_bin:
            _offer_debug(ws, logger)
            return False

        _, jver = run_quiet([java_bin, "-version"])
        print(f"    Java    : {jver.strip().splitlines()[0] if jver.strip() else 'unknown'}")
        _, fver = run_quiet([flutter_bin, "--version"])
        print(f"    Flutter : {fver.strip().splitlines()[0] if fver.strip() else 'unknown'}")

        # ------- 6. flutter pub get -------
        info("[6/8] flutter pub get...")
        code, _ = run_stream(
            [flutter_bin, "pub", "get"],
            cwd=project_root, logger=logger, timeout=cfg.PUB_GET_TIMEOUT_SEC,
        )
        if code != 0:
            err(f"flutter pub get gagal (exit {code})")
            _offer_debug(ws, logger)
            return False
        ok("Dependencies resolved")

        # ------- 7. flutter build apk --release -------
        info("[7/8] flutter build apk --release ...")
        warn("Proses ini bisa memakan waktu 10-30+ menit pada perangkat ARM64.")
        code, _ = run_stream(
            [flutter_bin, "build", "apk", "--release"],
            cwd=project_root, logger=logger, timeout=cfg.BUILD_TIMEOUT_SEC,
        )
        if code != 0:
            err(f"Build gagal (exit {code})")
            warn("Cek log lengkap di: " + str(logger.path))
            _offer_debug(ws, logger)
            return False

        # ------- 8. Cari & copy APK -------
        info("[8/8] Mencari APK hasil build...")
        build_dir = project_root / "build"
        apk = find_apk(build_dir)
        if apk is None:
            err("APK tidak ditemukan setelah build.")
            _offer_debug(ws, logger)
            return False

        ok(f"APK ditemukan: {apk.name}")

        dest_dir = zip_path.parent
        base = safe_apk_name(zip_path.stem)
        dest = unique_dest(dest_dir, base, ".apk")
        try:
            shutil.copy2(apk, dest)
        except Exception as e:
            err(f"Gagal menyalin APK ke {dest_dir}: {e}")
            _offer_debug(ws, logger)
            return False

        elapsed = time.time() - build_start
        size = dest.stat().st_size

        print()
        box_top()
        box_center("BUILD SUCCESS")
        box_bot()
        print()
        print(f"  Project : {zip_path.stem}")
        print(f"  APK     : {dest.name}")
        print(f"  Output  : {dest}")
        print()
        print(f"  Size    : {human_size(size)}")
        print(f"  Durasi  : {int(elapsed//60)}m {int(elapsed%60)}s")
        print()
        ok("APK berhasil dibuat.")
        print()

        # Cleanup prompt
        ans = prompt("Hapus temporary workspace? [Y/n]: ").strip().lower()
        if ans in ("", "y", "ya"):
            try:
                shutil.rmtree(ws)
                ok(f"Workspace dihapus: {ws}")
            except Exception as e:
                warn(f"Gagal hapus workspace: {e}")
        else:
            info(f"Workspace dipertahankan: {ws}")

        logger.section("BUILD SUCCESS")
        return True

    except KeyboardInterrupt:
        print()
        warn("Build dibatalkan oleh user.")
        logger.log("Dibatalkan oleh user (KeyboardInterrupt).")
        warn(f"Workspace disimpan untuk debugging: {ws}")
        return False
    except Exception as e:
        err(f"Error tak terduga: {e}")
        logger.log(traceback.format_exc())
        warn(f"Traceback disimpan di: {logger.path}")
        _offer_debug(ws, logger)
        return False
    finally:
        logger.close()

def _offer_debug(ws: Path, logger: Logger) -> None:
    print()
    warn(f"Workspace debugging: {ws}")
    info(f"Log: {logger.path}")
    print()
    print("  [1] Kembali ke menu")
    print("  [2] Tampilkan lokasi workspace")
    print("  [3] Keluar")
    ans = prompt("Pilih [default 1]: ").strip() or "1"
    if ans == "2":
        print(f"    {ws}")
    elif ans == "3":
        sys.exit(0)

# ====================== Menu actions =============================
def action_build():
    clear_screen()
    banner()
    print(c("── Build APK ──", "bold"))
    print()
    if not cfg.STORAGE_ROOT.is_dir():
        warn("Storage /storage/emulated/0 tidak tersedia.")
        warn("Jalankan: termux-setup-storage")
        print()

    check_resources()

    raw = prompt("Masukkan path file ZIP project Flutter: ").strip()
    if not raw:
        warn("Path kosong.")
        return

    # Buang kutip di ujung (user kadang menempel "...")
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]

    zp = Path(raw).expanduser()
    try:
        zp = zp.resolve(strict=False)
    except Exception:
        pass

    if not zp.exists():
        err(f"File ZIP tidak ditemukan: {zp}")
        return
    if not zp.is_file():
        err("Path yang diberikan bukan file.")
        return
    if zp.suffix.lower() != ".zip":
        err("Ekstensi file bukan .zip.")
        return

    try:
        build_apk(zp)
    except KeyboardInterrupt:
        print()
        warn("Dibatalkan.")
    except Exception:
        # Sudah di-handle di build_apk, tapi tetap jaga-jaga
        err("Terjadi error tak terduga. Cek logs/.")

def action_env_check():
    clear_screen()
    banner()
    show_env_check()
    prompt("Tekan ENTER untuk kembali...")

def action_close():
    print()
    info("Terima kasih telah menggunakan Flutter APK Builder.")
    sys.exit(0)

# ====================== Signal handling ==========================
_interrupted = False
def _sigint(signum, frame):
    global _interrupted
    _interrupted = True
    print()
    warn("Sinyal interrupt diterima (Ctrl+C).")
    # Biarkan exception mengalir ke handler di build_apk
    raise KeyboardInterrupt

def main():
    signal.signal(signal.SIGINT, _sigint)
    try:
        while True:
            clear_screen()
            banner()
            choice = prompt("Pilih menu: ").strip()
            if choice == "1":
                action_build()
                if not _interrupted:
                    prompt("Tekan ENTER untuk kembali ke menu...")
            elif choice == "2":
                action_close()
            elif choice == "3":
                action_env_check()
            else:
                warn("Pilihan tidak valid.")
                time.sleep(1)
    except KeyboardInterrupt:
        print()
        info("Keluar dari builder.")
        sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Jangan tampilkan traceback panjang ke user
        err("Terjadi error tak terduga.")
        try:
            ts = datetime.now().strftime(cfg.LOG_TS_FORMAT)
            p = cfg.LOGS_DIR / f"fatal_{ts}.log"
            p.write_text(traceback.format_exc())
            warn(f"Detail: {p}")
        except Exception:
            pass
        sys.exit(1)
